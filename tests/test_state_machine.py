"""Type 1 多頭狀態機測試（累積漲幅偵測 + Method B）"""

import time
import pytest
from collections import deque
from app.setting import models
from app.strategy.state_machine import (
    StrategyPhase,
    on_new_4h_candle,
    on_new_4h_candle_short,
    on_new_15m_candle,
    on_new_15m_candle_short,
    on_new_1h_candle,
    check_invalidation_realtime,
    replay_historical_4h_candles,
)
from tests.conftest import (
    make_flat_candle,
    make_15m_ohlc_deque,
    make_1h_candle,
    setup_symbol_state,
    trigger_long_tracking,
    trigger_short_tracking,
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
        c3 = (ts2, open3, high3, open3 * 0.998, open3 * 1.001)
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
        c3 = (ts2, open3, high3, open3 * 0.998, open3 * 0.999)
        on_new_4h_candle(SYM, c3)
        assert _st().get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

    def test_bearish_candle_resets_run_below_threshold(self):
        """累積 5% 時出現陰線 → run 重置，不進 TRACKING。"""
        candles, _ = make_long_run(TS0, [5.0])
        on_new_4h_candle(SYM, candles[0])
        ts1   = TS0 + _4H_MS
        close1 = candles[0][4]
        bear  = (ts1, close1, close1 * 1.001, close1 * 0.995, close1 * 0.998)
        on_new_4h_candle(SYM, bear)
        assert _st().get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

    def test_state_fields_after_tracking_entry(self):
        """進入 TRACKING 後，底部 = run 第一根的 low，頂部 = run 的最高 high。"""
        candles, _ = make_long_run(TS0, [5.0, 4.0])
        for c in candles:
            on_new_4h_candle(SYM, c)
        ts2   = TS0 + 2 * _4H_MS
        open3 = candles[-1][4]
        c3 = (ts2, open3, candles[-1][2] * 0.99, open3 * 0.998, open3 * 1.001)
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
        bear = (ts1, c0[4], c0[4] * 1.001, c0[4] * 0.995, c0[4] * 0.997)
        on_new_4h_candle(SYM, bear)

        # 新 run：兩根陽線累積 10%
        ts2    = ts1 + _4H_MS
        open2  = bear[4]
        close2 = round(open2 * 1.05, 8)
        high2  = round(close2 * 1.002, 8)
        low2   = round(open2 * 0.998, 8)
        c2 = (ts2, open2, high2, low2, close2)
        on_new_4h_candle(SYM, c2)

        ts3    = ts2 + _4H_MS
        open3  = close2
        close3 = round(open3 * 1.05, 8)
        high3  = round(close3 * 1.002, 8)
        c3 = (ts3, open3, high3, open3 * 0.998, close3)
        on_new_4h_candle(SYM, c3)

        # 第四根不創新高 → TRACKING，底部應是新 run 的 low2
        ts4   = ts3 + _4H_MS
        c4 = (ts4, close3, high3 * 0.99, close3 * 0.998, close3 * 1.001)
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
        c = (ts_next, prev_high, new_high, prev_high * 0.99, new_high * 0.998)
        on_new_4h_candle(SYM, c)
        assert _st()["consolidation_high"]     == pytest.approx(new_high, rel=1e-6)
        assert _st()["consolidation_start_ts"] == pytest.approx(ts_next / 1000, rel=1e-6)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_no_new_high_does_not_update_top(self):
        trigger_long_tracking(SYM, TS0, gain_pct=9.0)
        prev_high = _st()["consolidation_high"]
        ts_next   = TS0 + 2 * _4H_MS
        c = (ts_next, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995)
        on_new_4h_candle(SYM, c)
        assert _st()["consolidation_high"] == pytest.approx(prev_high, rel=1e-6)

    def test_tracking_to_ready_after_min_hours(self):
        trigger_long_tracking(SYM, TS0, gain_pct=9.0)
        peak_ts  = _st()["consolidation_start_ts"]
        ts_ready = int((peak_ts + 13 * 3600) * 1000)
        prev_high = _st()["consolidation_high"]
        c = (ts_ready, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.READY

    def test_new_high_in_ready_resets_to_tracking(self):
        trigger_long_tracking(SYM, TS0, gain_pct=9.0)
        peak_ts   = _st()["consolidation_start_ts"]
        prev_high = _st()["consolidation_high"]
        ts_ready  = int((peak_ts + 13 * 3600) * 1000)
        c_flat = (ts_ready, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995)
        on_new_4h_candle(SYM, c_flat)
        assert _st()["phase"] == StrategyPhase.READY

        ts_ext   = ts_ready + _4H_MS
        new_high = prev_high * 1.03
        c_ext = (ts_ext, prev_high, new_high, prev_high * 0.99, new_high * 0.998)
        on_new_4h_candle(SYM, c_ext)
        assert _st()["phase"] == StrategyPhase.TRACKING
        assert _st()["consolidation_high"] == pytest.approx(new_high, rel=1e-6)


# ─── 廢棄條件 ─────────────────────────────────────────────────────────────────

class TestInvalidation:
    def test_low_below_bottom_resets_to_idle(self):
        trigger_long_tracking(SYM, TS0, gain_pct=9.0)
        bottom  = _st()["consolidation_low"]
        ts_next = TS0 + 2 * _4H_MS
        c = (ts_next, bottom * 0.99, bottom * 1.01, bottom * 0.98, bottom * 0.985)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.IDLE

    def test_low_equal_to_bottom_is_not_invalidated(self):
        trigger_long_tracking(SYM, TS0, gain_pct=9.0)
        bottom  = _st()["consolidation_low"]
        ts_next = TS0 + 2 * _4H_MS
        c = (ts_next, bottom * 1.01, bottom * 1.02, bottom, bottom * 1.015)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] != StrategyPhase.IDLE


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
        c1 = (ts_base, sub_open, sub_high, sub_low, sub_close)
        on_new_4h_candle(SYM, c1)

        ts2    = ts_base + _4H_MS
        open2  = sub_close
        close2 = round(open2 * 1.042, 8)
        high2  = round(close2 * 1.002, 8)
        c2 = (ts2, open2, high2, open2 * 0.998, close2)
        on_new_4h_candle(SYM, c2)

        # 第三根不創新高 → sub-run 結束 → Method B
        ts3   = ts2 + _4H_MS
        open3 = close2
        c3 = (ts3, open3, high2 * 0.99, open3 * 0.998, open3 * 1.001)
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
        c1 = (ts_base, sub_open, sub_high, sub_low, sub_close)
        on_new_4h_candle(SYM, c1)

        ts2    = ts_base + _4H_MS
        open2  = sub_close
        close2 = round(open2 * 1.045, 8)
        high2  = round(close2 * 1.002, 8)
        c2 = (ts2, open2, high2, open2 * 0.998, close2)
        on_new_4h_candle(SYM, c2)

        ts3   = ts2 + _4H_MS
        c3 = (ts3, close2, high2 * 0.99, close2 * 0.998, close2 * 1.001)
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
        c = (ts_next, old_top * 0.99, new_high, old_top * 0.985, new_high * 0.998)
        on_new_4h_candle(SYM, c)

        assert _st()["consolidation_high"] == pytest.approx(new_high, rel=1e-6)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_method_b_in_ready_phase(self):
        """READY 階段出現底部更高的 8% sub-run → Method B 觸發，退回 TRACKING。"""
        trigger_long_tracking(SYM, TS0, base=100.0, gain_pct=20.0)
        peak_ts  = _st()["consolidation_start_ts"]
        ts_ready = int((peak_ts + 13 * 3600) * 1000)
        top      = _st()["consolidation_high"]
        on_new_4h_candle(SYM, (ts_ready, top * 0.99, top * 0.999, top * 0.985, top * 0.995))
        assert _st()["phase"] == StrategyPhase.READY

        old_bottom = _st()["consolidation_low"]
        old_top    = _st()["consolidation_high"]

        ts_base   = ts_ready + _4H_MS
        sub_open  = old_top * 0.85
        assert sub_open > old_bottom
        sub_close = round(sub_open * 1.05, 8)
        sub_high  = round(sub_close * 1.002, 8)
        sub_low   = round(sub_open * 0.998, 8)
        on_new_4h_candle(SYM, (ts_base, sub_open, sub_high, sub_low, sub_close))

        ts2    = ts_base + _4H_MS
        open2  = sub_close
        close2 = round(open2 * 1.042, 8)
        high2  = round(close2 * 1.002, 8)
        on_new_4h_candle(SYM, (ts2, open2, high2, open2 * 0.998, close2))

        ts3 = ts2 + _4H_MS
        on_new_4h_candle(SYM, (ts3, close2, high2 * 0.99, close2 * 0.998, close2 * 1.001))

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
        ts_ready = int((peak_ts + 13 * 3600) * 1000)
        top      = st["consolidation_high"]
        c = (ts_ready, top * 0.99, top * 0.999, top * 0.985, top * 0.995)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.READY
        setup_symbol_state(SYM, kline_15m_ohlc=make_15m_ohlc_deque(count=200, base_volume=100.0))

    def test_breakout_above_top_with_volume_triggers(self):
        self._setup_ready()
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, top, top * 1.02, top * 0.995, top * 1.015, 400.0)
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
        candle = (ts_candle, top, top * 1.02, top * 0.995, top * 1.015, 400.0)
        r1 = on_new_15m_candle(SYM, candle)
        assert r1 is not None
        candle2 = (ts_candle + 15 * 60 * 1000, top * 1.01, top * 1.03, top, top * 1.02, 400.0)
        assert on_new_15m_candle(SYM, candle2) is None

    def test_no_signal_when_insufficient_history(self):
        """15m 歷史資料不足 193 根 → 不觸發（即使突破且放量）。"""
        self._setup_ready()
        setup_symbol_state(SYM, kline_15m_ohlc=make_15m_ohlc_deque(count=100, base_volume=100.0))
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, top, top * 1.02, top * 0.995, top * 1.015, 400.0)
        assert on_new_15m_candle(SYM, candle) is None


