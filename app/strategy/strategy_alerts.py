from datetime import datetime
from ..setting import models
from ..setting.config import CHAT_ID
from ..extension.utils import setup_logging

log = setup_logging()

_SIGNAL_STRATEGY_KEY = {
    "type1": "long_breakout",
    "type2": "short_bounce",
}

_REMINDER_TEXT = "⚠️ 請更新設定以便接收新訊號（輸入 /setup 查看範本）"


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


def format_type2_alert(symbol: str, signal: dict) -> str:
    sym_display  = symbol.replace("USDT", "USDT.P")
    close        = signal["close"]
    stop         = signal["stop_loss"]
    entry_level  = signal["entry_level"]
    resistance   = signal["short_resistance"]
    vol_r        = signal["vol_ratio"]
    candle_str   = datetime.fromtimestamp(signal["candle_open_time_ms"] / 1000).strftime("%Y/%m/%d %H:%M")

    stop_pct  = (stop  - close) / close * 100
    entry_pct = (entry_level - close) / close * 100

    return (
        f"🎯 *策略訊號 — Type 2 跌破做空*\n"
        f"幣種：`{sym_display}` ｜ {_now_str()}\n"
        f"\n"
        f"⏰ 跌破 K 棒：`{candle_str}`（15m）\n"
        f"\n"
        f"💰 收盤：`{_fmt_price(close)}`\n"
        f"🔻 跌破線：`{_fmt_price(entry_level)}` (-{entry_pct:.2f}%)\n"
        f"⬆️ 壓力位：`{_fmt_price(resistance)}`\n"
        f"📊 量能：`{vol_r:.1f}×` 均值\n"
        f"\n"
        f"🔴 止損：`{_fmt_price(stop)}`（拒絕 K 最高，+{stop_pct:.2f}%）\n"
        f"\n"
        f"[📈 查看圖表](https://www.binance.com/zh-TC/futures/{symbol})"
    )


async def send_strategy_alert(symbol: str, signal: dict) -> bool:
    """發送策略訊號告警到 Telegram。

    - CHAT_ID（若設定）：永遠接收所有訊號
    - 個別使用者：依 NOTIFY_STRATEGY 篩選；無設定或欄位缺失時發送提醒
    """
    try:
        sig_type     = signal.get("type")
        strategy_key = _SIGNAL_STRATEGY_KEY.get(sig_type)
        if strategy_key is None:
            return False

        if sig_type == "type1":
            text = format_type1_alert(symbol, signal)
        elif sig_type == "type2":
            text = format_type2_alert(symbol, signal)
        else:
            return False

        bot = models.bot
        if bot is None:
            log.error("[策略] bot 尚未初始化，無法發送告警")
            return False

        from ..user.user_config import get_all_registered_chat_ids, get_all_trading_configs_with_chat_id

        # chat_id → config（無設定檔的用戶值為 None）
        config_by_chat_id: dict[int, dict | None] = {
            cid: cfg
            for _, cid, cfg in get_all_trading_configs_with_chat_id()
            if cid
        }
        for cid in get_all_registered_chat_ids():
            if cid not in config_by_chat_id:
                config_by_chat_id[cid] = None

        success = False

        # CHAT_ID 頻道：永遠接收所有訊號
        if CHAT_ID:
            try:
                await bot.send_message(
                    chat_id=str(CHAT_ID),
                    text=text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
                success = True
            except Exception as e:
                log.error(f"[策略] Telegram 發送失敗 CHAT_ID={CHAT_ID}: {e}")

        # 個別使用者：依 NOTIFY_STRATEGY 篩選
        for cid, cfg in config_by_chat_id.items():
            if CHAT_ID and cid == CHAT_ID:
                continue  # 已在上方處理

            if cfg is None or "NOTIFY_STRATEGY" not in cfg:
                # 無設定或欄位缺失 → 提醒更新設定
                try:
                    await bot.send_message(chat_id=cid, text=_REMINDER_TEXT)
                except Exception as e:
                    log.error(f"[策略] 提醒發送失敗 chat_id={cid}: {e}")
                continue

            notify = cfg["NOTIFY_STRATEGY"]
            if not notify:
                continue  # 空陣列 → 靜默跳過
            if strategy_key not in notify:
                continue  # 不在清單 → 靜默跳過

            try:
                await bot.send_message(
                    chat_id=cid,
                    text=text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
                success = True
            except Exception as e:
                log.error(f"[策略] Telegram 發送失敗 chat_id={cid}: {e}")

        if success:
            log.info(f"[策略] 告警已發送 {symbol} {sig_type}")
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
