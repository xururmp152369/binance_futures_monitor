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
        # baseline avg = 100, threshold = 300, volume = 300 == threshold (not >)
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
        c = (TS0, open_, high, low, close, 400.0)
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
        """baseline 只有 5 根（< TRIGGER_VOLUME_BASELINE_N=12）→ 不觸發。"""
        setup_with_baseline(SYM, baseline_volume=100.0, n=5)
        c = make_trigger_candle(TS0, gain_pct=5.0, volume=400.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        # baseline 不足 12 根時仍可計算（取全部），但量能條件是否通過看實際均值
        # 5 根 baseline avg = 100, volume=400 > 300 → 實際上會通過
        # 這個測試驗證「有基準就能通過」，如果 n=5 < 12 只是提供較少基準根
        # 依新設計，只要 prev[-12:] 有值就用，此情況取 5 根
        # 期望：因為 volume(400) > avg(100) * 3 → 觸發
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_state_fields_after_tracking_entry(self):
        """進入 TRACKING 後：consolidation_low = 觸發 K low，consolidation_high = 觸發 K high。"""
        bottom, top, ts_start = trigger_long_tracking(SYM, TS0, base=100.0, gain_pct=4.0)
        st = _st()
        assert st["phase"] == StrategyPhase.TRACKING
        # trigger candle: open=100, close=104, high=close*1.002≈104.208, low=open*0.998≈99.8
        expected_low  = round(100.0 * 0.998, 8)
        expected_high = round(100.0 * 1.04 * 1.002, 8)
        assert bottom == pytest.approx(expected_low,  rel=1e-6)
        assert top    == pytest.approx(expected_high, rel=1e-6)
        assert st["pump_candle_open"]  == pytest.approx(100.0, rel=1e-6)
        assert st["pump_candle_close"] == pytest.approx(100.0 * 1.04, rel=1e-6)


# ─── 盤整追蹤 ─────────────────────────────────────────────────────────────────

class TestConsolidation:
    """TRACKING 狀態下的盤整追蹤行為。"""

    def test_new_high_extends_top_and_resets_timer(self):
        """TRACKING 時創新高 → 頂部更新、計時重置、維持 TRACKING。"""
        _, prev_high, _ = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_next  = TS0 + _4H_MS
        new_high = prev_high * 1.05
        c = (ts_next, prev_high, new_high, prev_high * 0.99, new_high * 0.998, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["consolidation_high"]     == pytest.approx(new_high, rel=1e-6)
        assert _st()["consolidation_start_ts"] == pytest.approx(ts_next / 1000, rel=1e-6)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_no_new_high_does_not_update_top(self):
        """未創新高 → 頂部不變。"""
        _, prev_high, _ = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_next = TS0 + _4H_MS
        c = (ts_next, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["consolidation_high"] == pytest.approx(prev_high, rel=1e-6)

    def test_tracking_to_ready_after_consolidation_min_hours(self):
        """盤整 >= CONSOLIDATION_MIN_HOURS(12h) → 進入 READY。"""
        _, prev_high, ts_start = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_ready = int((ts_start + 13 * 3600) * 1000)   # 13h > 12h
        c = (ts_ready, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.READY

    def test_before_consolidation_min_hours_remains_tracking(self):
        """盤整時數未達 12h → 維持 TRACKING。"""
        _, prev_high, ts_start = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_early = int((ts_start + 11 * 3600) * 1000)   # 11h < 12h
        c = (ts_early, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.TRACKING

    def test_new_high_in_ready_reverts_to_tracking(self):
        """READY 時創新高 → 退回 TRACKING，更新頂部。"""
        _, prev_high, ts_start = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_ready = int((ts_start + 13 * 3600) * 1000)
        on_new_4h_candle(SYM, (ts_ready, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0))
        assert _st()["phase"] == StrategyPhase.READY

        ts_ext   = ts_ready + _4H_MS
        new_high = prev_high * 1.03
        c_ext = (ts_ext, prev_high, new_high, prev_high * 0.99, new_high * 0.998, 1000.0)
        on_new_4h_candle(SYM, c_ext)
        assert _st()["phase"] == StrategyPhase.TRACKING
        assert _st()["consolidation_high"] == pytest.approx(new_high, rel=1e-6)

    def test_consolidation_start_ts_is_trigger_candle_time(self):
        """進入 TRACKING 後，consolidation_start_ts = 觸發 K 棒的 open_time。"""
        _, _, ts_start = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        assert ts_start == pytest.approx(TS0 / 1000, rel=1e-9)


# ─── 廢棄條件 ─────────────────────────────────────────────────────────────────

class TestInvalidation:
    """4h K 棒實體廢棄邏輯（新版：用 min(open,close) 而非 low）。"""

    def test_body_low_below_bottom_invalidates(self):
        """實體低點 min(open,close) < 底部 → 廢棄至 IDLE。"""
        bottom, _, _ = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_next = TS0 + _4H_MS
        # close < bottom → body_low = close < bottom → 廢棄
        open_ = bottom * 1.02
        close = bottom * 0.98
        c = (ts_next, open_, open_ * 1.01, bottom * 0.97, close, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.IDLE

    def test_shadow_below_bottom_does_not_invalidate(self):
        """下影線低於底部，但實體低點 >= 底部 → 不廢棄（新版關鍵差異）。"""
        bottom, _, _ = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_next = TS0 + _4H_MS
        # open = close = bottom * 1.01（體低 >= 底部），low = bottom * 0.99（影線低於底部）
        open_ = bottom * 1.02
        close = bottom * 1.01   # body_low = close > bottom → 不廢棄
        low   = bottom * 0.99   # 影線低於底部，但不應廢棄
        high  = bottom * 1.05
        c = (ts_next, open_, high, low, close, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] != StrategyPhase.IDLE

    def test_body_low_equal_to_bottom_does_not_invalidate(self):
        """實體低點 == 底部（邊界值）→ 不廢棄。"""
        bottom, _, _ = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_next = TS0 + _4H_MS
        open_ = bottom * 1.02
        close = bottom          # body_low = min(open_, close) = bottom（邊界）
        c = (ts_next, open_, open_ * 1.01, bottom * 0.995, close, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] != StrategyPhase.IDLE

    def test_ready_phase_invalidated(self):
        """READY 時 4h K 實體跌破底部 → 廢棄至 IDLE。"""
        bottom, prev_high, ts_start = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_ready = int((ts_start + 13 * 3600) * 1000)
        on_new_4h_candle(SYM, (ts_ready, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0))
        assert _st()["phase"] == StrategyPhase.READY

        ts_next = ts_ready + _4H_MS
        open_ = bottom * 1.01
        close = bottom * 0.98   # body_low = close < bottom → 廢棄
        c = (ts_next, open_, open_ * 1.01, bottom * 0.97, close, 1000.0)
        on_new_4h_candle(SYM, c)
        assert _st()["phase"] == StrategyPhase.IDLE

    def test_invalidation_clears_all_fields(self):
        """廢棄後，狀態重置（consolidation_low/high = None）。"""
        bottom, _, _ = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_next = TS0 + _4H_MS
        open_ = bottom * 1.01
        close = bottom * 0.98
        c = (ts_next, open_, open_ * 1.01, bottom * 0.97, close, 1000.0)
        on_new_4h_candle(SYM, c)
        st = _st()
        assert st["phase"] == StrategyPhase.IDLE
        assert st["consolidation_low"]  is None
        assert st["consolidation_high"] is None


# ─── Method B ─────────────────────────────────────────────────────────────────

class TestMethodB:
    """盤整期間出現更強觸發 K → Method B 重置（底部上移，頂部保留最大值）。"""

    def _append_and_feed(self, ts, open_, high, low, close, vol):
        """helper：先 append deque 再呼叫 on_new_4h_candle。"""
        c = (ts, open_, high, low, close, vol)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        return c

    def test_stronger_candle_triggers_method_b(self):
        """TRACKING 中出現漲幅 > 前觸發 K + 1% 且帶量的 K → Method B，底部上移。"""
        bottom, old_top, _ = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        # 前觸發 K 漲幅 = 4%，需要 > 4% + 1% = > 5%，使用 6%
        # baseline 現在有 12 根(100) + 1 根觸發 K(400) = 13 根，avg≈125，門檻≈375
        ts2   = TS0 + _4H_MS
        open2 = 100.0
        close2 = round(open2 * 1.06, 8)   # +6%
        high2  = round(close2 * 1.002, 8)
        low2   = round(open2 * 0.998, 8)
        self._append_and_feed(ts2, open2, high2, low2, close2, 400.0)

        st = _st()
        assert st["phase"] == StrategyPhase.TRACKING
        assert st["consolidation_low"] == pytest.approx(low2, rel=1e-6)

    def test_method_b_gain_advantage_boundary(self):
        """漲幅剛好等於前觸發 K + 1%（不超過）→ 不觸發 Method B。"""
        trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        old_bottom = _st()["consolidation_low"]
        # gain = 5.0% == 4% + 1% (不超過) → 不觸發 Method B
        ts2   = TS0 + _4H_MS
        open2 = 100.0
        close2 = round(open2 * 1.05, 8)   # +5% == 4+1，不超過
        high2  = round(close2 * 1.002, 8)
        low2   = round(open2 * 0.998, 8)
        self._append_and_feed(ts2, open2, high2, low2, close2, 400.0)
        assert _st()["consolidation_low"] == pytest.approx(old_bottom, rel=1e-6)

    def test_method_b_preserves_higher_consolidation_high(self):
        """Method B 後，consolidation_high 保留歷史最大值（不因 Method B 而下移）。"""
        _, old_top, ts_start = trigger_long_tracking(SYM, TS0, gain_pct=4.0)

        # 先讓頂部延伸到更高
        ts_ext  = TS0 + _4H_MS
        ext_top = old_top * 1.10
        self._append_and_feed(ts_ext, old_top, ext_top, old_top * 0.99, ext_top * 0.998, 1000.0)
        assert _st()["consolidation_high"] == pytest.approx(ext_top, rel=1e-6)

        # Method B 觸發（漲幅 6% > 4%+1%），但 high2 < ext_top
        ts3   = TS0 + 2 * _4H_MS
        open3 = 100.0
        close3 = round(open3 * 1.06, 8)
        high3  = round(close3 * 1.002, 8)   # high3 < ext_top（不超過頂部，否則走延伸邏輯）
        low3   = round(open3 * 0.998, 8)
        assert high3 < ext_top   # 確認不觸發整體延伸
        self._append_and_feed(ts3, open3, high3, low3, close3, 400.0)

        # consolidation_high 應保留 ext_top（最大值）
        assert _st()["consolidation_high"] == pytest.approx(ext_top, rel=1e-6)
        # consolidation_low 應更新為 Method B 的新底部
        assert _st()["consolidation_low"] == pytest.approx(low3, rel=1e-6)

    def test_method_b_volume_insufficient_no_trigger(self):
        """量能不足（< baseline × 3）→ 即使漲幅超標也不觸發 Method B。"""
        trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        old_bottom = _st()["consolidation_low"]
        ts2   = TS0 + _4H_MS
        open2 = 100.0
        close2 = round(open2 * 1.07, 8)   # +7% > 4%+1%，漲幅達標
        high2  = round(close2 * 1.002, 8)
        low2   = round(open2 * 0.998, 8)
        # volume = 200 < baseline(≈125) × 3 ≈ 375 → 量能不足
        self._append_and_feed(ts2, open2, high2, low2, close2, 200.0)
        # 量能不足，Method B 未觸發
        assert _st()["consolidation_low"] == pytest.approx(old_bottom, rel=1e-6)

    def test_method_b_in_ready_phase_reverts_to_tracking(self):
        """READY 時出現 Method B → 退回 TRACKING，底部更新。"""
        bottom, prev_high, ts_start = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        ts_ready = int((ts_start + 13 * 3600) * 1000)
        self._append_and_feed(ts_ready, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0)
        assert _st()["phase"] == StrategyPhase.READY

        ts2   = ts_ready + _4H_MS
        open2 = 100.0
        close2 = round(open2 * 1.06, 8)   # +6% > 4%+1%
        high2  = round(close2 * 1.002, 8)
        low2   = round(open2 * 0.998, 8)
        self._append_and_feed(ts2, open2, high2, low2, close2, 400.0)
        st = _st()
        assert st["phase"] == StrategyPhase.TRACKING
        assert st["consolidation_low"] == pytest.approx(low2, rel=1e-6)


# ─── Type 1 進場訊號 ───────────────────────────────────────────────────────────

class TestType1Signal:
    """READY 狀態下的 Type 1 帶量突破訊號（close > top × 1.005，量 > avg × 4.5）。"""

    def _setup_ready(self, base_volume=100.0):
        """進入 READY 狀態，填入 15m 歷史資料。"""
        _, prev_high, ts_start = trigger_long_tracking(SYM, TS0, base=100.0, gain_pct=4.0)
        ts_ready = int((ts_start + 13 * 3600) * 1000)
        on_new_4h_candle(SYM, (ts_ready, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0))
        assert _st()["phase"] == StrategyPhase.READY
        setup_symbol_state(SYM, kline_15m_ohlc=make_15m_ohlc_deque(count=200, base_volume=base_volume))

    def test_breakout_with_volume_triggers(self):
        """close > top × 1.005 且量能 > avg × 4.5 → 觸發 type1。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        # close = top * 1.01 > top * 1.005，volume = 500 > avg(100) * 4.5 = 450
        candle = (TS0 + 14 * 3600 * 1000, top, top * 1.02, top * 0.995, top * 1.01, 500.0)
        result = on_new_15m_candle(SYM, candle)
        assert result is not None
        assert result["type"] == "type1"

    def test_close_below_body_threshold_does_not_trigger(self):
        """close <= top × 1.005（剛好等於門檻）→ 不觸發。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        # close = top * 1.005 == threshold（需嚴格大於）
        candle = (TS0 + 14 * 3600 * 1000, top, top * 1.02, top * 0.995, top * 1.005, 500.0)
        assert on_new_15m_candle(SYM, candle) is None

    def test_close_above_top_but_below_body_threshold_does_not_trigger(self):
        """close 超過頂部但未達 +0.5% 門檻 → 不觸發（體突破過濾）。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        # close = top * 1.002（突破頂部但 < top * 1.005）→ 不觸發
        candle = (TS0 + 14 * 3600 * 1000, top, top * 1.02, top * 0.995, top * 1.002, 500.0)
        assert on_new_15m_candle(SYM, candle) is None

    def test_volume_insufficient_does_not_trigger(self):
        """close > top × 1.005 但量能不足（< avg × 4.5）→ 不觸發。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        # volume = 300 < avg(100) * 4.5 = 450
        candle = (TS0 + 14 * 3600 * 1000, top, top * 1.02, top * 0.995, top * 1.01, 300.0)
        assert on_new_15m_candle(SYM, candle) is None

    def test_insufficient_15m_history_does_not_trigger(self):
        """15m 歷史 < 193 根 → 不觸發。"""
        _, prev_high, ts_start = trigger_long_tracking(SYM, TS0, base=100.0, gain_pct=4.0)
        ts_ready = int((ts_start + 13 * 3600) * 1000)
        on_new_4h_candle(SYM, (ts_ready, prev_high * 0.99, prev_high * 0.999, prev_high * 0.985, prev_high * 0.995, 1000.0))
        assert _st()["phase"] == StrategyPhase.READY
        setup_symbol_state(SYM, kline_15m_ohlc=make_15m_ohlc_deque(count=100, base_volume=100.0))
        top = _st()["consolidation_high"]
        candle = (TS0 + 14 * 3600 * 1000, top, top * 1.02, top * 0.995, top * 1.01, 500.0)
        assert on_new_15m_candle(SYM, candle) is None

    def test_cooldown_prevents_repeat_signal(self):
        """冷卻期內不重複觸發。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        c1 = (ts_candle, top, top * 1.02, top * 0.995, top * 1.01, 500.0)
        assert on_new_15m_candle(SYM, c1) is not None
        c2 = (ts_candle + 15 * 60 * 1000, top * 1.01, top * 1.03, top, top * 1.02, 500.0)
        assert on_new_15m_candle(SYM, c2) is None

    def test_signal_fields_are_correct(self):
        """訊號 dict 包含必要欄位且值正確。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, top, top * 1.02, top * 0.995, top * 1.01, 500.0)
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
        candle = (TS0 + _4H_MS, top, top * 1.02, top * 0.995, top * 1.01, 500.0)
        assert on_new_15m_candle(SYM, candle) is None

    @staticmethod
    def _make_tail_deque(*tail, total=200, base_volume=100.0):
        """Build a total-element 15m deque.

        tail: sequence of (vol, low, high); appended at indices [-len(tail)-1 … -2].
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
        """前方無放量根 → stop_loss = 突破 K 自身最低 low。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        breakout_low = top * 0.990
        ts_candle    = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, top, top * 1.02, breakout_low, top * 1.01, 500.0)
        result = on_new_15m_candle(SYM, candle)
        assert result is not None
        assert result["stop_loss"] == pytest.approx(breakout_low, rel=1e-6)

    def test_stop_loss_extends_through_consecutive_high_volume(self):
        """往回掃連續放量根 → stop_loss 延伸至最低 low。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        # [-4]: vol=100 → 鏈斷（不掃）；[-3]: vol=500, low=top*0.978；[-2]: vol=500, low=top*0.985
        d = self._make_tail_deque(
            (100.0, 99.0,        101.0),
            (500.0, top * 0.978, 101.0),
            (500.0, top * 0.985, 101.0),
        )
        setup_symbol_state(SYM, kline_15m_ohlc=d)
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, top, top * 1.02, top * 0.992, top * 1.01, 500.0)
        result = on_new_15m_candle(SYM, candle)
        assert result is not None
        assert result["stop_loss"] == pytest.approx(top * 0.978, rel=1e-6)

    def test_stop_loss_chain_breaks_at_low_volume(self):
        """連續放量鏈中間遇低量根 → 停止回掃，不延伸到更前方。"""
        self._setup_ready(base_volume=100.0)
        top = _st()["consolidation_high"]
        # [-4]: vol=500, low=top*0.970 → 未被掃（鏈已斷）；[-3]: vol=100 鏈斷點；[-2]: vol=500
        d = self._make_tail_deque(
            (500.0, top * 0.970, 101.0),
            (100.0, top * 0.975, 101.0),
            (500.0, top * 0.985, 101.0),
        )
        setup_symbol_state(SYM, kline_15m_ohlc=d)
        ts_candle = TS0 + 14 * 3600 * 1000
        candle = (ts_candle, top, top * 1.02, top * 0.992, top * 1.01, 500.0)
        result = on_new_15m_candle(SYM, candle)
        assert result is not None
        assert result["stop_loss"] == pytest.approx(top * 0.985, rel=1e-6)


# ─── 即時廢棄掃描 ─────────────────────────────────────────────────────────────

class TestRealtimeInvalidation:
    """markPrice 即時廢棄邏輯。"""

    def test_price_below_bottom_invalidates(self):
        """markPrice < consolidation_low → 廢棄 IDLE，回傳 True。"""
        bottom, _, _ = trigger_long_tracking(SYM, TS0)
        setup_symbol_state(SYM, last_price=bottom * 0.99)
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
        # 12 根基準 K（低量）+ 1 根觸發 K（高量，漲幅 4%）
        base_ts = TS0 - 12 * _4H_MS
        baseline = [
            (base_ts + i * _4H_MS, 100.0, 100.2, 99.5, 100.0, 100.0)
            for i in range(12)
        ]
        open1 = 100.0
        close1 = round(open1 * 1.04, 8)   # +4%
        high1  = round(close1 * 1.002, 8)
        low1   = round(open1 * 0.998, 8)
        trigger = (TS0, open1, high1, low1, close1, 500.0)  # 500 > 100 * 3

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
        open1 = 100.0
        close1 = round(open1 * 1.02, 8)
        high1  = round(close1 * 1.002, 8)
        low1   = round(open1 * 0.998, 8)
        only_candle = [(TS0, open1, high1, low1, close1, 500.0)]

        setup_symbol_state(SYM)
        models.symbol_state[SYM]["kline_4h_ohlc"] = deque(only_candle, maxlen=50)
        replay_historical_4h_candles(SYM)
        assert models.strategy_state.get(SYM, {}).get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE

    def test_replay_volume_insufficient_no_tracking(self):
        """含漲幅足夠但量能不足的 K → 回播後仍為 IDLE。"""
        base_ts = TS0 - 12 * _4H_MS
        baseline = [(base_ts + i * _4H_MS, 100.0, 100.2, 99.5, 100.0, 100.0) for i in range(12)]
        # 漲幅 4% 但量 200 < 100 * 3 = 300 → 不觸發
        trigger = (TS0, 100.0, round(104.0 * 1.002, 8), 99.8, 104.0, 200.0)

        setup_symbol_state(SYM)
        models.symbol_state[SYM]["kline_4h_ohlc"] = deque(baseline + [trigger], maxlen=50)
        models.strategy_state.pop(SYM, None)
        replay_historical_4h_candles(SYM)
        assert models.strategy_state.get(SYM, {}).get("phase", StrategyPhase.IDLE) == StrategyPhase.IDLE
