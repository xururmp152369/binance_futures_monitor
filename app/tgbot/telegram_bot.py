import time
from ..setting import models
from datetime import datetime
from ..setting.config import CHAT_ID, ALERT_COOLDOWN
from ..setting.models import symbol_state, last_alert
from ..extension.utils import setup_logging

log = setup_logging()

# ================== Telegram Bot ==================

async def send_alert(symbol: str, alert_data: dict):
    """發送告警訊息到指定 Telegram 聊天室。

    冷卻機制改為「間隔 N 根 15m K」：
    - 記錄上次告警的 15m K 收盤時間（last_kline_close_time_15m）
    - 只有當前 K 收盤時間與上次間隔 >= ALERT_COOLDOWN 秒時才允許告警
    - 這樣可確保至少間隔 N 根 15m K（例如 3600 秒 = 4 根 15m K）

    Args:
        symbol: 幣種（例如 BTCUSDT）
        alert_data: 由 conditions.py 組出的告警資料（包含 reason/price_pct/oi_pct）

    Returns:
        1 表示成功送出；0 表示送出失敗；None 表示被 cooldown 擋下。
    """
    try:
        state = symbol_state[symbol]
        current_kline_time = state["last_kline_close_time_15m"]
        
        # 冷卻檢查：基於 15m K 收盤時間間隔
        if symbol in last_alert:
            last_kline_time = last_alert[symbol]
            if current_kline_time - last_kline_time < ALERT_COOLDOWN:
                return

        # 記錄本次告警的 K 線收盤時間
        last_alert[symbol] = current_kline_time

        price = state["last_price"]
        oi_pct = alert_data.get("oi_pct")
        funding = state["funding_rate"]
        reason = alert_data["reason"]
        current_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

        title = f"🚨 {symbol}.P 異動警報！ ⌚ 觸發時間：{current_time}"
        price_line = f"💰 價格：`{price:,.8f}` USDT"
        trigger_line = ""
        if alert_data.get("price_pct") is not None:
            trigger_line = "🟢📈 上漲" if alert_data.get("price_pct", 0) >= 0 else "🔴📉 下跌" if alert_data.get("price_pct") is not None else ""
            pct = alert_data["price_pct"]
            sign = "+" if pct >= 0 else ""
            price_line += f" （`{sign}{pct:.2f}%`）"

        oi_line = f"📊 持倉量變化：`{oi_pct:+.1f}%`" if oi_pct is not None else "📊 持倉量變化：`N/A`"
        fund_line = f"💲 資金費率：`{funding:.4f}%`"
        
        # 趨勢狀態（參考項目）
        trend_1h = alert_data.get("trend_1h")
        trend_4h = alert_data.get("trend_4h")
        trend_1h_icon = "✅" if trend_1h else "❌"
        trend_4h_icon = "✅" if trend_4h else "❌"
        trend_line = f"📊 趨勢：1h {trend_1h_icon} / 4h {trend_4h_icon}"
        
        if isinstance(reason, (list, tuple)):
            reason_text = "\n".join(reason)
        else:
            reason_text = str(reason)
        reason_line = f"🧩 觸發原因：{reason_text}"
        chart_link = f"📈 [查看圖表](https://www.binance.com/en/futures/{symbol})"

        text = "\n".join(filter(None, [title, trigger_line, price_line, oi_line, fund_line, trend_line, reason_line, chart_link]))

        await models.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown", disable_web_page_preview=True)
        log.info(f"告警 → {symbol}：{reason}")
        return 1
    except Exception as e:
        log.info(f"[Telegram 錯誤] {symbol}: {e} \n {symbol_state[symbol]} \n {alert_data}")
        return 0