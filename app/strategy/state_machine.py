"""策略協調器（Orchestrator）。

對外維持與原 state_machine 完全相同的公開 API，
內部分派給 long_breakout 與 short_bounce 各自的策略模組。

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
from .short_bounce import (
    enter_short_watching,
    on_new_4h_candle_short,
    on_new_15m_candle_short,
    check_short_invalidation_realtime,
)
from .death_cross_short import (
    DeathCrossPhase,
    on_new_daily_candle as dc_on_new_daily_candle,
    on_new_1h_candle as dc_on_new_1h_candle,
    replay_historical_daily_candles,
)
from ..extension.utils import setup_logging

log = setup_logging()


# ─── 向下相容的公開別名 ───────────────────────────────────────────────────────

def get_or_init_strategy_state(symbol: str) -> dict:
    return get_or_init_long_state(symbol)


def reset_to_idle(symbol: str, reason: str = "") -> None:
    reset_long_to_idle(symbol, reason)


# ─── 策略協調器 ──────────────────────────────────────────────────────────────

def on_new_4h_candle(
    symbol: str, candle: tuple,
    direction: Direction = Direction.LONG,
) -> None:
    """協調多頭與空頭策略的 4h 狀態轉換。

    多頭廢棄（4h 收盤確認）時自動觸發空頭進入 SHORT_WATCHING。
    """
    event = on_new_4h_candle_long(symbol, candle, direction)
    if event and event.get("event") == "abandoned":
        enter_short_watching(
            symbol,
            event["abandonment_high"],
            event["abandonment_low"],
            event["ts"],
        )
    on_new_4h_candle_short(symbol, candle)


def on_new_15m_candle(
    symbol: str, candle: tuple,
    direction: Direction = Direction.LONG,
) -> dict | None:
    """依序檢查 Type 1（多頭）和 Type 2（空頭）訊號，回傳第一個觸發的訊號。"""
    signal = on_new_15m_candle_long(symbol, candle, direction)
    if signal:
        return signal
    return on_new_15m_candle_short(symbol, candle)


def check_invalidation_realtime(
    symbol: str,
    direction: Direction = Direction.LONG,
) -> bool:
    """即時廢棄：多頭廢棄只重置多頭狀態，等待 4h 收盤確認後才觸發空頭策略。"""
    return check_long_invalidation_realtime(symbol, direction)


def scan_strategy(symbol: str) -> None:
    """periodic_screen 呼叫入口：執行多頭與空頭即時廢棄檢查。"""
    check_long_invalidation_realtime(symbol)
    check_short_invalidation_realtime(symbol)


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
