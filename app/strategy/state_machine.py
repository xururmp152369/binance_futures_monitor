import time
from enum import Enum
from ..setting import models
from ..extension.utils import setup_logging

log = setup_logging()


class StrategyPhase(Enum):
    IDLE      = "idle"      # 閒置，尚未偵測到拉漲
    TRACKING  = "tracking"  # 偵測到拉漲，追蹤盤整中（< 12h）
    READY     = "ready"     # 盤整 >= 12h，開始監控進場訊號


def _init_state() -> dict:
    return {
        "phase":                  StrategyPhase.IDLE,
        "pump_candle_open":       None,
        "pump_candle_close":      None,
        "pump_candle_low":        None,  # 盤整底部 / 廢棄門檻（固定不變）
        "pump_candle_high":       None,
        "pump_candle_time":       None,  # Unix 秒（open_time）
        "consolidation_low":      None,  # = pump_candle_low
        "consolidation_high":     None,  # 後續所有 4h K 高點的最大值（持續更新）
        "consolidation_start_ts": None,
        "last_alert_ts":          0.0,
        "last_signal_type":       None,  # "type1" / "type2"
    }


def get_or_init_strategy_state(symbol: str) -> dict:
    if symbol not in models.strategy_state:
        models.strategy_state[symbol] = _init_state()
    return models.strategy_state[symbol]


def reset_to_idle(symbol: str, reason: str = "") -> None:
    current = models.strategy_state.get(symbol, {})
    if current.get("phase") not in (None, StrategyPhase.IDLE):
        log.info(f"[策略] {symbol} → IDLE | {reason}")
    new = _init_state()
    new["last_alert_ts"] = current.get("last_alert_ts", 0.0)
    models.strategy_state[symbol] = new


def _check_pump_candle(candle: tuple) -> bool:
    """單根 4h K 棒是否符合拉漲條件：(close-open)/open >= PUMP_THRESHOLD%。

    candle: (open_time_ms, open, high, low, close)
    """
    _, open_, _high, _low, close = candle
    if open_ <= 0:
        return False
    return (close - open_) / open_ * 100 >= models.runtime_config["PUMP_THRESHOLD"]


def _recalc_consolidation_range(symbol: str, current_open_time_ms: int) -> tuple[float, float] | None:
    """計算滾動 CONSOLIDATION_MIN_HOURS 視窗內的盤整頂底。

    從 kline_4h_ohlc 取出在 (current_open_time - CONSOLIDATION_MIN_HOURS) 之後的所有 K 棒，
    回傳 (consolidation_high, consolidation_low)。
    """
    ohlc = models.symbol_state.get(symbol, {}).get("kline_4h_ohlc")
    if not ohlc:
        return None
    window_start = current_open_time_ms / 1000 - models.runtime_config["CONSOLIDATION_MIN_HOURS"] * 3600
    relevant = [c for c in ohlc if c[0] / 1000 >= window_start]
    if not relevant:
        return None
    return max(c[2] for c in relevant), min(c[3] for c in relevant)


