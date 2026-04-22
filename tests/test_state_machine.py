"""
策略狀態機單元測試。

每個測試用 mock candle 資料驅動 state_machine 函數，
驗證狀態轉換、訊號條件、廢棄條件、邊界值。
不連接 Binance / Telegram。
"""
from unittest.mock import patch
from app.strategy.state_machine import (
    StrategyPhase,
    on_new_4h_candle,
    on_new_15m_candle,
    on_new_1h_candle,
    check_invalidation_realtime,
)
from app.setting import models
from tests.conftest import (
    make_pump_candle, make_flat_candle, make_15m_candle,
    make_1h_candle, make_15m_ohlc_deque, setup_symbol_state,
)

SYM = "BTCUSDT"
BASE_TS = 1700000000000  # ms，固定假時間（2023-11-15）
FAKE_NOW = 1700100000.0  # 測試用 time.time() 回傳值（冷卻期外）


# ────────────────────────────────────────────────────────────────
# 輔助：常用狀態建立
# ────────────────────────────────────────────────────────────────

def _enter_tracking(low=100.0, high=110.0):
    """讓 SYM 進入 TRACKING，回傳 pump candle 的 ts。"""
    candle = make_pump_candle(BASE_TS, low=low, high=high)
    on_new_4h_candle(SYM, candle)
    assert models.strategy_state[SYM]["phase"] == StrategyPhase.TRACKING
    return BASE_TS


def _enter_ready(pump_low=100.0, pump_high=110.0):
    """讓 SYM 進入 READY（pump → 12h 後一根不創新高的 K 棒）。"""
    _enter_tracking(low=pump_low, high=pump_high)
    ts_12h = BASE_TS + 12 * 3600 * 1000
    # 陰線（close < open）確保不觸發拉漲偵測；high < pump_high 不創新高；low > pump_low 不廢棄
    candle = (ts_12h, pump_high - 2, pump_high - 1, pump_low + 2, pump_low + 3)
    on_new_4h_candle(SYM, candle)
    assert models.strategy_state[SYM]["phase"] == StrategyPhase.READY


# ════════════════════════════════════════════════════════════════
# 1. 拉漲偵測（IDLE → TRACKING）
# ════════════════════════════════════════════════════════════════

class TestPumpDetection:

    def test_idle_to_tracking_on_valid_pump(self):
        on_new_4h_candle(SYM, make_pump_candle(BASE_TS))
        assert models.strategy_state[SYM]["phase"] == StrategyPhase.TRACKING

    def test_pump_sets_correct_levels(self):
        on_new_4h_candle(SYM, make_pump_candle(BASE_TS, low=100.0, high=110.0))
        st = models.strategy_state[SYM]
        assert st["consolidation_low"]  == 100.0
        assert st["consolidation_high"] == 110.0
        assert st["pump_candle_low"]    == 100.0

    def test_no_tracking_if_below_threshold(self):
        # (107-100)/100 = 7% < 8%
        candle = (BASE_TS, 100.0, 107.0, 100.0, 106.0)
        on_new_4h_candle(SYM, candle)
        assert models.strategy_state[SYM]["phase"] == StrategyPhase.IDLE

    def test_no_tracking_if_bearish_candle(self):
        # close < open → 陰線，即使振幅 > 8%
        candle = (BASE_TS, 108.0, 120.0, 100.0, 102.0)
        on_new_4h_candle(SYM, candle)
        assert models.strategy_state[SYM]["phase"] == StrategyPhase.IDLE

    def test_boundary_exactly_8pct(self):
        # (108-100)/100 = 8.0% → 剛好達標
        candle = (BASE_TS, 100.0, 108.0, 100.0, 107.0)
        on_new_4h_candle(SYM, candle)
        assert models.strategy_state[SYM]["phase"] == StrategyPhase.TRACKING


# ════════════════════════════════════════════════════════════════
# 2. 盤整計時（TRACKING → READY）
# ════════════════════════════════════════════════════════════════

