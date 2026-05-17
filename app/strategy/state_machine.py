import time
from enum import Enum
from ..setting import models
from ..setting.config import (
    CONSOLIDATION_MIN_HOURS,
    PUMP_THRESHOLD, TRIGGER_VOLUME_MULT, TRIGGER_VOLUME_BASELINE_N,
    METHOD_B_GAIN_ADVANTAGE, METHOD_B_RELAXED_THRESHOLD,
    BREAKOUT_VOLUME_MULT, BREAKOUT_BODY_PCT, STRATEGY_COOLDOWN,
    LOOKBACK_VOLUME_MULT,
)
from ..extension.utils import setup_logging

log = setup_logging()


class StrategyPhase(Enum):
    IDLE     = "idle"      # 閒置，尚未偵測到觸發 K
    TRACKING = "tracking"  # 偵測到觸發 K，追蹤盤整中
    READY    = "ready"     # 盤整成熟，監控進場訊號


class Direction(Enum):
    LONG  = "long"
    SHORT = "short"   # 保留，供後續空頭策略擴充


# ─── 狀態初始化 ───────────────────────────────────────────────────────────────

def _init_state() -> dict:
    return {
        "phase":                  StrategyPhase.IDLE,
        # ── 觸發 K 棒資訊 ─────────────────────────────
        "pump_candle_open":         None,
        "pump_candle_close":        None,
        "pump_candle_low":          None,
        "pump_candle_high":         None,
        "pump_candle_time":         None,
        "pump_candle_gain_pct":     None,
        "pump_candle_volume_ratio": None,
        # ── 盤整邊界 ──────────────────────────────────
        "consolidation_low":      None,   # 多頭廢棄線 / 空頭突破目標
        "consolidation_high":     None,   # 多頭突破目標 / 空頭廢棄線
        "consolidation_start_ts": None,   # 最後一次創新高/低的時間（計時起點）
        # ── 冷卻 ──────────────────────────────────────
        "last_alert_ts":          0.0,
        "last_signal_type":       None,
    }


# ─── 方向感知工具函數 ─────────────────────────────────────────────────────────
# 設計原則：所有比較邏輯透過這組函數隔離，新增空頭策略只需對應 Direction.SHORT 分支。

def _candle_gain_pct(open_: float, close: float, direction: Direction) -> float:
    """多頭：漲幅；空頭：跌幅。正值代表「朝方向移動」。"""
    if direction == Direction.LONG:
        return (close - open_) / open_ * 100
    return (open_ - close) / open_ * 100


def _is_directional_candle(open_: float, close: float, direction: Direction) -> bool:
    """多頭：陽線（close > open）；空頭：陰線（close < open）。"""
    return close > open_ if direction == Direction.LONG else close < open_


def _body_barrier_price(open_: float, close: float, direction: Direction) -> float:
    """廢棄比較用的實體邊界：多頭取實體低點，空頭取實體高點。"""
    return min(open_, close) if direction == Direction.LONG else max(open_, close)


def _is_extension(high: float, low: float, st: dict, direction: Direction) -> bool:
    """K 棒是否突破（延伸）盤整邊界：多頭創新高，空頭創新低。"""
    return (
        high > st["consolidation_high"] if direction == Direction.LONG
        else low < st["consolidation_low"]
    )


def _extension_price(high: float, low: float, direction: Direction) -> float:
    """延伸時要更新的邊界值。"""
    return high if direction == Direction.LONG else low


def _is_invalidated(open_: float, close: float, st: dict, direction: Direction) -> bool:
    """實體收破廢棄線：多頭實體低點 < consolidation_low，空頭實體高點 > consolidation_high。"""
    barrier = _body_barrier_price(open_, close, direction)
    return (
        barrier < st["consolidation_low"] if direction == Direction.LONG
        else barrier > st["consolidation_high"]
    )


def _breakout_target(st: dict, direction: Direction) -> float:
    """進場突破目標價：多頭 = 頂部，空頭 = 底部。"""
    return st["consolidation_high"] if direction == Direction.LONG else st["consolidation_low"]


def _invalidation_level(st: dict, direction: Direction) -> float | None:
    """即時廢棄比較基準：多頭 = 底部，空頭 = 頂部。"""
    return st["consolidation_low"] if direction == Direction.LONG else st["consolidation_high"]


def _is_price_invalidated(price: float, st: dict, direction: Direction) -> bool:
    """即時 markPrice 是否觸發廢棄：多頭跌破底部，空頭漲破頂部。"""
    level = _invalidation_level(st, direction)
    if level is None:
        return False
    return price < level if direction == Direction.LONG else price > level


# ─── 量能工具 ─────────────────────────────────────────────────────────────────

def _get_trigger_volume_baseline(symbol: str) -> float | None:
    """取前 TRIGGER_VOLUME_BASELINE_N 根 4h K 的基準均量（排除最後一根=當根）。"""
    ohlc = models.symbol_state.get(symbol, {}).get("kline_4h_ohlc")
    if not ohlc:
        return None
    prev = list(ohlc)[:-1]
    baseline = prev[-TRIGGER_VOLUME_BASELINE_N:]
    if not baseline:
        return None
    return sum(c[5] for c in baseline) / len(baseline)



