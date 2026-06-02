import time
from enum import Enum
from ..setting import models
from ..setting.config import (
    CONSOLIDATION_MIN_HOURS,
    PUMP_THRESHOLD, TRIGGER_VOLUME_MULT,
    METHOD_B_GAIN_ADVANTAGE, METHOD_B_RELAXED_THRESHOLD,
    BREAKOUT_VOLUME_MULT, BREAKOUT_BODY_PCT, STRATEGY_COOLDOWN,
    LOOKBACK_VOLUME_MULT,
)
from .analysis_utils import (
    Direction,
    candle_gain_pct, is_directional_candle, body_barrier_price,
    is_extension, extension_price, is_invalidated,
    breakout_target, invalidation_level, is_price_invalidated,
    get_4h_volume_baseline,
)
from ..extension.utils import setup_logging

log = setup_logging()


class StrategyPhase(Enum):
    IDLE     = "idle"
    TRACKING = "tracking"
    READY    = "ready"


# ─── 狀態初始化 ───────────────────────────────────────────────────────────────

def _init_state() -> dict:
    return {
        "phase":                  StrategyPhase.IDLE,
        "pump_candle_open":         None,
        "pump_candle_close":        None,
        "pump_candle_low":          None,
        "pump_candle_high":         None,
        "pump_candle_time":         None,
        "pump_candle_gain_pct":     None,
        "pump_candle_volume_ratio": None,
        "consolidation_low":      None,
        "consolidation_high":     None,
        "consolidation_start_ts": None,
        "last_alert_ts":          0.0,
        "last_signal_type":       None,
    }


def get_or_init_long_state(symbol: str) -> dict:
    if symbol not in models.strategy_state:
        models.strategy_state[symbol] = _init_state()
    return models.strategy_state[symbol]


# ─── 狀態重置 ─────────────────────────────────────────────────────────────────

def _reset_to_idle(symbol: str, reason: str, state_dict: dict) -> None:
    current = state_dict.get(symbol, {})
    if current.get("phase") not in (None, StrategyPhase.IDLE):
        log.info(f"[策略-L] {symbol} → IDLE | {reason}")
    new = _init_state()
    new["last_alert_ts"] = current.get("last_alert_ts", 0.0)
    state_dict[symbol] = new


def reset_long_to_idle(symbol: str, reason: str = "") -> None:
    _reset_to_idle(symbol, reason, models.strategy_state)


# ─── 觸發判斷 ─────────────────────────────────────────────────────────────────

def _check_trigger(
    symbol: str, open_: float, close: float, quote_volume: float,
    direction: Direction,
) -> tuple[bool, float, float]:
    if not is_directional_candle(open_, close, direction):
        return False, 0.0, 0.0
    gain_pct = candle_gain_pct(open_, close, direction)
    if gain_pct < PUMP_THRESHOLD:
        return False, gain_pct, 0.0
    avg = get_4h_volume_baseline(symbol)
    if avg is None or avg <= 0:
        return False, gain_pct, 0.0
    vol_ratio = quote_volume / avg
    if vol_ratio <= TRIGGER_VOLUME_MULT:  # 規格要求嚴格 > threshold
        return False, gain_pct, vol_ratio
    return True, gain_pct, vol_ratio


def _pump_gain_pct(st: dict, direction: Direction) -> float:
    return candle_gain_pct(st["pump_candle_open"], st["pump_candle_close"], direction)


# ─── 狀態操作 ─────────────────────────────────────────────────────────────────

