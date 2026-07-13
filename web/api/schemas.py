from __future__ import annotations
from pydantic import BaseModel
from typing import Optional


class LongBreakoutCoin(BaseModel):
    symbol: str
    phase: str
    trigger_time_ts: Optional[float]
    gain_pct: Optional[float]
    volume_ratio: Optional[float]
    taker_buy_ratio: Optional[float]
    is_method_b: bool
    consolidation_hours: Optional[float]
    consolidation_low: Optional[float]
    consolidation_high: Optional[float]
    current_price: Optional[float]
    distance_from_top_pct: Optional[float]


class DeathCrossCoin(BaseModel):
    symbol: str
    phase: str
    alert_time_ts: Optional[float]
    close_t0: Optional[float]
    entry_count: int
    max_entries: int
    alert_elapsed_hours: Optional[float]
    alert_window_hours: int
    current_price: Optional[float]


class FibonacciCoin(BaseModel):
    symbol: str
    long_reset_bar_time: float
    short_reset_bar_time: float
    current_price: Optional[float]


class PaginatedLongBreakout(BaseModel):
    total: int
    page: int
    per_page: int
    items: list[LongBreakoutCoin]


class PaginatedDeathCross(BaseModel):
    total: int
    page: int
    per_page: int
    items: list[DeathCrossCoin]


class PaginatedFibonacci(BaseModel):
    total: int
    page: int
    per_page: int
    items: list[FibonacciCoin]


class HealthResponse(BaseModel):
    status: str
    total_symbols: int
    long_breakout_tracking: int
    long_breakout_ready: int
    death_cross_watching: int
    death_cross_alert: int
    fibonacci_symbols: int