class TestConsolidation:

    def test_tracking_to_ready_after_12h(self):
        _enter_tracking()
        ts_12h = BASE_TS + 12 * 3600 * 1000
        on_new_4h_candle(SYM, make_flat_candle(ts_12h, high=109.0))
        assert models.strategy_state[SYM]["phase"] == StrategyPhase.READY

    def test_still_tracking_at_8h(self):
        _enter_tracking()
        ts_8h = BASE_TS + 8 * 3600 * 1000
        on_new_4h_candle(SYM, make_flat_candle(ts_8h, high=109.0))
        assert models.strategy_state[SYM]["phase"] == StrategyPhase.TRACKING

    def test_new_high_updates_consolidation_high_and_resets_timer(self):
        _enter_tracking()
        ts_4h = BASE_TS + 4 * 3600 * 1000
        new_high_candle = (ts_4h, 108.0, 115.0, 104.0, 112.0)  # high=115 > 110
        on_new_4h_candle(SYM, new_high_candle)
        st = models.strategy_state[SYM]
        assert st["consolidation_high"]     == 115.0
        assert st["consolidation_start_ts"] == ts_4h / 1000    # 計時重置

    def test_ready_reverts_to_tracking_on_new_high(self):
        _enter_ready()
        ts_16h = BASE_TS + 16 * 3600 * 1000
        new_high_candle = (ts_16h, 108.0, 120.0, 104.0, 118.0)  # high=120 > 110
        on_new_4h_candle(SYM, new_high_candle)
        assert models.strategy_state[SYM]["phase"] == StrategyPhase.TRACKING

    def test_new_pump_in_tracking_resets_all_levels(self):
        _enter_tracking(low=100.0, high=110.0)
        ts_4h = BASE_TS + 4 * 3600 * 1000
        # 新一根拉漲 K 棒，低點更高
        new_pump = make_pump_candle(ts_4h, low=108.0, high=120.0, open_=108.0, close=118.0)
        on_new_4h_candle(SYM, new_pump)
        st = models.strategy_state[SYM]
        assert st["phase"]             == StrategyPhase.TRACKING
        assert st["pump_candle_low"]   == 108.0
        assert st["consolidation_high"] == 120.0


# ════════════════════════════════════════════════════════════════
# 3. 廢棄條件
# ════════════════════════════════════════════════════════════════

class TestInvalidation:

    def test_4h_low_breaks_pump_low_resets_to_idle(self):
        _enter_ready()
        ts_16h = BASE_TS + 16 * 3600 * 1000
        bad_candle = (ts_16h, 105.0, 108.0, 99.0, 103.0)  # low=99 < pump_low=100
        on_new_4h_candle(SYM, bad_candle)
        assert models.strategy_state[SYM]["phase"] == StrategyPhase.IDLE

    def test_1h_low_breaks_pump_low_resets_to_idle(self):
        _enter_ready()
        setup_symbol_state(SYM)
        candle = make_1h_candle(BASE_TS + 14 * 3600 * 1000,
                                open_=105.0, high=107.0, low=99.0, close=103.0)
        with patch("time.time", return_value=FAKE_NOW):
            signal = on_new_1h_candle(SYM, candle)
        assert signal is None
        assert models.strategy_state[SYM]["phase"] == StrategyPhase.IDLE

    def test_realtime_invalidation_when_price_below_pump_low(self):
        _enter_ready()
        setup_symbol_state(SYM, last_price=99.0)  # 跌破 pump_low=100
        result = check_invalidation_realtime(SYM)
        assert result is True
        assert models.strategy_state[SYM]["phase"] == StrategyPhase.IDLE

    def test_realtime_no_invalidation_at_exact_pump_low(self):
        _enter_ready()
        setup_symbol_state(SYM, last_price=100.0)  # 剛好等於底部，不廢棄
        result = check_invalidation_realtime(SYM)
        assert result is False
        assert models.strategy_state[SYM]["phase"] == StrategyPhase.READY

    def test_realtime_no_invalidation_when_idle(self):
        # IDLE 狀態下即時廢棄應直接回傳 False
        setup_symbol_state(SYM, last_price=50.0)
        result = check_invalidation_realtime(SYM)
        assert result is False


# ════════════════════════════════════════════════════════════════
# 4. Type 1 帶量突破訊號
# ════════════════════════════════════════════════════════════════

