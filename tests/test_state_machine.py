"""Type 1 多頭狀態機測試（累積漲幅偵測 + Method B）"""

import time
import pytest
from collections import deque
from app.setting import models
from app.strategy.state_machine import (
    StrategyPhase,
    on_new_4h_candle,
    on_new_15m_candle,
    check_invalidation_realtime,
    replay_historical_4h_candles,
)
from tests.conftest import (
    make_flat_candle,
    make_15m_ohlc_deque,
    setup_symbol_state,
    trigger_long_tracking,
    make_long_run,
    _4H_MS,
)

SYM = "BTCUSDT"
TS0 = 1_000_000_000_000   # 測試用基準時間戳（ms）


def _st() -> dict:
    return models.strategy_state.get(SYM, {})


# ─── 多頭 Run 偵測 ─────────────────────────────────────────────────────────────

class TestRunDetection:
    def test_single_candle_8pct_triggers_on_second(self):
        """一根 8% 陽線 + 下一根不創新高 → TRACKING。"""
        trigger_long_tracking(SYM, TS0, gain_pct=8.0)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_multi_candle_cumulative_triggers(self):
        """3+5% 兩根陽線累積 8% → 第三根不創新高時進入 TRACKING。"""
        candles, _ = make_long_run(TS0, [3.0, 5.0])
        for c in candles:
            on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.IDLE  # 尚未停止創新高

        # 第三根不創新高 → run 結束，累積達標 → TRACKING
        ts2   = TS0 + 2 * _4H_MS
        open3 = candles[-1][4]
        high3 = candles[-1][2] * 0.999
        c3 = (ts2, open3, high3, open3 * 0.998, open3 * 1.001, 1000.0)
        on_new_4h_candle(SYM, c3)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_cumulative_below_threshold_does_not_trigger(self):
        """累積 3+3=6% < 8%，第三根不創新高 → 不進 TRACKING。"""
        candles, _ = make_long_run(TS0, [3.0, 3.0])
        for c in candles:
            on_new_4h_candle(SYM, c)
        ts2   = TS0 + 2 * _4H_MS
        open3 = candles[-1][4]
        high3 = candles[-1][2] * 0.99
        c3 = (ts2, open3, high3, open3 * 0.998, open3 * 0.999, 1000.0)
        on_new_4h_candle(SYM, c3)
        assert _st().get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

    def test_bearish_candle_resets_run_below_threshold(self):
        """累積 5% 時出現陰線 → run 重置，不進 TRACKING。"""
        candles, _ = make_long_run(TS0, [5.0])
        on_new_4h_candle(SYM, candles[0])
        ts1   = TS0 + _4H_MS
        close1 = candles[0][4]
        bear  = (ts1, close1, close1 * 1.001, close1 * 0.995, close1 * 0.998, 1000.0)
        on_new_4h_candle(SYM, bear)
        assert _st().get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

    def test_run_triggered_by_bearish_stop_candle_no_new_run(self):
        """達標 run + 陰線停止根 → 進入 TRACKING，但不啟動新 run。"""
        candles, _ = make_long_run(TS0, [9.0])
        on_new_4h_candle(SYM, candles[0])
        # 陰線：high < run_high（停止 run），close < open（不啟動新 run）
        ts1    = TS0 + _4H_MS
        open2  = candles[0][4]
        close2 = round(open2 * 0.99, 8)
        high2  = round(candles[0][2] * 0.999, 8)
        low2   = round(close2 * 0.998, 8)
        c2 = (ts1, open2, high2, low2, close2, 1000.0)
        on_new_4h_candle(SYM, c2)

        st = _st()
        assert st["phase"] == StrategyPhase.TRACKING
        assert st["run_high"] is None

    def test_state_fields_after_tracking_entry(self):
        """進入 TRACKING 後，底部 = run 第一根的 low，頂部 = run 的最高 high。"""
        candles, _ = make_long_run(TS0, [5.0, 4.0])
        for c in candles:
            on_new_4h_candle(SYM, c)
        ts2   = TS0 + 2 * _4H_MS
        open3 = candles[-1][4]
        c3 = (ts2, open3, candles[-1][2] * 0.99, open3 * 0.998, open3 * 1.001, 1000.0)
        on_new_4h_candle(SYM, c3)

        st = _st()
        assert st["phase"] == StrategyPhase.TRACKING
        assert st["consolidation_low"]  == pytest.approx(candles[0][3], rel=1e-6)
        assert st["consolidation_high"] == pytest.approx(candles[-1][2], rel=1e-6)

    def test_new_run_after_failed_run_uses_new_low(self):
        """累積不足的 run 結束後，新 run 的底部應是新 run 的第一根 low。"""
        candles, _ = make_long_run(TS0, [3.0])
        on_new_4h_candle(SYM, candles[0])
        # 陰線結束 run（3% < 8%，重置）
        ts1  = TS0 + _4H_MS
        c0   = candles[0]
        bear = (ts1, c0[4], c0[4] * 1.001, c0[4] * 0.995, c0[4] * 0.997, 1000.0)
        on_new_4h_candle(SYM, bear)

        # 新 run：兩根陽線累積 10%
        ts2    = ts1 + _4H_MS
        open2  = bear[4]
        close2 = round(open2 * 1.05, 8)
        high2  = round(close2 * 1.002, 8)
        low2   = round(open2 * 0.998, 8)
        c2 = (ts2, open2, high2, low2, close2, 1000.0)
        on_new_4h_candle(SYM, c2)

        ts3    = ts2 + _4H_MS
        open3  = close2
        close3 = round(open3 * 1.05, 8)
        high3  = round(close3 * 1.002, 8)
        c3 = (ts3, open3, high3, open3 * 0.998, close3, 1000.0)
        on_new_4h_candle(SYM, c3)

        # 第四根不創新高 → TRACKING，底部應是新 run 的 low2
        ts4   = ts3 + _4H_MS
        c4 = (ts4, close3, high3 * 0.99, close3 * 0.998, close3 * 1.001, 1000.0)
        on_new_4h_candle(SYM, c4)

        st = _st()
        assert st["phase"] == StrategyPhase.TRACKING
        assert st["consolidation_low"] == pytest.approx(low2, rel=1e-6)


