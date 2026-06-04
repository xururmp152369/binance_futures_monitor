"""回測核心引擎：將歷史 K 棒餵入現有策略狀態機，收集進場訊號。

關鍵設計：正式系統啟動時只 replay 最後 N 根 K 棒（由 deque maxlen 決定）。
回測必須對齊此行為，否則狀態機會因處理過多歷史而與正式系統產生截然不同的狀態。

  4h deque maxlen = 50  → 只對最後 50 根 4h 呼叫 on_new_4h_candle
  1d deque maxlen = 250 → 只對最後 250 根 daily 呼叫 on_new_daily_candle
  1h / 15m：正式系統無 replay，回測全程都呼叫（訊號只記錄回測期間）
"""
import time as _time_module
from collections import deque

from app.setting import models
from app.strategy.state_machine import (
    on_new_4h_candle,
    on_new_15m_candle,
    on_new_1h_candle,
    on_new_daily_candle,
)

# ─── 時間補丁（讓冷卻邏輯使用 K 棒時間而非真實時間） ─────────────────────────────

_orig_time = _time_module.time


def _patch_time(ts: float) -> None:
    _time_module.time = lambda: ts


def _restore_time() -> None:
    _time_module.time = _orig_time


# ─── 正式系統 replay 視窗（對齊 deque maxlen）────────────────────────────────

_DEQUE_MAXLEN = {
    "4h": 200,
    "1d": 250,
}

# 正式系統 replay 覆蓋天數（超過此天數的回測不限制狀態機啟動點）
_REPLAY_DAYS = {
    "4h": _DEQUE_MAXLEN["4h"] * 4 / 24,  # 200根 × 4h ≈ 33天
    "1d": _DEQUE_MAXLEN["1d"],            # 250根 × 1d = 250天
}


# ─── 狀態初始化 ──────────────────────────────────────────────────────────────

def _init_symbol_state(symbol: str) -> None:
    models.symbol_state[symbol] = {
        "last_price":                0.0,
        "funding_rate":              0.0,
        "last_kline_close_time_15m": 0,
        "last_kline_close_time_1h":  0,
        "last_kline_close_time_daily": 0,
        "kline_4h_ohlc":    deque(maxlen=_DEQUE_MAXLEN["4h"]),
        "kline_15m_ohlc":   deque(maxlen=200),
        "kline_1h_ohlc":    deque(maxlen=250),
        "kline_daily_ohlc": deque(maxlen=_DEQUE_MAXLEN["1d"]),
    }
    models.strategy_state.pop(symbol, None)
    models.death_cross_state.pop(symbol, None)


# ─── 回測期間計算 ─────────────────────────────────────────────────────────────

def _backtest_start_ms(data_15m: list, backtest_days: int) -> int:
    if not data_15m:
        return 0
    return data_15m[-1][0] - backtest_days * 24 * 3600 * 1000


# ─── 多時框事件佇列 ─────────────────────────────────────────────────────────

_TF_MS = {
    "1d":  86400_000,
    "4h":  14400_000,
    "1h":   3600_000,
    "15m":   900_000,
}

_PRIORITY = {"1d": 0, "4h": 1, "1h": 2, "15m": 3}


def _build_event_queue(candles_by_tf: dict[str, list]) -> list[tuple]:
    events = []
    for interval, candles in candles_by_tf.items():
        tf_ms = _TF_MS[interval]
        prio  = _PRIORITY[interval]
        for candle in candles:
            close_time_ms = candle[0] + tf_ms
            events.append((close_time_ms, prio, interval, candle))
    events.sort(key=lambda e: (e[0], e[1]))
    return events


# ─── 單一幣種回測 ────────────────────────────────────────────────────────────