class TestType1Signal:

    def _setup(self, pump_high=110.0):
        _enter_ready(pump_high=pump_high)
        ohlc = make_15m_ohlc_deque(count=200, base_volume=1000.0)
        setup_symbol_state(SYM, kline_15m_ohlc=ohlc)

    def _candle(self, close, volume, low=108.0):
        ts = BASE_TS + 14 * 3600 * 1000
        return make_15m_candle(ts, low=low, close=close, volume=volume)

    def test_type1_triggers_on_valid_signal(self):
        self._setup()
        candle = self._candle(close=111.0, volume=3100.0)
        with patch("time.time", return_value=FAKE_NOW):
            signal = on_new_15m_candle(SYM, candle)
        assert signal is not None
        assert signal["type"] == "type1"
        assert signal["close"] == 111.0
        assert signal["top"]   == 110.0    # pump 的 high，flat candle 不更新 consolidation_high

    def test_type1_no_signal_if_close_below_top(self):
        self._setup()
        candle = self._candle(close=108.0, volume=3100.0)
        signal = on_new_15m_candle(SYM, candle)
        assert signal is None

    def test_type1_no_signal_if_close_equal_top(self):
        self._setup()
        # consolidation_high = pump_high - 1 = 109.0，close 剛好等於 top 不算突破
        candle = self._candle(close=109.0, volume=3100.0)
        signal = on_new_15m_candle(SYM, candle)
        assert signal is None

    def test_type1_no_signal_if_volume_insufficient(self):
        self._setup()
        # volume=2999 < 1000 * 3 = 3000
        candle = self._candle(close=111.0, volume=2999.0)
        with patch("time.time", return_value=FAKE_NOW):
            signal = on_new_15m_candle(SYM, candle)
        assert signal is None

    def test_type1_boundary_volume_exactly_3x(self):
        self._setup()
        # volume=3000 = 3x，剛好達標（條件是 < 3，所以 3.0 通過）
        candle = self._candle(close=111.0, volume=3000.0)
        with patch("time.time", return_value=FAKE_NOW):
            signal = on_new_15m_candle(SYM, candle)
        assert signal is not None

    def test_type1_no_signal_in_cooldown(self):
        self._setup()
        candle = self._candle(close=111.0, volume=3100.0)
        with patch("time.time", return_value=FAKE_NOW):
            on_new_15m_candle(SYM, candle)
        # 冷卻期內（100 秒後）再次觸發
        with patch("time.time", return_value=FAKE_NOW + 100):
            signal = on_new_15m_candle(SYM, candle)
        assert signal is None

    def test_type1_triggers_after_cooldown_expires(self):
        self._setup()
        candle = self._candle(close=111.0, volume=3100.0)
        with patch("time.time", return_value=FAKE_NOW):
            on_new_15m_candle(SYM, candle)
        with patch("time.time", return_value=FAKE_NOW + 14401):  # 冷卻過後
            signal = on_new_15m_candle(SYM, candle)
        assert signal is not None

    def test_type1_no_signal_if_phase_is_tracking(self):
        _enter_tracking()  # TRACKING，非 READY
        ohlc = make_15m_ohlc_deque(count=200)
        setup_symbol_state(SYM, kline_15m_ohlc=ohlc)
        candle = self._candle(close=111.0, volume=3100.0)
        signal = on_new_15m_candle(SYM, candle)
        assert signal is None

    def test_type1_no_signal_if_not_enough_candles(self):
        _enter_ready()
        ohlc = make_15m_ohlc_deque(count=100)  # < 193 根
        setup_symbol_state(SYM, kline_15m_ohlc=ohlc)
        candle = self._candle(close=111.0, volume=3100.0)
        with patch("time.time", return_value=FAKE_NOW):
            signal = on_new_15m_candle(SYM, candle)
        assert signal is None


# ════════════════════════════════════════════════════════════════
# 5. Type 2 均線反彈訊號
# ════════════════════════════════════════════════════════════════