# ─── 盤整追蹤 ─────────────────────────────────────────────────────────────────

class TestConsolidation:
    def test_new_high_extends_and_resets_timer(self):
        trigger_long_tracking(SYM, TS0, gain_pct=9.0)
        prev_high = _st()["consolidation_high"]
        ts_next   = TS0 + 2 * _4H_MS
        new_high  = prev_high * 1.05
        c = (ts_next, prev_high, new_high, prev_high * 0.99, new_high * 0.998, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["consolidation_high"]     == pytest.approx(new_high, rel=1e-6)
        assert _st()["consolidation_start_ts"] == pytest.approx(ts_next / 1000, rel=1e-6)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_no_new_high_does_not_update_top(self):
        trigger_long_tracking(SYM, TS0, gain_pct=9.0)
        prev_high = _st()["consolidation_high"]
        ts_next   = TS0 + 2 * _4H_MS
        c = (ts_next, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["consolidation_high"] == pytest.approx(prev_high, rel=1e-6)

    def test_tracking_to_ready_after_min_hours(self):
        trigger_long_tracking(SYM, TS0, gain_pct=9.0)
        peak_ts  = _st()["consolidation_start_ts"]
        ts_ready = int((peak_ts + 17 * 3600) * 1000)
        prev_high = _st()["consolidation_high"]
        c = (ts_ready, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.READY

    def test_new_high_in_ready_resets_to_tracking(self):
        trigger_long_tracking(SYM, TS0, gain_pct=9.0)
        peak_ts   = _st()["consolidation_start_ts"]
        prev_high = _st()["consolidation_high"]
        ts_ready  = int((peak_ts + 17 * 3600) * 1000)
        c_flat = (ts_ready, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0)
        on_new_4h_candle(SYM, c_flat)
        assert _st()["phase"] == StrategyPhase.READY

        ts_ext   = ts_ready + _4H_MS
        new_high = prev_high * 1.03
        c_ext = (ts_ext, prev_high, new_high, prev_high * 0.99, new_high * 0.998, 1000.0)
        on_new_4h_candle(SYM, c_ext)
        assert _st()["phase"] == StrategyPhase.TRACKING
        assert _st()["consolidation_high"] == pytest.approx(new_high, rel=1e-6)


# ─── 廢棄條件 ─────────────────────────────────────────────────────────────────

class TestInvalidation:
    def test_low_below_bottom_resets_to_idle(self):
        trigger_long_tracking(SYM, TS0, gain_pct=9.0)
        bottom  = _st()["consolidation_low"]
        ts_next = TS0 + 2 * _4H_MS
        c = (ts_next, bottom * 0.99, bottom * 1.01, bottom * 0.98, bottom * 0.985, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.IDLE

    def test_low_equal_to_bottom_is_not_invalidated(self):
        trigger_long_tracking(SYM, TS0, gain_pct=9.0)
        bottom  = _st()["consolidation_low"]
        ts_next = TS0 + 2 * _4H_MS
        c = (ts_next, bottom * 1.01, bottom * 1.02, bottom, bottom * 1.015, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] != StrategyPhase.IDLE

    def test_ready_phase_invalidated_by_4h_low(self):
        """READY 階段 4h K low < consolidation_low → 廢棄至 IDLE。"""
        trigger_long_tracking(SYM, TS0, gain_pct=9.0)
        peak_ts  = _st()["consolidation_start_ts"]
        ts_ready = int((peak_ts + 17 * 3600) * 1000)
        top      = _st()["consolidation_high"]
        on_new_4h_candle(SYM, (ts_ready, top * 0.99, top * 0.999, top * 0.985, top * 0.995, 1000.0))
        assert _st()["phase"] == StrategyPhase.READY

        bottom  = _st()["consolidation_low"]
        ts_next = ts_ready + _4H_MS
        c = (ts_next, bottom * 0.99, bottom * 1.01, bottom * 0.98, bottom * 0.985, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.IDLE


# ─── Method B（盤整內 sub-run 重置）─────────────────────────────────────────

class TestMethodB:
    def test_sub_run_8pct_with_higher_bottom_resets(self):
        """TRACKING 期間，底部更高的 8% sub-run → Method B 重置。"""
        trigger_long_tracking(SYM, TS0, base=100.0, gain_pct=20.0)
        old_bottom = _st()["consolidation_low"]
        old_top    = _st()["consolidation_high"]

        # sub-run 起始於盤整區間內，底部高於 old_bottom
        ts_base  = TS0 + 3 * _4H_MS
        sub_open = old_top * 0.85
        assert sub_open > old_bottom

        sub_close = round(sub_open * 1.05, 8)
        sub_high  = round(sub_close * 1.002, 8)
        sub_low   = round(sub_open * 0.998, 8)
        c1 = (ts_base, sub_open, sub_high, sub_low, sub_close, 1000.0)
        on_new_4h_candle(SYM, c1)

        ts2    = ts_base + _4H_MS
        open2  = sub_close
        close2 = round(open2 * 1.042, 8)
        high2  = round(close2 * 1.002, 8)
        c2 = (ts2, open2, high2, open2 * 0.998, close2, 1000.0)
        on_new_4h_candle(SYM, c2)

        # 第三根不創新高 → sub-run 結束 → Method B
        ts3   = ts2 + _4H_MS
        open3 = close2
        c3 = (ts3, open3, high2 * 0.99, open3 * 0.998, open3 * 1.001, 1000.0)
        on_new_4h_candle(SYM, c3)

        st = _st()
        assert st["phase"] == StrategyPhase.TRACKING
        assert st["consolidation_low"] > old_bottom
        assert st["consolidation_low"]  == pytest.approx(sub_low, rel=1e-6)
        assert st["consolidation_high"] == pytest.approx(high2, rel=1e-6)

    def test_sub_run_with_same_bottom_no_reset(self):
        """sub-run 起始 low == 現有底部 → 不觸發 Method B。"""
        trigger_long_tracking(SYM, TS0, base=100.0, gain_pct=20.0)
        old_bottom = _st()["consolidation_low"]
        old_top    = _st()["consolidation_high"]

        ts_base  = TS0 + 3 * _4H_MS
        sub_open = old_bottom * 1.005
        sub_low  = old_bottom   # run_start_low == old_bottom，不大於
        sub_close = round(sub_open * 1.05, 8)
        sub_high  = round(sub_close * 1.002, 8)
        c1 = (ts_base, sub_open, sub_high, sub_low, sub_close, 1000.0)
        on_new_4h_candle(SYM, c1)

        ts2    = ts_base + _4H_MS
        open2  = sub_close
        close2 = round(open2 * 1.045, 8)
        high2  = round(close2 * 1.002, 8)
        c2 = (ts2, open2, high2, open2 * 0.998, close2, 1000.0)
        on_new_4h_candle(SYM, c2)

        ts3   = ts2 + _4H_MS
        c3 = (ts3, close2, high2 * 0.99, close2 * 0.998, close2 * 1.001, 1000.0)
        on_new_4h_candle(SYM, c3)

        st = _st()
        assert st["consolidation_low"]  == pytest.approx(old_bottom, rel=1e-6)
        assert st["consolidation_high"] == pytest.approx(old_top,    rel=1e-6)

    def test_sub_run_exceeds_top_extends_instead(self):
        """sub-run 最高點超過 consolidation_high → 整體延伸，不走 Method B。"""
        trigger_long_tracking(SYM, TS0, gain_pct=9.0)
        old_top = _st()["consolidation_high"]

        ts_next  = TS0 + 3 * _4H_MS
        new_high = old_top * 1.05
        c = (ts_next, old_top * 0.99, new_high, old_top * 0.985, new_high * 0.998, 1000.0)
        on_new_4h_candle(SYM, c)

        assert _st()["consolidation_high"] == pytest.approx(new_high, rel=1e-6)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_method_b_in_ready_phase(self):
        """READY 階段出現底部更高的 8% sub-run → Method B 觸發，退回 TRACKING。"""
        trigger_long_tracking(SYM, TS0, base=100.0, gain_pct=20.0)
        peak_ts  = _st()["consolidation_start_ts"]
        ts_ready = int((peak_ts + 17 * 3600) * 1000)
        top      = _st()["consolidation_high"]
        on_new_4h_candle(SYM, (ts_ready, top * 0.99, top * 0.999, top * 0.985, top * 0.995, 1000.0))
        assert _st()["phase"] == StrategyPhase.READY

        old_bottom = _st()["consolidation_low"]
        old_top    = _st()["consolidation_high"]

        ts_base   = ts_ready + _4H_MS
        sub_open  = old_top * 0.85
        assert sub_open > old_bottom
        sub_close = round(sub_open * 1.05, 8)
        sub_high  = round(sub_close * 1.002, 8)
        sub_low   = round(sub_open * 0.998, 8)
        on_new_4h_candle(SYM, (ts_base, sub_open, sub_high, sub_low, sub_close, 1000.0))

        ts2    = ts_base + _4H_MS
        open2  = sub_close
        close2 = round(open2 * 1.042, 8)
        high2  = round(close2 * 1.002, 8)
        on_new_4h_candle(SYM, (ts2, open2, high2, open2 * 0.998, close2, 1000.0))

        ts3 = ts2 + _4H_MS
        on_new_4h_candle(SYM, (ts3, close2, high2 * 0.99, close2 * 0.998, close2 * 1.001, 1000.0))

        st = _st()
        assert st["phase"] == StrategyPhase.TRACKING
        assert st["consolidation_low"] > old_bottom
        assert st["consolidation_low"]  == pytest.approx(sub_low, rel=1e-6)
        assert st["consolidation_high"] == pytest.approx(high2,   rel=1e-6)


# ─── Type 1 訊號 ──────────────────────────────────────────────────────────────

class TestType1Signal:
    def _setup_ready(self):
        trigger_long_tracking(SYM, TS0, base=100.0, gain_pct=9.0)
        st       = _st()
        peak_ts  = st["consolidation_start_ts"]
        ts_ready = int((peak_ts + 17 * 3600) * 1000)
        top      = st["consolidation_high"]
        c = (ts_ready, top * 0.99, top * 0.999, top * 0.985, top * 0.995, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.READY
        setup_symbol_state(SYM, kline_15m_ohlc=make_15m_ohlc_deque(count=200, base_volume=100.0))

    def test_breakout_above_top_with_volume_triggers(self):
        self._setup_ready()
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, top, top * 1.02, top * 0.995, top * 1.015, 500.0)
        result = on_new_15m_candle(SYM, candle)
        assert result is not None
        assert result["type"] == "type1"

    def test_no_signal_when_close_at_or_below_top(self):
        self._setup_ready()
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, top * 0.99, top, top * 0.985, top, 400.0)
        assert on_new_15m_candle(SYM, candle) is None

    def test_no_signal_when_volume_insufficient(self):
        self._setup_ready()
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, top, top * 1.02, top * 0.995, top * 1.015, 150.0)
        assert on_new_15m_candle(SYM, candle) is None

    def test_cooldown_prevents_repeat_signal(self):
        self._setup_ready()
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, top, top * 1.02, top * 0.995, top * 1.015, 500.0)
        r1 = on_new_15m_candle(SYM, candle)
        assert r1 is not None
        candle2 = (ts_candle + 15 * 60 * 1000, top * 1.01, top * 1.03, top, top * 1.02, 500.0)
        assert on_new_15m_candle(SYM, candle2) is None

    def test_no_signal_when_insufficient_history(self):
        """15m 歷史資料不足 193 根 → 不觸發（即使突破且放量）。"""
        self._setup_ready()
        setup_symbol_state(SYM, kline_15m_ohlc=make_15m_ohlc_deque(count=100, base_volume=100.0))
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, top, top * 1.02, top * 0.995, top * 1.015, 400.0)
        assert on_new_15m_candle(SYM, candle) is None

    @staticmethod
    def _make_tail_deque(*tail, total=200, base_volume=100.0):
        """Build a `total`-element 15m deque.

        tail: sequence of (vol, low, high); appended at indices [-len-1 … -2].
        Index -1 is always base (skipped by the scan loop).
        """
        d = deque(maxlen=total)
        base_ts = 1_700_000_000_000
        n_base = total - len(tail) - 1
        for i in range(n_base):
            d.append((base_ts + i * 900_000, 100.0, 101.0, 99.0, 100.5, base_volume))
        for j, (vol, low, high) in enumerate(tail):
            d.append((base_ts + (n_base + j) * 900_000, 100.0, high, low, 100.5, vol))
        d.append((base_ts + (total - 1) * 900_000, 100.0, 101.0, 99.0, 100.5, base_volume))
        return d

    def test_stop_loss_no_prior_high_volume(self):
        """前方無放量根 → stop_loss 等於突破根自身的最低 low。"""
        self._setup_ready()
        top = _st()["consolidation_high"]
        setup_symbol_state(SYM, kline_15m_ohlc=make_15m_ohlc_deque(count=200, base_volume=100.0))
        breakout_low = top * 0.990
        ts_candle    = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, top, top * 1.02, breakout_low, top * 1.015, 500.0)
        result = on_new_15m_candle(SYM, candle)
        assert result is not None
        assert result["stop_loss"] == pytest.approx(breakout_low, rel=1e-6)

    def test_stop_loss_extends_through_consecutive_high_volume_candles(self):
        """往回掃連續放量根 → stop_loss 取到最低 low。"""
        self._setup_ready()
        top = _st()["consolidation_high"]
        # ohlc_list[-4]: vol=100 → chain break（不掃）
        # ohlc_list[-3]: vol=500, low=top*0.978 → 被掃，stop_loss 降至最低
        # ohlc_list[-2]: vol=500, low=top*0.985 → 首先被掃
        d = self._make_tail_deque(
            (100.0, 99.0,        101.0),
            (500.0, top * 0.978, 101.0),
            (500.0, top * 0.985, 101.0),
        )
        setup_symbol_state(SYM, kline_15m_ohlc=d)
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, top, top * 1.02, top * 0.992, top * 1.015, 500.0)
        result = on_new_15m_candle(SYM, candle)
        assert result is not None
        assert result["stop_loss"] == pytest.approx(top * 0.978, rel=1e-6)

    def test_stop_loss_chain_breaks_at_low_volume_candle(self):
        """連續放量鏈中間遇低量根 → 回掃停止，不延伸到更前方的根。"""
        self._setup_ready()
        top = _st()["consolidation_high"]
        # ohlc_list[-4]: vol=500, low=top*0.970 → 未被掃（鏈已斷）
        # ohlc_list[-3]: vol=100, low=top*0.975 → 鏈斷點
        # ohlc_list[-2]: vol=500, low=top*0.985 → 首先被掃
        d = self._make_tail_deque(
            (500.0, top * 0.970, 101.0),
            (100.0, top * 0.975, 101.0),
            (500.0, top * 0.985, 101.0),
        )
        setup_symbol_state(SYM, kline_15m_ohlc=d)
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, top, top * 1.02, top * 0.992, top * 1.015, 500.0)
        result = on_new_15m_candle(SYM, candle)
        assert result is not None
        assert result["stop_loss"] == pytest.approx(top * 0.985, rel=1e-6)