# ─── Type 2 訊號（EMA 反彈）─────────────────────────────────────────────────

class TestType2Signal:
    # EMA 設低讓 close 能在 (bottom, midpoint) 之間，確保 RR >= 1
    EMA_VAL = 101.0

    def _setup_ready_with_ema(self):
        trigger_long_tracking(SYM, TS0, base=100.0, gain_pct=9.0)
        st       = _st()
        peak_ts  = st["consolidation_start_ts"]
        ts_ready = int((peak_ts + 13 * 3600) * 1000)
        top      = st["consolidation_high"]
        c = (ts_ready, top * 0.99, top * 0.999, top * 0.985, top * 0.995)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.READY
        setup_symbol_state(
            SYM,
            ema_4h={15: self.EMA_VAL},
            last_price=110.0,
            kline_15m_ohlc=make_15m_ohlc_deque(),
        )

    def test_ema_touch_with_wick_and_rr_triggers(self):
        self._setup_ready_with_ema()
        st     = _st()
        bottom = st["consolidation_low"]
        top    = st["consolidation_high"]
        ema    = self.EMA_VAL

        # low 觸碰 EMA，close 高於 low 2.4%（滿足 WICK_THRESHOLD=2%），且 RR > 1
        low   = ema * 0.999
        open_ = ema + 0.1          # open > EMA（多頭確認）
        close = low * 1.024        # wick 2.4% > 2% 門檻，且 close 夠低讓 RR > 1
        assert (top - close) / (close - bottom) >= 1.0, "RR 不足，請調整 EMA_VAL"

        ts     = TS0 + 14 * 3600 * 1000
        candle = make_1h_candle(ts, open_, top * 0.99, low, close)
        result = on_new_1h_candle(SYM, candle)
        assert result is not None
        assert result["type"] == "type2"

    def test_no_signal_when_open_below_ema(self):
        self._setup_ready_with_ema()
        ema    = self.EMA_VAL
        low    = ema * 0.999
        open_  = ema - 0.1
        close  = ema + 2.0
        ts     = TS0 + 14 * 3600 * 1000
        candle = make_1h_candle(ts, open_, close * 1.01, low, close)
        assert on_new_1h_candle(SYM, candle) is None

    def test_no_signal_when_wick_insufficient(self):
        self._setup_ready_with_ema()
        ema    = self.EMA_VAL
        low    = ema * 0.999
        open_  = ema + 0.1
        close  = low * 1.005   # 收針僅 0.5%，不足 2%
        ts     = TS0 + 14 * 3600 * 1000
        candle = make_1h_candle(ts, open_, close * 1.005, low, close)
        assert on_new_1h_candle(SYM, candle) is None

    def test_no_signal_when_rr_insufficient(self):
        """close 太高導致 RR < 1.0 → 不觸發。"""
        self._setup_ready_with_ema()
        st    = _st()
        top   = st["consolidation_high"]
        ema   = self.EMA_VAL
        low   = ema * 0.999
        open_ = ema + 0.1
        close = top * 0.998   # close 幾乎等於頂部 → RR << 1
        ts    = TS0 + 14 * 3600 * 1000
        assert on_new_1h_candle(SYM, make_1h_candle(ts, open_, top * 0.99, low, close)) is None

    def test_1h_low_below_bottom_resets_to_idle(self):
        """1h K low 跌破 consolidation_low → 廢棄，回到 IDLE。"""
        self._setup_ready_with_ema()
        bottom = _st()["consolidation_low"]
        low    = bottom * 0.99
        open_  = bottom * 1.01
        close  = bottom * 1.005
        ts     = TS0 + 14 * 3600 * 1000
        result = on_new_1h_candle(SYM, make_1h_candle(ts, open_, close * 1.01, low, close))
        assert result is None
        assert _st()["phase"] == StrategyPhase.IDLE

    def test_no_signal_when_no_ema_touched(self):
        """1h K low 未觸碰任何 EMA → 不觸發。"""
        self._setup_ready_with_ema()
        setup_symbol_state(SYM, ema_4h={15: 50.0})   # EMA 遠低於 low，不觸碰
        low   = 103.0
        open_ = 104.0
        close = 105.0
        ts    = TS0 + 14 * 3600 * 1000
        assert on_new_1h_candle(SYM, make_1h_candle(ts, open_, 106.0, low, close)) is None