class TestType2Signal:
    """
    測試基準：
      pump_low=100, pump_high=120 → consolidation_low=100, consolidation_high=120
      EMA15=105, EMA30=104, EMA45=103, EMA60=102
      touch_limit = 105 * 1.005 = 105.525

    Type 2 全通過範例：
      low=104  <= 105.525 ✓（EMA15 觸碰）
      open=106 > 105      ✓（多頭趨勢）
      close=107 > 104*1.02=106.08 ✓（有效收針）
      RR=(120-107)/(107-100)=13/7=1.86 >= 1.0 ✓
    """

    _EMA = {15: 105.0, 30: 104.0, 45: 103.0, 60: 102.0}
    _TS  = BASE_TS + 14 * 3600 * 1000

    def _setup(self):
        _enter_ready(pump_low=100.0, pump_high=120.0)
        setup_symbol_state(SYM, ema_4h=self._EMA)

    def _candle(self, open_, high, low, close):
        return make_1h_candle(self._TS, open_=open_, high=high, low=low, close=close)

    def test_type2_triggers_on_valid_signal(self):
        self._setup()
        candle = self._candle(open_=106.0, high=108.0, low=104.0, close=107.0)
        with patch("time.time", return_value=FAKE_NOW):
            signal = on_new_1h_candle(SYM, candle)
        assert signal is not None
        assert signal["type"]            == "type2"
        assert signal["touched_ema"][0]  == 15

    def test_type2_no_signal_if_low_above_all_ema(self):
        self._setup()
        # low=108 > 105*1.005=105.525，未觸碰任何 EMA
        candle = self._candle(open_=109.0, high=111.0, low=108.0, close=110.0)
        with patch("time.time", return_value=FAKE_NOW):
            signal = on_new_1h_candle(SYM, candle)
        assert signal is None

    def test_type2_no_signal_if_open_below_ema(self):
        self._setup()
        # open=104.0 <= ema15=105.0 → 非多頭趨勢確認
        candle = self._candle(open_=104.0, high=108.0, low=104.0, close=107.0)
        with patch("time.time", return_value=FAKE_NOW):
            signal = on_new_1h_candle(SYM, candle)
        assert signal is None

    def test_type2_no_signal_if_wick_insufficient(self):
        self._setup()
        # low=104, close=105.5 <= 104*1.02=106.08 → 收針不足
        candle = self._candle(open_=106.0, high=107.0, low=104.0, close=105.5)
        with patch("time.time", return_value=FAKE_NOW):
            signal = on_new_1h_candle(SYM, candle)
        assert signal is None

    def test_type2_no_signal_if_rr_below_min(self):
        self._setup()
        # close=115: RR=(120-115)/(115-100)=5/15=0.33 < 1.0
        candle = self._candle(open_=106.0, high=116.0, low=104.0, close=115.0)
        with patch("time.time", return_value=FAKE_NOW):
            signal = on_new_1h_candle(SYM, candle)
        assert signal is None

    def test_type2_boundary_rr_exactly_1(self):
        self._setup()
        # close=110: RR=(120-110)/(110-100)=10/10=1.0，剛好達標
        # wick: 110 > 104*1.02=106.08 ✓
        candle = self._candle(open_=106.0, high=112.0, low=104.0, close=110.0)
        with patch("time.time", return_value=FAKE_NOW):
            signal = on_new_1h_candle(SYM, candle)
        assert signal is not None

    def test_type2_no_signal_in_cooldown(self):
        self._setup()
        candle = self._candle(open_=106.0, high=108.0, low=104.0, close=107.0)
        with patch("time.time", return_value=FAKE_NOW):
            on_new_1h_candle(SYM, candle)
        with patch("time.time", return_value=FAKE_NOW + 100):
            signal = on_new_1h_candle(SYM, candle)
        assert signal is None

    def test_type2_falls_back_to_idle_on_invalidation(self):
        self._setup()
        # low=99 < pump_low=100 → 廢棄
        candle = self._candle(open_=106.0, high=108.0, low=99.0, close=103.0)
        with patch("time.time", return_value=FAKE_NOW):
            signal = on_new_1h_candle(SYM, candle)
        assert signal is None
        assert models.strategy_state[SYM]["phase"] == StrategyPhase.IDLE

    def test_type2_touches_ema30_when_ema15_not_touched(self):
        self._setup()
        # EMA15=105, touch_limit=105.525
        # low=105.6 > 105.525 → EMA15 未觸碰
        # EMA30=104, touch_limit=104.52，low=105.6 > 104.52 → EMA30 也未觸碰
        # 實際上 low 要 <= 104.52 才能觸碰 EMA30
        # 用 low=104.0 <= 105.525 → 仍會觸碰 EMA15（順序優先）
        # 這裡改測 EMA15 剛好不觸碰、但 EMA30 觸碰的情況
        setup_symbol_state(SYM, ema_4h={15: 103.0, 30: 105.0, 45: 103.0, 60: 102.0})
        # EMA15=103, touch_limit=103.515; EMA30=105, touch_limit=105.525
        # low=104.5: 104.5 > 103.515（EMA15 未觸碰），104.5 <= 105.525（EMA30 觸碰）
        # open=106 > ema30=105 ✓
        candle = self._candle(open_=106.0, high=108.0, low=104.5, close=107.0)
        with patch("time.time", return_value=FAKE_NOW):
            signal = on_new_1h_candle(SYM, candle)
        assert signal is not None
        assert signal["touched_ema"][0] == 30
