"""策略協調器（Orchestrator）。

對外維持與原 state_machine 完全相同的公開 API，
內部分派給 long_breakout、death_cross_short、long_short_fibonacci 各自的策略模組。

外部模組（binance_opendata, monitor, command, tests）的 import 無需修改。
"""
from .analysis_utils import Direction
from .long_breakout import (
    StrategyPhase,
    get_or_init_long_state,
    reset_long_to_idle,
    on_new_4h_candle_long,
    on_new_15m_candle_long,
    check_long_invalidation_realtime,
    replay_historical_4h_candles_long,
)
from .death_cross_short import (
    DeathCrossPhase,
    on_new_daily_candle as dc_on_new_daily_candle,
    on_new_1h_candle as dc_on_new_1h_candle,
    replay_historical_daily_candles,
)
from .long_short_fibonacci import on_new_fib_candle as fib_on_new_candle
from ..extension.utils import setup_logging

log = setup_logging()


# ─── 向下相容的公開別名 ───────────────────────────────────────────────────────

def get_or_init_strategy_state(symbol: str) -> dict:
    return get_or_init_long_state(symbol)


def reset_to_idle(symbol: str, reason: str = "") -> None:
    reset_long_to_idle(symbol, reason)


# ─── 策略協調器 ──────────────────────────────────────────────────────────────

def on_new_4h_candle(symbol: str, candle: tuple, direction: Direction = Direction.LONG) -> None:
    on_new_4h_candle_long(symbol, candle, direction)


def on_new_15m_candle(
    symbol: str, candle: tuple,
    direction: Direction = Direction.LONG,
) -> dict | None:
    return on_new_15m_candle_long(symbol, candle, direction)


def check_invalidation_realtime(
    symbol: str,
    direction: Direction = Direction.LONG,
) -> bool:
    """即時廢棄：多頭廢棄只重置多頭狀態，等待 4h 收盤確認後才觸發空頭策略。"""
    return check_long_invalidation_realtime(symbol, direction)


def scan_strategy(symbol: str) -> None:
    check_long_invalidation_realtime(symbol)


def replay_historical_4h_candles(
    symbol: str,
    direction: Direction = Direction.LONG,
) -> None:
    """啟動時恢復多頭歷史狀態。空頭策略由廢棄事件驅動，不需歷史回播。"""
    replay_historical_4h_candles_long(symbol, direction)


def on_new_daily_candle(symbol: str, candle: tuple) -> None:
    """協調死亡叉策略的 Daily 狀態轉換。"""
    dc_on_new_daily_candle(symbol, candle)


def on_new_1h_candle(symbol: str, candle: tuple) -> dict | None:
    """協調死亡叉策略的 1H 進場信號，回傳 Type 3 訊號或 None。"""
    return dc_on_new_1h_candle(symbol, candle)


def replay_historical_daily_candles_dc(symbol: str) -> None:
    """啟動時恢復死亡叉策略的日線歷史狀態。"""
    replay_historical_daily_candles(symbol)


def on_new_fib_candle(symbol: str, candle: tuple, interval: str) -> list[dict]:
    """協調 Fibonacci 策略，回傳觸發的訊號列表（0～2 個）。

    interval 需符合 FIB_K_INTERVAL（config）才會處理，
    在 15m / 1h / 4h 的 handler 中均可呼叫，不符合的直接回傳空列表。
    """
    return fib_on_new_candle(symbol, candle, interval)
