import asyncio
from .setting import models
from .command import bot_enum
from binance import AsyncClient
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from .setting.config import BOT_TOKEN
from .setting.models import running, symbol_state
from .datacenter.binance_opendata import initialize_symbols, monitor_price_websocket, update_open_interest
from .tgbot.monitor import periodic_screen
from .extension.utils import setup_logging
from .command import command

log = setup_logging()

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
       - monitor_price_websocket：WebSocket 即時更新價格/成交量/EMA
       - update_open_interest：REST 週期更新 OI
       - periodic_screen：週期掃描條件並發告警
    5) 啟動 Telegram polling 讓使用者可下指令
    """
    global running, bot

    log.info("啟動 Binance 異動監控 Bot...")

    # 建立 Application
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_error_handler(error_handler)
    models.bot = application.bot
    # 註冊指令（用你自己的 enum）
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.COMMAND, command.command))
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.SEARCH, command.search))
    application.add_handler(CommandHandler(bot_enum.TGBotCommand.CHECK, command.check))

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
        oi_task      = asyncio.create_task(update_open_interest(client))
        screen_task      = asyncio.create_task(periodic_screen(client))

        log.info("三個背景任務已啟動，準備啟動 Telegram polling...")

        # 關鍵：Windows 下不能用 application.run_polling()
        # 改用手動四步驟，徹底解決巢狀 event loop 問題
        await application.initialize()                    # 第1步
        await application.start()                          # 第2步
        await application.updater.start_polling(           # 第3步
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )

        log.info("Telegram Bot 已上線，開始接收使用者指令！")

        # 只等待你的三個 Binance 任務即可
        # Telegram polling 已經在背景跑了
        await asyncio.gather(price_task, oi_task, screen_task)

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