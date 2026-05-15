import time
from enum import Enum
from ..setting import models
from ..extension.utils import setup_logging

log = setup_logging()


class StrategyPhase(Enum):
    IDLE      = "idle"      # 閒置，尚未偵測到趨勢
    TRACKING  = "tracking"  # 偵測到結構，追蹤盤整中
    READY     = "ready"     # 盤整成熟，監控進場訊號


# ─── 共用狀態操作 ─────────────────────────────────────────────────────────────

def _init_state() -> dict:
    return {
        "phase":                  StrategyPhase.IDLE,
        "pump_candle_open":       None,
        "pump_candle_close":      None,
        "pump_candle_low":        None,   # long: 廢棄線；short: run 最低點
        "pump_candle_high":       None,   # short: 廢棄線；long: run 最高點
        "pump_candle_time":       None,   # run peak 時間（Unix 秒）
        "consolidation_low":      None,
        "consolidation_high":     None,
        "consolidation_start_ts": None,
        "last_alert_ts":          0.0,
        "last_signal_type":       None,   # "type1" / "type2" / "type1_short"
        # ── Run 追蹤（多頭：偵測連續創新高；空頭：連續創新低）──────────
        "run_start_open":         None,   # 第一根 K 棒的 open
        "run_start_low":          None,   # 多頭廢棄線候選（第一根的 low）
        "run_high":               None,   # 多頭：追蹤最高點
        "run_high_ts":            None,   # 多頭：最後創新高的時間
        "run_start_high":         None,   # 空頭廢棄線候選（第一根的 high）
        "run_low":                None,   # 空頭：追蹤最低點
        "run_low_ts":             None,   # 空頭：最後創新低的時間
        # ── Run 量能追蹤 ─────────────────────────────────────────────────
        "run_candle_count":       0,      # run 期間已累積根數
        "run_volume_sum":         0.0,    # run 期間量能總和
        "run_volume_baseline":    None,   # run 啟動時的基準均量（前 RUN_VOLUME_BASELINE_N 根）
    }


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


def _maybe_transition_to_ready(st: dict, current_ts: float, symbol: str) -> None:
    """TRACKING → READY：從最後一次創新高/低起，已盤整 >= CONSOLIDATION_MIN_HOURS。"""
    if st["phase"] != StrategyPhase.TRACKING:
        return
    elapsed_h = (current_ts - st["consolidation_start_ts"]) / 3600
    if elapsed_h >= models.runtime_config["CONSOLIDATION_MIN_HOURS"]:
        st["phase"] = StrategyPhase.READY
        log.info(
            f"[策略] {symbol} TRACKING → READY | "
            f"已盤整 {elapsed_h:.1f}h | "
            f"底部={st['consolidation_low']:.6f} 頂部={st['consolidation_high']:.6f}"
        )


def _capture_run_volume_baseline(symbol: str) -> float | None:
    """在 run 啟動時，從 kline_4h_ohlc 取前 RUN_VOLUME_BASELINE_N 根計算基準均量。"""
    n = models.runtime_config["RUN_VOLUME_BASELINE_N"]
    ohlc = models.symbol_state.get(symbol, {}).get("kline_4h_ohlc")
    if not ohlc:
        return None
    # 排除剛加入的當前根（[-1]），取其之前最多 N 根
    candidates = list(ohlc)[:-1]
    baseline_candles = candidates[-n:] if len(candidates) >= n else candidates
    if not baseline_candles:
        return None
    return sum(c[5] for c in baseline_candles) / len(baseline_candles)


def _check_run_volume(st: dict) -> bool:
    """檢查 run 均量是否 >= baseline × RUN_VOLUME_MULT；baseline 不足時視為通過。"""
    baseline = st["run_volume_baseline"]
    if baseline is None or baseline <= 0:
        return True
    run_avg = st["run_volume_sum"] / st["run_candle_count"]
    return run_avg >= baseline * models.runtime_config["RUN_VOLUME_MULT"]


def _reset_run_tracking(st: dict) -> None:
    """清空 run 追蹤欄位（多頭空頭共用欄位）。"""
    st["run_start_open"]      = None
    st["run_start_low"]       = None
    st["run_high"]            = None
    st["run_high_ts"]         = None
    st["run_start_high"]      = None
    st["run_low"]             = None
    st["run_low_ts"]          = None
    st["run_candle_count"]    = 0
    st["run_volume_sum"]      = 0.0
    st["run_volume_baseline"] = None