# ─── 即時廢棄掃描 ─────────────────────────────────────────────────────────────

class TestRealtimeInvalidation:
    def test_price_below_bottom_resets_long_to_idle(self):
        """即時價格跌破多頭底部 → 多頭廢棄為 IDLE。"""
        trigger_long_tracking(SYM, TS0)
        bottom = _st()["consolidation_low"]
        setup_symbol_state(SYM, last_price=bottom * 0.99)
        assert check_invalidation_realtime(SYM) is True
        assert _st()["phase"] == StrategyPhase.IDLE

    def test_price_above_top_resets_short_to_idle(self):
        """即時價格突破空頭頂部 → 空頭廢棄為 IDLE。"""
        trigger_short_tracking(SYM, TS0)
        top = models.strategy_state_short[SYM]["consolidation_high"]
        setup_symbol_state(SYM, last_price=top * 1.01)
        assert check_invalidation_realtime(SYM) is True
        assert models.strategy_state_short[SYM]["phase"] == StrategyPhase.IDLE

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
        candles, _ = make_long_run(TS0, [9.0])
        open2 = candles[-1][4]
        c2 = (TS0 + _4H_MS, open2, candles[-1][2] * 0.999, open2 * 0.998, open2 * 1.001)
        all_candles = candles + [c2]

        setup_symbol_state(SYM)
        models.symbol_state[SYM]["kline_4h_ohlc"] = deque(all_candles, maxlen=50)
        models.strategy_state.pop(SYM, None)
        models.strategy_state_short.pop(SYM, None)
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


