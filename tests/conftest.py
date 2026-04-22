import pytest
from collections import deque
from app.setting import models

_DEFAULT_RUNTIME_CONFIG = {
    "PUMP_THRESHOLD":          8,
    "CONSOLIDATION_MIN_HOURS": 12,
    "BREAKOUT_VOLUME_MULT":    3,
    "EMA_TOUCH_THRESHOLD":     0.5,
    "WICK_THRESHOLD":          2,
    "STRATEGY_RR_MIN":         1.0,
    "STRATEGY_COOLDOWN":       14400,
}


@pytest.fixture(autouse=True)
def reset_global_state():
    """每個測試前後清空全域狀態，並還原 runtime_config 為預設值。"""
    models.strategy_state.clear()
    models.symbol_state.clear()
    models.runtime_config.update(_DEFAULT_RUNTIME_CONFIG)
    yield
    models.strategy_state.clear()
    models.symbol_state.clear()


# ── 共用 Helper ────────────────────────────────────────────────

def make_4h_candle(ts, open_, high, low, close):
    return (ts, open_, high, low, close)


def make_pump_candle(ts, low=100.0, high=110.0, open_=100.0, close=108.0):
    """預設：(high-low)/low = 10% >= 8%，陽線。"""
    return (ts, open_, high, low, close)


def make_flat_candle(ts, low=102.0, high=109.0, open_=104.0, close=106.0):
    """預設：(high-low)/low ≈ 6.9% < 8%，不觸發拉漲。"""
    return (ts, open_, high, low, close)


def make_15m_candle(ts, open_=109.0, high=112.0, low=108.0, close=111.0, volume=1000.0):
    return (ts, open_, high, low, close, volume)


def make_1h_candle(ts, open_, high, low, close):
    return (ts, open_, high, low, close)


def make_15m_ohlc_deque(count=200, base_volume=1000.0, base_ts=1700000000000):
    """產生 count 根假 15m K 棒，量能固定為 base_volume。"""
    d = deque(maxlen=200)
    for i in range(count):
        ts = base_ts + i * 15 * 60 * 1000
        d.append((ts, 100.0, 101.0, 99.0, 100.5, base_volume))
    return d


def setup_symbol_state(symbol, ema_4h=None, last_price=105.0, kline_15m_ohlc=None):
    """設定 symbol_state，填入測試所需最小欄位。"""
    models.symbol_state[symbol] = {
        "ema_4h":         ema_4h or {},
        "last_price":     last_price,
        "kline_15m_ohlc": kline_15m_ohlc if kline_15m_ohlc is not None else make_15m_ohlc_deque(),
        "kline_1h_ohlc":  deque(maxlen=100),
        "kline_4h_ohlc":  deque(maxlen=50),
    }
