import time
from enum import Enum
from ..setting import models
from ..setting.config import (
    CONSOLIDATION_MIN_HOURS,
    PUMP_THRESHOLD_BULL, PUMP_THRESHOLD_NORMAL, PUMP_THRESHOLD_BEAR,
    BTC_BULL_THRESHOLD, BTC_BEAR_THRESHOLD,
    TRIGGER_VOLUME_MULT,
    METHOD_B_GAIN_ADVANTAGE, METHOD_B_RELAXED_THRESHOLD, METHOD_B_VOLUME_RATIO,
    BREAKOUT_VOLUME_MULT, BREAKOUT_BODY_PCT,
    BREAKOUT_BODY_RATIO, BREAKOUT_ATR_PERIOD, BREAKOUT_ATR_RATIO,
    PUMP_CANDLE_TAKER_BUY_MIN,
    TREND_FILTER_SMA_PERIOD, TREND_FILTER_ENABLED,
    STRATEGY_COOLDOWN,
    LOOKBACK_VOLUME_MULT,
    LIQUIDATION_BUFFER_CONFIRM_COUNT,
)
from .analysis_utils import (
    Direction,
    candle_gain_pct, is_directional_candle, body_barrier_price,
    is_extension, extension_price, is_invalidated,
    breakout_target, invalidation_level, is_price_invalidated,
    get_4h_volume_baseline,
    get_4h_atr, get_btc_24h_change,
)
from ..extension.utils import setup_logging

log = setup_logging()


# ─── [DIAG] 診斷計數器（暫時加入，用完後移除，見 docs/debug/diag_filter_counters.md） ──
_DIAG: dict[str, int] = {
    "ready_candles":    0,   # READY 狀態下收到的 15m K 棒總數
    "price_breakout":   0,   # 通過實體突破頂部 0.5%
    "volume_ok":        0,   # 通過量能 3.5× 檢查
    "sma_passed":       0,   # 通過 SMA 200 技術面濾波
    "body_passed":      0,   # 通過實體強度 60% 檢查
    "atr_passed":       0,   # 通過 ATR 突破力度 30% 檢查
    "taker_passed":     0,   # 通過 Pump Candle Taker Buy Ratio 65% 檢查
    "cooldown_passed":  0,   # 通過三層冷卻期檢查
    "signal_fired":     0,   # 最終成功發出訊號
}


def get_diag_stats() -> dict[str, int]:
    """回傳並重置診斷計數器（供回測結束後列印）。"""
    snapshot = dict(_DIAG)
    for k in _DIAG:
        _DIAG[k] = 0
    return snapshot


def print_diag_stats() -> None:
    """印出漏斗式診斷報告，顯示各層過濾的淘汰率。"""
    s = dict(_DIAG)
    print("\n===== [DIAG] Type 1 進場過濾漏斗 =====")
    labels = [
        ("ready_candles",   "READY 狀態 15m K"),
        ("price_breakout",  "✓ 實體突破頂部 0.5%"),
        ("volume_ok",       "✓ 量能 ≥ 3.5×"),
        ("sma_passed",      "✓ SMA 200 技術面濾波"),
        ("body_passed",     "✓ 實體強度 ≥ 60%"),
        ("atr_passed",      "✓ ATR 突破力度 ≥ 30%"),
        ("taker_passed",    "✓ Taker Buy Ratio ≥ 65%"),
        ("cooldown_passed", "✓ 三層冷卻通過"),
        ("signal_fired",    "★ 最終發出訊號"),
    ]
    prev = None
    for key, label in labels:
        val  = s[key]
        pct  = f"({val/prev*100:.1f}%)" if prev and prev > 0 else ""
        print(f"  {label:<30} {val:>6} {pct}")
        prev = val if val > 0 else prev
    print("=" * 42)
# ─── [DIAG END] ───────────────────────────────────────────────────────────────


class StrategyPhase(Enum):
    IDLE     = "idle"
    TRACKING = "tracking"
    READY    = "ready"


# ─── 狀態初始化 ───────────────────────────────────────────────────────────────

