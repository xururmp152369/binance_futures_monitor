from telegram import Update
from telegram.ext import (
    ContextTypes,
)
from models import symbol_state, price_history
from conditions import check_conditions_manual
from binance import AsyncClient

async def command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    await update.message.reply_text(
        f"嗨 {user.first_name}！\n\n"
        "可用指令：\n"
        "/s <coin> 搜尋指定幣種的歷史資料，ex: btc\n"
        "/c <coin> 檢查是否符合發送條件，ex: btc\n"
        "試試看吧！"
    )

# /s 指令主處理器
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args  # 這就是使用者在指令後面打的所有文字（已自動分割）

    if not args:
        await update.message.reply_text("用法：\n/s btc eth\n輸入一個或多個幣對名稱")
        return

    # 處理多個幣對
    symbols = [arg.upper() if "USDT" in arg.upper() else f"{arg.upper()}USDT" for arg in args]  # 轉為大寫
    
    for symbol in symbols:
        # 檢查是否有歷史資料
        price_hist = price_history.get(symbol, [])
        
        if not price_hist:
            await update.message.reply_text(f"{symbol}：無歷史資料")
            continue
            
        # 格式化輸出
        symbol_info = [f"{symbol}，已儲存的歷史訊息參考："]
        
        # 價格歷史
        if price_hist:
            symbol_info.append(f"\nsymbol_state：{symbol_state[symbol]}\n📈 價格歷史：")
            for i, (timestamp, price) in enumerate(price_hist, 1):
                from datetime import datetime
                time_str = datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')
                symbol_info.append(f"紀錄時間: {time_str}，紀錄價格: {price:.6f} (第{i}筆)")
        
        # 每個幣種單獨發送一條訊息
        symbol_message = "\n".join(symbol_info)
        await update.message.reply_text(symbol_message)

# /c 指令主處理器
async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    if not args:
        await update.message.reply_text("用法：\n/c btc eth\n手動檢查指定幣對的告警條件")
        return

    # 處理多個幣對
    symbols = [arg.upper() if "USDT" in arg.upper() else f"{arg.upper()}USDT" for arg in args]
    
    # 建立 Binance 客戶端
    client = await AsyncClient.create()
    
    try:
        for symbol in symbols:
            # 檢查幣對是否存在
            if symbol not in symbol_state:
                await update.message.reply_text(f"{symbol}：未監控的幣對")
                continue
                
            # 執行手動檢查
            result, logs = await check_conditions_manual(client, symbol)
            
            # 組合日誌訊息
            log_message = f"{symbol} 條件檢查結果：\n\n" + "\n".join(logs)
            
            # 發送日誌
            await update.message.reply_text(log_message)
            
            # 如果有結果，發送告警訊息
            if result:
                alert_message = f"🚨 手動檢查觸發告警！\n\n"
                alert_message += f"幣對：{symbol}\n"
                alert_message += f"價格變化：{result['price_pct']:+.2f}%\n"
                alert_message += f"持倉量變化：{result['oi_pct'] or 0:+.2f}%\n\n"
                alert_message += result['reason'][0]
                
                await update.message.reply_text(alert_message)
                
    except Exception as e:
        await update.message.reply_text(f"檢查過程發生錯誤：{str(e)}")
    finally:
        await client.close_connection()