def on_new_4h_candle(symbol: str, candle: tuple) -> None:
    """處理新 4h K 棒收盤：偵測拉漲、更新盤整範圍、驅動狀態轉移。

    candle: (open_time_ms, open, high, low, close)
    在 handle_price_websocket 的 @kline_4h 收盤段呼叫。
    """
    st = get_or_init_strategy_state(symbol)
    open_time_ms, open_, high, low, close = candle
    current_ts = open_time_ms / 1000

    if st["phase"] != StrategyPhase.IDLE:
        is_new_pump = _check_pump_candle(candle)

        if is_new_pump:
            # 新拉漲 K 棒：更新基準資料，重置盤整計時回 TRACKING
            pct = (close - open_) / open_ * 100
            st["pump_candle_open"]       = open_
            st["pump_candle_close"]      = close
            st["pump_candle_low"]        = low
            st["pump_candle_high"]       = high
            st["pump_candle_time"]       = current_ts
            st["consolidation_start_ts"] = current_ts
            st["phase"]                  = StrategyPhase.TRACKING
            log.info(
                f"[策略] {symbol} 偵測到新拉漲 K 棒 → 重置為 TRACKING | "
                f"{open_:.6f}→{close:.6f} (+{pct:.1f}%)"
            )
        else:
            # 廢棄檢查：非拉漲 K 棒的 low 跌破現有盤整底部
            if low < st["consolidation_low"]:
                reset_to_idle(
                    symbol,
                    f"4h K low={low:.6f} < 盤整底部={st['consolidation_low']:.6f}",
                )
                return

        # 動態更新盤整頂底（滾動 CONSOLIDATION_MIN_HOURS 視窗）
        range_result = _recalc_consolidation_range(symbol, open_time_ms)
        if range_result:
            st["consolidation_high"], st["consolidation_low"] = range_result

        # TRACKING → READY：檢查是否達到最低盤整時數
        if st["phase"] == StrategyPhase.TRACKING:
            elapsed_h = (current_ts - st["consolidation_start_ts"]) / 3600
            if elapsed_h >= models.runtime_config["CONSOLIDATION_MIN_HOURS"]:
                st["phase"] = StrategyPhase.READY
                log.info(
                    f"[策略] {symbol} TRACKING → READY | "
                    f"已盤整 {elapsed_h:.1f}h | "
                    f"底部={st['consolidation_low']:.6f} 頂部={st['consolidation_high']:.6f}"
                )
        return

    # IDLE：檢查是否是拉漲 K 棒
    if _check_pump_candle(candle):
        pct = (close - open_) / open_ * 100
        st["phase"]                  = StrategyPhase.TRACKING
        st["pump_candle_open"]       = open_
        st["pump_candle_close"]      = close
        st["pump_candle_low"]        = low
        st["pump_candle_high"]       = high
        st["pump_candle_time"]       = current_ts
        st["consolidation_start_ts"] = current_ts
        # 初始化盤整範圍（滾動視窗，包含本根 K 棒）
        range_result = _recalc_consolidation_range(symbol, open_time_ms)
        if range_result:
            st["consolidation_high"], st["consolidation_low"] = range_result
        else:
            st["consolidation_high"] = high
            st["consolidation_low"]  = low
        log.info(
            f"[策略] {symbol} IDLE → TRACKING | "
            f"4h 拉漲 {open_:.6f}→{close:.6f} (+{pct:.1f}%)"
        )


def on_new_15m_candle(symbol: str, candle: tuple) -> dict | None:
    """處理新 15m K 棒收盤：檢查 Type 1 帶量突破訊號。

    candle: (open_time_ms, open, high, low, close, quote_volume)
    回傳訊號 dict 或 None。
    """
    st = models.strategy_state.get(symbol)
    if not st or st["phase"] != StrategyPhase.READY:
        return None

    open_time_ms, _open, _high, low, close, volume = candle
    top = st["consolidation_high"]

    # Type 1 條件 1：收盤必須超過盤整頂部
    if close <= top:
        return None

    # Type 1 條件 2：量能檢查
    ohlc_deque = models.symbol_state.get(symbol, {}).get("kline_15m_ohlc")
    if ohlc_deque is None or len(ohlc_deque) < 193:
        return None

    ohlc_list = list(ohlc_deque)
    # baseline = 倒數第 193 根到倒數第 2 根（共 192 根，排除當前根）
    baseline_vols = [c[5] for c in ohlc_list[-193:-1]]
    avg_vol = sum(baseline_vols) / len(baseline_vols) if baseline_vols else 0
    if avg_vol <= 0:
        return None

    vol_ratio = volume / avg_vol
    if vol_ratio < models.runtime_config["BREAKOUT_VOLUME_MULT"]:
        log.debug(
            f"[策略-T1] {symbol} 突破頂部但量能不足 {vol_ratio:.1f}× < {models.runtime_config['BREAKOUT_VOLUME_MULT']}×"
        )
        return None

    # 冷卻檢查
    now = time.time()
    if now - st["last_alert_ts"] < models.runtime_config["STRATEGY_COOLDOWN"]:
        return None

    st["last_alert_ts"]    = now
    st["last_signal_type"] = "type1"

    log.info(
        f"[策略-T1] {symbol} 觸發！close={close:.6f} > top={top:.6f} | "
        f"量能 {vol_ratio:.1f}× | 止損={low:.6f}"
    )
    return {
        "type":      "type1",
        "symbol":    symbol,
        "close":     close,
        "stop_loss": low,
        "top":       top,
        "bottom":    st["consolidation_low"],
        "vol_ratio": vol_ratio,
    }


