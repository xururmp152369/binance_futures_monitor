"""Type 1 Short 空頭狀態機測試（與多頭邏輯完全對稱）"""

import pytest
from app.setting import models
from app.strategy.state_machine import (
    StrategyPhase,
    on_new_4h_candle_short,
    on_new_15m_candle_short,
)
from tests.conftest import (
    make_dump_candle,
    make_flat_candle,
    make_15m_ohlc_deque,
    setup_symbol_state,
)

SYM = "BTCUSDT"
_4H = 4 * 3600 * 1000


def _st() -> dict:
    return models.strategy_state_short.get(SYM, {})


# ─── 暴跌偵測 ─────────────────────────────────────────────────────────────────

class TestDumpDetection:
    def test_bearish_large_range_triggers_tracking(self):
        candle = make_dump_candle(1000)
        on_new_4h_candle_short(SYM, candle)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_bullish_candle_does_not_trigger(self):
        # 陽線不觸發空頭
        candle = (1000, 90.0, 100.0, 88.0, 99.0)  # close > open
        on_new_4h_candle_short(SYM, candle)
        assert _st().get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

    def test_small_range_dump_does_not_trigger(self):
        # (high-low)/low = 5% < 8%
        candle = (1000, 100.0, 104.0, 99.0, 99.5)
        on_new_4h_candle_short(SYM, candle)
        assert _st().get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

    def test_dump_state_fields(self):
        candle = make_dump_candle(2000, low=90.0, high=100.0, open_=99.0, close=91.0)
        on_new_4h_candle_short(SYM, candle)
        st = _st()
        assert st["pump_candle_high"] == 100.0   # 固定失效線（空頭 = 頂部）
        assert st["pump_candle_low"]  == 90.0
        assert st["consolidation_low"]  == 90.0  # 動態底部（初始）
        assert st["consolidation_high"] == 100.0 # 固定頂部

    def test_new_dump_in_tracking_resets_to_new_base(self):
        on_new_4h_candle_short(SYM, make_dump_candle(1000, low=90.0, high=100.0, open_=99.0, close=91.0))
        # 新的暴跌 K 棒：底部更低
        on_new_4h_candle_short(SYM, make_dump_candle(5000, low=80.0, high=92.0, open_=91.0, close=82.0))
        st = _st()
        assert st["pump_candle_high"]   == 92.0
        assert st["consolidation_low"]  == 80.0
        assert st["consolidation_high"] == 92.0


# ─── 盤整追蹤 ─────────────────────────────────────────────────────────────────

class TestShortConsolidation:
    def test_new_low_extends_consolidation_and_resets_timer(self):
        on_new_4h_candle_short(SYM, make_dump_candle(1000, low=90.0, high=100.0, open_=99.0, close=91.0))
        # 創新低 → 更新 consolidation_low，重置計時
        candle2 = (5000, 91.5, 92.0, 85.0, 86.0)  # low=85 < 90，陰線，range ok
        on_new_4h_candle_short(SYM, candle2)
        st = _st()
        assert st["consolidation_low"]      == 85.0
        assert st["consolidation_start_ts"] == 5000 / 1000
        assert st["phase"] == StrategyPhase.TRACKING

    def test_no_new_low_does_not_update(self):
        on_new_4h_candle_short(SYM, make_dump_candle(1000, low=90.0, high=100.0, open_=99.0, close=91.0))
        # low=93 > 90，不創新低
        candle2 = (5000, 93.0, 95.0, 93.0, 93.5)
        on_new_4h_candle_short(SYM, candle2)
        assert _st()["consolidation_low"] == 90.0

    def test_tracking_to_ready_after_min_hours(self):
        ts0 = 1_000_000_000_000
        on_new_4h_candle_short(SYM, make_dump_candle(ts0, low=90.0, high=100.0, open_=99.0, close=91.0))
        # 超過 12h 沒創新低 → READY
        ts_ready = ts0 + 13 * 3600 * 1000
        candle2 = (ts_ready, 91.5, 93.0, 91.0, 91.8)
        on_new_4h_candle_short(SYM, candle2)
        assert _st()["phase"] == StrategyPhase.READY

    def test_new_low_in_ready_resets_to_tracking(self):
        ts0 = 1_000_000_000_000
        on_new_4h_candle_short(SYM, make_dump_candle(ts0, low=90.0, high=100.0, open_=99.0, close=91.0))
        ts_ready = ts0 + 13 * 3600 * 1000
        on_new_4h_candle_short(SYM, (ts_ready, 91.5, 93.0, 91.0, 91.8))
        assert _st()["phase"] == StrategyPhase.READY
        # 創新低 → 退回 TRACKING
        ts_new_low = ts_ready + _4H
        on_new_4h_candle_short(SYM, (ts_new_low, 91.0, 92.0, 85.0, 86.0))
        assert _st()["phase"] == StrategyPhase.TRACKING
        assert _st()["consolidation_low"] == 85.0


