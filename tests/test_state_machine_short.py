"""Type 1 Short 空頭狀態機測試（與多頭邏輯完全對稱）"""

import pytest
from app.setting import models
from app.strategy.state_machine import (
    StrategyPhase,
    on_new_4h_candle_short,
    on_new_15m_candle_short,
)
from tests.conftest import (
    make_15m_ohlc_deque,
    setup_symbol_state,
    trigger_short_tracking,
    make_short_run,
    _4H_MS,
)

SYM = "BTCUSDT"
TS0 = 1_000_000_000_000


def _st() -> dict:
    return models.strategy_state_short.get(SYM, {})


# ─── 空頭 Run 偵測 ─────────────────────────────────────────────────────────────

class TestShortRunDetection:
    def test_single_candle_8pct_triggers_on_second(self):
        """一根 8% 陰線 + 下一根不創新低 → TRACKING。"""
        trigger_short_tracking(SYM, TS0, drop_pct=8.0)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_multi_candle_cumulative_triggers(self):
        """3+5% 兩根陰線累積 8% → 第三根不創新低時進入 TRACKING。"""
        candles, _ = make_short_run(TS0, [3.0, 5.0])
        for c in candles:
            on_new_4h_candle_short(SYM, c)
        assert _st()["phase"] == StrategyPhase.IDLE  # 尚未停止創新低

        ts2   = TS0 + 2 * _4H_MS
        open3 = candles[-1][4]
        low3  = candles[-1][3] * 1.001   # 高於最後一根 low → 不創新低
        c3 = (ts2, open3, open3 * 1.002, low3, open3 * 0.999)
        on_new_4h_candle_short(SYM, c3)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_cumulative_below_threshold_does_not_trigger(self):
        """累積 3+3=6% < 8%，第三根不創新低 → 不進 TRACKING。"""
        candles, _ = make_short_run(TS0, [3.0, 3.0])
        for c in candles:
            on_new_4h_candle_short(SYM, c)
        ts2   = TS0 + 2 * _4H_MS
        open3 = candles[-1][4]
        low3  = candles[-1][3] * 1.01
        c3 = (ts2, open3, open3 * 1.001, low3, open3 * 1.001)
        on_new_4h_candle_short(SYM, c3)
        assert _st().get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

    def test_bullish_candle_resets_run_below_threshold(self):
        """累積 5% 時出現陽線 → run 重置，不進 TRACKING。"""
        candles, _ = make_short_run(TS0, [5.0])
        on_new_4h_candle_short(SYM, candles[0])
        ts1    = TS0 + _4H_MS
        close1 = candles[0][4]
        bull   = (ts1, close1, close1 * 1.005, close1 * 0.999, close1 * 1.002)
        on_new_4h_candle_short(SYM, bull)
        assert _st().get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

    def test_state_fields_after_tracking_entry(self):
        """進入 TRACKING 後，頂部 = run 第一根的 high，底部 = run 的最低 low。"""
        candles, _ = make_short_run(TS0, [5.0, 4.0])
        for c in candles:
            on_new_4h_candle_short(SYM, c)
        ts2   = TS0 + 2 * _4H_MS
        open3 = candles[-1][4]
        low3  = candles[-1][3] * 1.001
        c3 = (ts2, open3, open3 * 1.001, low3, open3 * 0.999)
        on_new_4h_candle_short(SYM, c3)

        st = _st()
        assert st["phase"] == StrategyPhase.TRACKING
        assert st["consolidation_high"] == pytest.approx(candles[0][2], rel=1e-6)
        assert st["consolidation_low"]  == pytest.approx(candles[-1][3], rel=1e-6)

    def test_new_run_after_failed_run_uses_new_high(self):
        """累積不足的 run 結束後，新 run 的頂部應是新 run 的第一根 high。"""
        candles, _ = make_short_run(TS0, [3.0])
        on_new_4h_candle_short(SYM, candles[0])
        ts1  = TS0 + _4H_MS
        c0   = candles[0]
        bull = (ts1, c0[4], c0[4] * 1.005, c0[4] * 0.999, c0[4] * 1.002)
        on_new_4h_candle_short(SYM, bull)

        # 新 run：兩根陰線累積 10%
        ts2    = ts1 + _4H_MS
        open2  = bull[4]
        close2 = round(open2 * 0.95, 8)
        high2  = round(open2 * 1.002, 8)
        low2   = round(close2 * 0.998, 8)
        c2 = (ts2, open2, high2, low2, close2)
        on_new_4h_candle_short(SYM, c2)

        ts3    = ts2 + _4H_MS
        open3  = close2
        close3 = round(open3 * 0.95, 8)
        high3  = round(open3 * 1.002, 8)
        low3   = round(close3 * 0.998, 8)
        c3 = (ts3, open3, high3, low3, close3)
        on_new_4h_candle_short(SYM, c3)

        ts4  = ts3 + _4H_MS
        c4 = (ts4, close3, close3 * 1.001, low3 * 1.001, close3 * 1.0005)
        on_new_4h_candle_short(SYM, c4)

        st = _st()
        assert st["phase"] == StrategyPhase.TRACKING
        assert st["consolidation_high"] == pytest.approx(high2, rel=1e-6)


