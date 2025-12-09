import asyncio
import models
from command import bot_enum
from binance import AsyncClient
from telegram import Update
from telegram.ext import Application, CommandHandler
from config import BOT_TOKEN
from models import running, symbol_state
from binance_opendata import initialize_symbols, monitor_price_websocket, update_open_interest
from monitor import periodic_screen
from utils import setup_logging
from command import command

log = setup_logging()

async def main():
    global running, bot

    log.info("啟動 Binance 異動監控 Bot...")

    # 建立 Application
    application = Application.builder().token(BOT_TOKEN).build()
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
    asyncio.run(main())