# ─── 即時廢棄掃描 ─────────────────────────────────────────────────────────────

class TestRealtimeInvalidation:
    def test_price_below_bottom_resets_long_to_idle(self):
        """即時價格跌破多頭底部 → 多頭廢棄為 IDLE。"""
        trigger_long_tracking(SYM, TS0)
        bottom = _st()["consolidation_low"]
        setup_symbol_state(SYM, last_price=bottom * 0.99)
        assert check_invalidation_realtime(SYM) is True
        assert _st()["phase"] == StrategyPhase.IDLE

    def test_price_equal_to_bottom_not_invalidated(self):
        """即時價格等於多頭底部 → 邊界不廢棄。"""
        trigger_long_tracking(SYM, TS0)
        bottom = _st()["consolidation_low"]
        setup_symbol_state(SYM, last_price=bottom)
        assert check_invalidation_realtime(SYM) is False
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_returns_false_when_no_price(self):
        """無法取得即時價格（symbol_state 未設定）→ 不廢棄，回傳 False。"""
        trigger_long_tracking(SYM, TS0)
        # symbol_state 未設定，last_price = None
        assert check_invalidation_realtime(SYM) is False
        assert _st()["phase"] == StrategyPhase.TRACKING


# ─── 歷史回播 ─────────────────────────────────────────────────────────────────

