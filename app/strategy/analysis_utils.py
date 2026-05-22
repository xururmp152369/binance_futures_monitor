from enum import Enum
from ..setting import models
from ..setting.config import TRIGGER_VOLUME_BASELINE_N


class Direction(Enum):
    LONG  = "long"
    SHORT = "short"


# ─── 方向感知工具函數 ──────────────────────────────────────────────────────────

def candle_gain_pct(open_: float, close: float, direction: Direction) -> float:
    """多頭：漲幅；空頭：跌幅。正值代表「朝方向移動」。"""
    if direction == Direction.LONG:
        return (close - open_) / open_ * 100
    return (open_ - close) / open_ * 100


def is_directional_candle(open_: float, close: float, direction: Direction) -> bool:
    """多頭：陽線（close > open）；空頭：陰線（close < open）。"""
    return close > open_ if direction == Direction.LONG else close < open_


def body_barrier_price(open_: float, close: float, direction: Direction) -> float:
    """廢棄比較用的實體邊界：多頭取實體低點，空頭取實體高點。"""
    return min(open_, close) if direction == Direction.LONG else max(open_, close)


def is_extension(high: float, low: float, st: dict, direction: Direction) -> bool:
    """K 棒是否突破（延伸）盤整邊界：多頭創新高，空頭創新低。"""
    return (
        high > st["consolidation_high"] if direction == Direction.LONG
        else low < st["consolidation_low"]
    )


def extension_price(high: float, low: float, direction: Direction) -> float:
    """延伸時要更新的邊界值。"""
    return high if direction == Direction.LONG else low


def is_invalidated(open_: float, close: float, st: dict, direction: Direction) -> bool:
    """實體收破廢棄線：多頭實體低點 < consolidation_low，空頭實體高點 > consolidation_high。"""
    barrier = body_barrier_price(open_, close, direction)
    return (
        barrier < st["consolidation_low"] if direction == Direction.LONG
        else barrier > st["consolidation_high"]
    )


def breakout_target(st: dict, direction: Direction) -> float:
    """進場突破目標價：多頭 = 頂部，空頭 = 底部。"""
    return st["consolidation_high"] if direction == Direction.LONG else st["consolidation_low"]


def invalidation_level(st: dict, direction: Direction) -> float | None:
    """即時廢棄比較基準：多頭 = 底部，空頭 = 頂部。"""
    return st["consolidation_low"] if direction == Direction.LONG else st["consolidation_high"]


def is_price_invalidated(price: float, st: dict, direction: Direction) -> bool:
    """即時 markPrice 是否觸發廢棄：多頭跌破底部，空頭漲破頂部。"""
    level = invalidation_level(st, direction)
    if level is None:
        return False
    return price < level if direction == Direction.LONG else price > level


# ─── 量能工具 ─────────────────────────────────────────────────────────────────

def get_4h_volume_baseline(symbol: str) -> float | None:
    """取前 TRIGGER_VOLUME_BASELINE_N 根 4h K 的基準均量（排除最後一根=當根）。"""
    ohlc = models.symbol_state.get(symbol, {}).get("kline_4h_ohlc")
    if not ohlc:
        return None
    prev = list(ohlc)[:-1]
    baseline = prev[-TRIGGER_VOLUME_BASELINE_N:]
    if not baseline:
        return None
    return sum(c[5] for c in baseline) / len(baseline)


# ─── EMA 計算 ─────────────────────────────────────────────────────────────────

def calc_ema(values: list[float], period: int) -> float | None:
    """計算指數移動平均。values 需按時間順序排列（舊→新）。"""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period  # SMA 作為種子值
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def get_4h_ema(symbol: str, period: int) -> float | None:
    """從 kline_4h_ohlc 的 close 序列計算 EMA。至少需要 period 根 K 棒。"""
    ohlc = models.symbol_state.get(symbol, {}).get("kline_4h_ohlc")
    if not ohlc or len(ohlc) < period:
        return None
    closes = [c[4] for c in ohlc]
    return calc_ema(closes, period)


def get_daily_ema(symbol: str, period: int) -> float | None:
    """從 kline_daily_ohlc 的 close 序列計算 EMA。至少需要 period 根 K 棒。"""
    ohlc = models.symbol_state.get(symbol, {}).get("kline_daily_ohlc")
    if not ohlc or len(ohlc) < period:
        return None
    closes = [c[4] for c in ohlc]
    return calc_ema(closes, period)


def get_1h_ema(symbol: str, period: int) -> float | None:
    """從 kline_1h_ohlc 的 close 序列計算 EMA。至少需要 period 根 K 棒。"""
    ohlc = models.symbol_state.get(symbol, {}).get("kline_1h_ohlc")
    if not ohlc or len(ohlc) < period:
        return None
    closes = [c[4] for c in ohlc]
    return calc_ema(closes, period)


def calc_atr(candles: list, period: int) -> float | None:
    """計算 ATR(period)。candles 格式：[(open_time_ms, o, h, l, c, vol), ...]，舊→新。"""
    if len(candles) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(candles)):
        high       = candles[i][2]
        low        = candles[i][3]
        prev_close = candles[i - 1][4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    if len(true_ranges) < period:
        return None
    return sum(true_ranges[-period:]) / period


def get_1h_atr(symbol: str, period: int = 14) -> float | None:
    """從 kline_1h_ohlc 計算 ATR(period)。至少需要 period + 1 根 K 棒。"""
    ohlc = models.symbol_state.get(symbol, {}).get("kline_1h_ohlc")
    if not ohlc or len(ohlc) < period + 1:
        return None
    return calc_atr(list(ohlc), period)


# ─── K棒形態分析 ──────────────────────────────────────────────────────────────

def upper_wick_size(open_: float, high: float, close: float) -> float:
    """上影線長度 = high - max(open, close)。"""
    return high - max(open_, close)


def body_size(open_: float, close: float) -> float:
    """實體長度 = abs(close - open)。"""
    return abs(close - open_)


def is_shooting_star(
    open_: float, high: float, low: float, close: float,
    wick_body_ratio: float,
) -> bool:
    """射擊之星判斷：上影線 >= 實體 × wick_body_ratio。實體為零時只要有上影線即成立。"""
    wick = upper_wick_size(open_, high, close)
    b = body_size(open_, close)
    if b == 0:
        return wick > 0
    return wick >= b * wick_body_ratio