# ─── 盤整追蹤 ─────────────────────────────────────────────────────────────────

class TestShortConsolidation:
    def test_new_low_extends_and_resets_timer(self):
        trigger_short_tracking(SYM, TS0, drop_pct=9.0)
        prev_low = _st()["consolidation_low"]
        ts_next  = TS0 + 2 * _4H_MS
        new_low  = prev_low * 0.95
        c = (ts_next, prev_low, prev_low * 1.002, new_low, new_low * 1.002)
        on_new_4h_candle_short(SYM, c)
        assert _st()["consolidation_low"]      == pytest.approx(new_low, rel=1e-6)
        assert _st()["consolidation_start_ts"] == pytest.approx(ts_next / 1000, rel=1e-6)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_no_new_low_does_not_update_bottom(self):
        trigger_short_tracking(SYM, TS0, drop_pct=9.0)
        prev_low = _st()["consolidation_low"]
        ts_next  = TS0 + 2 * _4H_MS
        c = (ts_next, prev_low * 1.01, prev_low * 1.015, prev_low * 1.001, prev_low * 1.005)
        on_new_4h_candle_short(SYM, c)
        assert _st()["consolidation_low"] == pytest.approx(prev_low, rel=1e-6)

    def test_tracking_to_ready_after_min_hours(self):
        trigger_short_tracking(SYM, TS0, drop_pct=9.0)
        peak_ts  = _st()["consolidation_start_ts"]
        ts_ready = int((peak_ts + 13 * 3600) * 1000)
        prev_low = _st()["consolidation_low"]
        c = (ts_ready, prev_low * 1.01, prev_low * 1.015, prev_low * 1.001, prev_low * 1.005)
        on_new_4h_candle_short(SYM, c)
        assert _st()["phase"] == StrategyPhase.READY

    def test_new_low_in_ready_resets_to_tracking(self):
        trigger_short_tracking(SYM, TS0, drop_pct=9.0)
        peak_ts  = _st()["consolidation_start_ts"]
        prev_low = _st()["consolidation_low"]
        ts_ready = int((peak_ts + 13 * 3600) * 1000)
        c_flat = (ts_ready, prev_low * 1.01, prev_low * 1.015, prev_low * 1.001, prev_low * 1.005)
        on_new_4h_candle_short(SYM, c_flat)
        assert _st()["phase"] == StrategyPhase.READY

        ts_ext  = ts_ready + _4H_MS
        new_low = prev_low * 0.97
        c_ext = (ts_ext, prev_low, prev_low * 1.002, new_low, new_low * 1.002)
        on_new_4h_candle_short(SYM, c_ext)
        assert _st()["phase"] == StrategyPhase.TRACKING
        assert _st()["consolidation_low"] == pytest.approx(new_low, rel=1e-6)


# ─── 廢棄條件 ─────────────────────────────────────────────────────────────────

