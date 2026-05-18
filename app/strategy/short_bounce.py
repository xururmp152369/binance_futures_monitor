import time
from enum import Enum
from ..setting import models
from ..setting.config import (
    TRIGGER_VOLUME_MULT,
    SHORT_WATCHING_MAX_CANDLES,
    EMA_LONG_PERIOD, EMA_SHORT_PERIOD,
    EMA_TOLERANCE_PCT, BODY_SUPPRESS_PCT, WICK_BODY_RATIO,
    SHORT_BOUNCE_VOLUME_MAX, SHORT_ENTRY_VOLUME_MIN,
    BREAKOUT_BODY_PCT, STRATEGY_COOLDOWN,
)
from .analysis_utils import (
    get_4h_volume_baseline, get_4h_ema,
    upper_wick_size, body_size, is_shooting_star,
)
from ..extension.utils import setup_logging

log = setup_logging()


class ShortPhase(Enum):
    IDLE     = "idle"
    WATCHING = "watching"
    READY    = "ready"


# ─── 狀態初始化 ───────────────────────────────────────────────────────────────

def _init_short_state() -> dict:
    return {
        "phase":                ShortPhase.IDLE,
        "short_resistance":     None,  # 廢棄 K 的 high（靜態壓力位）
        "abandonment_low":      None,  # 廢棄 K 的 low（空頭進場觸發線）
        "short_watch_start_ts": None,  # 觀察計時起點（秒）
        "short_rejection_high": None,  # 壓制 K 的 high（止損參考）
        "last_alert_ts":        0.0,
    }


def _get_or_init(symbol: str) -> dict:
    if symbol not in models.short_strategy_state:
        models.short_strategy_state[symbol] = _init_short_state()
    return models.short_strategy_state[symbol]


def _reset_to_idle(symbol: str, reason: str) -> None:
    current = models.short_strategy_state.get(symbol, {})
    if current.get("phase") not in (None, ShortPhase.IDLE):
        log.info(f"[策略-S] {symbol} → SHORT_IDLE | {reason}")
    new = _init_short_state()
    new["last_alert_ts"] = current.get("last_alert_ts", 0.0)
    models.short_strategy_state[symbol] = new


# ─── 進入觀察（由協調器呼叫）────────────────────────────────────────────────

def enter_short_watching(
    symbol: str,
    abandonment_high: float,
    abandonment_low: float,
    ts: float,
) -> None:
    """多頭廢棄時由 state_machine 協調器呼叫，啟動空頭反彈觀察。"""
    st = _get_or_init(symbol)
    if st["phase"] != ShortPhase.IDLE:
        return
    st["phase"]                = ShortPhase.WATCHING
    st["short_resistance"]     = abandonment_high
    st["abandonment_low"]      = abandonment_low
    st["short_watch_start_ts"] = ts
    st["short_rejection_high"] = None
    log.info(
        f"[策略-S] {symbol} → SHORT_WATCHING | "
        f"壓力位={abandonment_high:.6f} 廢棄低={abandonment_low:.6f}"
    )


# ─── 壓制判斷 ─────────────────────────────────────────────────────────────────

def _is_valid_rejection(
    symbol: str,
    open_: float, high: float, low: float, close: float, quote_volume: float,
    reference_level: float,
) -> bool:
    """判斷反彈 4h K 是否構成有效的射擊之星拒絕（靜態或 EMA 基準）。"""
    body_high = max(open_, close)
    # wick 觸到壓力位
    if high < reference_level:
        return False
    # wick 未過深超出容忍值
    if high > reference_level * (1 + EMA_TOLERANCE_PCT):
        return False
    # 實體被壓在壓力位以下
    if body_high > reference_level * (1 - BODY_SUPPRESS_PCT):
        return False
    # 射擊之星形態
    if not is_shooting_star(open_, high, low, close, WICK_BODY_RATIO):
        return False
    # 反彈無量
    avg = get_4h_volume_baseline(symbol)
    if avg is None or avg <= 0:
        return False
    if quote_volume >= avg * SHORT_BOUNCE_VOLUME_MAX:
        return False
    return True


# ─── 4h 狀態機 ───────────────────────────────────────────────────────────────