# ─── 觸發判斷 ─────────────────────────────────────────────────────────────────

def _check_trigger(
    symbol: str, open_: float, close: float, quote_volume: float,
    direction: Direction,
) -> tuple[bool, float, float]:
    """
    單根 4h K 棒觸發判斷。
    回傳 (is_trigger, gain_pct, vol_ratio)。
    三個條件全部通過才觸發：方向、漲跌幅、量能。
    """
    if not _is_directional_candle(open_, close, direction):
        return False, 0.0, 0.0
    gain_pct = _candle_gain_pct(open_, close, direction)
    if gain_pct < PUMP_THRESHOLD:
        return False, gain_pct, 0.0
    avg = _get_trigger_volume_baseline(symbol)
    if avg is None or avg <= 0:
        return False, gain_pct, 0.0
    vol_ratio = quote_volume / avg
    if vol_ratio < TRIGGER_VOLUME_MULT:
        return False, gain_pct, vol_ratio
    return True, gain_pct, vol_ratio


def _pump_gain_pct(st: dict, direction: Direction) -> float:
    """當前觸發 K 棒的漲幅，供 Method B 比較用。"""
    return _candle_gain_pct(st["pump_candle_open"], st["pump_candle_close"], direction)


# ─── 狀態操作 ─────────────────────────────────────────────────────────────────

def _get_or_init(symbol: str, state_dict: dict) -> dict:
    if symbol not in state_dict:
        state_dict[symbol] = _init_state()
    return state_dict[symbol]


def _reset_to_idle(symbol: str, reason: str, state_dict: dict) -> None:
    current = state_dict.get(symbol, {})
    if current.get("phase") not in (None, StrategyPhase.IDLE):
        log.info(f"[策略] {symbol} → IDLE | {reason}")
    new = _init_state()
    new["last_alert_ts"] = current.get("last_alert_ts", 0.0)
    state_dict[symbol] = new


