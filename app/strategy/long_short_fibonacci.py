"""Fibonacci 延伸策略（fibonacci_long / fibonacci_short）。

辨識「底底高」（多單）或「頂頂低」（空單）形態，
結合 EMA 方向確認與 Fib 1.73 影線確認（bar5/bar8），
在 bar9 發出進場訊號。

掃描方式：每根 K 棒閉合後做固定窗口掃描，
  barA = klines[-9]，bar9 = klines[-1]（當前 K 棒）。
  自然實現無效化重評估：barA 位置每根 K 棒推進一格。
"""
import time
from ..setting import models
from ..setting.config import FIB_K_INTERVAL, FIB_EMA_PERIOD, FIB_CONFIRM_LEVEL, FIB_TP1_LEVEL
from .analysis_utils import calc_ema
from ..extension.utils import setup_logging

log = setup_logging()

_INTERVAL_BUFFER = {
    "15m": "kline_15m_ohlc",
    "1h":  "kline_1h_ohlc",
    "4h":  "kline_4h_ohlc",
}

_EPSILON = 1e-9  # 浮點數容差


def get_or_init_fib_state(symbol: str) -> dict:
    if symbol not in models.fibonacci_state:
        models.fibonacci_state[symbol] = {
            # 開單後重置點：barA 時間戳必須 >= 此值才允許觸發
            # 初始值 0 表示尚未開過單，任何 barA 都允許
            "long_reset_bar_time":  0,
            "short_reset_bar_time": 0,
        }
    return models.fibonacci_state[symbol]


# ─── 基礎工具 ─────────────────────────────────────────────────────────────────

def _body_low(candle: tuple) -> float:
    return min(candle[1], candle[4])  # min(open, close)


def _body_high(candle: tuple) -> float:
    return max(candle[1], candle[4])  # max(open, close)


# ─── Fibonacci 計算 ───────────────────────────────────────────────────────────

def _calc_fib_levels(bar_a: tuple, bar_b: tuple, direction: str) -> dict | None:
    """計算 Fibonacci 關鍵層級。回傳 None 表示 fib_range 為零。"""
    if direction == "LONG":
        fib0 = min(_body_low(bar_a), _body_low(bar_b))
        fib1 = max(_body_high(bar_a), _body_high(bar_b))
    else:
        fib0 = max(_body_high(bar_a), _body_high(bar_b))
        fib1 = min(_body_low(bar_a), _body_low(bar_b))

    fib_range = abs(fib1 - fib0)
    if fib_range < _EPSILON:
        return None

    if direction == "LONG":
        fib_1_73 = fib0 + fib_range * FIB_CONFIRM_LEVEL
        fib_6_92 = fib0 + fib_range * FIB_TP1_LEVEL
    else:
        fib_1_73 = fib0 - fib_range * FIB_CONFIRM_LEVEL
        fib_6_92 = fib0 - fib_range * FIB_TP1_LEVEL

    return {
        "fib0":      fib0,
        "fib1":      fib1,
        "fib_range": fib_range,
        "fib_1_73":  fib_1_73,
        "fib_6_92":  fib_6_92,
    }


def _calc_sl(bar_a: tuple, bar_b: tuple, bar8: tuple, direction: str) -> float:
    if direction == "LONG":
        return min(bar_a[3], bar_b[3], bar8[3])   # min(low)
    else:
        return max(bar_a[2], bar_b[2], bar8[2])   # max(high)


# ─── 核心掃描 ─────────────────────────────────────────────────────────────────