def _apply_trigger(
    st: dict, symbol: str,
    open_: float, close: float, high: float, low: float,
    current_ts: float, gain_pct: float, vol_ratio: float,
    direction: Direction, is_method_b: bool = False,
) -> None:
    label = "Method B" if is_method_b else "IDLE → TRACKING"
    log.info(
        f"[策略-L] {symbol} {label} | 漲幅={gain_pct:.1f}% 底={low:.6f} 頂={high:.6f}"
    )
    if is_method_b:
        new_conso_high = (
            max(st["consolidation_high"], high) if direction == Direction.LONG else high
        )
        new_conso_low = (
            low if direction == Direction.LONG else min(st["consolidation_low"], low)
        )
    else:
        new_conso_high = high
        new_conso_low  = low

    st.update({
        "phase":                  StrategyPhase.TRACKING,
        "pump_candle_open":         open_,
        "pump_candle_close":        close,
        "pump_candle_low":          low,
        "pump_candle_high":         high,
        "pump_candle_time":         current_ts,
        "pump_candle_gain_pct":     gain_pct,
        "pump_candle_volume_ratio": vol_ratio,
        "consolidation_low":      new_conso_low,
        "consolidation_high":     new_conso_high,
        "consolidation_start_ts": current_ts,
    })


def _maybe_transition_to_ready(st: dict, current_ts: float, symbol: str) -> None:
    if st["phase"] != StrategyPhase.TRACKING:
        return
    elapsed_h = (current_ts - st["consolidation_start_ts"]) / 3600
    if elapsed_h >= CONSOLIDATION_MIN_HOURS:
        st["phase"] = StrategyPhase.READY
        log.info(
            f"[策略-L] {symbol} TRACKING → READY | "
            f"已盤整 {elapsed_h:.1f}h | "
            f"底部={st['consolidation_low']:.6f} 頂部={st['consolidation_high']:.6f}"
        )


# ─── 4h 狀態機 ───────────────────────────────────────────────────────────────

def on_new_4h_candle_long(
    symbol: str, candle: tuple,
    direction: Direction = Direction.LONG,
) -> None:
    """處理新 4h K 棒收盤，更新多頭狀態機。"""
    st = get_or_init_long_state(symbol)
    open_time_ms, open_, high, low, close, quote_volume = candle
    current_ts = open_time_ms / 1000

    # 廢棄：實體收破廢棄線
    if st["phase"] != StrategyPhase.IDLE and st["consolidation_low"] is not None:
        if is_invalidated(open_, close, st, direction):
            _reset_to_idle(
                symbol,
                f"實體={body_barrier_price(open_, close, direction):.6f} 破廢棄線={invalidation_level(st, direction):.6f}",
                models.strategy_state,
            )
            return

    # 延伸：TRACKING/READY 創新高（多）或新低（空）→ 更新邊界、重置計時
    if st["phase"] != StrategyPhase.IDLE and st["consolidation_high"] is not None:
        if is_extension(high, low, st, direction):
            ext_price  = extension_price(high, low, direction)
            prev_phase = st["phase"]
            if direction == Direction.LONG:
                st["consolidation_high"] = ext_price
            else:
                st["consolidation_low"] = ext_price
            st["consolidation_start_ts"] = current_ts
            st["phase"] = StrategyPhase.TRACKING
            note = "（原 READY 回退）" if prev_phase == StrategyPhase.READY else ""
            log.info(f"[策略-L] {symbol} 延伸 {ext_price:.6f}{note} → 計時重置")
            _maybe_transition_to_ready(st, current_ts, symbol)
            return None

    # 觸發判斷（IDLE 首次觸發 / TRACKING+READY 的 Method B）
    is_trigger, gain_pct, vol_ratio = _check_trigger(symbol, open_, close, quote_volume, direction)

    if is_trigger:
        if st["phase"] == StrategyPhase.IDLE:
            _apply_trigger(st, symbol, open_, close, high, low, current_ts, gain_pct, vol_ratio, direction)
        elif st["phase"] == StrategyPhase.READY:
            prev_gain = _pump_gain_pct(st, direction)
            if prev_gain > METHOD_B_RELAXED_THRESHOLD:
                _apply_trigger(
                    st, symbol, open_, close, high, low,
                    current_ts, gain_pct, vol_ratio, direction, is_method_b=False,
                )
            elif gain_pct > prev_gain * (1 + METHOD_B_GAIN_ADVANTAGE / 100):
                _apply_trigger(
                    st, symbol, open_, close, high, low,
                    current_ts, gain_pct, vol_ratio, direction, is_method_b=True,
                )

    _maybe_transition_to_ready(st, current_ts, symbol)
    return None