def _apply_trigger(
    st: dict, symbol: str,
    open_: float, close: float, high: float, low: float,
    current_ts: float, gain_pct: float, vol_ratio: float,
    direction: Direction, is_method_b: bool = False,
) -> None:
    """將觸發 K 套用到狀態（IDLE→TRACKING 或 Method B 完整重置）。

    Method B 時 consolidation_high（多頭）保留歷史最高值，確保頂部不因 Method B 而下移。
    """
    label = "Method B" if is_method_b else "IDLE → TRACKING"
    log.info(
        f"[策略-{'L' if direction == Direction.LONG else 'S'}] "
        f"{symbol} {label} | 漲幅={gain_pct:.1f}% 底={low:.6f} 頂={high:.6f}"
    )

    # Method B：保留已延伸的頂部（多頭）或底部（空頭）不讓其退縮
    if is_method_b:
        new_conso_high = (
            max(st["consolidation_high"], high) if direction == Direction.LONG
            else high
        )
        new_conso_low = (
            low if direction == Direction.LONG
            else min(st["consolidation_low"], low)
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
    """TRACKING → READY：從最後一次創新高/低起已盤整 >= CONSOLIDATION_MIN_HOURS。"""
    if st["phase"] != StrategyPhase.TRACKING:
        return
    elapsed_h = (current_ts - st["consolidation_start_ts"]) / 3600
    if elapsed_h >= CONSOLIDATION_MIN_HOURS:
        st["phase"] = StrategyPhase.READY
        log.info(
            f"[策略] {symbol} TRACKING → READY | "
            f"已盤整 {elapsed_h:.1f}h | "
            f"底部={st['consolidation_low']:.6f} 頂部={st['consolidation_high']:.6f}"
        )


# ─── 向下相容的公開別名 ───────────────────────────────────────────────────────

def get_or_init_strategy_state(symbol: str) -> dict:
    return _get_or_init(symbol, models.strategy_state)


def reset_to_idle(symbol: str, reason: str = "") -> None:
    _reset_to_idle(symbol, reason, models.strategy_state)


# ─── 4h 狀態機 ───────────────────────────────────────────────────────────────

def on_new_4h_candle(
    symbol: str, candle: tuple,
    direction: Direction = Direction.LONG,
) -> None:
    """處理新 4h K 棒收盤：更新策略狀態機（IDLE / TRACKING / READY）。

    candle: (open_time_ms, open, high, low, close, quote_volume)
    direction: 預設多頭；空頭策略未來傳入 Direction.SHORT。
    """
    st = _get_or_init(symbol, models.strategy_state)
    open_time_ms, open_, high, low, close, quote_volume = candle
    current_ts = open_time_ms / 1000

    # ── 廢棄：實體收破廢棄線 ────────────────────────────────────────────────
    if st["phase"] != StrategyPhase.IDLE and st["consolidation_low"] is not None:
        if _is_invalidated(open_, close, st, direction):
            _reset_to_idle(
                symbol,
                f"實體={_body_barrier_price(open_, close, direction):.6f} 破廢棄線={_invalidation_level(st, direction):.6f}",
                models.strategy_state,
            )
            return

    # ── 整體延伸：TRACKING/READY 創新高（多）或新低（空）→ 更新邊界、重置計時 ──
    if st["phase"] != StrategyPhase.IDLE and st["consolidation_high"] is not None:
        if _is_extension(high, low, st, direction):
            ext_price  = _extension_price(high, low, direction)
            prev_phase = st["phase"]
            if direction == Direction.LONG:
                st["consolidation_high"] = ext_price
            else:
                st["consolidation_low"] = ext_price
            st["consolidation_start_ts"] = current_ts
            st["phase"] = StrategyPhase.TRACKING
            note = "（原 READY 回退）" if prev_phase == StrategyPhase.READY else ""
            log.info(f"[策略] {symbol} 延伸 {ext_price:.6f}{note} → 計時重置")
            _maybe_transition_to_ready(st, current_ts, symbol)
            return

    # ── 觸發判斷（IDLE 首次觸發 / TRACKING+READY 的 Method B）──────────────
    is_trigger, gain_pct, vol_ratio = _check_trigger(symbol, open_, close, quote_volume, direction)

    if is_trigger:
        if st["phase"] == StrategyPhase.IDLE:
            _apply_trigger(st, symbol, open_, close, high, low, current_ts, gain_pct, vol_ratio, direction)
        else:
            prev_gain = _pump_gain_pct(st, direction)
            if prev_gain > METHOD_B_RELAXED_THRESHOLD:
                # 原始觸發 K 漲幅過大，無法被 N+1% 超越：滿足觸發條件即完整重置
                _apply_trigger(
                    st, symbol, open_, close, high, low,
                    current_ts, gain_pct, vol_ratio, direction, is_method_b=False,
                )
            elif gain_pct > prev_gain + METHOD_B_GAIN_ADVANTAGE:
                # Method B：新觸發 K 漲幅 > 前觸發 K 漲幅 + METHOD_B_GAIN_ADVANTAGE
                _apply_trigger(
                    st, symbol, open_, close, high, low,
                    current_ts, gain_pct, vol_ratio, direction, is_method_b=True,
                )

    _maybe_transition_to_ready(st, current_ts, symbol)


# ─── 15m 進場訊號 ─────────────────────────────────────────────────────────────

def on_new_15m_candle(
    symbol: str, candle: tuple,
    direction: Direction = Direction.LONG,
) -> dict | None:
    """處理新 15m K 棒收盤：Type 1 帶量突破做多（或未來空頭版本）。

    candle: (open_time_ms, open, high, low, close, quote_volume)
    回傳訊號 dict 或 None。
    """
    st = models.strategy_state.get(symbol)
    if not st or st["phase"] != StrategyPhase.READY:
        return None

    open_time_ms, _open, _high, low, close, volume = candle
    top               = _breakout_target(st, direction)
    breakout_threshold = top * (1 + BREAKOUT_BODY_PCT)

    if close <= breakout_threshold:
        return None

    ohlc_deque = models.symbol_state.get(symbol, {}).get("kline_15m_ohlc")
    if ohlc_deque is None or len(ohlc_deque) < 193:
        return None

    ohlc_list     = list(ohlc_deque)
    baseline_vols = [c[5] for c in ohlc_list[-193:-1]]  # 前 192 根，排除當根
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

    # 止損：往回掃連續放量 K 的最低 low，不超過當前 4h K 棒起點
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

def check_invalidation_realtime(
    symbol: str,
    direction: Direction = Direction.LONG,
) -> bool:
    """即時廢棄：markPrice 突破廢棄線時廢棄。由 periodic_screen 每 10 秒呼叫。"""
    price = models.symbol_state.get(symbol, {}).get("last_price")
    if price is None:
        return False

    st = models.strategy_state.get(symbol)
    if not st or st["phase"] == StrategyPhase.IDLE:
        return False

    if _is_price_invalidated(price, st, direction):
        level = _invalidation_level(st, direction)
        op    = "<" if direction == Direction.LONG else ">"
        _reset_to_idle(
            symbol,
            f"即時價格 {price:.6f} {op} 廢棄線 {level:.6f}",
            models.strategy_state,
        )
        return True

    return False


def scan_strategy(symbol: str) -> None:
    """periodic_screen 呼叫的入口：執行即時廢棄檢查。"""
    check_invalidation_realtime(symbol)


# ─── 歷史回播 ─────────────────────────────────────────────────────────────────

def replay_historical_4h_candles(
    symbol: str,
    direction: Direction = Direction.LONG,
) -> None:
    """重播歷史 4h OHLC，啟動時恢復進行中的盤整狀態。"""
    ohlc_deque = models.symbol_state.get(symbol, {}).get("kline_4h_ohlc")
    if not ohlc_deque:
        return

    models.strategy_state.pop(symbol, None)

    for candle in ohlc_deque:
        on_new_4h_candle(symbol, candle, direction)

    phase = models.strategy_state.get(symbol, {}).get("phase", StrategyPhase.IDLE).value
    log.info(f"[策略] {symbol} 歷史回播完成 | {'多頭' if direction == Direction.LONG else '空頭'}={phase}")