# ─── 冷卻時間隔離 ─────────────────────────────────────────────────────────────

class TestCooldownIsolation:
    def test_long_and_short_cooldowns_are_independent(self):
        """多頭進入冷卻後，空頭訊號仍可正常觸發（冷卻互不影響）。"""
        # 讓多頭進入冷卻狀態
        trigger_long_tracking(SYM, TS0, gain_pct=9.0)
        models.strategy_state[SYM]["last_alert_ts"] = time.time()

        # 建立空頭 READY
        trigger_short_tracking(SYM, TS0 + 100 * _4H_MS, drop_pct=9.0)
        st_short  = models.strategy_state_short[SYM]
        peak_ts_s = st_short["consolidation_start_ts"]
        ts_ready  = int((peak_ts_s + 13 * 3600) * 1000)
        bottom    = st_short["consolidation_low"]
        on_new_4h_candle_short(
            SYM, (ts_ready, bottom * 1.01, bottom * 1.015, bottom * 1.001, bottom * 1.005)
        )
        assert models.strategy_state_short[SYM]["phase"] == StrategyPhase.READY
        setup_symbol_state(SYM, kline_15m_ohlc=make_15m_ohlc_deque(count=200, base_volume=100.0))

        # 空頭訊號應觸發（不受多頭冷卻影響）
        bottom    = models.strategy_state_short[SYM]["consolidation_low"]
        ts_candle = ts_ready + _4H_MS
        candle    = (ts_candle, bottom, bottom * 1.005, bottom * 0.985, bottom * 0.99, 400.0)
        result    = on_new_15m_candle_short(SYM, candle)
        assert result is not None
        assert result["type"] == "type1_short"