# ─── 15m 進場訊號 ─────────────────────────────────────────────────────────────

def on_new_15m_candle_long(
    symbol: str, candle: tuple,
    direction: Direction = Direction.LONG,
) -> dict | None:
    """Type 1 帶量突破做多訊號。"""
    st = models.strategy_state.get(symbol)
    if not st or st["phase"] != StrategyPhase.READY:
        return None

    open_time_ms, _open, _high, low, close, volume = candle
    top               = breakout_target(st, direction)
    breakout_threshold = top * (1 + BREAKOUT_BODY_PCT)

    if close <= breakout_threshold:
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
    if vol_ratio < BREAKOUT_VOLUME_MULT:
        log.debug(
            f"[策略-T1] {symbol} 突破但量能不足 {vol_ratio:.1f}× < {BREAKOUT_VOLUME_MULT}×"
        )
        return None

    now = time.time()
    if now - st["last_alert_ts"] < STRATEGY_COOLDOWN:
        return None

    _4H_MS             = 4 * 3600 * 1000
    current_4h_open_ms = (open_time_ms // _4H_MS) * _4H_MS
    lookback_threshold = avg_vol * LOOKBACK_VOLUME_MULT

    stop_loss = low
    for i in range(len(ohlc_list) - 2, -1, -1):
        prev = ohlc_list[i]
        if prev[0] < current_4h_open_ms:
            break
        if prev[5] > lookback_threshold:
            stop_loss = min(stop_loss, prev[3])
        else:
            break

    st["last_alert_ts"]    = now
    st["last_signal_type"] = "type1"

    log.info(
        f"[策略-T1] {symbol} 觸發！"
        f"close={close:.6f} > threshold={breakout_threshold:.6f} | "
        f"量能 {vol_ratio:.1f}× | 止損={stop_loss:.6f}"
    )
    return {
        "type":                "type1",
        "symbol":              symbol,
        "close":               close,
        "stop_loss":           stop_loss,
        "top":                 top,
        "bottom":              st["consolidation_low"],
        "vol_ratio":           vol_ratio,
        "pump_time":           st["pump_candle_time"],
        "pump_high":           st["pump_candle_high"],
        "pump_low":            st["pump_candle_low"],
        "candle_open_time_ms": open_time_ms,
    }


# ─── 即時廢棄掃描 ─────────────────────────────────────────────────────────────

def check_long_invalidation_realtime(
    symbol: str,
    direction: Direction = Direction.LONG,
) -> bool:
    """即時廢棄：markPrice 突破廢棄線時重置多頭狀態。
    不觸發空頭策略（等待 4h 收盤確認後由 on_new_4h_candle_long 觸發）。
    """
    price = models.symbol_state.get(symbol, {}).get("last_price")
    if price is None:
        return False

    st = models.strategy_state.get(symbol)
    if not st or st["phase"] == StrategyPhase.IDLE:
        return False

    if is_price_invalidated(price, st, direction):
        level = invalidation_level(st, direction)
        op    = "<" if direction == Direction.LONG else ">"
        _reset_to_idle(
            symbol,
            f"即時價格 {price:.6f} {op} 廢棄線 {level:.6f}",
            models.strategy_state,
        )
        return True

    return False


# ─── 歷史回播 ─────────────────────────────────────────────────────────────────

def replay_historical_4h_candles_long(
    symbol: str,
    direction: Direction = Direction.LONG,
) -> None:
    """重播歷史 4h OHLC，啟動時恢復多頭盤整狀態。"""
    ohlc_deque = models.symbol_state.get(symbol, {}).get("kline_4h_ohlc")
    if not ohlc_deque:
        return

    models.strategy_state.pop(symbol, None)

    for candle in ohlc_deque:
        on_new_4h_candle_long(symbol, candle, direction)

    phase = models.strategy_state.get(symbol, {}).get("phase", StrategyPhase.IDLE).value
    log.info(f"[策略-L] {symbol} 歷史回播完成 | 多頭={phase}")