def _init_state() -> dict:
    return {
        "phase":                        StrategyPhase.IDLE,
        "pump_candle_open":             None,
        "pump_candle_close":            None,
        "pump_candle_low":              None,
        "pump_candle_high":             None,
        "pump_candle_time":             None,
        "pump_candle_gain_pct":         None,
        "pump_candle_volume_ratio":     None,
        "pump_candle_volume":           None,      # Method B 體量驗證
        "pump_candle_taker_buy_ratio":  None,      # 進場前 Pump 有效性驗證
        "is_method_b":                  False,     # 是否透過 Method B 觸發
        "consolidation_low":            None,
        "consolidation_high":           None,
        "consolidation_start_ts":       None,
        "consolidation_id":             None,      # 識別此次 consolidation（三層冷卻第一層）
        "liquidation_buffer_count":     0,         # 即時廢棄三次確認計數器
        "last_alert_ts":                0.0,
        "last_signal_consolidation_id": None,      # 已發訊號的 consolidation_id
        "last_signal_15m_time":         None,      # 已發訊號的 15m K 時間
        "last_signal_type":             None,
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
    # 保留三層冷卻所需的跨 consolidation 狀態
    new["last_alert_ts"]                = current.get("last_alert_ts", 0.0)
    new["last_signal_consolidation_id"] = current.get("last_signal_consolidation_id")
    new["last_signal_15m_time"]         = current.get("last_signal_15m_time")
    state_dict[symbol] = new


def reset_long_to_idle(symbol: str, reason: str = "") -> None:
    _reset_to_idle(symbol, reason, models.strategy_state)


# ─── 動態 PUMP_THRESHOLD ──────────────────────────────────────────────────────

def _get_dynamic_pump_threshold() -> float:
    """依 BTC 1d K 漲幅動態選擇觸發漲幅門檻。資料不足時使用正常值。"""
    btc_change = get_btc_24h_change()
    if btc_change is None:
        return PUMP_THRESHOLD_NORMAL
    if btc_change > BTC_BULL_THRESHOLD:
        return PUMP_THRESHOLD_BULL
    if btc_change < BTC_BEAR_THRESHOLD:
        return PUMP_THRESHOLD_BEAR
    return PUMP_THRESHOLD_NORMAL


# ─── 觸發判斷 ─────────────────────────────────────────────────────────────────

def _check_trigger(
    symbol: str, open_: float, close: float, quote_volume: float,
    direction: Direction,
) -> tuple[bool, float, float]:
    if not is_directional_candle(open_, close, direction):
        return False, 0.0, 0.0
    gain_pct  = candle_gain_pct(open_, close, direction)
    threshold = _get_dynamic_pump_threshold()
    if gain_pct < threshold:
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
    quote_volume: float, taker_buy_ratio: float,
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
        "phase":                       StrategyPhase.TRACKING,
        "pump_candle_open":            open_,
        "pump_candle_close":           close,
        "pump_candle_low":             low,
        "pump_candle_high":            high,
        "pump_candle_time":            current_ts,
        "pump_candle_gain_pct":        gain_pct,
        "pump_candle_volume_ratio":    vol_ratio,
        "pump_candle_volume":          quote_volume,
        "pump_candle_taker_buy_ratio": taker_buy_ratio,
        "is_method_b":                 is_method_b,
        "consolidation_low":           new_conso_low,
        "consolidation_high":          new_conso_high,
        "consolidation_start_ts":      current_ts,
        "consolidation_id":            current_ts,  # 觸發 K 時間戳作唯一 ID
        "liquidation_buffer_count":    0,
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


def _maybe_apply_method_c(
    st: dict, symbol: str,
    open_: float, close: float, high: float, low: float,
    current_ts: float, gain_pct: float, vol_ratio: float,
    quote_volume: float, taker_buy_ratio: float,
    direction: Direction,
) -> None:
    """Method C（ADR-006）：TRACKING 階段，從原基準K棒頂/底延伸超過 METHOD_B_RELAXED_THRESHOLD
    後，允許更換基準K棒。體量比較基準為當下 pump_candle_volume，不是任何延伸K棒。
    """
    if direction == Direction.LONG:
        pump_ref    = st.get("pump_candle_high") or 0
        current_ext = st.get("consolidation_high") or 0
        gate_pct    = (current_ext - pump_ref) / pump_ref * 100 if pump_ref > 0 else 0
    else:
        pump_ref    = st.get("pump_candle_low") or 0
        current_ext = st.get("consolidation_low") or float("inf")
        gate_pct    = (pump_ref - current_ext) / pump_ref * 100 if pump_ref > 0 else 0

    if gate_pct <= METHOD_B_RELAXED_THRESHOLD:
        log.debug(
            f"[策略-L] {symbol} Method C gate 未開啟 "
            f"{gate_pct:.1f}% ≤ {METHOD_B_RELAXED_THRESHOLD}%"
        )
        return

    prev_volume = st.get("pump_candle_volume") or 0
    if prev_volume > 0 and quote_volume < prev_volume * METHOD_B_VOLUME_RATIO:
        log.debug(
            f"[策略-L] {symbol} Method C 體量不足 "
            f"{quote_volume:.0f} < {prev_volume * METHOD_B_VOLUME_RATIO:.0f}"
        )
        return

    _apply_trigger(
        st, symbol, open_, close, high, low,
        current_ts, gain_pct, vol_ratio, quote_volume, taker_buy_ratio,
        direction, is_method_b=True,
    )
    log.info(
        f"[策略-L] {symbol} Method C 觸發 "
        f"漲幅={gain_pct:.1f}% gate={gate_pct:.1f}% → 更換基準K棒"
    )


# ─── 4h 狀態機 ───────────────────────────────────────────────────────────────

def on_new_4h_candle_long(
    symbol: str, candle: tuple,
    direction: Direction = Direction.LONG,
) -> None:
    """處理新 4h K 棒收盤，更新多頭狀態機。"""
    st = get_or_init_long_state(symbol)
    open_time_ms, open_, high, low, close, quote_volume, taker_buy_vol = candle
    current_ts      = open_time_ms / 1000
    taker_buy_ratio = taker_buy_vol / quote_volume if quote_volume > 0 else 0.0

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
            # Method C：延伸根若同時是有效拉漲K且 gate 已開啟，更換基準K棒
            is_c_trigger, c_gain, c_vol_ratio = _check_trigger(
                symbol, open_, close, quote_volume, direction
            )
            if is_c_trigger:
                _maybe_apply_method_c(
                    st, symbol, open_, close, high, low,
                    current_ts, c_gain, c_vol_ratio, quote_volume, taker_buy_ratio, direction,
                )
            _maybe_transition_to_ready(st, current_ts, symbol)
            return None

    # 觸發判斷（IDLE 首次觸發 / READY 的 Method B）
    is_trigger, gain_pct, vol_ratio = _check_trigger(symbol, open_, close, quote_volume, direction)

    if is_trigger:
        if st["phase"] == StrategyPhase.IDLE:
            _apply_trigger(
                st, symbol, open_, close, high, low,
                current_ts, gain_pct, vol_ratio, quote_volume, taker_buy_ratio, direction,
            )
        elif st["phase"] == StrategyPhase.TRACKING:
            # Method C：非創新高的拉漲K，若 gate 已開啟則更換基準K棒
            _maybe_apply_method_c(
                st, symbol, open_, close, high, low,
                current_ts, gain_pct, vol_ratio, quote_volume, taker_buy_ratio, direction,
            )
        elif st["phase"] == StrategyPhase.READY:
            prev_volume = st.get("pump_candle_volume") or 0
            # Method B 前置體量驗證：新 K volume 需 ≥ 前觸發 K volume × METHOD_B_VOLUME_RATIO
            if prev_volume > 0 and quote_volume < prev_volume * METHOD_B_VOLUME_RATIO:
                log.debug(
                    f"[策略-L] {symbol} Method B 體量不足 "
                    f"{quote_volume:.0f} < {prev_volume * METHOD_B_VOLUME_RATIO:.0f}"
                )
            else:
                prev_gain = _pump_gain_pct(st, direction)
                if prev_gain > METHOD_B_RELAXED_THRESHOLD:
                    _apply_trigger(
                        st, symbol, open_, close, high, low,
                        current_ts, gain_pct, vol_ratio, quote_volume, taker_buy_ratio,
                        direction, is_method_b=False,
                    )
                elif gain_pct > prev_gain * (1 + METHOD_B_GAIN_ADVANTAGE / 100):
                    _apply_trigger(
                        st, symbol, open_, close, high, low,
                        current_ts, gain_pct, vol_ratio, quote_volume, taker_buy_ratio,
                        direction, is_method_b=True,
                    )

    _maybe_transition_to_ready(st, current_ts, symbol)
    return None


# ─── 15m 進場訊號 ─────────────────────────────────────────────────────────────

def on_new_15m_candle_long(
    symbol: str, candle: tuple,
    direction: Direction = Direction.LONG,
) -> dict | None:
    """Type 1 帶量突破做多訊號（V2 多層確認）。"""
    st = models.strategy_state.get(symbol)
    if not st or st["phase"] != StrategyPhase.READY:
        return None

    _DIAG["ready_candles"] += 1  # [DIAG]

    open_time_ms, open_15m, high, low, close, volume, _taker = candle
    top                = breakout_target(st, direction)
    breakout_threshold = top * (1 + BREAKOUT_BODY_PCT)

    if close <= breakout_threshold:
        return None

    _DIAG["price_breakout"] += 1  # [DIAG]

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

    _DIAG["volume_ok"] += 1  # [DIAG]

    # ── 第一層：技術面濾波（SMA 200）────────────────────────────────────────────
    if TREND_FILTER_ENABLED:
        ohlc_4h = models.symbol_state.get(symbol, {}).get("kline_4h_ohlc")
        if ohlc_4h and len(ohlc_4h) >= TREND_FILTER_SMA_PERIOD:
            closes_4h    = [c[4] for c in ohlc_4h]
            sma_200      = sum(closes_4h[-TREND_FILTER_SMA_PERIOD:]) / TREND_FILTER_SMA_PERIOD
            current_4h_c = closes_4h[-1]
            if current_4h_c < sma_200:
                log.debug(
                    f"[策略-T1] {symbol} 技術面濾波拒絕 "
                    f"close={current_4h_c:.6f} < SMA200={sma_200:.6f}"
                )
                return None

    _DIAG["sma_passed"] += 1  # [DIAG]

    # ── 第二層：突破確認強化 ─────────────────────────────────────────────────────
    candle_range      = high - low
    candle_body_ratio = (close - open_15m) / candle_range if candle_range > 0 else 0.0
    if candle_body_ratio < BREAKOUT_BODY_RATIO:
        log.debug(
            f"[策略-T1] {symbol} 實體強度不足 {candle_body_ratio:.2f} < {BREAKOUT_BODY_RATIO}"
        )
        return None

    _DIAG["body_passed"] += 1  # [DIAG]

    atr_4h = get_4h_atr(symbol, BREAKOUT_ATR_PERIOD)
    if atr_4h is not None and (close - top) < atr_4h * BREAKOUT_ATR_RATIO:
        log.debug(
            f"[策略-T1] {symbol} ATR 突破力度不足 "
            f"{close - top:.6f} < ATR×{BREAKOUT_ATR_RATIO}={atr_4h * BREAKOUT_ATR_RATIO:.6f}"
        )
        return None
    breakout_atr_ratio = (close - top) / atr_4h if (atr_4h and atr_4h > 0) else None

    _DIAG["atr_passed"] += 1  # [DIAG]

    # ── 第三層：Pump Candle Taker Buy Ratio ──────────────────────────────────────
    taker_ratio = st.get("pump_candle_taker_buy_ratio") or 0.0
    if taker_ratio < PUMP_CANDLE_TAKER_BUY_MIN:
        log.debug(
            f"[策略-T1] {symbol} Pump Candle Taker Buy Ratio 不足 "
            f"{taker_ratio:.2f} < {PUMP_CANDLE_TAKER_BUY_MIN}"
        )
        return None

    _DIAG["taker_passed"] += 1  # [DIAG]

    # ── 冷卻期三層檢查 ───────────────────────────────────────────────────────────
    now              = time.time()
    consolidation_id = st.get("consolidation_id")

    # 第一層：同一 consolidation 單次消耗
    if consolidation_id is not None and consolidation_id == st.get("last_signal_consolidation_id"):
        return None
    # 第二層：全局 4h 冷卻
    if now - st["last_alert_ts"] < STRATEGY_COOLDOWN:
        return None
    # 第三層：同一 15m K 單次發出（防技術故障重複）
    if st.get("last_signal_15m_time") == open_time_ms:
        return None

    _DIAG["cooldown_passed"] += 1  # [DIAG]

    # ── 止損計算（V2：非連續，找所有放量 K 最低點）───────────────────────────────
    _4H_MS             = 4 * 3600 * 1000
    current_4h_open_ms = (open_time_ms // _4H_MS) * _4H_MS
    lookback_threshold = avg_vol * LOOKBACK_VOLUME_MULT

    stop_loss   = low
    strong_lows = []
    start_i     = len(ohlc_list) - 2
    end_i       = max(len(ohlc_list) - 22, -1)  # 最多回掃 20 根

    for i in range(start_i, end_i, -1):
        if i < 0:
            break
        prev = ohlc_list[i]
        if prev[0] < current_4h_open_ms:
            break                           # 跨 4h 邊界停止
        if prev[5] > lookback_threshold:
            strong_lows.append(prev[3])     # 記錄放量 K 的 low，不要求連續

    if strong_lows:
        stop_loss = min(low, min(strong_lows))
    stop_loss = max(stop_loss, st["consolidation_low"])

    # ── 發出訊號，更新三層冷卻狀態 ──────────────────────────────────────────────
    st["last_alert_ts"]                = now
    st["last_signal_consolidation_id"] = consolidation_id
    st["last_signal_15m_time"]         = open_time_ms
    st["last_signal_type"]             = "type1"

    _DIAG["signal_fired"] += 1  # [DIAG]

    log.info(
        f"[策略-T1] {symbol} 觸發！"
        f"close={close:.6f} > threshold={breakout_threshold:.6f} | "
        f"量能 {vol_ratio:.1f}× | 止損={stop_loss:.6f}"
    )
    return {
        "type":                        "type1",
        "symbol":                      symbol,
        "close":                       close,
        "stop_loss":                   stop_loss,
        "top":                         top,
        "bottom":                      st["consolidation_low"],
        "vol_ratio":                   vol_ratio,
        "pump_time":                   st["pump_candle_time"],
        "pump_high":                   st["pump_candle_high"],
        "pump_low":                    st["pump_candle_low"],
        "candle_open_time_ms":         open_time_ms,
        # V2 新增欄位
        "trend_filter_status":         "passed",
        "pump_candle_taker_buy_ratio": taker_ratio,
        "candle_body_ratio":           candle_body_ratio,
        "breakout_atr_ratio":          breakout_atr_ratio,
        "method_b_triggered":          st.get("is_method_b", False),
    }


# ─── 即時廢棄掃描 ─────────────────────────────────────────────────────────────

def check_long_invalidation_realtime(
    symbol: str,
    direction: Direction = Direction.LONG,
) -> bool:
    """即時廢棄（V2：三次確認機制）。
    markPrice 連續 LIQUIDATION_BUFFER_CONFIRM_COUNT 次低於廢棄線才執行。
    中途反彈回去則重置計數。不觸發空頭策略。
    """
    price = models.symbol_state.get(symbol, {}).get("last_price")
    if price is None:
        return False

    st = models.strategy_state.get(symbol)
    if not st or st["phase"] == StrategyPhase.IDLE:
        if st and st.get("liquidation_buffer_count", 0) > 0:
            st["liquidation_buffer_count"] = 0
        return False

    if is_price_invalidated(price, st, direction):
        count = st.get("liquidation_buffer_count", 0) + 1
        st["liquidation_buffer_count"] = count
        log.debug(
            f"[策略-L] {symbol} 即時廢棄確認 {count}/{LIQUIDATION_BUFFER_CONFIRM_COUNT} "
            f"price={price:.6f}"
        )
        if count >= LIQUIDATION_BUFFER_CONFIRM_COUNT:
            level = invalidation_level(st, direction)
            op    = "<" if direction == Direction.LONG else ">"
            _reset_to_idle(
                symbol,
                f"即時三次確認廢棄 price={price:.6f} {op} 廢棄線={level:.6f}",
                models.strategy_state,
            )
            return True
    else:
        if st.get("liquidation_buffer_count", 0) > 0:
            st["liquidation_buffer_count"] = 0

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