def on_new_1h_candle(symbol: str, candle: tuple) -> dict | None:
    """處理新 1h K 棒收盤：檢查 Type 2 回踩 4h EMA 反彈訊號。

    candle: (open_time_ms, open, high, low, close)
    回傳訊號 dict 或 None。
    """
    st = models.strategy_state.get(symbol)
    if not st or st["phase"] != StrategyPhase.READY:
        return None

    _open_time_ms, _open, _high, low, close = candle

    # 廢棄檢查：1h K low 跌破盤整底部
    if low < st["consolidation_low"]:
        reset_to_idle(
            symbol,
            f"1h K low={low:.6f} < 盤整底部={st['consolidation_low']:.6f}",
        )
        return None

    # 條件 1：EMA 觸碰（1h K 最低價 <= 任一 4h EMA × (1 + 容忍%)）
    ema_4h = models.symbol_state.get(symbol, {}).get("ema_4h", {})
    touched_ema = None
    for period in (15, 30, 45, 60):
        ema_val = ema_4h.get(period)
        if ema_val is None:
            continue
        touch_limit = ema_val * (1 + models.runtime_config["EMA_TOUCH_THRESHOLD"] / 100)
        if low <= touch_limit:
            touched_ema = (period, ema_val)
            break

    if touched_ema is None:
        return None

    # 條件 2：有效收針（close > low × (1 + WICK_THRESHOLD%)）
    wick_min = low * (1 + models.runtime_config["WICK_THRESHOLD"] / 100)
    if close <= wick_min:
        return None

    # 條件 3：盈虧比 >= STRATEGY_RR_MIN
    top    = st["consolidation_high"]
    bottom = st["consolidation_low"]
    if close <= bottom:
        return None

    profit = top - close
    risk   = close - bottom
    if risk <= 0:
        return None

    rr = profit / risk
    if rr < models.runtime_config["STRATEGY_RR_MIN"]:
        log.debug(
            f"[策略-T2] {symbol} EMA 觸碰 + 收針成立，但盈虧比 {rr:.2f} < {models.runtime_config['STRATEGY_RR_MIN']}"
        )
        return None

    # 冷卻檢查
    now = time.time()
    if now - st["last_alert_ts"] < models.runtime_config["STRATEGY_COOLDOWN"]:
        return None

    wick_pct = (close - low) / low * 100
    st["last_alert_ts"]    = now
    st["last_signal_type"] = "type2"

    log.info(
        f"[策略-T2] {symbol} 觸發！close={close:.6f} | "
        f"觸碰 EMA{touched_ema[0]}(4h)={touched_ema[1]:.6f} | "
        f"收針 {wick_pct:.1f}% | 盈虧比 {rr:.2f} | 止損={bottom:.6f}"
    )
    return {
        "type":        "type2",
        "symbol":      symbol,
        "close":       close,
        "low":         low,
        "stop_loss":   bottom,
        "top":         top,
        "bottom":      bottom,
        "rr":          rr,
        "wick_pct":    wick_pct,
        "touched_ema": touched_ema,  # (period, value)
    }


def check_invalidation_realtime(symbol: str) -> bool:
    """即時廢棄檢查：若 markPrice 已跌破盤整底部則重置狀態。

    由 periodic_screen 每 10 秒呼叫。
    回傳 True 表示已廢棄。
    """
    st = models.strategy_state.get(symbol)
    if not st or st["phase"] == StrategyPhase.IDLE:
        return False

    price = models.symbol_state.get(symbol, {}).get("last_price")
    if price is None:
        return False

    if price < st["consolidation_low"]:
        reset_to_idle(
            symbol,
            f"即時價格 {price:.6f} < 盤整底部 {st['consolidation_low']:.6f}",
        )
        return True
    return False


def scan_strategy(symbol: str) -> None:
    """periodic_screen 呼叫的入口：執行即時廢棄檢查。"""
    check_invalidation_realtime(symbol)


def replay_historical_4h_candles(symbol: str) -> None:
    """重播歷史 4h OHLC，在啟動時恢復進行中的盤整狀態。

    在 load_historical_data_batch 完成後對每個 symbol 呼叫。
    """
    ohlc_deque = models.symbol_state.get(symbol, {}).get("kline_4h_ohlc")
    if not ohlc_deque:
        return

    # 重置為 IDLE 再重播，避免殘留狀態
    models.strategy_state.pop(symbol, None)

    for candle in ohlc_deque:
        on_new_4h_candle(symbol, candle)

    st = models.strategy_state.get(symbol, {})
    phase = st.get("phase", StrategyPhase.IDLE).value
    log.info(f"[策略] {symbol} 歷史回播完成 | phase={phase}")
