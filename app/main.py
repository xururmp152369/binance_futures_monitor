import asyncio
import sys
from pathlib import Path
from datetime import datetime, timedelta, time as dt_time
from .setting import models
from .command import bot_enum
from binance import AsyncClient
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from .setting.config import BOT_TOKEN
from .setting.models import running, symbol_state
from .datacenter.binance_opendata import initialize_symbols, monitor_price_websocket
from .tgbot.monitor import periodic_screen
from .extension.utils import setup_logging
from .command import command

log = setup_logging()

async def daily_restart_scheduler(client, restart_hour=4):
    """每日定時重啟任務。
    
    在指定時間（預設凌晨 4 點）觸發程式退出，由 Docker restart policy 自動重啟。
    """
    while models.running:
        try:
            now = datetime.now()
            # 計算到下個週一凌晨的時間差（weekday: 0=週一, 6=週日）
            days_until_monday = (7 - now.weekday()) % 7 or 7
            target_date = (now + timedelta(days=days_until_monday)).date()
            target_time = datetime.combine(target_date, dt_time(restart_hour, 0, 0))
                        
            wait_seconds = (target_time - now).total_seconds()
            log.info(f"⏰ 下次重啟時間：{target_time.strftime('%Y-%m-%d %H:%M:%S')} (約 {wait_seconds/3600:.1f} 小時後)")
            
            await asyncio.sleep(wait_seconds)
            
            log.info("🔄 到達每日重啟時間，準備重啟程式...")
            log.info("💡 提示：程式將完全退出，Docker 會自動重啟容器")
            
            # 設置 running = False 會觸發所有任務結束
            models.running = False
            
            # 等待 10 秒讓其他任務優雅退出
            log.info("⏳ 等待 10 秒讓所有任務退出...")
            await asyncio.sleep(10)
            
            # 強制退出程式，觸發 Docker 重啟
            log.info("👋 程式即將退出，等待 Docker 重啟...")
            sys.exit(0)
            
        except Exception as e:
            log.exception(f"每日重啟排程器錯誤: {e}")
            await asyncio.sleep(3600)  # 發生錯誤時等待 1 小時後重試

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram 全域錯誤處理器。

    任何 CommandHandler 發生未捕捉例外時會進到這裡：
    - 會把完整 traceback 記到 log
    - 若可回覆使用者，則回覆一則簡短錯誤訊息
    """
    log.exception("Telegram handler exception", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("發生錯誤，請稍後再試。")
    except Exception:
        pass

async def main():
    """程式主入口。

    主要流程：
    1) 初始化 Telegram Bot（註冊指令與 error handler）
    2) 建立 Binance AsyncClient
    3) 初始化監控幣種（建立 symbol_state 結構、載入歷史資料）
    4) 啟動三個背景任務：
       - monitor_price_websocket：WebSocket 即時更新價格/EMA，並觸發策略訊號
       - periodic_screen：週期更新幣種清單、即時廢棄策略狀態
       - daily_restart_scheduler：每日定時重啟
    5) 啟動 Telegram polling 讓使用者可下指令
    """
    global running, bot

    log.info("啟動 Binance 異動監控 Bot...")

    # 確保使用者設定檔目錄存在
    Path("data/users").mkdir(parents=True, exist_ok=True)

    # 建立 Application
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_error_handler(error_handler)
    models.bot = application.bot
    # 註冊指令
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.COMMAND,   command.command))
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.SEARCH,    command.search))
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.STRATEGY,  command.strategy))
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.CONFIG,    command.config))
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.SETUP,     command.setup))
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.MY_CONFIG, command.my_config))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, command.handle_json_message))

    # Binance client
    client = await AsyncClient.create()
    await client.ping()

    try:
        await initialize_symbols(client)
        log.info(f"初始化完成，共監控 {len(symbol_state)} 個合約")

        if not symbol_state:
            log.info("無合約，結束程式")
            return

        # 三個背景任務
        price_task   = asyncio.create_task(monitor_price_websocket(client))
        screen_task  = asyncio.create_task(periodic_screen(client))
        restart_task = asyncio.create_task(daily_restart_scheduler(client))

        log.info("三個背景任務已啟動（含每日重啟排程），準備啟動 Telegram polling...")

        # 關鍵：Windows 下不能用 application.run_polling()
        # 改用手動四步驟，徹底解決巢狀 event loop 問題
        await application.initialize()                    # 第1步
        await application.start()                          # 第2步
        await application.updater.start_polling(           # 第3步
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )

        log.info("Telegram Bot 已上線，開始接收使用者指令！")

        # 等待所有背景任務（包含每日重啟排程）
        # Telegram polling 已經在背景跑了
        await asyncio.gather(price_task, screen_task, restart_task)

    except KeyboardInterrupt:
        log.info("\n收到中斷信號，停止中...")
    except Exception as e:
        log.exception(f"程式發生未預期錯誤：{e}")
    finally:
        running = False
        log.info("正在關閉所有服務...")

        # 正確的關閉順序（Windows 必備）
        if application.updater.running:
            await application.updater.stop()               # 停止 polling
        await application.stop()                           # 停止 bot
        await application.shutdown()                       # 關閉 http session
        await client.close_connection()

        log.info("所有服務已安全關閉，掰掰")

if __name__ == "__main__":
    """直接執行此模組時，啟動 asyncio event loop 跑 main()。"""
    asyncio.run(main())