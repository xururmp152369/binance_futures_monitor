import pytest
from collections import deque
from app.setting import models

_4H_MS = 4 * 3600 * 1000


@pytest.fixture(autouse=True)
def reset_global_state():
    """每個測試前後清空全域狀態。"""
    models.strategy_state.clear()
    models.symbol_state.clear()
    yield
    models.strategy_state.clear()
    models.symbol_state.clear()


# ── 基礎 Candle Helper ────────────────────────────────────────────

def make_4h_candle(ts, open_, high, low, close, volume=1000.0, taker_vol=None):
    taker_buy_vol = taker_vol if taker_vol is not None else volume * 0.7
    return (ts, open_, high, low, close, volume, taker_buy_vol)


def make_15m_candle(ts, open_=109.0, high=112.0, low=108.0, close=111.0, volume=1000.0, taker_vol=None):
    taker_buy_vol = taker_vol if taker_vol is not None else volume * 0.7
    return (ts, open_, high, low, close, volume, taker_buy_vol)


def make_15m_ohlc_deque(count=200, base_volume=1000.0, base_ts=1700000000000):
    """產生 count 根假 15m K 棒（6 欄位，供 deque 歷史計算用）。"""
    d = deque(maxlen=200)
    for i in range(count):
        ts = base_ts + i * 15 * 60 * 1000
        d.append((ts, 100.0, 101.0, 99.0, 100.5, base_volume))
    return d


def make_trigger_candle(ts, base=100.0, gain_pct=4.0, volume=400.0):
    """產生符合觸發條件的單根 4h K 棒（body_ratio ≥ 0.75，量 volume）。"""
    open_ = base
    close = round(open_ * (1 + gain_pct / 100), 8)
    # 設計高 body_ratio：上下影線各約 0.1 * body
    body  = close - open_
    high  = round(close + body * 0.1, 8)
    low   = round(open_ - body * 0.1, 8)
    taker = volume * 0.7
    return (ts, open_, high, low, close, volume, taker)


def make_tight_breakout_candle(ts, top, vol=500.0):
    """產生 15m 突破 K：body_ratio = 0.75，close = top × 1.01。

    止損距離由外部放量 K 提供（>=3%）；本 K 棒自身 low 不設置止損。
    """
    open_ = top
    close = round(top * 1.01, 8)
    high  = round(close * 1.001, 8)
    low   = round(high - (close - open_) / 0.75, 8)
    taker = vol * 0.7
    return (ts, open_, high, low, close, vol, taker)


# ── symbol_state 設定 ────────────────────────────────────────────

def setup_symbol_state(symbol, last_price=105.0, kline_15m_ohlc=None):
    """設定 symbol_state，kline_4h_ohlc 保持空（不含基準量能）。"""
    models.symbol_state[symbol] = {
        "last_price":     last_price,
        "kline_15m_ohlc": kline_15m_ohlc if kline_15m_ohlc is not None else make_15m_ohlc_deque(),
        "kline_4h_ohlc":  deque(maxlen=50),
    }


def setup_with_baseline(symbol, baseline_volume=100.0, n=12, base_ts=None):
    """設定 symbol_state，kline_4h_ohlc 填入 n 根基準 K（7 欄位）。"""
    if base_ts is None:
        base_ts = 1_000_000_000_000 - n * _4H_MS
    baseline = [
        (base_ts + i * _4H_MS, 100.0, 100.5, 99.5, 100.0, baseline_volume, baseline_volume * 0.7)
        for i in range(n)
    ]
    models.symbol_state[symbol] = {
        "last_price":     100.0,
        "kline_15m_ohlc": make_15m_ohlc_deque(count=200, base_volume=100.0),
        "kline_4h_ohlc":  deque(baseline, maxlen=50),
    }
    return baseline


def trigger_long_tracking(symbol, ts0, base=100.0, gain_pct=4.0, baseline_volume=100.0):
    """建立最小多頭 TRACKING 狀態：12 根基準 K + 1 根觸發 K。

    觸發 K 量能 = baseline_volume × 4（確保 > baseline × 3 的門檻）。
    body_ratio ≥ 0.75（由 make_trigger_candle 保證）。
    回傳 (consolidation_low, consolidation_high, consolidation_start_ts)。
    """
    from app.strategy.state_machine import on_new_4h_candle

    setup_with_baseline(symbol, baseline_volume=baseline_volume)

    trigger_vol = baseline_volume * 4   # 4 倍確保通過 > 3 倍門檻
    c = make_trigger_candle(ts0, base=base, gain_pct=gain_pct, volume=trigger_vol)

    models.symbol_state[symbol]["kline_4h_ohlc"].append(c)
    on_new_4h_candle(symbol, c)

    st = models.strategy_state.get(symbol, {})
    return st.get("consolidation_low"), st.get("consolidation_high"), st.get("consolidation_start_ts")
