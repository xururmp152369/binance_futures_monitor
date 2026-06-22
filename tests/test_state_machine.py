"""多頭狀態機測試（單根帶量 K 棒觸發 + Method B + Type 1 訊號）"""

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
    make_4h_candle,
    make_15m_ohlc_deque,
    make_trigger_candle,
    make_tight_breakout_candle,
    setup_symbol_state,
    setup_with_baseline,
    trigger_long_tracking,
    _4H_MS,
)

SYM = "BTCUSDT"
TS0 = 1_000_000_000_000   # 測試用基準時間戳（ms）


def _st() -> dict:
    return models.strategy_state.get(SYM, {})


# ─── 觸發偵測 ──────────────────────────────────────────────────────────────────

class TestTriggerDetection:
    """單根 4h 帶量陽線觸發 IDLE → TRACKING。"""

    def test_single_candle_triggers_immediately(self):
        """符合條件的單根 K 棒立即進入 TRACKING（不需等第二根）。"""
        trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_gain_below_threshold_does_not_trigger(self):
        """漲幅不足 PUMP_THRESHOLD(3%) → 不觸發。"""
        setup_with_baseline(SYM, baseline_volume=100.0)
        c = make_trigger_candle(TS0, gain_pct=2.0, volume=400.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _st().get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

    def test_gain_at_threshold_triggers(self):
        """漲幅剛好 == PUMP_THRESHOLD(3%) → 觸發。"""
        setup_with_baseline(SYM, baseline_volume=100.0)
        c = make_trigger_candle(TS0, gain_pct=3.0, volume=400.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_volume_below_threshold_does_not_trigger(self):
        """量能不足（< baseline × TRIGGER_VOLUME_MULT(3)）→ 不觸發。"""
        setup_with_baseline(SYM, baseline_volume=100.0)
        # baseline avg = 100, threshold = 300, volume = 250 < 300
        c = make_trigger_candle(TS0, gain_pct=4.0, volume=250.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _st().get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

    def test_volume_at_threshold_does_not_trigger(self):
        """量能剛好等於 baseline × 3（不超過）→ 不觸發（需嚴格大於）。"""
        setup_with_baseline(SYM, baseline_volume=100.0)
        c = make_trigger_candle(TS0, gain_pct=4.0, volume=300.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _st().get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

    def test_bearish_candle_does_not_trigger(self):
        """陰線（close < open）→ 不觸發。"""
        setup_with_baseline(SYM, baseline_volume=100.0)
        open_ = 100.0
        close = 96.0   # -4%，陰線
        high  = 100.5
        low   = 95.5
        c = make_4h_candle(TS0, open_, high, low, close, 400.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _st().get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

    def test_empty_baseline_does_not_trigger(self):
        """kline_4h_ohlc 無基準 K（baseline=None）→ 量能條件失敗，不觸發。"""
        setup_symbol_state(SYM)   # kline_4h_ohlc 保持空
        c = make_trigger_candle(TS0, gain_pct=5.0, volume=9999.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _st().get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

    def test_insufficient_baseline_candles_does_not_trigger(self):
        """baseline 只有 5 根（< TRIGGER_VOLUME_BASELINE_N=12）→ 取全部計算，量能仍足夠則觸發。"""
        setup_with_baseline(SYM, baseline_volume=100.0, n=5)
        c = make_trigger_candle(TS0, gain_pct=5.0, volume=400.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_state_fields_after_tracking_entry(self):
        """進入 TRACKING 後：consolidation_low / high 由觸發 K 決定。"""
        bottom, top, ts_start = trigger_long_tracking(SYM, TS0, base=100.0, gain_pct=4.0)
        st = _st()
        assert st["phase"] == StrategyPhase.TRACKING
        # make_trigger_candle with gain=4%: body=4, high=close+body*0.1=104+0.4=104.4, low=100-0.4=99.6
        expected_low  = round(100.0 - 4.0 * 0.1, 8)   # 99.6
        expected_high = round(104.0 + 4.0 * 0.1, 8)   # 104.4
        assert bottom == pytest.approx(expected_low,  rel=1e-6)
        assert top    == pytest.approx(expected_high, rel=1e-6)
        assert st["pump_candle_open"]  == pytest.approx(100.0, rel=1e-6)
        assert st["pump_candle_close"] == pytest.approx(104.0, rel=1e-6)


# ─── 盤整追蹤 ─────────────────────────────────────────────────────────────────

class TestConsolidation:
    """TRACKING 狀態下的盤整追蹤行為。"""

    def test_new_high_extends_top_and_resets_timer(self):
        """TRACKING 時創新高 → 頂部更新、計時重置、維持 TRACKING。"""
        _, prev_high, _ = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_next  = TS0 + _4H_MS
        new_high = prev_high * 1.05
        c = make_4h_candle(ts_next, prev_high, new_high, prev_high * 0.99, new_high * 0.998, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["consolidation_high"]     == pytest.approx(new_high, rel=1e-6)
        assert _st()["consolidation_start_ts"] == pytest.approx(ts_next / 1000, rel=1e-6)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_no_new_high_does_not_update_top(self):
        """未創新高 → 頂部不變。"""
        _, prev_high, _ = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_next = TS0 + _4H_MS
        c = make_4h_candle(ts_next, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["consolidation_high"] == pytest.approx(prev_high, rel=1e-6)

    def test_tracking_to_ready_after_consolidation_min_hours(self):
        """盤整 >= CONSOLIDATION_MIN_HOURS(12h) → 進入 READY。"""
        _, prev_high, ts_start = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_ready = int((ts_start + 13 * 3600) * 1000)   # 13h > 12h
        c = make_4h_candle(ts_ready, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.READY

    def test_before_consolidation_min_hours_remains_tracking(self):
        """盤整時數未達 12h → 維持 TRACKING。"""
        _, prev_high, ts_start = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_early = int((ts_start + 11 * 3600) * 1000)   # 11h < 12h
        c = make_4h_candle(ts_early, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_new_high_in_ready_reverts_to_tracking(self):
        """READY 時創新高 → 退回 TRACKING，更新頂部。"""
        _, prev_high, ts_start = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_ready = int((ts_start + 13 * 3600) * 1000)
        on_new_4h_candle(SYM, make_4h_candle(ts_ready, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0))
        assert _st()["phase"] == StrategyPhase.READY

        ts_ext   = ts_ready + _4H_MS
        new_high = prev_high * 1.03
        c_ext = make_4h_candle(ts_ext, prev_high, new_high, prev_high * 0.99, new_high * 0.998, 1000.0)
        on_new_4h_candle(SYM, c_ext)
        assert _st()["phase"] == StrategyPhase.TRACKING
        assert _st()["consolidation_high"] == pytest.approx(new_high, rel=1e-6)

    def test_consolidation_start_ts_is_trigger_candle_time(self):
        """進入 TRACKING 後，consolidation_start_ts = 觸發 K 棒的 open_time。"""
        _, _, ts_start = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        assert ts_start == pytest.approx(TS0 / 1000, rel=1e-9)


# ─── 廢棄條件 ─────────────────────────────────────────────────────────────────

class TestInvalidation:
    """4h K 棒實體廢棄邏輯（用 min(open,close) 而非 low）。"""

    def test_body_low_below_bottom_invalidates(self):
        """實體低點 min(open,close) < 底部 → 廢棄至 IDLE。"""
        bottom, _, _ = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_next = TS0 + _4H_MS
        open_ = bottom * 1.02
        close = bottom * 0.98
        c = make_4h_candle(ts_next, open_, open_ * 1.01, bottom * 0.97, close, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.IDLE

    def test_shadow_below_bottom_does_not_invalidate(self):
        """下影線低於底部，但實體低點 >= 底部 → 不廢棄。"""
        bottom, _, _ = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_next = TS0 + _4H_MS
        open_ = bottom * 1.02
        close = bottom * 1.01
        low   = bottom * 0.99
        high  = bottom * 1.05
        c = make_4h_candle(ts_next, open_, high, low, close, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] != StrategyPhase.IDLE

    def test_body_low_equal_to_bottom_does_not_invalidate(self):
        """實體低點 == 底部（邊界值）→ 不廢棄。"""
        bottom, _, _ = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_next = TS0 + _4H_MS
        open_ = bottom * 1.02
        close = bottom
        c = make_4h_candle(ts_next, open_, open_ * 1.01, bottom * 0.995, close, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] != StrategyPhase.IDLE

    def test_ready_phase_invalidated(self):
        """READY 時 4h K 實體跌破底部 → 廢棄至 IDLE。"""
        bottom, prev_high, ts_start = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_ready = int((ts_start + 13 * 3600) * 1000)
        on_new_4h_candle(SYM, make_4h_candle(ts_ready, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0))
        assert _st()["phase"] == StrategyPhase.READY

        ts_next = ts_ready + _4H_MS
        open_ = bottom * 1.01
        close = bottom * 0.98
        c = make_4h_candle(ts_next, open_, open_ * 1.01, bottom * 0.97, close, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.IDLE

    def test_invalidation_clears_all_fields(self):
        """廢棄後，狀態重置（consolidation_low/high = None）。"""
        bottom, _, _ = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_next = TS0 + _4H_MS
        open_ = bottom * 1.01
        close = bottom * 0.98
        c = make_4h_candle(ts_next, open_, open_ * 1.01, bottom * 0.97, close, 1000.0)
        on_new_4h_candle(SYM, c)
        st = _st()
        assert st["phase"] == StrategyPhase.IDLE
        assert st["consolidation_low"]  is None
        assert st["consolidation_high"] is None


# ─── Method B ─────────────────────────────────────────────────────────────────

class TestMethodB:
    """盤整期間出現更強觸發 K → Method B 重置（底部上移，頂部保留最大值）。"""

    def _append_and_feed(self, ts, open_, high, low, close, vol, taker_vol=None):
        """helper：先 append deque 再呼叫 on_new_4h_candle（7 欄位）。"""
        taker = taker_vol if taker_vol is not None else vol * 0.7
        c = (ts, open_, high, low, close, vol, taker)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        return c

    def test_stronger_candle_triggers_method_b(self):
        """READY 中出現漲幅 >= 前觸發 K × 0.8 且帶量的 K → Method B，底部上移至新 low。"""
        trigger_long_tracking(SYM, TS0, base=100.0, gain_pct=4.0)
        old_bottom = _st()["consolidation_low"]   # ≈ 99.6
        ts_start   = _st()["consolidation_start_ts"]

        # 先延伸頂部到 150
        ts_ext = TS0 + _4H_MS
        ext_c  = make_4h_candle(ts_ext, 105.0, 150.0, 104.0, 149.8, 50.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(ext_c)
        on_new_4h_candle(SYM, ext_c)

        # 延伸後等 13h → READY
        ts_ready = ts_ext + int(13 * 3600 * 1000)
        ready_c  = make_4h_candle(ts_ready, 149.0, 149.5, 148.5, 149.0, 50.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(ready_c)
        on_new_4h_candle(SYM, ready_c)
        assert _st()["phase"] == StrategyPhase.READY

        # Method B 觸發 K：gain=6% >= 4%×0.8=3.2%，high2 < 150
        ts2    = ts_ready + _4H_MS
        open2  = 101.0
        close2 = round(open2 * 1.06, 8)
        body2  = close2 - open2
        high2  = round(close2 + body2 * 0.1, 8)
        low2   = round(open2 - body2 * 0.1, 8)
        assert high2 < 150.0
        assert low2 != pytest.approx(old_bottom, rel=1e-3)

        self._append_and_feed(ts2, open2, high2, low2, close2, 1000.0)

        st = _st()
        assert st["phase"] == StrategyPhase.TRACKING
        assert st["consolidation_low"] == pytest.approx(low2, rel=1e-6)

    def test_method_b_triggered_in_tracking_phase(self):
        """TRACKING 時出現 gain >= prev_gain × 0.8 且帶量 K → Method B 觸發，底部上移。"""
        trigger_long_tracking(SYM, TS0, base=100.0, gain_pct=4.0)
        old_bottom = _st()["consolidation_low"]
        assert _st()["phase"] == StrategyPhase.TRACKING

        # 先延伸頂部，確保後續 K 不走延伸邏輯
        ts_ext = TS0 + _4H_MS
        ext_c  = make_4h_candle(ts_ext, 105.0, 150.0, 104.0, 149.8, 50.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(ext_c)
        on_new_4h_candle(SYM, ext_c)

        # 出現 6% 強觸發 K（6% >= 4%×0.8=3.2%）在 TRACKING → Method B 觸發
        ts2    = TS0 + 2 * _4H_MS
        open2  = 101.0
        close2 = round(open2 * 1.06, 8)
        body2  = close2 - open2
        high2  = round(close2 + body2 * 0.1, 8)  # < 150，不觸延伸
        low2   = round(open2 - body2 * 0.1, 8)
        assert low2 != pytest.approx(old_bottom, rel=1e-3)
        self._append_and_feed(ts2, open2, high2, low2, close2, 1000.0)

        # Method B 應觸發，底部更新至 low2
        assert _st()["consolidation_low"] == pytest.approx(low2, rel=1e-6)

    def test_method_b_gain_relaxed_threshold_triggers(self):
        """gain = prev_gain × 0.8（最低觸發門檻）→ Method B 觸發。"""
        trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        old_bottom = _st()["consolidation_low"]
        # gain = 4% × 0.8 = 3.2% → 剛好觸發 Method B
        ts2   = TS0 + _4H_MS
        open2 = 100.0
        prev_gain = 4.0
        trigger_gain = prev_gain * 0.8   # 3.2%
        close2 = round(open2 * (1 + trigger_gain / 100), 8)
        body2  = close2 - open2
        high2  = round(close2 + body2 * 0.1, 8)
        low2   = round(open2 - body2 * 0.1, 8)
        self._append_and_feed(ts2, open2, high2, low2, close2, 400.0)
        # 3.2% >= 3.2% → 觸發，底部更新
        assert _st()["consolidation_low"] == pytest.approx(low2, rel=1e-6)

    def test_method_b_gain_below_relaxed_threshold_no_trigger(self):
        """gain < prev_gain × 0.8 → Method B 不觸發。"""
        trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        old_bottom = _st()["consolidation_low"]
        # gain = 3.1% < 4% × 0.8 = 3.2% → 不觸發
        ts2   = TS0 + _4H_MS
        open2 = 100.0
        close2 = round(open2 * 1.031, 8)
        body2  = close2 - open2
        high2  = round(close2 + body2 * 0.1, 8)
        low2   = round(open2 - body2 * 0.1, 8)
        self._append_and_feed(ts2, open2, high2, low2, close2, 400.0)
        assert _st()["consolidation_low"] == pytest.approx(old_bottom, rel=1e-6)

    def test_method_b_preserves_higher_consolidation_high(self):
        """Method B 後，consolidation_high 保留歷史最大值（不因 Method B 而下移）。"""
        _, old_top, ts_start = trigger_long_tracking(SYM, TS0, gain_pct=4.0)

        # 先讓頂部延伸到更高（5% 延伸，gate < 10% 確保 Method C 不觸發）
        ts_ext  = TS0 + _4H_MS
        ext_top = old_top * 1.05
        self._append_and_feed(ts_ext, old_top, ext_top, old_top * 0.99, ext_top * 0.998, 1000.0)
        assert _st()["consolidation_high"] == pytest.approx(ext_top, rel=1e-6)

        # Method B 觸發 K（漲幅 6% >= 4%×0.8=3.2%），但 high2 < ext_top
        # vol=700 確保 vol_ratio=3.5×>3 通過觸發量能門檻
        ts3    = TS0 + 2 * _4H_MS
        open3  = 100.0
        close3 = round(open3 * 1.06, 8)
        body3  = close3 - open3
        high3  = round(close3 + body3 * 0.1, 8)
        low3   = round(open3 - body3 * 0.1, 8)
        assert high3 < ext_top   # 確認不觸發整體延伸
        self._append_and_feed(ts3, open3, high3, low3, close3, 700.0)

        # consolidation_high 應保留 ext_top（最大值）
        assert _st()["consolidation_high"] == pytest.approx(ext_top, rel=1e-6)
        # consolidation_low 應更新為 Method B 的新底部
        assert _st()["consolidation_low"] == pytest.approx(low3, rel=1e-6)

    def test_method_b_relaxed_threshold_complete_reset(self):
        """原觸發 K 漲幅 >= METHOD_B_RELAXED_THRESHOLD(10%) → 任何符合觸發條件的 K 完整重置。"""
        trigger_long_tracking(SYM, TS0, base=100.0, gain_pct=11.0)
        ts_start = _st()["consolidation_start_ts"]

        # 等 13h → READY
        ts_ready = TS0 + int(13 * 3600 * 1000)
        old_high = _st()["consolidation_high"]
        self._append_and_feed(ts_ready, old_high * 0.99, old_high * 0.999, old_high * 0.985, old_high * 0.995, 50.0)
        assert _st()["phase"] == StrategyPhase.READY

        # 延伸頂部到 150，讓後續 Relaxed K 不觸延伸邏輯
        ts_ext = ts_ready + _4H_MS
        ext_c  = make_4h_candle(ts_ext, 112.0, 150.0, 111.0, 149.8, 50.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(ext_c)
        on_new_4h_candle(SYM, ext_c)

        # 再等 13h → 回到 READY
        ts_ready2 = ts_ext + int(13 * 3600 * 1000)
        ready2_c  = make_4h_candle(ts_ready2, 149.0, 149.5, 148.5, 149.0, 50.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(ready2_c)
        on_new_4h_candle(SYM, ready2_c)
        assert _st()["phase"] == StrategyPhase.READY
        assert _st()["consolidation_high"] == pytest.approx(150.0, rel=1e-6)

        # Relaxed 重置 K：gain=4%（< 11%）但 prev_gain=11% >= threshold → 直接完整重置
        ts2    = ts_ready2 + _4H_MS
        open2  = 101.0
        close2 = round(open2 * 1.04, 8)
        body2  = close2 - open2
        high2  = round(close2 + body2 * 0.1, 8)  # < 150
        low2   = round(open2 - body2 * 0.1, 8)
        assert high2 < 150.0
        self._append_and_feed(ts2, open2, high2, low2, close2, 1000.0)

        st = _st()
        # 完整重置（is_method_b=False）：底部和頂部都更新為新 K 的值
        assert st["consolidation_low"]  == pytest.approx(low2,  rel=1e-6)
        assert st["consolidation_high"] == pytest.approx(high2, rel=1e-6)
        assert st["pump_candle_open"]   == pytest.approx(open2, rel=1e-6)

    def test_method_b_volume_insufficient_no_trigger(self):
        """體量不足（< prev_volume × 0.8）→ 即使漲幅超標也不觸發 Method B。"""
        trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        old_bottom = _st()["consolidation_low"]
        # prev_volume = 400, need new_vol >= 320; use 200 < 320
        ts2   = TS0 + _4H_MS
        open2 = 100.0
        close2 = round(open2 * 1.07, 8)
        body2  = close2 - open2
        high2  = round(close2 + body2 * 0.1, 8)
        low2   = round(open2 - body2 * 0.1, 8)
        self._append_and_feed(ts2, open2, high2, low2, close2, 200.0)
        assert _st()["consolidation_low"] == pytest.approx(old_bottom, rel=1e-6)

    def test_method_b_in_ready_phase_reverts_to_tracking(self):
        """READY 時出現 Method B → 退回 TRACKING，底部更新。"""
        bottom, prev_high, ts_start = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_ready = int((ts_start + 13 * 3600) * 1000)
        self._append_and_feed(ts_ready, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0)
        assert _st()["phase"] == StrategyPhase.READY

        # gain=3.5%：high2=103.85 < consolidation_high=104.4，不觸延伸邏輯
        # vol=700：vol_ratio=3.5×>3，通過觸發量能門檻
        ts2    = ts_ready + _4H_MS
        open2  = 100.0
        close2 = round(open2 * 1.035, 8)
        body2  = close2 - open2
        high2  = round(close2 + body2 * 0.1, 8)
        low2   = round(open2 - body2 * 0.1, 8)
        assert high2 < prev_high  # 確認不觸發延伸邏輯
        self._append_and_feed(ts2, open2, high2, low2, close2, 700.0)
        st = _st()
        assert st["phase"] == StrategyPhase.TRACKING
        assert st["consolidation_low"] == pytest.approx(low2, rel=1e-6)


# ─── Type 1 進場訊號 ───────────────────────────────────────────────────────────

class TestType1Signal:
    """READY 狀態下的 Type 1 帶量突破訊號。

    突破 K 設計：使用 make_tight_breakout_candle（body_ratio=0.75）。
    止損設置：15m deque 含一根放量 K（low=101.0，4h 窗口內），確保 risk_pct ≈ 4.2%。
    """

    def _setup_ready(self, base_volume=100.0):
        """進入 READY 狀態，15m deque 含放量 K 供止損 3-5% 設置。"""
        _, prev_high, ts_start = trigger_long_tracking(SYM, TS0, base=100.0, gain_pct=4.0)
        ts_ready = int((ts_start + 13 * 3600) * 1000)
        on_new_4h_candle(SYM, make_4h_candle(ts_ready, prev_high * 0.99, prev_high * 0.999,
                                              prev_high * 0.985, prev_high * 0.995, 1000.0))
        assert _st()["phase"] == StrategyPhase.READY

        # 突破 K 時間 = TS0 + 14h
        ts_breakout = TS0 + 14 * 3600 * 1000
        cur_4h_start = (ts_breakout // _4H_MS) * _4H_MS

        # deque: 198 基礎根 + 1 放量根（同 4h，low=101.0）+ 1 占位根（-1 排除）
        d = make_15m_ohlc_deque(count=198, base_volume=base_volume)
        prior_vol_ts = cur_4h_start + 900_000  # 4h 起點 +15m
        d.append((prior_vol_ts, 100.0, 102.0, 101.0, 101.5, base_volume * 4))
        d.append((ts_breakout, 100.0, 101.0, 99.0, 100.5, base_volume))
        setup_symbol_state(SYM, kline_15m_ohlc=d)

    def test_breakout_with_volume_triggers(self):
        """close > top × 1.005 且量能 > avg × 3.5 → 觸發 type1。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = make_tight_breakout_candle(ts_candle, top, vol=500.0)
        result = on_new_15m_candle(SYM, candle)
        assert result is not None
        assert result["type"] == "type1"

    def test_close_below_body_threshold_does_not_trigger(self):
        """close <= top × 1.005（剛好等於門檻）→ 不觸發。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        # close = top * 1.005 == threshold（需嚴格大於）
        candle = (ts_candle, top, top * 1.02, top * 0.995, top * 1.005, 500.0, 350.0)
        assert on_new_15m_candle(SYM, candle) is None

    def test_close_above_top_but_below_body_threshold_does_not_trigger(self):
        """close 超過頂部但未達 +0.5% 門檻 → 不觸發（體突破過濾）。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, top, top * 1.02, top * 0.995, top * 1.002, 500.0, 350.0)
        assert on_new_15m_candle(SYM, candle) is None

    def test_volume_insufficient_does_not_trigger(self):
        """close > top × 1.005 但量能不足（< avg × 3.5）→ 不觸發。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        # volume = 300 < avg(≈101.5) * 3.5 ≈ 355
        candle = (ts_candle, top, top * 1.02, top * 0.995, top * 1.01, 300.0, 210.0)
        assert on_new_15m_candle(SYM, candle) is None

    def test_insufficient_15m_history_does_not_trigger(self):
        """15m 歷史 < 193 根 → 不觸發。"""
        _, prev_high, ts_start = trigger_long_tracking(SYM, TS0, base=100.0, gain_pct=4.0)
        ts_ready = int((ts_start + 13 * 3600) * 1000)
        on_new_4h_candle(SYM, make_4h_candle(ts_ready, prev_high * 0.99, prev_high * 0.999,
                                              prev_high * 0.985, prev_high * 0.995, 1000.0))
        assert _st()["phase"] == StrategyPhase.READY
        setup_symbol_state(SYM, kline_15m_ohlc=make_15m_ohlc_deque(count=100, base_volume=100.0))
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = make_tight_breakout_candle(ts_candle, top, vol=500.0)
        assert on_new_15m_candle(SYM, candle) is None

    def test_cooldown_prevents_repeat_signal(self):
        """冷卻期內不重複觸發。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        c1 = make_tight_breakout_candle(ts_candle, top, vol=500.0)
        assert on_new_15m_candle(SYM, c1) is not None
        c2 = make_tight_breakout_candle(ts_candle + 15 * 60 * 1000, top * 1.005, vol=500.0)
        assert on_new_15m_candle(SYM, c2) is None

    def test_signal_fields_are_correct(self):
        """訊號 dict 包含必要欄位且值正確。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = make_tight_breakout_candle(ts_candle, top, vol=500.0)
        result = on_new_15m_candle(SYM, candle)
        assert result is not None
        for key in ("type", "symbol", "close", "stop_loss", "top", "bottom", "vol_ratio"):
            assert key in result
        assert result["symbol"] == SYM
        assert result["top"]    == pytest.approx(top, rel=1e-6)
        assert result["type"]   == "type1"

    def test_not_triggered_in_tracking_phase(self):
        """TRACKING（非 READY）時不觸發訊號。"""
        trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        assert _st()["phase"] == StrategyPhase.TRACKING
        setup_symbol_state(SYM, kline_15m_ohlc=make_15m_ohlc_deque(count=200, base_volume=100.0))
        top = _st()["consolidation_high"]
        ts_candle = TS0 + _4H_MS
        candle = make_tight_breakout_candle(ts_candle, top, vol=500.0)
        assert on_new_15m_candle(SYM, candle) is None

    def test_risk_in_range_passes(self):
        """無放量 15m 根時止損來自突破 K 自身 low（risk ≈ 1.22%，∈ [1%, 10%]）→ 訊號發出。"""
        _, prev_high, ts_start = trigger_long_tracking(SYM, TS0, base=100.0, gain_pct=4.0)
        ts_ready = int((ts_start + 13 * 3600) * 1000)
        on_new_4h_candle(SYM, make_4h_candle(ts_ready, prev_high * 0.99, prev_high * 0.999,
                                              prev_high * 0.985, prev_high * 0.995, 1000.0))
        assert _st()["phase"] == StrategyPhase.READY
        # 無放量根，stop_loss = tight candle 自身 low ≈ 104.157；risk ≈ 1.22% ∈ [1%, 10%]
        setup_symbol_state(SYM, kline_15m_ohlc=make_15m_ohlc_deque(count=200, base_volume=100.0))
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = make_tight_breakout_candle(ts_candle, top, vol=500.0)
        result = on_new_15m_candle(SYM, candle)
        assert result is not None

    @staticmethod
    def _make_tail_deque(*tail, total=200, base_volume=100.0):
        """Build a total-element 15m deque.

        tail: sequence of (vol, low, high); appended at indices [-len(tail)-1 … -2].
        Index -1 is always base (skipped by baseline calc [-193:-1] end).
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

    def test_stop_loss_extends_via_high_volume_candles(self):
        """有放量根 → stop_loss 延伸至最低放量根 low（非連續掃描）。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        # [-3]: vol=500, low=top*0.978；[-2]: vol=500, low=top*0.985
        # (avg ≈ 101.5, lookback_threshold = 101.5*2.5 = 253.75)
        d = self._make_tail_deque(
            (500.0, top * 0.978, 101.0),
            (500.0, top * 0.985, 101.0),
        )
        setup_symbol_state(SYM, kline_15m_ohlc=d)
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = make_tight_breakout_candle(ts_candle, top, vol=500.0)
        result = on_new_15m_candle(SYM, candle)
        assert result is not None
        # 非連續掃描：兩根放量根都計入，stop = min(tight_low, top*0.978) = top*0.978
        assert result["stop_loss"] == pytest.approx(top * 0.978, rel=1e-6)

    def test_stop_loss_does_not_cross_4h_boundary(self):
        """止損回掃不跨越當前 4h K 棒的起點。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]

        ts_candle       = TS0 + 14 * 3600 * 1000
        cur_4h_open_ms  = (ts_candle // _4H_MS) * _4H_MS

        # 邊界前（上一 4h）高量，low=top*0.960（不應計入）
        # 邊界後（當前 4h）高量，low=top*0.985（應計入）
        ts_before = cur_4h_open_ms - _4H_MS
        ts_after  = cur_4h_open_ms + 15 * 60 * 1000

        total = 200
        d = deque(maxlen=total)
        base_ts = 1_700_000_000_000
        for i in range(total - 3):
            d.append((base_ts + i * 900_000, 100.0, 101.0, 99.0, 100.5, 100.0))
        d.append((ts_before, 100.0, 101.0, top * 0.960, 100.5, 500.0))
        d.append((ts_after,  100.0, 101.0, top * 0.970, 100.5, 500.0))
        d.append((ts_candle, 100.0, 101.0, top * 0.992, 100.5, 100.0))
        setup_symbol_state(SYM, kline_15m_ohlc=d)

        candle = make_tight_breakout_candle(ts_candle, top, vol=500.0)
        result = on_new_15m_candle(SYM, candle)
        assert result is not None
        # 邊界前 top*0.960 不計入，止損來自邊界後 top*0.970（risk≈3.96%）
        assert result["stop_loss"] == pytest.approx(top * 0.970, rel=1e-6)

    def test_stop_loss_non_consecutive_includes_all_high_vol(self):
        """非連續掃描：低量根中間不中斷，兩側高量根都計入。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        # [-3]: vol=500, low=top*0.970；[-2]: vol=100（低量）；[-1]最後占位被排除
        # 等同於 _make_tail_deque 但我們測試非連續
        d = self._make_tail_deque(
            (500.0, top * 0.970, 101.0),   # 高量，low=top*0.970
            (100.0, top * 0.975, 101.0),   # 低量（不計入）
            (500.0, top * 0.985, 101.0),   # 高量，low=top*0.985
        )
        setup_symbol_state(SYM, kline_15m_ohlc=d)
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = make_tight_breakout_candle(ts_candle, top, vol=500.0)
        result = on_new_15m_candle(SYM, candle)
        assert result is not None
        # 非連續掃描：兩根高量根（top*0.985 和 top*0.970）都計入
        # stop = min(tight_low, top*0.970) = top*0.970（最低）
        assert result["stop_loss"] == pytest.approx(top * 0.970, rel=1e-6)


# ─── 即時廢棄掃描 ─────────────────────────────────────────────────────────────

class TestRealtimeInvalidation:
    """markPrice 即時廢棄邏輯。"""

    def test_price_below_bottom_invalidates(self):
        """markPrice < consolidation_low 連續 3 次確認 → 廢棄 IDLE，回傳 True。"""
        bottom, _, _ = trigger_long_tracking(SYM, TS0)
        setup_symbol_state(SYM, last_price=bottom * 0.99)
        # 即時廢棄需連續 LIQUIDATION_BUFFER_CONFIRM_COUNT=3 次確認才執行
        check_invalidation_realtime(SYM)
        check_invalidation_realtime(SYM)
        assert check_invalidation_realtime(SYM) is True
        assert _st()["phase"] == StrategyPhase.IDLE

    def test_price_equal_to_bottom_not_invalidated(self):
        """markPrice == consolidation_low → 邊界不廢棄，回傳 False。"""
        bottom, _, _ = trigger_long_tracking(SYM, TS0)
        setup_symbol_state(SYM, last_price=bottom)
        assert check_invalidation_realtime(SYM) is False
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_price_above_bottom_not_invalidated(self):
        """markPrice > consolidation_low → 不廢棄。"""
        bottom, _, _ = trigger_long_tracking(SYM, TS0)
        setup_symbol_state(SYM, last_price=bottom * 1.05)
        assert check_invalidation_realtime(SYM) is False

    def test_no_price_does_not_invalidate(self):
        """symbol_state 無 last_price → 不廢棄，回傳 False。"""
        trigger_long_tracking(SYM, TS0)
        models.symbol_state.pop(SYM, None)
        assert check_invalidation_realtime(SYM) is False
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_idle_phase_not_affected(self):
        """IDLE 狀態下即時廢棄掃描不影響，回傳 False。"""
        setup_symbol_state(SYM, last_price=50.0)
        assert check_invalidation_realtime(SYM) is False


# ─── 歷史回播 ─────────────────────────────────────────────────────────────────

class TestReplay:
    """啟動時歷史 4h K 棒回播，恢復盤整狀態。"""

    def test_replay_restores_tracking_state(self):
        """含觸發 K 的歷史 → 回播後恢復 TRACKING。"""
        base_ts = TS0 - 12 * _4H_MS
        baseline = [
            (base_ts + i * _4H_MS, 100.0, 100.2, 99.5, 100.0, 100.0, 70.0)
            for i in range(12)
        ]
        trigger = make_trigger_candle(TS0, gain_pct=4.0, volume=500.0)

        all_candles = baseline + [trigger]
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
        """回播不足觸發門檻的歷史 → 原有 TRACKING 狀態清除，回到 IDLE。"""
        trigger_long_tracking(SYM, TS0)
        assert _st()["phase"] == StrategyPhase.TRACKING

        # 只有 1 根 2% 陽線，漲幅不足 3% → 不觸發
        only_candle = [make_trigger_candle(TS0, gain_pct=2.0, volume=500.0)]

        setup_symbol_state(SYM)
        models.symbol_state[SYM]["kline_4h_ohlc"] = deque(only_candle, maxlen=50)
        replay_historical_4h_candles(SYM)
        assert models.strategy_state.get(SYM, {}).get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

    def test_replay_volume_insufficient_no_tracking(self):
        """含漲幅足夠但量能不足的 K → 回播後仍為 IDLE。"""
        base_ts = TS0 - 12 * _4H_MS
        baseline = [(base_ts + i * _4H_MS, 100.0, 100.2, 99.5, 100.0, 100.0, 70.0) for i in range(12)]
        # 漲幅 4% 但量 200 < 100 * 3 = 300 → 不觸發
        trigger = make_trigger_candle(TS0, gain_pct=4.0, volume=200.0)

        setup_symbol_state(SYM)
        models.symbol_state[SYM]["kline_4h_ohlc"] = deque(baseline + [trigger], maxlen=50)
        models.strategy_state.pop(SYM, None)
        replay_historical_4h_candles(SYM)
        assert models.strategy_state.get(SYM, {}).get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE
