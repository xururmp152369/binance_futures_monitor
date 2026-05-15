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
    close      = signal["close"]
    stop       = signal["stop_loss"]
    top        = signal["top"]
    bottom     = signal["bottom"]
    vol_r      = signal["vol_ratio"]
    pump_str   = datetime.fromtimestamp(signal["pump_time"]).strftime("%Y/%m/%d %H:%M")
    pump_high  = signal["pump_high"]
    pump_low   = signal["pump_low"]
    candle_str = datetime.fromtimestamp(signal["candle_open_time_ms"] / 1000).strftime("%Y/%m/%d %H:%M")

    stop_pct  = (close - stop)  / close * 100
    top_pct   = (top   - close) / close * 100
    range_pct = (top - bottom)  / bottom * 100

    return (
        f"🎯 *策略訊號 — Type 1 帶量突破*\n"
        f"幣種：`{sym_display}` ｜ {_now_str()}\n"
        f"\n"
        f"📅 拉漲 K 棒：`{pump_str}` ｜ 最高 `{_fmt_price(pump_high)}` ｜ 最低 `{_fmt_price(pump_low)}`\n"
        f"⏰ 突破 K 棒：`{candle_str}`（15m）\n"
        f"\n"
        f"💰 收盤：`{_fmt_price(close)}`\n"
        f"🔼 突破頂部：`{_fmt_price(top)}` (+{top_pct:.2f}%)\n"
        f"📊 量能：`{vol_r:.1f}×` 均值\n"
        f"\n"
        f"🔴 止損：`{_fmt_price(stop)}`（放量起始，-{stop_pct:.2f}%）\n"
        f"\n"
        f"📦 盤整區間：`{_fmt_price(bottom)}` ～ `{_fmt_price(top)}`（幅度 {range_pct:.1f}%）\n"
        f"[📈 查看圖表](https://www.binance.com/zh-TC/futures/{symbol})"
    )


async def send_strategy_alert(symbol: str, signal: dict) -> bool:
    """發送策略訊號告警到 Telegram。

    發送對象：
    - CHAT_ID（若設定，作為公開播報頻道）
    - 所有已綁定 TG 帳號的使用者（個人廣播）

    冷卻已由 state_machine 內部管理，此處不做重複判斷。
    例外不向上拋出，避免中斷 WebSocket 連線。
    """
    try:
        sig_type = signal.get("type")
        if sig_type == "type1":
            text = format_type1_alert(symbol, signal)
        else:
            return False

        bot = models.bot
        if bot is None:
            log.error("[策略] bot 尚未初始化，無法發送告警")
            return False

        from ..user.user_config import get_all_registered_chat_ids
        targets: set[str] = set()
        if CHAT_ID:
            targets.add(str(CHAT_ID))
        for cid in get_all_registered_chat_ids():
            targets.add(str(cid))

        if not targets:
            log.warning(f"[策略] 無目標頻道，告警未發送 {symbol} {sig_type}")
            return False

        success = False
        for cid in targets:
            try:
                await bot.send_message(
                    chat_id=cid,
                    text=text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
                success = True
            except Exception as e:
                log.error(f"[策略] Telegram 發送失敗 chat_id={cid} {symbol}: {e}")

        if success:
            log.info(f"[策略] 告警已發送 {symbol} {sig_type} → {len(targets)} 個頻道")
        return success

    except Exception as e:
        log.error(f"[策略] 告警處理失敗 {symbol}: {e}")
        return False


async def send_order_results(symbol: str, results: dict[int, tuple[bool, str]]) -> None:
    """對每位使用者發送個別的開單結果通知。"""
    if not results:
        return
    bot = models.bot
    if bot is None:
        return
    sym_display = symbol.replace("USDT", "USDT.P")
    for chat_id, (success, message) in results.items():
        if success:
            text = f"✅ *自動開倉成功* — `{sym_display}`\n{message}"
        else:
            text = f"❌ *自動開倉失敗* — `{sym_display}`\n{message}"
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error(f"[策略] 開單結果通知失敗 chat_id={chat_id}: {e}")
