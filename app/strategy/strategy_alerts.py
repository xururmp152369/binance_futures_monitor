from datetime import datetime
from ..setting import models
from ..setting.config import CHAT_ID
from ..extension.utils import setup_logging

log = setup_logging()


def _fmt_price(price: float) -> str:
    """格式化價格：移除多餘的尾零，最多保留 8 位小數。"""
    return f"{price:,.8f}".rstrip("0").rstrip(".")


def _now_str() -> str:
    return datetime.now().strftime("%Y/%m/%d %H:%M")


def format_type1_alert(symbol: str, signal: dict) -> str:
    sym_display = symbol.replace("USDT", "USDT.P")
    close   = signal["close"]
    stop    = signal["stop_loss"]
    top     = signal["top"]
    bottom  = signal["bottom"]
    vol_r   = signal["vol_ratio"]

    stop_pct  = (close - stop)  / close * 100
    top_pct   = (top   - close) / close * 100
    range_pct = (top - bottom)  / bottom * 100

    return (
        f"🎯 *策略訊號 — Type 1 帶量突破*\n"
        f"幣種：`{sym_display}` ｜ {_now_str()}\n"
        f"\n"
        f"💰 收盤：`{_fmt_price(close)}`\n"
        f"🔼 突破頂部：`{_fmt_price(top)}` (+{top_pct:.2f}%)\n"
        f"📊 量能：`{vol_r:.1f}×` 均值\n"
        f"\n"
        f"🔴 止損：`{_fmt_price(stop)}`（本 K 低點，-{stop_pct:.2f}%）\n"
        f"🟢 目標：`{_fmt_price(top)}`（盤整頂部）\n"
        f"\n"
        f"📦 盤整區間：`{_fmt_price(bottom)}` ～ `{_fmt_price(top)}`（幅度 {range_pct:.1f}%）\n"
        f"[📈 查看圖表](https://www.binance.com/zh-TC/futures/{symbol.replace('USDT', '')})"
    )


def format_type2_alert(symbol: str, signal: dict) -> str:
    sym_display = symbol.replace("USDT", "USDT.P")
    close       = signal["close"]
    stop        = signal["stop_loss"]
    top         = signal["top"]
    bottom      = signal["bottom"]
    rr          = signal["rr"]
    wick_pct    = signal["wick_pct"]
    ema_period, ema_val = signal["touched_ema"]

    stop_pct = (close - stop) / close * 100
    top_pct  = (top - close)  / close * 100

    return (
        f"🎯 *策略訊號 — Type 2 均線反彈*\n"
        f"幣種：`{sym_display}` ｜ {_now_str()}\n"
        f"\n"
        f"💰 收盤：`{_fmt_price(close)}`\n"
        f"📉 觸碰：4h EMA{ema_period} = `{_fmt_price(ema_val)}`\n"
        f"🕯️ 收針幅度：`{wick_pct:.1f}%`\n"
        f"📊 盈虧比：`{rr:.2f}:1`\n"
        f"\n"
        f"🔴 止損：`{_fmt_price(stop)}`（盤整底部，-{stop_pct:.2f}%）\n"
        f"🟢 目標：`{_fmt_price(top)}`（盤整頂部，+{top_pct:.2f}%）\n"
        f"\n"
        f"📦 盤整區間：`{_fmt_price(bottom)}` ～ `{_fmt_price(top)}`\n"
        f"[📈 查看圖表](https://www.binance.com/zh-TC/futures/{symbol.replace('USDT', '')})"
    )


def format_type0_alert(symbol: str, signal: dict) -> str:
    sym_display = symbol.replace("USDT", "USDT.P")
    price     = signal["close"]
    price_pct = signal["price_pct"]
    vol_ratio = signal["vol_ratio"]
    trend_1h  = signal.get("trend_1h")
    trend_4h  = signal.get("trend_4h")

    direction      = "📈 上漲" if price_pct >= 0 else "📉 下跌"
    trend_1h_icon  = "✅" if trend_1h else "❌"
    trend_4h_icon  = "✅" if trend_4h else "❌"

    return (
        f"🚨 *{sym_display} 量價異動*\n"
        f"⌚ {_now_str()}\n"
        f"\n"
        f"{direction} `{price_pct:+.2f}%`\n"
        f"💰 價格：`{_fmt_price(price)}`\n"
        f"📊 量能：`{vol_ratio:.1f}×` 均值\n"
        f"📊 趨勢：1h {trend_1h_icon} / 4h {trend_4h_icon}\n"
        f"\n"
        f"[📈 查看圖表](https://www.binance.com/zh-TC/futures/{symbol.replace('USDT', '')})"
    )


async def send_strategy_alert(symbol: str, signal: dict) -> bool:
    """發送策略訊號告警到 Telegram。

    冷卻已由 state_machine 內部管理，此處不做重複判斷。
    例外不向上拋出，避免中斷 WebSocket 連線。
    """
    try:
        sig_type = signal.get("type")
        if sig_type == "type0":
            text = format_type0_alert(symbol, signal)
        elif sig_type == "type1":
            text = format_type1_alert(symbol, signal)
        elif sig_type == "type2":
            text = format_type2_alert(symbol, signal)
        else:
            return False

        bot = models.bot
        if bot is None:
            log.error("[策略] bot 尚未初始化，無法發送告警")
            return False

        await bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        log.info(f"[策略] 告警已發送 {symbol} {sig_type}")
        return True

    except Exception as e:
        log.error(f"[策略] Telegram 發送失敗 {symbol}: {e}")
        return False