# ─── 廢棄條件 ─────────────────────────────────────────────────────────────────

class TestShortInvalidation:
    def test_high_above_dump_candle_high_resets_to_idle(self):
        on_new_4h_candle_short(SYM, make_dump_candle(1000, low=90.0, high=100.0, open_=99.0, close=91.0))
        # high=101 > 100 → 廢棄
        on_new_4h_candle_short(SYM, (5000, 95.0, 101.0, 94.0, 95.5))
        assert _st()["phase"] == StrategyPhase.IDLE

    def test_high_equal_to_dump_candle_high_is_not_invalidated(self):
        on_new_4h_candle_short(SYM, make_dump_candle(1000, low=90.0, high=100.0, open_=99.0, close=91.0))
        on_new_4h_candle_short(SYM, (5000, 95.0, 100.0, 94.0, 95.5))
        assert _st()["phase"] != StrategyPhase.IDLE


# ─── Type 1 Short 訊號 ────────────────────────────────────────────────────────

class TestType1ShortSignal:
    def _setup_ready(self, bottom=90.0, top=100.0):
        """建立 READY 狀態，consolidation_low=bottom，consolidation_high=top。"""
        ts0 = 1_000_000_000_000
        on_new_4h_candle_short(SYM, make_dump_candle(ts0, low=bottom, high=top, open_=top - 1, close=bottom + 1))
        ts_ready = ts0 + 13 * 3600 * 1000
        on_new_4h_candle_short(SYM, (ts_ready, bottom + 2, bottom + 4, bottom + 1.5, bottom + 2.5))
        assert _st()["phase"] == StrategyPhase.READY
        setup_symbol_state(SYM, kline_15m_ohlc=make_15m_ohlc_deque(count=200, base_volume=100.0))

    def test_breakout_below_bottom_with_volume_triggers(self):
        self._setup_ready(bottom=90.0)
        # close=89 < 90，成交量是均量的 4 倍
        ts_candle = 1_000_000_000_000 + 14 * 3600 * 1000
        candle = (ts_candle, 90.5, 90.8, 88.5, 89.0, 400.0)
        result = on_new_15m_candle_short(SYM, candle)
        assert result is not None
        assert result["type"] == "type1_short"
        assert result["close"] == 89.0

    def test_no_signal_when_close_above_bottom(self):
        self._setup_ready(bottom=90.0)
        ts_candle = 1_000_000_000_000 + 14 * 3600 * 1000
        candle = (ts_candle, 90.5, 91.0, 89.5, 90.5, 400.0)  # close=90.5 >= 90
        result = on_new_15m_candle_short(SYM, candle)
        assert result is None

    def test_no_signal_when_volume_insufficient(self):
        self._setup_ready(bottom=90.0)
        ts_candle = 1_000_000_000_000 + 14 * 3600 * 1000
        candle = (ts_candle, 90.5, 90.8, 88.5, 89.0, 150.0)  # 1.5× < 3×
        result = on_new_15m_candle_short(SYM, candle)
        assert result is None

    def test_stop_loss_is_highest_high_of_volume_surge(self):
        self._setup_ready(bottom=90.0)
        # 在同一個 4h K 棒內塞兩根放量 K（high 不同）
        ts0 = 1_000_000_000_000 + 13 * 3600 * 1000
        current_4h_open = (ts0 // _4H) * _4H
        ohlc = make_15m_ohlc_deque(count=200, base_volume=100.0)
        # 倒數第 2 根：high=91.5，放量
        ohlc[-2] = (current_4h_open + 15 * 60 * 1000, 91.0, 91.5, 89.0, 89.5, 400.0)
        # 倒數第 3 根：high=92.0，放量（更高）
        ohlc[-3] = (current_4h_open, 92.0, 92.0, 89.5, 90.0, 400.0)
        setup_symbol_state(SYM, kline_15m_ohlc=ohlc)

        ts_candle = current_4h_open + 2 * 15 * 60 * 1000
        candle = (ts_candle, 90.5, 90.8, 88.5, 89.0, 400.0)
        result = on_new_15m_candle_short(SYM, candle)
        assert result is not None
        # 止損應取連續放量序列內的最高 high
        assert result["stop_loss"] >= 91.5

    def test_cooldown_prevents_repeat_signal(self):
        self._setup_ready(bottom=90.0)
        ts_candle = 1_000_000_000_000 + 14 * 3600 * 1000
        candle = (ts_candle, 90.5, 90.8, 88.5, 89.0, 400.0)
        r1 = on_new_15m_candle_short(SYM, candle)
        assert r1 is not None
        candle2 = (ts_candle + 15 * 60 * 1000, 89.5, 90.0, 88.0, 88.5, 400.0)
        r2 = on_new_15m_candle_short(SYM, candle2)
        assert r2 is None  # 冷卻中