def run_symbol_backtest(
    symbol: str,
    candles_by_tf: dict[str, list],
    backtest_days: int = 30,
    backtest_start_ms: int | None = None,
    backtest_end_ms: int | None = None,
) -> list[dict]:
    """回測單一幣種，回傳回測期間偵測到的訊號 list。

    backtest_start_ms: 訊號記錄起點（ms）；None 時由 backtest_days 反推。
    backtest_end_ms:   訊號記錄終點（ms）；None 表示無上限（取到最後一根）。

    對齊正式系統行為：
    - 4h 狀態機只從最後 50 根開始呼叫（對應 replay_historical_4h_candles_long）
    - Daily 狀態機只從最後 250 根開始呼叫（對應 replay_historical_daily_candles_dc）
    - 1h/15m 全程呼叫（正式系統無 replay）
    """
    _init_symbol_state(symbol)

    data_15m = candles_by_tf.get("15m", [])
    if backtest_start_ms is None:
        backtest_start_ms = _backtest_start_ms(data_15m, backtest_days)

    # 計算各時框狀態機的啟動邊界（open_time_ms）
    # 短期回測（≤ replay 天數）：對齊正式系統啟動 replay 窗口，避免抓到正式系統看不到的舊泵
    # 長期回測（> replay 天數）：從最早可用資料開始，讓狀態機完整模擬全期行為
    sm_start: dict[str, int] = {}
    for tf in ("4h", "1d"):
        candles = candles_by_tf.get(tf, [])
        maxlen  = _DEQUE_MAXLEN[tf]
        if backtest_days > _REPLAY_DAYS[tf]:
            sm_start[tf] = 0  # 長期回測：全程呼叫
        else:
            sm_start[tf] = candles[-maxlen][0] if len(candles) >= maxlen else 0

    events    = _build_event_queue(candles_by_tf)
    sym_state = models.symbol_state[symbol]
    signals: list[dict] = []

    deque_key_map = {
        "1d":  "kline_daily_ohlc",
        "4h":  "kline_4h_ohlc",
        "1h":  "kline_1h_ohlc",
        "15m": "kline_15m_ohlc",
    }

    for close_time_ms, _prio, interval, candle in events:
        candle_close_ts = close_time_ms / 1000
        in_backtest     = candle[0] >= backtest_start_ms

        # 更新 deque（全程，供 EMA/ATR/量能計算使用）
        sym_state[deque_key_map[interval]].append(candle)

        _patch_time(candle_close_ts)
        try:
            signal = None

            if interval == "1d":
                # 對齊 replay：只從最後 250 根開始呼叫
                if candle[0] >= sm_start["1d"]:
                    on_new_daily_candle(symbol, candle)

            elif interval == "4h":
                # 對齊 replay：只從最後 50 根開始呼叫
                if candle[0] >= sm_start["4h"]:
                    on_new_4h_candle(symbol, candle)

            elif interval == "1h":
                signal = on_new_1h_candle(symbol, candle)

            elif interval == "15m":
                signal = on_new_15m_candle(symbol, candle)

        finally:
            _restore_time()

        in_range = (
            in_backtest
            and (backtest_end_ms is None or candle[0] < backtest_end_ms)
        )
        if signal and in_range:
            signals.append(signal)

    return signals


# ─── 多幣種批次回測 ──────────────────────────────────────────────────────────

def run_backtest(
    all_data: dict[str, dict[str, list]],
    strategies: set[str],
    backtest_days: int = 30,
    backtest_start_ms: int | None = None,
    backtest_end_ms: int | None = None,
) -> list[dict]:
    """對所有幣種執行回測，回傳過濾後的訊號 list。"""
    type_filter = _strategy_to_types(strategies)
    all_signals: list[dict] = []
    total = len(all_data)

    for i, (symbol, candles_by_tf) in enumerate(all_data.items(), 1):
        try:
            signals  = run_symbol_backtest(
                symbol, candles_by_tf, backtest_days,
                backtest_start_ms=backtest_start_ms,
                backtest_end_ms=backtest_end_ms,
            )
            filtered = [s for s in signals if s.get("type") in type_filter]
            all_signals.extend(filtered)
        except Exception as exc:
            print(f"[engine] {symbol} 回測失敗：{exc}")

        if i % 50 == 0 or i == total:
            print(f"[engine] {i}/{total} 個幣種完成")

    return all_signals


def _strategy_to_types(strategies: set[str]) -> set[str]:
    mapping = {
        "long_breakout":     "type1",
        "death_cross_short": "type3",
    }
    return {mapping[s] for s in strategies if s in mapping}