def _scan_fib_pattern(klines: list, direction: str) -> dict | None:
    """掃描固定 9 根窗口，驗證完整 Fibonacci 形態。

    K 棒位置（相對 klines 末端）：
      barA = klines[-9], barB = klines[-8]
      bar5 = klines[-5], bar8 = klines[-2], bar9 = klines[-1]
    """
    n = len(klines)
    min_len = FIB_EMA_PERIOD + 9
    if n < min_len:
        return None

    a_idx    = n - 9
    b_idx    = n - 8
    bar5_idx = n - 5
    bar8_idx = n - 2
    bar9_idx = n - 1

    bar_a = klines[a_idx]
    bar_b = klines[b_idx]
    bar5  = klines[bar5_idx]
    bar8  = klines[bar8_idx]
    bar9  = klines[bar9_idx]

    # EMA（計算截至 barA / barB 的收盤序列）
    closes = [c[4] for c in klines]
    ema_a = calc_ema(closes[:a_idx + 1], FIB_EMA_PERIOD)
    ema_b = calc_ema(closes[:b_idx + 1], FIB_EMA_PERIOD)
    if ema_a is None or ema_b is None:
        return None

    # 條件 2：收盤在 EMA 同側
    if direction == "LONG":
        if bar_a[4] < ema_a or bar_b[4] < ema_b:
            return None
    else:
        if bar_a[4] > ema_a or bar_b[4] > ema_b:
            return None

    # 條件 3：底底高 / 頂頂低
    if direction == "LONG":
        if bar_b[3] < bar_a[3]:          # barB.low < barA.low
            return None
    else:
        if bar_b[2] > bar_a[2]:          # barB.high > barA.high
            return None

    # 計算 Fib 層級
    fib = _calc_fib_levels(bar_a, bar_b, direction)
    if fib is None:
        return None

    # 條件 4：barB+1 到 bar8 之間無收盤無效化
    for j in range(b_idx + 1, bar8_idx + 1):
        c = klines[j]
        if direction == "LONG":
            if c[4] < bar_a[3] - _EPSILON:    # close < barA.low
                return None
        else:
            if c[4] > bar_a[2] + _EPSILON:    # close > barA.high
                return None

    # bar5/bar8 影線確認（wick 觸及 Fib_1.73）
    fib_1_73 = fib["fib_1_73"]
    if direction == "LONG":
        if bar5[2] < fib_1_73 - _EPSILON:     # bar5.high < Fib_1.73
            return None
        if bar8[2] < fib_1_73 - _EPSILON:     # bar8.high < Fib_1.73
            return None
    else:
        if bar5[3] > fib_1_73 + _EPSILON:     # bar5.low > Fib_1.73
            return None
        if bar8[3] > fib_1_73 + _EPSILON:     # bar8.low > Fib_1.73
            return None

    # 計算 SL / TP1
    sl  = _calc_sl(bar_a, bar_b, bar8, direction)
    tp1 = fib["fib_6_92"]

    # 開單條件 2/3：bar5 到 bar9 無 SL / TP1 觸碰
    for j in range(bar5_idx, n):
        c = klines[j]
        if direction == "LONG":
            if c[3] <= sl:    # low <= SL
                return None
            if c[2] >= tp1:   # high >= TP1
                return None
        else:
            if c[2] >= sl:    # high >= SL
                return None
            if c[3] <= tp1:   # low <= TP1
                return None

    # 開單條件 4：方向正確（bar9 收盤 vs SL）
    current_price = bar9[4]
    if direction == "LONG" and current_price <= sl:
        return None
    if direction == "SHORT" and current_price >= sl:
        return None

    return {
        "bar_a":  bar_a,
        "bar_b":  bar_b,
        "bar8":   bar8,
        "bar9":   bar9,
        "sl":     sl,
        "tp1":    tp1,
        **fib,
    }


# ─── 主入口 ───────────────────────────────────────────────────────────────────

def on_new_fib_candle(symbol: str, candle: tuple, interval: str) -> list[dict]:
    """K 棒閉合後掃描 Fibonacci 形態。

    interval 需符合 FIB_K_INTERVAL 才處理，
    回傳觸發的訊號列表（0～2 個）。
    """
    if interval != FIB_K_INTERVAL:
        return []

    buf_key = _INTERVAL_BUFFER.get(FIB_K_INTERVAL)
    if buf_key is None:
        log.error(f"[Fib] 不支援的 FIB_K_INTERVAL='{FIB_K_INTERVAL}'")
        return []

    klines = list(models.symbol_state.get(symbol, {}).get(buf_key, []))
    if not klines:
        return []

    state   = get_or_init_fib_state(symbol)
    signals = []

    for direction in ("LONG", "SHORT"):
        result = _scan_fib_pattern(klines, direction)
        if result is None:
            continue

        bar9_time  = result["bar9"][0]
        bar_a_time = result["bar_a"][0]
        reset_key  = f"{direction.lower()}_reset_bar_time"

        # 開單後重置：barA 必須在上次 bar9（重置點）之後才允許觸發
        # 確保開單後形態窗口不會滑動重疊而連續觸發
        if bar_a_time < state[reset_key]:
            continue

        state[reset_key] = bar9_time  # 記錄本次 bar9 為新的重置點

        sig_type = "fibonacci_long" if direction == "LONG" else "fibonacci_short"
        bar_a    = result["bar_a"]
        bar_b    = result["bar_b"]
        bar9     = result["bar9"]

        signal = {
            "type":               sig_type,
            "direction":          direction,
            "symbol":             symbol,
            "close":              bar9[4],       # 與 order_manager 欄位一致
            "stop_loss":          result["sl"],
            "take_profit_1":      result["tp1"],
            "candle_open_time_ms": bar9[0],      # 與 evaluator / reporter 欄位一致
            "bar_a_time":         bar_a[0] // 1000,
            "bar_b_time":         bar_b[0] // 1000,
            "bar_9_time":         bar9[0] // 1000,
            "fib_0":         result["fib0"],
            "fib_1":         result["fib1"],
            "fib_range":     result["fib_range"],
            "fib_1_73":      result["fib_1_73"],
            "fib_6_92":      result["fib_6_92"],
            "interval":      FIB_K_INTERVAL,
            "scan_timestamp": int(time.time()),
        }
        signals.append(signal)
        log.info(
            f"[Fib] {symbol} {direction} 訊號觸發 "
            f"bar9_time={bar9_time} entry={bar9[4]:.6f} sl={result['sl']:.6f} tp1={result['tp1']:.6f} "
            f"→ 重置 barA，下次需 bar_a_time >= {bar9_time}"
        )

    return signals