# 向下相容的公開別名（原有呼叫端使用）
def get_or_init_strategy_state(symbol: str) -> dict:
    return _get_or_init(symbol, models.strategy_state)


def reset_to_idle(symbol: str, reason: str = "") -> None:
    _reset_to_idle(symbol, reason, models.strategy_state)


# ─── 多頭 Run 套用 ────────────────────────────────────────────────────────────

def _apply_run_long(st: dict, symbol: str, close: float, cumulative_pct: float) -> None:
    """將已達標的多頭 run 套用到狀態（IDLE→TRACKING 或 Method B 重置）。"""
    run_low     = st["run_start_low"]
    run_high    = st["run_high"]
    run_peak_ts = st["run_high_ts"]

    if st["phase"] == StrategyPhase.IDLE:
        log.info(
            f"[策略-L] {symbol} IDLE → TRACKING | "
            f"累積漲幅={cumulative_pct:.1f}% 底={run_low:.6f} 頂={run_high:.6f}"
        )
        st.update({
            "phase":                  StrategyPhase.TRACKING,
            "pump_candle_open":       st["run_start_open"],
            "pump_candle_close":      close,
            "pump_candle_low":        run_low,
            "pump_candle_high":       run_high,
            "pump_candle_time":       run_peak_ts,
            "consolidation_low":      run_low,
            "consolidation_high":     run_high,
            "consolidation_start_ts": run_peak_ts,
        })
    elif run_low > st["consolidation_low"]:
        # Method B：盤整內 sub-run 達標且起始底部更高 → 完整重置為新 run
        log.info(
            f"[策略-L] {symbol} Method B | sub-run {cumulative_pct:.1f}% "
            f"底 {st['consolidation_low']:.6f}→{run_low:.6f} 頂={run_high:.6f}"
        )
        st.update({
            "phase":                  StrategyPhase.TRACKING,
            "pump_candle_open":       st["run_start_open"],
            "pump_candle_close":      close,
            "pump_candle_low":        run_low,
            "pump_candle_high":       run_high,
            "pump_candle_time":       run_peak_ts,
            "consolidation_low":      run_low,
            "consolidation_high":     run_high,
            "consolidation_start_ts": run_peak_ts,
        })


# ─── 多頭狀態機（4h） ────────────────────────────────────────────────────────

def on_new_4h_candle(symbol: str, candle: tuple) -> None:
    """處理新 4h K 棒收盤：多頭狀態機（IDLE/TRACKING/READY）。

    偵測連續陽線累積漲幅達 PUMP_THRESHOLD%，進入盤整追蹤。
    candle: (open_time_ms, open, high, low, close, quote_volume)
    """
    st = _get_or_init(symbol, models.strategy_state)
    open_time_ms, open_, high, low, close, quote_volume = candle
    current_ts = open_time_ms / 1000
    threshold  = models.runtime_config["PUMP_THRESHOLD"]

    # ─── 廢棄：跌破盤整底部 ─────────────────────────
    if st["phase"] != StrategyPhase.IDLE and st["consolidation_low"] is not None:
        if low < st["consolidation_low"]:
            _reset_to_idle(
                symbol,
                f"4h K low={low:.6f} < 底部={st['consolidation_low']:.6f}",
                models.strategy_state,
            )
            return

    # ─── 整體延伸：TRACKING/READY 創新高 ────────────
    if st["phase"] != StrategyPhase.IDLE and st["consolidation_high"] is not None:
        if high > st["consolidation_high"]:
            prev_phase = st["phase"]
            st["consolidation_high"]     = high
            st["consolidation_start_ts"] = current_ts
            st["phase"]                  = StrategyPhase.TRACKING
            note = "（原 READY 回退）" if prev_phase == StrategyPhase.READY else ""
            log.info(f"[策略-L] {symbol} 延伸新高 {high:.6f}{note} → 盤整計時重置")
            # 整體延伸 → 吸收任何 sub-run，清空 run 追蹤
            _reset_run_tracking(st)
            _maybe_transition_to_ready(st, current_ts, symbol)
            return

    # ─── Run 追蹤（IDLE：偵測趨勢；TRACKING/READY：sub-run / Method B）───
    if st["run_high"] is not None:
        if high > st["run_high"]:
            # 創新高 → 延伸 run
            st["run_high"]         = high
            st["run_high_ts"]      = current_ts
            st["run_candle_count"] += 1
            st["run_volume_sum"]   += quote_volume
        else:
            # 未創新高 → run 停止，三重評估
            cumulative_pct = (st["run_high"] - st["run_start_open"]) / st["run_start_open"] * 100
            max_candles    = models.runtime_config["RUN_MAX_CANDLES"]
            if (cumulative_pct >= threshold
                    and st["run_candle_count"] <= max_candles
                    and _check_run_volume(st)):
                _apply_run_long(st, symbol, close, cumulative_pct)
            elif cumulative_pct >= threshold:
                log.debug(
                    f"[策略-L] {symbol} run 達幅 {cumulative_pct:.1f}% 但未通過篩選 "
                    f"| 根數={st['run_candle_count']}/{max_candles} "
                    f"| 均量={st['run_volume_sum']/st['run_candle_count']:.0f} "
                    f"| baseline={st['run_volume_baseline']}"
                )

            # 重置 run
            _reset_run_tracking(st)

            # 本根若為陽線，立刻開始新的 run
            if close > open_:
                st["run_start_open"]      = open_
                st["run_start_low"]       = low
                st["run_high"]            = high
                st["run_high_ts"]         = current_ts
                st["run_candle_count"]    = 1
                st["run_volume_sum"]      = quote_volume
                st["run_volume_baseline"] = _capture_run_volume_baseline(symbol)
    else:
        # 尚無進行中的 run → 陽線啟動
        if close > open_:
            st["run_start_open"]      = open_
            st["run_start_low"]       = low
            st["run_high"]            = high
            st["run_high_ts"]         = current_ts
            st["run_candle_count"]    = 1
            st["run_volume_sum"]      = quote_volume
            st["run_volume_baseline"] = _capture_run_volume_baseline(symbol)

    _maybe_transition_to_ready(st, current_ts, symbol)


