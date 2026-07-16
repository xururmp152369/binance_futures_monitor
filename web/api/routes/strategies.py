import time
import httpx
from fastapi import APIRouter, Query, HTTPException
from app.setting import models
from app.strategy.long_breakout import StrategyPhase
from app.strategy.death_cross_short import DeathCrossPhase
from app.setting.config import DC_ALERT_WINDOW_HOURS, DC_MAX_ENTRIES_PER_ALERT
from web.api.schemas import (
    LongBreakoutCoin,
    DeathCrossCoin,
    FibonacciCoin,
    Paginated,
    HealthResponse,
)

router = APIRouter()


def _current_price(symbol: str) -> float | None:
    st = models.symbol_state.get(symbol)
    if not st:
        return None
    return st.get("last_price")


@router.get("/health", response_model=HealthResponse)
def health():
    lb_tracking = lb_ready = 0
    for st in models.strategy_state.values():
        p = st.get("phase")
        if p == StrategyPhase.TRACKING:
            lb_tracking += 1
        elif p == StrategyPhase.READY:
            lb_ready += 1

    dc_watching = dc_alert = 0
    for st in models.death_cross_state.values():
        p = st.get("phase")
        if p == DeathCrossPhase.WATCHING:
            dc_watching += 1
        elif p == DeathCrossPhase.ALERT:
            dc_alert += 1
    return HealthResponse(
        status="ok",
        total_symbols=len(models.symbol_state),
        long_breakout_tracking=lb_tracking,
        long_breakout_ready=lb_ready,
        death_cross_watching=dc_watching,
        death_cross_alert=dc_alert,
        fibonacci_symbols=len(models.fibonacci_state),
    )


@router.get("/strategies/long_breakout", response_model=Paginated[LongBreakoutCoin])
def long_breakout(
    phase: str = Query("ALL", description="TRACKING | READY | ALL"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
):
    phase_upper = phase.upper()
    now = time.time()
    items: list[LongBreakoutCoin] = []

    for symbol, st in models.strategy_state.items():
        p = st.get("phase", StrategyPhase.IDLE)
        if phase_upper == "TRACKING" and p != StrategyPhase.TRACKING:
            continue
        if phase_upper == "READY" and p != StrategyPhase.READY:
            continue
        if p == StrategyPhase.IDLE:
            continue

        pump_time = st.get("pump_candle_time")
        consolidation_start = st.get("consolidation_start_ts")
        consolidation_hours: float | None = None
        if consolidation_start is not None:
            consolidation_hours = round((now - consolidation_start) / 3600, 1)

        top = st.get("consolidation_high")
        price = _current_price(symbol)
        distance: float | None = None
        if top and price:
            distance = round((price - top) / top * 100, 2)

        items.append(LongBreakoutCoin(
            symbol=symbol,
            phase=p.value,
            trigger_time_ts=pump_time,
            gain_pct=st.get("pump_candle_gain_pct"),
            volume_ratio=st.get("pump_candle_volume_ratio"),
            taker_buy_ratio=st.get("pump_candle_taker_buy_ratio"),
            is_method_b=st.get("is_method_b", False),
            consolidation_hours=consolidation_hours,
            consolidation_low=st.get("consolidation_low"),
            consolidation_high=top,
            current_price=price,
            distance_from_top_pct=distance,
        ))

    items.sort(
        key=lambda x: (x.gain_pct or 0) * (x.volume_ratio or 0),
        reverse=True,
    )

    total = len(items)
    start = (page - 1) * per_page
    return Paginated[LongBreakoutCoin](
        total=total,
        page=page,
        per_page=per_page,
        items=items[start : start + per_page],
    )


@router.get("/strategies/death_cross", response_model=Paginated[DeathCrossCoin])
def death_cross(
    phase: str = Query("ALL", description="WATCHING | ALERT | ALL"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
):
    phase_upper = phase.upper()
    now = time.time()
    items: list[DeathCrossCoin] = []

    for symbol, st in models.death_cross_state.items():
        p = st.get("phase", DeathCrossPhase.IDLE)
        if phase_upper == "WATCHING" and p != DeathCrossPhase.WATCHING:
            continue
        if phase_upper == "ALERT" and p != DeathCrossPhase.ALERT:
            continue
        if p == DeathCrossPhase.IDLE:
            continue

        alert_time = st.get("alert_time")
        elapsed: float | None = None
        if alert_time is not None:
            elapsed = round((now - alert_time) / 3600, 1)

        items.append(DeathCrossCoin(
            symbol=symbol,
            phase=p.value,
            alert_time_ts=alert_time,
            close_t0=st.get("close_t0"),
            entry_count=st.get("entry_count", 0),
            max_entries=DC_MAX_ENTRIES_PER_ALERT,
            alert_elapsed_hours=elapsed,
            alert_window_hours=DC_ALERT_WINDOW_HOURS,
            current_price=_current_price(symbol),
        ))

    items.sort(key=lambda x: x.symbol)
    total = len(items)
    start = (page - 1) * per_page
    return Paginated[DeathCrossCoin](
        total=total,
        page=page,
        per_page=per_page,
        items=items[start : start + per_page],
    )


@router.get("/strategies/fibonacci", response_model=Paginated[FibonacciCoin])
def fibonacci(
    direction: str = Query("long", description="long | short"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
):
    is_long = direction.lower() != "short"
    items: list[FibonacciCoin] = []
    for symbol, st in models.fibonacci_state.items():
        long_ts = st.get("long_reset_bar_time", 0)
        short_ts = st.get("short_reset_bar_time", 0)
        target_ts = long_ts if is_long else short_ts
        if target_ts == 0:
            continue
        items.append(FibonacciCoin(
            symbol=symbol,
            long_reset_bar_time=long_ts,
            short_reset_bar_time=short_ts,
            current_price=_current_price(symbol),
        ))

    items.sort(
        key=lambda x: max(x.long_reset_bar_time, x.short_reset_bar_time),
        reverse=True,
    )
    total = len(items)
    start = (page - 1) * per_page
    return Paginated[FibonacciCoin](
        total=total,
        page=page,
        per_page=per_page,
        items=items[start : start + per_page],
    )


@router.get("/chart/klines")
async def chart_klines(
    symbol: str = Query(...),
    interval: str = Query("4h"),
    limit: int = Query(500, ge=1, le=1000),
):
    url = "https://fapi.binance.com/fapi/v1/klines"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                url,
                params={"symbol": symbol, "interval": interval, "limit": limit},
                timeout=15,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=str(e))
    return [
        {
            "time": int(k[0]) // 1000,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        }
        for k in resp.json()
    ]