def on_new_4h_candle_short(symbol: str, candle: tuple) -> None:
    """處理 4h K 棒：觀察反彈性質，決定進入 SHORT_READY 或廢棄觀察。"""
    st = _get_or_init(symbol)
    if st["phase"] == ShortPhase.IDLE:
        return

    open_time_ms, open_, high, low, close, quote_volume = candle
    current_ts    = open_time_ms / 1000
    short_resistance = st["short_resistance"]

    # SHORT_READY 廢棄：帶量 4h K 實體收超壓力位
    if st["phase"] == ShortPhase.READY:
        avg = get_4h_volume_baseline(symbol)
        if avg and quote_volume >= avg * TRIGGER_VOLUME_MULT:
            if max(open_, close) > short_resistance:
                _reset_to_idle(symbol, f"帶量衝超壓力位 body_high={max(open_, close):.6f}")
                return

    # SHORT_WATCHING 超時
    if st["phase"] == ShortPhase.WATCHING:
        elapsed_candles = (current_ts - st["short_watch_start_ts"]) / (4 * 3600)
        if elapsed_candles > SHORT_WATCHING_MAX_CANDLES:
            _reset_to_idle(symbol, f"觀察超時 {elapsed_candles:.1f} 根 4h K")
            return

    if st["phase"] != ShortPhase.WATCHING:
        return

    # 移除觀察（2.1）：反彈帶量實體收超壓力位
    avg = get_4h_volume_baseline(symbol)
    if avg and quote_volume >= avg * TRIGGER_VOLUME_MULT:
        if max(open_, close) > short_resistance:
            _reset_to_idle(symbol, f"反彈帶量突破壓力位 body_high={max(open_, close):.6f}")
            return

    # [A] 靜態壓力壓制
    if _is_valid_rejection(symbol, open_, high, low, close, quote_volume, short_resistance):
        st["phase"]               = ShortPhase.READY
        st["short_rejection_high"] = high
        log.info(f"[策略-S] {symbol} → SHORT_READY | 靜態壓制 rejection_high={high:.6f}")
        return

    # [C1] EMA60 壓制
    ema60 = get_4h_ema(symbol, EMA_LONG_PERIOD)
    if ema60 is not None:
        if _is_valid_rejection(symbol, open_, high, low, close, quote_volume, ema60):
            st["phase"]               = ShortPhase.READY
            st["short_rejection_high"] = high
            log.info(f"[策略-S] {symbol} → SHORT_READY | EMA{EMA_LONG_PERIOD}壓制 rejection_high={high:.6f}")
            return

    # [C2] EMA15 壓制＋死叉確認
    ema15 = get_4h_ema(symbol, EMA_SHORT_PERIOD)
    if ema15 is not None and ema60 is not None and ema15 < ema60:
        if _is_valid_rejection(symbol, open_, high, low, close, quote_volume, ema15):
            st["phase"]               = ShortPhase.READY
            st["short_rejection_high"] = high
            log.info(
                f"[策略-S] {symbol} → SHORT_READY | "
                f"EMA{EMA_SHORT_PERIOD}壓制（死叉）rejection_high={high:.6f}"
            )


# ─── 15m 進場訊號 ─────────────────────────────────────────────────────────────

def on_new_15m_candle_short(symbol: str, candle: tuple) -> dict | None:
    """Type 2 帶量跌破做空訊號。"""
    st = models.short_strategy_state.get(symbol)
    if not st or st["phase"] != ShortPhase.READY:
        return None

    open_time_ms, _open, _high, _low, close, volume = candle
    entry_threshold = st["abandonment_low"] * (1 - BREAKOUT_BODY_PCT)

    if close >= entry_threshold:
        return None

    ohlc_deque = models.symbol_state.get(symbol, {}).get("kline_15m_ohlc")
    if ohlc_deque is None or len(ohlc_deque) < 193:
        return None

    ohlc_list     = list(ohlc_deque)
    baseline_vols = [c[5] for c in ohlc_list[-193:-1]]
    avg_vol       = sum(baseline_vols) / len(baseline_vols) if baseline_vols else 0
    if avg_vol <= 0:
        return None

    vol_ratio = volume / avg_vol
    if vol_ratio < SHORT_ENTRY_VOLUME_MIN:
        return None

    now = time.time()
    if now - st["last_alert_ts"] < STRATEGY_COOLDOWN:
        return None

    st["last_alert_ts"] = now

    stop_loss = st["short_rejection_high"]
    log.info(
        f"[策略-T2] {symbol} 觸發！"
        f"close={close:.6f} < threshold={entry_threshold:.6f} | "
        f"量能 {vol_ratio:.1f}× | 止損={stop_loss:.6f}"
    )
    return {
        "type":                "type2",
        "symbol":              symbol,
        "close":               close,
        "stop_loss":           stop_loss,
        "entry_level":         st["abandonment_low"],
        "short_resistance":    st["short_resistance"],
        "vol_ratio":           vol_ratio,
        "candle_open_time_ms": open_time_ms,
    }


# ─── 即時廢棄掃描 ─────────────────────────────────────────────────────────────

def check_short_invalidation_realtime(symbol: str) -> bool:
    """即時廢棄：markPrice 衝超壓力位時重置空頭狀態。"""
    price = models.symbol_state.get(symbol, {}).get("last_price")
    if price is None:
        return False

    st = models.short_strategy_state.get(symbol)
    if not st or st["phase"] == ShortPhase.IDLE:
        return False

    resistance = st.get("short_resistance")
    if resistance and price > resistance:
        _reset_to_idle(symbol, f"即時價格 {price:.6f} > 壓力位 {resistance:.6f}")
        return True

    return False