# ─── 空頭 Run 套用 ────────────────────────────────────────────────────────────

def _apply_run_short(st: dict, symbol: str, close: float, cumulative_pct: float) -> None:
    """將已達標的空頭 run 套用到狀態（IDLE→TRACKING 或 Method B 重置）。"""
    run_high    = st["run_start_high"]
    run_low     = st["run_low"]
    run_peak_ts = st["run_low_ts"]

    if st["phase"] == StrategyPhase.IDLE:
        log.info(
            f"[策略-S] {symbol} IDLE → TRACKING | "
            f"累積跌幅={cumulative_pct:.1f}% 頂={run_high:.6f} 底={run_low:.6f}"
        )
        st.update({
            "phase":                  StrategyPhase.TRACKING,
            "pump_candle_open":       st["run_start_open"],
            "pump_candle_close":      close,
            "pump_candle_high":       run_high,
            "pump_candle_low":        run_low,
            "pump_candle_time":       run_peak_ts,
            "consolidation_high":     run_high,
            "consolidation_low":      run_low,
            "consolidation_start_ts": run_peak_ts,
        })
    elif run_high < st["consolidation_high"]:
        # Method B：盤整內 sub-run 達標且起始頂部更低 → 完整重置為新 run
        log.info(
            f"[策略-S] {symbol} Method B | sub-run {cumulative_pct:.1f}% "
            f"頂 {st['consolidation_high']:.6f}→{run_high:.6f} 底={run_low:.6f}"
        )
        st.update({
            "phase":                  StrategyPhase.TRACKING,
            "pump_candle_open":       st["run_start_open"],
            "pump_candle_close":      close,
            "pump_candle_high":       run_high,
            "pump_candle_low":        run_low,
            "pump_candle_time":       run_peak_ts,
            "consolidation_high":     run_high,
            "consolidation_low":      run_low,
            "consolidation_start_ts": run_peak_ts,
        })


# ─── 空頭狀態機（4h） ────────────────────────────────────────────────────────

