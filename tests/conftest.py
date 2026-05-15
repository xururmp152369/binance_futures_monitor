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


# ── 共用 Helper ────────────────────────────────────────────────

def make_4h_candle(ts, open_, high, low, close, volume=1000.0):
    return (ts, open_, high, low, close, volume)


def make_flat_candle(ts, low=102.0, high=109.0, open_=104.0, close=106.0, volume=1000.0):
    """預設：(high-low)/low ≈ 6.9% < 8%，不觸發拉漲。"""
    return (ts, open_, high, low, close, volume)


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


def setup_symbol_state(symbol, last_price=105.0, kline_15m_ohlc=None):
    """設定 symbol_state，填入測試所需最小欄位。"""
    models.symbol_state[symbol] = {
        "last_price":     last_price,
        "kline_15m_ohlc": kline_15m_ohlc if kline_15m_ohlc is not None else make_15m_ohlc_deque(),
        "kline_4h_ohlc":  deque(maxlen=50),
    }


# ── 多頭 Run 輔助 ──────────────────────────────────────────────

def make_long_run(ts0, gains, base=100.0, volume=1000.0):
    """產生連續陽線的多頭 run。

    gains: 每根 K 棒的漲幅百分比清單（正數代表陽線）。
    傳回 (candles, final_price)，其中每根 candle 的 open=前根 close，
    high = close * 1.002，low = open * 0.998。
    """
    candles = []
    price = base
    for i, gain_pct in enumerate(gains):
        open_ = price
        close = round(open_ * (1 + gain_pct / 100), 8)
        high  = round(max(open_, close) * 1.002, 8)
        low   = round(min(open_, close) * 0.998, 8)
        ts    = ts0 + i * _4H_MS
        candles.append((ts, open_, high, low, close, volume))
        price = close
    return candles, price


def make_short_run(ts0, drops, base=100.0, volume=1000.0):
    """產生連續陰線的空頭 run。

    drops: 每根 K 棒的跌幅百分比清單（正數代表跌幅）。
    傳回 (candles, final_price)。
    """
    candles = []
    price = base
    for i, drop_pct in enumerate(drops):
        open_ = price
        close = round(open_ * (1 - drop_pct / 100), 8)
        high  = round(max(open_, close) * 1.002, 8)
        low   = round(min(open_, close) * 0.998, 8)
        ts    = ts0 + i * _4H_MS
        candles.append((ts, open_, high, low, close, volume))
        price = close
    return candles, price


def feed_long_run(symbol, ts0, gains, base=100.0):
    """直接將多頭 run 餵入狀態機，回傳最後收盤價。"""
    from app.strategy.state_machine import on_new_4h_candle
    candles, final = make_long_run(ts0, gains, base)
    for c in candles:
        on_new_4h_candle(symbol, c)
    return final


def trigger_long_tracking(symbol, ts0, base=100.0, gain_pct=9.0, volume=1000.0):
    """建立最小多頭 TRACKING 狀態：一根 gain_pct% 陽線 + 一根不創新高的中性 K 棒。

    回傳 (consolidation_low, consolidation_high, ts_peak)。
    """
    from app.strategy.state_machine import on_new_4h_candle
    # 第一根：陽線，gain_pct%
    open1  = base
    close1 = round(open1 * (1 + gain_pct / 100), 8)
    high1  = round(close1 * 1.002, 8)
    low1   = round(open1  * 0.998, 8)
    c1 = (ts0, open1, high1, low1, close1, volume)
    on_new_4h_candle(symbol, c1)

    # 第二根：不創新高（high < high1），觸發 run 達標 → TRACKING
    ts1    = ts0 + _4H_MS
    open2  = close1
    close2 = round(open2 * 1.001, 8)       # 微陽
    high2  = round(high1 * 0.999, 8)       # 低於 high1
    low2   = round(open2 * 0.998, 8)
    c2 = (ts1, open2, high2, low2, close2, volume)
    on_new_4h_candle(symbol, c2)

    return low1, high1, ts0 / 1000


