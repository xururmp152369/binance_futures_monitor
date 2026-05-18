import pytest
from collections import deque
from app.setting import models

_4H_MS = 4 * 3600 * 1000


@pytest.fixture(autouse=True)
def reset_global_state():
    """每個測試前後清空全域狀態。"""
    models.strategy_state.clear()
    models.short_strategy_state.clear()
    models.symbol_state.clear()
    yield
    models.strategy_state.clear()
    models.short_strategy_state.clear()
    models.symbol_state.clear()


# ── 基礎 Candle Helper ────────────────────────────────────────────

def make_4h_candle(ts, open_, high, low, close, volume=1000.0):
    return (ts, open_, high, low, close, volume)


def make_15m_candle(ts, open_=109.0, high=112.0, low=108.0, close=111.0, volume=1000.0):
    return (ts, open_, high, low, close, volume)


def make_15m_ohlc_deque(count=200, base_volume=1000.0, base_ts=1700000000000):
    """產生 count 根假 15m K 棒，量能固定為 base_volume。"""
    d = deque(maxlen=200)
    for i in range(count):
        ts = base_ts + i * 15 * 60 * 1000
        d.append((ts, 100.0, 101.0, 99.0, 100.5, base_volume))
    return d


def make_trigger_candle(ts, base=100.0, gain_pct=4.0, volume=400.0):
    """產生符合觸發條件的單根 4h K 棒（陽線，漲幅 gain_pct%，量 volume）。"""
    open_ = base
    close = round(open_ * (1 + gain_pct / 100), 8)
    high  = round(close * 1.002, 8)
    low   = round(open_ * 0.998, 8)
    return (ts, open_, high, low, close, volume)


# ── symbol_state 設定 ────────────────────────────────────────────

def setup_symbol_state(symbol, last_price=105.0, kline_15m_ohlc=None):
    """設定 symbol_state，kline_4h_ohlc 保持空（不含基準量能）。"""
    models.symbol_state[symbol] = {
        "last_price":     last_price,
        "kline_15m_ohlc": kline_15m_ohlc if kline_15m_ohlc is not None else make_15m_ohlc_deque(),
        "kline_4h_ohlc":  deque(maxlen=50),
    }


def setup_with_baseline(symbol, baseline_volume=100.0, n=12, base_ts=None):
    """設定 symbol_state，kline_4h_ohlc 填入 n 根基準 K（用於觸發量能計算）。

    預設 n=12，對應 TRIGGER_VOLUME_BASELINE_N=12。
    觸發 K 量能門檻 = baseline_volume × TRIGGER_VOLUME_MULT(3)。
    """
    if base_ts is None:
        base_ts = 1_000_000_000_000 - n * _4H_MS
    baseline = [
        (base_ts + i * _4H_MS, 100.0, 100.5, 99.5, 100.0, baseline_volume)
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
    回傳 (consolidation_low, consolidation_high, consolidation_start_ts)。
    """
    from app.strategy.state_machine import on_new_4h_candle

    setup_with_baseline(symbol, baseline_volume=baseline_volume)

    trigger_vol = baseline_volume * 4   # 4 倍確保通過 > 3 倍門檻
    open_ = base
    close = round(open_ * (1 + gain_pct / 100), 8)
    high  = round(close * 1.002, 8)
    low   = round(open_ * 0.998, 8)
    c = (ts0, open_, high, low, close, trigger_vol)

    # 把觸發 K append 到 deque（模擬 live 行為：先 append 再呼叫 handler）
    models.symbol_state[symbol]["kline_4h_ohlc"].append(c)
    on_new_4h_candle(symbol, c)

    st = models.strategy_state.get(symbol, {})
    return st.get("consolidation_low"), st.get("consolidation_high"), st.get("consolidation_start_ts")