class TestReplay:
    def test_replay_restores_tracking_state(self):
        """回播含達標 run 的 4h 歷史 → 多頭恢復 TRACKING。"""
        # 20 根低量陰線基準 K（bearish → 不啟動 run，volume=300）
        # run candle 使用高量(2000)，確保 run 均量 >= baseline(≈300) × 1.5 通過
        base_ts = TS0 - 20 * _4H_MS
        baseline_candles = [
            (base_ts + i * _4H_MS, 100.0, 100.2, 99.0, 99.5, 300.0)
            for i in range(20)
        ]
        candles, _ = make_long_run(TS0, [9.0], volume=2000.0)
        open2 = candles[-1][4]
        c2 = (TS0 + _4H_MS, open2, candles[-1][2] * 0.999, open2 * 0.998, open2 * 1.001, 1000.0)
        all_candles = baseline_candles + candles + [c2]

        setup_symbol_state(SYM)
        models.symbol_state[SYM]["kline_4h_ohlc"] = deque(all_candles, maxlen=50)
        models.strategy_state.pop(SYM, None)
        replay_historical_4h_candles(SYM)

        assert models.strategy_state.get(SYM, {}).get("phase") == StrategyPhase.TRACKING

    def test_replay_empty_history_does_not_crash(self):
        """空歷史回播不崩潰，狀態為 IDLE。"""
        setup_symbol_state(SYM)
        replay_historical_4h_candles(SYM)
        assert models.strategy_state.get(SYM, {}).get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

    def test_replay_resets_previous_state(self):
        """回播不足觸發門檻的歷史 → 之前的 TRACKING 狀態被清除，回到 IDLE。"""
        trigger_long_tracking(SYM, TS0)
        assert _st()["phase"] == StrategyPhase.TRACKING

        candles, _ = make_long_run(TS0, [5.0])   # 只有一根 5%，不足以進 TRACKING
        setup_symbol_state(SYM)
        models.symbol_state[SYM]["kline_4h_ohlc"] = deque(candles, maxlen=50)
        replay_historical_4h_candles(SYM)

        assert models.strategy_state.get(SYM, {}).get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