class TestShortInvalidation:
    def test_high_above_top_resets_to_idle(self):
        trigger_short_tracking(SYM, TS0, drop_pct=9.0)
        top     = _st()["consolidation_high"]
        ts_next = TS0 + 2 * _4H_MS
        c = (ts_next, top * 0.99, top * 1.01, top * 0.985, top * 1.005)
        on_new_4h_candle_short(SYM, c)
        assert _st()["phase"] == StrategyPhase.IDLE

    def test_high_equal_to_top_is_not_invalidated(self):
        trigger_short_tracking(SYM, TS0, drop_pct=9.0)
        top     = _st()["consolidation_high"]
        ts_next = TS0 + 2 * _4H_MS
        c = (ts_next, top * 0.99, top, top * 0.985, top * 0.995)
        on_new_4h_candle_short(SYM, c)
        assert _st()["phase"] != StrategyPhase.IDLE


# ─── Method B（盤整內 sub-run 重置）─────────────────────────────────────────

class TestShortMethodB:
    def test_sub_run_8pct_with_lower_top_resets(self):
        """TRACKING 期間，頂部更低的 8% sub-run → Method B 重置。"""
        trigger_short_tracking(SYM, TS0, base=100.0, drop_pct=20.0)
        old_top    = _st()["consolidation_high"]
        old_bottom = _st()["consolidation_low"]

        # sub-run 起始於盤整範圍內，頂部低於 old_top
        ts_base   = TS0 + 3 * _4H_MS
        sub_open  = old_bottom * 1.15
        assert sub_open < old_top

        sub_close = round(sub_open * 0.95, 8)
        sub_high  = round(sub_open * 1.002, 8)
        sub_low   = round(sub_close * 0.998, 8)
        c1 = (ts_base, sub_open, sub_high, sub_low, sub_close)
        on_new_4h_candle_short(SYM, c1)

        ts2    = ts_base + _4H_MS
        open2  = sub_close
        close2 = round(open2 * 0.958, 8)
        low2   = round(close2 * 0.998, 8)
        high2  = round(open2 * 1.002, 8)
        c2 = (ts2, open2, high2, low2, close2)
        on_new_4h_candle_short(SYM, c2)

        # 第三根不創新低 → sub-run 結束 → Method B
        ts3   = ts2 + _4H_MS
        open3 = close2
        c3 = (ts3, open3, open3 * 1.001, low2 * 1.001, open3 * 1.0005)
        on_new_4h_candle_short(SYM, c3)

        st = _st()
        assert st["phase"] == StrategyPhase.TRACKING
        assert st["consolidation_high"] < old_top
        assert st["consolidation_high"] == pytest.approx(sub_high, rel=1e-6)
        assert st["consolidation_low"]  == pytest.approx(low2,     rel=1e-6)

    def test_sub_run_with_same_top_no_reset(self):
        """sub-run 起始 high == 現有頂部 → 不觸發 Method B。"""
        trigger_short_tracking(SYM, TS0, base=100.0, drop_pct=20.0)
        old_top    = _st()["consolidation_high"]
        old_bottom = _st()["consolidation_low"]

        ts_base  = TS0 + 3 * _4H_MS
        sub_open = old_top * 0.995
        sub_high = old_top   # run_start_high == old_top，不小於
        sub_close = round(sub_open * 0.95, 8)
        sub_low   = round(sub_close * 0.998, 8)
        c1 = (ts_base, sub_open, sub_high, sub_low, sub_close)
        on_new_4h_candle_short(SYM, c1)

        ts2    = ts_base + _4H_MS
        open2  = sub_close
        close2 = round(open2 * 0.958, 8)
        low2   = round(close2 * 0.998, 8)
        c2 = (ts2, open2, sub_high * 0.999, low2, close2)
        on_new_4h_candle_short(SYM, c2)

        ts3   = ts2 + _4H_MS
        c3 = (ts3, close2, close2 * 1.001, low2 * 1.001, close2 * 1.0005)
        on_new_4h_candle_short(SYM, c3)

        st = _st()
        assert st["consolidation_high"] == pytest.approx(old_top,    rel=1e-6)
        assert st["consolidation_low"]  == pytest.approx(old_bottom, rel=1e-6)

    def test_sub_run_exceeds_bottom_extends_instead(self):
        """空頭盤整內的 sub-run 若直接跌破 consolidation_low → 整體延伸，頂部不變。"""
        trigger_short_tracking(SYM, TS0, base=100.0, drop_pct=9.0)
        old_bottom = _st()["consolidation_low"]
        old_top    = _st()["consolidation_high"]

        # 開始一個 sub-run（先下跌一根，run_low 已設定）
        ts_base   = TS0 + 3 * _4H_MS
        sub_open  = old_bottom * 1.05
        sub_close = round(sub_open * 0.97, 8)
        sub_high  = round(sub_open * 1.002, 8)
        sub_low   = round(sub_close * 0.998, 8)
        on_new_4h_candle_short(SYM, (ts_base, sub_open, sub_high, sub_low, sub_close))

        # 下一根直接跌破 consolidation_low → 整體延伸（不是 Method B）
        ts2     = ts_base + _4H_MS
        new_low = old_bottom * 0.95
        on_new_4h_candle_short(SYM, (ts2, sub_close, sub_close * 1.002, new_low, new_low * 1.002))

        st = _st()
        assert st["phase"] == StrategyPhase.TRACKING
        assert st["consolidation_low"]      == pytest.approx(new_low,  rel=1e-6)
        assert st["consolidation_high"]     == pytest.approx(old_top,  rel=1e-6)
        assert st["consolidation_start_ts"] == pytest.approx(ts2 / 1000, rel=1e-6)


