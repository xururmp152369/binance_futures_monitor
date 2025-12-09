import time
import models
from datetime import datetime
from config import CHAT_ID, ALERT_COOLDOWN
from models import symbol_state, last_alert
from utils import setup_logging

log = setup_logging()

# ================== Telegram Bot ==================

async def send_alert(symbol: str, alert_data: dict):
    try:
        now = time.time()
        if now - last_alert[symbol] < ALERT_COOLDOWN:
            return
        last_alert[symbol] = now

        state = symbol_state[symbol]
        price = state["last_price"]
        oi_pct = alert_data.get("oi_pct")
        funding = state["funding_rate"]
        reason = alert_data["reason"]
        current_time = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

        title = f"🚨 {symbol} 異動警報！ ⌚ 觸發時間：{current_time}"
        price_line = f"💰 價格：`{price:,.8f}` USDT"
        trigger_line = ""
        if alert_data.get("price_pct") is not None:
            trigger_line = "🟢📈 上漲" if alert_data.get("price_pct", 0) >= 0 else "🔴📉 下跌" if alert_data.get("price_pct") is not None else ""
            pct = alert_data["price_pct"]
            sign = "+" if pct >= 0 else ""
            price_line += f" （`{sign}{pct:.2f}%`）"

        oi_line = f"📊 持倉量變化：`{oi_pct:+.1f}%`" if oi_pct is not None else "📊 持倉量變化：`N/A`"
        fund_line = f"💲 資金費率：`{funding:.4f}%`"
        if isinstance(reason, (list, tuple)):
            reason_text = "\n".join(reason)
        else:
            reason_text = str(reason)
        reason_line = f"🧩 觸發原因：{reason_text}"
        chart_link = f"📈 [查看圖表](https://www.binance.com/en/futures/{symbol})"

        text = "\n".join(filter(None, [title, trigger_line, price_line, oi_line, fund_line, reason_line, chart_link]))

        await models.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown", disable_web_page_preview=True)
        log.info(f"告警 → {symbol}：{reason}")
        return 1
    except Exception as e:
        log.info(f"[Telegram 錯誤] {symbol}: {e} \n {symbol_state[symbol]} \n {alert_data}")
        return 0