def on_new_4h_candle_short(symbol: str, candle: tuple) -> None:
    """處理新 4h K 棒收盤：空頭狀態機（IDLE/TRACKING/READY）。

    偵測連續陰線累積跌幅達 PUMP_THRESHOLD%，進入盤整追蹤。
    與多頭完全對稱：創新低延伸；突破頂部廢棄。
    candle: (open_time_ms, open, high, low, close, quote_volume)
    """
    st = _get_or_init(symbol, models.strategy_state_short)
    open_time_ms, open_, high, low, close, quote_volume = candle
    current_ts = open_time_ms / 1000
    threshold  = models.runtime_config["PUMP_THRESHOLD"]

    # ─── 廢棄：突破盤整頂部 ─────────────────────────
    if st["phase"] != StrategyPhase.IDLE and st["consolidation_high"] is not None:
        if high > st["consolidation_high"]:
            _reset_to_idle(
                symbol,
                f"4h K high={high:.6f} > 頂部={st['consolidation_high']:.6f}",
                models.strategy_state_short,
            )
            return

    # ─── 整體延伸：TRACKING/READY 創新低 ────────────
    if st["phase"] != StrategyPhase.IDLE and st["consolidation_low"] is not None:
        if low < st["consolidation_low"]:
            prev_phase = st["phase"]
            st["consolidation_low"]      = low
            st["consolidation_start_ts"] = current_ts
            st["phase"]                  = StrategyPhase.TRACKING
            note = "（原 READY 回退）" if prev_phase == StrategyPhase.READY else ""
            log.info(f"[策略-S] {symbol} 延伸新低 {low:.6f}{note} → 盤整計時重置")
            # 整體延伸 → 吸收任何 sub-run，清空 run 追蹤
            _reset_run_tracking(st)
            _maybe_transition_to_ready(st, current_ts, symbol)
            return

    # ─── Run 追蹤（IDLE：偵測趨勢；TRACKING/READY：sub-run / Method B）───
    if st["run_low"] is not None:
        if low < st["run_low"]:
            # 創新低 → 延伸 run
            st["run_low"]          = low
            st["run_low_ts"]       = current_ts
            st["run_candle_count"] += 1
            st["run_volume_sum"]   += quote_volume
        else:
            # 未創新低 → run 停止，三重評估
            cumulative_pct = (st["run_start_open"] - st["run_low"]) / st["run_start_open"] * 100
            max_candles    = models.runtime_config["RUN_MAX_CANDLES"]
            if (cumulative_pct >= threshold
                    and st["run_candle_count"] <= max_candles
                    and _check_run_volume(st)):
                _apply_run_short(st, symbol, close, cumulative_pct)
            elif cumulative_pct >= threshold:
                log.debug(
                    f"[策略-S] {symbol} run 達幅 {cumulative_pct:.1f}% 但未通過篩選 "
                    f"| 根數={st['run_candle_count']}/{max_candles} "
                    f"| 均量={st['run_volume_sum']/st['run_candle_count']:.0f} "
                    f"| baseline={st['run_volume_baseline']}"
                )

            # 重置 run
            _reset_run_tracking(st)

            # 本根若為陰線，立刻開始新的 run
            if close < open_:
                st["run_start_open"]      = open_
                st["run_start_high"]      = high
                st["run_low"]             = low
                st["run_low_ts"]          = current_ts
                st["run_candle_count"]    = 1
                st["run_volume_sum"]      = quote_volume
                st["run_volume_baseline"] = _capture_run_volume_baseline(symbol)
    else:
        # 尚無進行中的 run → 陰線啟動
        if close < open_:
            st["run_start_open"]      = open_
            st["run_start_high"]      = high
            st["run_low"]             = low
            st["run_low_ts"]          = current_ts
            st["run_candle_count"]    = 1
            st["run_volume_sum"]      = quote_volume
            st["run_volume_baseline"] = _capture_run_volume_baseline(symbol)

    _maybe_transition_to_ready(st, current_ts, symbol)


# ─── 多頭訊號（15m） ─────────────────────────────────────────────────────────