# ─── Type 1 Short 訊號 ────────────────────────────────────────────────────────

class TestType1ShortSignal:
    def _setup_ready(self):
        trigger_short_tracking(SYM, TS0, base=100.0, drop_pct=9.0)
        st       = _st()
        peak_ts  = st["consolidation_start_ts"]
        ts_ready = int((peak_ts + 13 * 3600) * 1000)
        bottom   = st["consolidation_low"]
        c = (ts_ready, bottom * 1.01, bottom * 1.015, bottom * 1.001, bottom * 1.005)
        on_new_4h_candle_short(SYM, c)
        assert _st()["phase"] == StrategyPhase.READY
        setup_symbol_state(SYM, kline_15m_ohlc=make_15m_ohlc_deque(count=200, base_volume=100.0))

    def test_breakout_below_bottom_with_volume_triggers(self):
        self._setup_ready()
        bottom    = _st()["consolidation_low"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, bottom, bottom * 1.005, bottom * 0.985, bottom * 0.99, 400.0)
        result = on_new_15m_candle_short(SYM, candle)
        assert result is not None
        assert result["type"] == "type1_short"

    def test_no_signal_when_close_at_or_above_bottom(self):
        self._setup_ready()
        bottom    = _st()["consolidation_low"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, bottom * 1.01, bottom * 1.015, bottom, bottom, 400.0)
        assert on_new_15m_candle_short(SYM, candle) is None

    def test_no_signal_when_volume_insufficient(self):
        self._setup_ready()
        bottom    = _st()["consolidation_low"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, bottom, bottom * 1.005, bottom * 0.985, bottom * 0.99, 150.0)
        assert on_new_15m_candle_short(SYM, candle) is None

    def test_cooldown_prevents_repeat_signal(self):
        self._setup_ready()
        bottom    = _st()["consolidation_low"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, bottom, bottom * 1.005, bottom * 0.985, bottom * 0.99, 400.0)
        r1 = on_new_15m_candle_short(SYM, candle)
        assert r1 is not None
        candle2 = (ts_candle + 15 * 60 * 1000, bottom * 0.99, bottom, bottom * 0.98, bottom * 0.985, 400.0)
        assert on_new_15m_candle_short(SYM, candle2) is None

    def test_no_signal_when_insufficient_history(self):
        """15m 歷史資料不足 193 根 → 不觸發（即使跌破且放量）。"""
        self._setup_ready()
        setup_symbol_state(SYM, kline_15m_ohlc=make_15m_ohlc_deque(count=100, base_volume=100.0))
        bottom    = _st()["consolidation_low"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, bottom, bottom * 1.005, bottom * 0.985, bottom * 0.99, 400.0)
        assert on_new_15m_candle_short(SYM, candle) is None
