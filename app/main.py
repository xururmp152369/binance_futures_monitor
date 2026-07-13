import asyncio
import sys
import urllib.request
from pathlib import Path
from datetime import datetime
import uvicorn
from .setting import models
from .command import bot_enum
from binance import AsyncClient
from telegram import Update

from telegram.ext import Application, CommandHandler, MessageHandler, TypeHandler, filters, ContextTypes
from .setting.config import BOT_TOKEN, ENCRYPTION_KEY
from .setting.models import running, symbol_state
from .datacenter.binance_opendata import initialize_symbols, monitor_price_websocket
from .tgbot.monitor import periodic_screen
from .extension.utils import setup_logging
from .command import command
from .user.user_config import get_account_by_chat_id, is_session_valid, refresh_session

log = setup_logging()


async def _session_refresh_handler(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """攔截所有 update，若使用者已登入則自動延長 session。group=-1 先於其他 handler 執行。"""
    if not update.effective_user:
        return
    result = get_account_by_chat_id(update.effective_user.id)
    if not result:
        return
    account_name, acc = result
    if is_session_valid(acc):
        refresh_session(account_name, acc)

async def monthly_restart_scheduler(client, restart_hour=4):
    """每月一號定時重啟任務。

    在每月 1 日凌晨 restart_hour 點觸發程式退出，由 Docker restart policy 自動重啟。
    若本月 1 日的重啟時間尚未過，則等到今天；否則等到下個月 1 日。
    """
    while models.running:
        try:
            now = datetime.now()
            this_first = datetime(now.year, now.month, 1, restart_hour, 0, 0)
            if now < this_first:
                target_time = this_first
            else:
                year  = now.year + (1 if now.month == 12 else 0)
                month = 1 if now.month == 12 else now.month + 1
                target_time = datetime(year, month, 1, restart_hour, 0, 0)

            wait_seconds = (target_time - now).total_seconds()
            log.info(f"⏰ 下次重啟時間：{target_time.strftime('%Y-%m-%d %H:%M:%S')} (約 {wait_seconds/3600:.1f} 小時後)")

            await asyncio.sleep(wait_seconds)

            log.info("🔄 到達每月重啟時間，準備重啟程式...")
            log.info("💡 提示：程式將完全退出，Docker 會自動重啟容器")

            models.running = False

            log.info("⏳ 等待 10 秒讓所有任務退出...")
            await asyncio.sleep(10)

            log.info("👋 程式即將退出，等待 Docker 重啟...")
            sys.exit(0)

        except Exception as e:
            log.exception(f"每月重啟排程器錯誤: {e}")
            await asyncio.sleep(3600)

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

    # 確認加密金鑰已設定
    if not ENCRYPTION_KEY:
        log.error("=" * 60)
        log.error("ENCRYPTION_KEY 未設定！")
        log.error("請執行以下指令生成 key，並加入 .env：")
        log.error('  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"')
        log.error("在 .env 中加入：  ENCRYPTION_KEY=<generated key>")
        log.error("=" * 60)
        sys.exit(1)

    log.info("啟動 Binance 異動監控 Bot...")

    try:
        public_ip = urllib.request.urlopen("https://api.ipify.org", timeout=5).read().decode()
        log.info(f"容器出口 IP：{public_ip}（請確認此 IP 已加入 Binance API 白名單）")
    except Exception as e:
        log.warning(f"無法取得出口 IP：{e}")

    # 確保資料目錄存在
    Path("data/accounts").mkdir(parents=True, exist_ok=True)
    Path("data/configs").mkdir(parents=True, exist_ok=True)

    # 建立 Application
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_error_handler(error_handler)
    models.bot = application.bot
    # group=-1：任何互動都自動延長 session
    application.add_handler(TypeHandler(Update, _session_refresh_handler), group=-1)
    # 註冊指令
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.COMMAND,   command.command))
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.SETUP,     command.setup))
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.MY_CONFIG, command.my_config))
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.REGISTER,  command.register))
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.LOGIN,     command.login))
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.LOGOUT,    command.logout))
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.READY,     command.ready_list))
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.TRACKING,  command.tracking_list))
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
        restart_task = asyncio.create_task(monthly_restart_scheduler(client))

        # 儀表板 Web Server
        from web.api.app import create_web_app
        web_config = uvicorn.Config(
            app=create_web_app(), host="0.0.0.0", port=8000, log_level="warning"
        )
        web_server = uvicorn.Server(web_config)
        web_task = asyncio.create_task(web_server.serve())

        log.info("三個背景任務已啟動（含每月重啟排程），準備啟動 Telegram polling...")

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
        await asyncio.gather(price_task, screen_task, restart_task, web_task)

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