def on_new_15m_candle(symbol: str, candle: tuple) -> dict | None:
    """處理新 15m K 棒收盤：Type 1 帶量突破（做多）。

    candle: (open_time_ms, open, high, low, close, quote_volume)
    回傳訊號 dict 或 None。
    """
    st = models.strategy_state.get(symbol)
    if not st or st["phase"] != StrategyPhase.READY:
        return None

    open_time_ms, _open, _high, low, close, volume = candle
    top = st["consolidation_high"]

    if close <= top:
        return None

    ohlc_deque = models.symbol_state.get(symbol, {}).get("kline_15m_ohlc")
    if ohlc_deque is None or len(ohlc_deque) < 193:
        return None

    ohlc_list = list(ohlc_deque)
    baseline_vols = [c[5] for c in ohlc_list[-193:-1]]
    avg_vol = sum(baseline_vols) / len(baseline_vols) if baseline_vols else 0
    if avg_vol <= 0:
        return None

    vol_ratio = volume / avg_vol
    if vol_ratio < models.runtime_config["BREAKOUT_VOLUME_MULT"]:
        log.debug(f"[策略-T1] {symbol} 突破頂部但量能不足 {vol_ratio:.1f}× < {models.runtime_config['BREAKOUT_VOLUME_MULT']}×")
        return None

    now = time.time()
    if now - st["last_alert_ts"] < models.runtime_config["STRATEGY_COOLDOWN"]:
        return None

    _4H_MS = 4 * 3600 * 1000
    current_4h_open_ms = (open_time_ms // _4H_MS) * _4H_MS
    lookback_threshold = avg_vol * models.runtime_config["LOOKBACK_VOLUME_MULT"]

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

    log.info(f"[策略-T1] {symbol} 觸發！close={close:.6f} > top={top:.6f} | 量能 {vol_ratio:.1f}× | 止損={stop_loss:.6f}")
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


# ─── 空頭訊號（15m） ─────────────────────────────────────────────────────────

def on_new_15m_candle_short(symbol: str, candle: tuple) -> dict | None:
    """處理新 15m K 棒收盤：Type 1 Short 帶量跌破（做空）。

    邏輯與 Type 1 做多完全對稱：
    - 收盤 < consolidation_low
    - 放量確認
    - 止損 = 往回掃連續放量 K 的最高 high

    candle: (open_time_ms, open, high, low, close, quote_volume)
    回傳訊號 dict 或 None。
    """
    st = models.strategy_state_short.get(symbol)
    if not st or st["phase"] != StrategyPhase.READY:
        return None

    open_time_ms, _open, high, _low, close, volume = candle
    bottom = st["consolidation_low"]

    if close >= bottom:
        return None

    ohlc_deque = models.symbol_state.get(symbol, {}).get("kline_15m_ohlc")
    if ohlc_deque is None or len(ohlc_deque) < 193:
        return None

    ohlc_list = list(ohlc_deque)
    baseline_vols = [c[5] for c in ohlc_list[-193:-1]]
    avg_vol = sum(baseline_vols) / len(baseline_vols) if baseline_vols else 0
    if avg_vol <= 0:
        return None

    vol_ratio = volume / avg_vol
    if vol_ratio < models.runtime_config["BREAKOUT_VOLUME_MULT"]:
        log.debug(f"[策略-T1S] {symbol} 跌破底部但量能不足 {vol_ratio:.1f}× < {models.runtime_config['BREAKOUT_VOLUME_MULT']}×")
        return None

    now = time.time()
    if now - st["last_alert_ts"] < models.runtime_config["STRATEGY_COOLDOWN"]:
        return None

    _4H_MS = 4 * 3600 * 1000
    current_4h_open_ms = (open_time_ms // _4H_MS) * _4H_MS
    lookback_threshold = avg_vol * models.runtime_config["LOOKBACK_VOLUME_MULT"]

    stop_loss = high
    for i in range(len(ohlc_list) - 2, -1, -1):
        prev = ohlc_list[i]
        if prev[0] < current_4h_open_ms:
            break
        if prev[5] > lookback_threshold:
            stop_loss = max(stop_loss, prev[2])
        else:
            break

    st["last_alert_ts"]    = now
    st["last_signal_type"] = "type1_short"

    log.info(f"[策略-T1S] {symbol} 觸發！close={close:.6f} < bottom={bottom:.6f} | 量能 {vol_ratio:.1f}× | 止損={stop_loss:.6f}")
    return {
        "type":                "type1_short",
        "symbol":              symbol,
        "close":               close,
        "stop_loss":           stop_loss,
        "top":                 st["consolidation_high"],
        "bottom":              bottom,
        "vol_ratio":           vol_ratio,
        "dump_time":           st["pump_candle_time"],
        "dump_high":           st["pump_candle_high"],
        "dump_low":            st["pump_candle_low"],
        "candle_open_time_ms": open_time_ms,
    }


# ─── 1h 訊號（Type 2，僅多頭） ───────────────────────────────────────────────

def on_new_1h_candle(symbol: str, candle: tuple) -> dict | None:
    """處理新 1h K 棒收盤：Type 2 回踩 4h EMA 反彈訊號（做多）。

    candle: (open_time_ms, open, high, low, close)
    回傳訊號 dict 或 None。
    """
    st = models.strategy_state.get(symbol)
    if not st or st["phase"] != StrategyPhase.READY:
        return None

    open_time_ms, open_, _high, low, close = candle

    if low < st["consolidation_low"]:
        _reset_to_idle(symbol, f"1h K low={low:.6f} < 底部={st['consolidation_low']:.6f}", models.strategy_state)
        return None

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

    if open_ <= touched_ema[1]:
        return None

    wick_min = low * (1 + models.runtime_config["WICK_THRESHOLD"] / 100)
    if close <= wick_min:
        return None

    top       = st["consolidation_high"]
    stop_loss = st["consolidation_low"]
    if close <= stop_loss:
        return None

    profit = top - close
    risk   = close - stop_loss
    if risk <= 0:
        return None

    rr = profit / risk
    if rr < models.runtime_config["STRATEGY_RR_MIN"]:
        log.debug(f"[策略-T2] {symbol} EMA 觸碰 + 收針成立，但盈虧比 {rr:.2f} < {models.runtime_config['STRATEGY_RR_MIN']}")
        return None

    now = time.time()
    if now - st["last_alert_ts"] < models.runtime_config["STRATEGY_COOLDOWN"]:
        return None

    wick_pct = (close - low) / low * 100
    st["last_alert_ts"]    = now
    st["last_signal_type"] = "type2"

    log.info(
        f"[策略-T2] {symbol} 觸發！close={close:.6f} | "
        f"觸碰 EMA{touched_ema[0]}(4h)={touched_ema[1]:.6f} | open={open_:.6f} | "
        f"收針 {wick_pct:.1f}% | 盈虧比 {rr:.2f} | 止損={stop_loss:.6f}"
    )
    return {
        "type":                "type2",
        "symbol":              symbol,
        "close":               close,
        "low":                 low,
        "stop_loss":           stop_loss,
        "top":                 top,
        "bottom":              stop_loss,
        "rr":                  rr,
        "wick_pct":            wick_pct,
        "touched_ema":         touched_ema,
        "pump_time":           st["pump_candle_time"],
        "pump_high":           st["pump_candle_high"],
        "pump_low":            st["pump_candle_low"],
        "candle_open_time_ms": open_time_ms,
    }


# ─── 即時廢棄掃描 ─────────────────────────────────────────────────────────────

def check_invalidation_realtime(symbol: str) -> bool:
    """即時廢棄檢查：多空兩個狀態都掃描。由 periodic_screen 每 10 秒呼叫。"""
    price = models.symbol_state.get(symbol, {}).get("last_price")
    if price is None:
        return False

    invalidated = False

    # 多頭：markPrice 跌破盤整底部
    st_long = models.strategy_state.get(symbol)
    if st_long and st_long["phase"] != StrategyPhase.IDLE:
        if price < st_long["consolidation_low"]:
            _reset_to_idle(
                symbol,
                f"即時價格 {price:.6f} < 底部 {st_long['consolidation_low']:.6f}",
                models.strategy_state,
            )
            invalidated = True

    # 空頭：markPrice 突破盤整頂部
    st_short = models.strategy_state_short.get(symbol)
    if st_short and st_short["phase"] != StrategyPhase.IDLE:
        if price > st_short["consolidation_high"]:
            _reset_to_idle(
                symbol,
                f"即時價格 {price:.6f} > 頂部 {st_short['consolidation_high']:.6f}",
                models.strategy_state_short,
            )
            invalidated = True

    return invalidated


def scan_strategy(symbol: str) -> None:
    """periodic_screen 呼叫的入口：執行即時廢棄檢查（多空）。"""
    check_invalidation_realtime(symbol)


# ─── 歷史回播 ─────────────────────────────────────────────────────────────────

def replay_historical_4h_candles(symbol: str) -> None:
    """重播歷史 4h OHLC，啟動時恢復進行中的多空盤整狀態。"""
    ohlc_deque = models.symbol_state.get(symbol, {}).get("kline_4h_ohlc")
    if not ohlc_deque:
        return

    models.strategy_state.pop(symbol, None)
    models.strategy_state_short.pop(symbol, None)

    for candle in ohlc_deque:
        on_new_4h_candle(symbol, candle)
        on_new_4h_candle_short(symbol, candle)

    long_phase  = models.strategy_state.get(symbol, {}).get("phase", StrategyPhase.IDLE).value
    short_phase = models.strategy_state_short.get(symbol, {}).get("phase", StrategyPhase.IDLE).value
    log.info(f"[策略] {symbol} 歷史回播完成 | 多頭={long_phase} 空頭={short_phase}")
