"""空頭策略狀態機測試（short_bounce.py）"""

import pytest
from collections import deque
from app.setting import models
from app.setting.config import (
    SHORT_WATCHING_MAX_CANDLES, EMA_LONG_PERIOD, EMA_SHORT_PERIOD,
    EMA_TOLERANCE_PCT, BODY_SUPPRESS_PCT, WICK_BODY_RATIO,
    SHORT_BOUNCE_VOLUME_MAX, TRIGGER_VOLUME_MULT, BREAKOUT_BODY_PCT,
    SHORT_ENTRY_VOLUME_MIN,
)
from app.strategy.short_bounce import ShortPhase, enter_short_watching, check_short_invalidation_realtime
from app.strategy.state_machine import on_new_4h_candle, on_new_15m_candle
from app.strategy.analysis_utils import calc_ema
from tests.conftest import (
    _4H_MS, trigger_long_tracking, setup_with_baseline, make_15m_ohlc_deque,
)

SYM  = "BTCUSDT"
TS0  = 1_000_000_000_000   # ms
RES  = 10_000.0            # 壓力位（short_resistance）
ELOW = 9_000.0             # 廢棄低點（abandonment_low）


def _sst() -> dict:
    return models.short_strategy_state.get(SYM, {})


# ── helpers ───────────────────────────────────────────────────────────────────

def _setup_watching(ts_ms=TS0, resistance=RES, entry_low=ELOW):
    """直接進入 SHORT_WATCHING 狀態。"""
    enter_short_watching(SYM, resistance, entry_low, ts_ms / 1000)
    return resistance, entry_low


def _setup_4h_baseline(n=14, close_price=8_000.0):
    """設定 kline_4h_ohlc，n 根收盤均為 close_price（供量能基準和 EMA 計算）。"""
    candles = [
        (TS0 - (n - i) * _4H_MS, close_price, close_price * 1.01,
         close_price * 0.99, close_price, 100.0)
        for i in range(n)
    ]
    if SYM not in models.symbol_state:
        models.symbol_state[SYM] = {
            "last_price":     close_price,
            "kline_15m_ohlc": deque(maxlen=200),
            "kline_4h_ohlc":  deque(maxlen=200),
        }
    models.symbol_state[SYM]["kline_4h_ohlc"] = deque(candles, maxlen=200)


def _make_rejection_candle(ts, reference, baseline_vol=100.0):
    """建立符合所有條件的射擊之星 K 棒（靠近 reference 且無量）。

    - high == reference（wick 剛好觸到壓力位）
    - body_high < reference × (1 - BODY_SUPPRESS_PCT)（實體壓制）
    - upper_wick > body × WICK_BODY_RATIO（射擊之星）
    - volume < baseline_vol × SHORT_BOUNCE_VOLUME_MAX（無量）
    """
    body_high = reference * (1 - BODY_SUPPRESS_PCT - 0.002)
    body = body_high * 0.003          # 小實體
    body_low = body_high - body
    open_ = body_low
    close = body_high
    high  = reference
    low   = body_low - body * 0.1
    volume = baseline_vol * SHORT_BOUNCE_VOLUME_MAX * 0.5   # 遠低於上限
    return (ts, open_, high, low, close, volume)


def _setup_ema_baseline(n_candles, close_price):
    """設定含足夠根數的 kline_4h_ohlc，供 EMA 計算用。"""
    candles = [
        (TS0 - (n_candles - i) * _4H_MS, close_price, close_price * 1.01,
         close_price * 0.99, close_price, 100.0)
        for i in range(n_candles)
    ]
    if SYM not in models.symbol_state:
        models.symbol_state[SYM] = {
            "last_price":     close_price,
            "kline_15m_ohlc": deque(maxlen=200),
            "kline_4h_ohlc":  deque(maxlen=200),
        }
    models.symbol_state[SYM]["kline_4h_ohlc"] = deque(candles, maxlen=200)


# ─── 進入觀察 ─────────────────────────────────────────────────────────────────

class TestShortWatching:
    """enter_short_watching 與 WATCHING 狀態基本行為。"""

    def test_enter_sets_phase_and_fields(self):
        """進入 SHORT_WATCHING 後，phase/resistance/entry_low 正確設定。"""
        _setup_watching()
        st = _sst()
        assert st["phase"]            == ShortPhase.WATCHING
        assert st["short_resistance"] == pytest.approx(RES,  rel=1e-6)
        assert st["abandonment_low"]  == pytest.approx(ELOW, rel=1e-6)

    def test_enter_ignored_if_not_idle(self):
        """已在 WATCHING 時再次呼叫 enter → 不變更狀態。"""
        _setup_watching(resistance=RES)
        enter_short_watching(SYM, RES * 2, ELOW * 2, TS0 / 1000 + 1000)
        assert _sst()["short_resistance"] == pytest.approx(RES, rel=1e-6)

    def test_timeout_resets_to_idle(self):
        """觀察超過 SHORT_WATCHING_MAX_CANDLES 根 4h K → IDLE。"""
        _setup_watching(ts_ms=TS0)
        _setup_4h_baseline(n=14)

        # 送出 MAX_CANDLES + 1 根後，累計時間超時
        for i in range(SHORT_WATCHING_MAX_CANDLES + 1):
            ts = TS0 + (i + 1) * _4H_MS
            c = (ts, RES * 0.97, RES * 0.98, RES * 0.96, RES * 0.97, 50.0)
            models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
            on_new_4h_candle(SYM, c)

        assert _sst()["phase"] == ShortPhase.IDLE

    def test_volume_breakout_removes_observation(self):
        """反彈帶量（body > resistance 且 volume > baseline × TRIGGER_VOLUME_MULT）→ IDLE。"""
        _setup_watching()
        _setup_4h_baseline(n=14)

        # body_high > resistance，帶量
        ts = TS0 + _4H_MS
        c = (ts, RES * 0.99, RES * 1.02, RES * 0.98, RES * 1.01, 400.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)

        assert _sst()["phase"] == ShortPhase.IDLE

    def test_4h_candle_idle_not_affected(self):
        """IDLE 時 on_new_4h_candle_short 不改變狀態。"""
        _setup_4h_baseline(n=14)
        c = (TS0 + _4H_MS, RES * 0.99, RES * 1.0, RES * 0.98, RES * 0.99, 50.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _sst().get("phase", ShortPhase.IDLE) == ShortPhase.IDLE


# ─── 靜態壓制（方案 A） ────────────────────────────────────────────────────────

class TestStaticRejection:
    """SHORT_WATCHING → SHORT_READY：靜態壓力位壓制。"""

    def test_valid_rejection_triggers_ready(self):
        """符合所有條件的射擊之星 → SHORT_READY，rejection_high 記錄。"""
        _setup_watching()
        _setup_4h_baseline(n=14)
        ts = TS0 + _4H_MS
        c = _make_rejection_candle(ts, RES)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        st = _sst()
        assert st["phase"]               == ShortPhase.READY
        assert st["short_rejection_high"] == pytest.approx(c[2], rel=1e-6)

    def test_high_below_resistance_no_ready(self):
        """high < resistance（wick 沒有觸到壓力位）→ 不觸發。"""
        _setup_watching()
        _setup_4h_baseline(n=14)
        ts = TS0 + _4H_MS
        # high = resistance * 0.99（未觸到）
        c = _make_rejection_candle(ts, RES * 0.99)
        # 但這根 K 的 reference 是 RES*0.99，所以它會觸 RES*0.99，不觸 RES
        # 直接手動構造：high 低於 RES
        open_ = RES * 0.97
        close = RES * 0.975
        high  = RES * 0.99  # < RES → 不符合
        low   = RES * 0.965
        c = (ts, open_, high, low, close, 50.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _sst()["phase"] == ShortPhase.WATCHING

    def test_high_above_tolerance_no_ready(self):
        """high > resistance × (1 + EMA_TOLERANCE_PCT)（wick 超出容忍值）→ 不觸發。"""
        _setup_watching()
        _setup_4h_baseline(n=14)
        ts = TS0 + _4H_MS
        # high = resistance * (1 + tolerance + 0.01)
        body_high = RES * (1 - BODY_SUPPRESS_PCT - 0.002)
        body = body_high * 0.003
        open_ = body_high - body
        close = body_high
        high  = RES * (1 + EMA_TOLERANCE_PCT + 0.01)   # 超出容忍
        low   = open_ - body * 0.1
        c = (ts, open_, high, low, close, 50.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _sst()["phase"] == ShortPhase.WATCHING

    def test_body_not_suppressed_no_ready(self):
        """body_high > resistance × (1 - BODY_SUPPRESS_PCT)（實體未壓制）→ 不觸發。"""
        _setup_watching()
        _setup_4h_baseline(n=14)
        ts = TS0 + _4H_MS
        # body_high = resistance * 1.0（實體過高，超過壓制門檻）
        open_ = RES * 0.999
        close = RES * 1.001   # body_high > RES * (1 - BODY_SUPPRESS_PCT)
        high  = RES
        low   = RES * 0.995
        c = (ts, open_, high, low, close, 50.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _sst()["phase"] == ShortPhase.WATCHING

    def test_not_shooting_star_no_ready(self):
        """上影線 < 實體 × WICK_BODY_RATIO（非射擊之星）→ 不觸發。"""
        _setup_watching()
        _setup_4h_baseline(n=14)
        ts = TS0 + _4H_MS
        # 大實體，小上影線
        body_low  = RES * 0.92
        body_high = RES * 0.97   # 大實體
        open_ = body_low
        close = body_high
        high  = RES              # wick = RES - body_high = 0.03 * RES
        low   = body_low - RES * 0.001
        # body = 0.05 * RES, wick = 0.03 * RES → wick < body * 1.5 → 非射擊之星
        c = (ts, open_, high, low, close, 50.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _sst()["phase"] == ShortPhase.WATCHING

    def test_high_volume_no_ready(self):
        """反彈量能 >= baseline × SHORT_BOUNCE_VOLUME_MAX → 不觸發（帶量反彈）。"""
        _setup_watching()
        _setup_4h_baseline(n=14, close_price=8_000.0)
        ts = TS0 + _4H_MS
        body_high = RES * (1 - BODY_SUPPRESS_PCT - 0.002)
        body = body_high * 0.003
        open_ = body_high - body
        close = body_high
        high  = RES
        low   = open_ - body * 0.1
        volume = 100.0 * SHORT_BOUNCE_VOLUME_MAX * 1.1   # 超過上限
        c = (ts, open_, high, low, close, volume)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _sst()["phase"] == ShortPhase.WATCHING


# ─── EMA 壓制 ─────────────────────────────────────────────────────────────────

class TestEmaRejection:
    """SHORT_WATCHING → SHORT_READY：EMA60 / EMA15 壓制。"""

    def test_ema60_rejection_triggers_ready(self):
        """EMA60 附近的射擊之星 → SHORT_READY。"""
        # 設定 EMA_LONG_PERIOD(60) 根均等收盤，EMA60 = close_price
        close_price = 9_500.0
        _setup_ema_baseline(EMA_LONG_PERIOD + 5, close_price)
        ema60 = close_price   # 收盤均等時 EMA = close

        # 壓力位設高（不觸靜態壓力位），讓 EMA60 成為壓制基準
        enter_short_watching(SYM, RES * 2, ELOW, TS0 / 1000)

        ts = TS0 + _4H_MS
        c = _make_rejection_candle(ts, ema60, baseline_vol=100.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _sst()["phase"] == ShortPhase.READY

    def test_ema15_death_cross_rejection_triggers_ready(self):
        """EMA15 < EMA60（死叉格局）且射擊之星碰 EMA15 → SHORT_READY。"""
        # 先用高收盤建 EMA60 基準，再用低收盤壓低 EMA15
        # 用 70 根高收盤 + 30 根低收盤，使 EMA15 < EMA60
        n_high = EMA_LONG_PERIOD + 10
        n_low  = EMA_SHORT_PERIOD + 5
        high_close = 10_000.0
        low_close  = 7_000.0

        high_candles = [
            (TS0 - (n_high + n_low - i) * _4H_MS, high_close, high_close * 1.01,
             high_close * 0.99, high_close, 100.0)
            for i in range(n_high)
        ]
        low_candles = [
            (TS0 - (n_low - i) * _4H_MS, low_close, low_close * 1.01,
             low_close * 0.99, low_close, 100.0)
            for i in range(n_low)
        ]
        all_candles = high_candles + low_candles
        models.symbol_state[SYM] = {
            "last_price":     low_close,
            "kline_15m_ohlc": deque(maxlen=200),
            "kline_4h_ohlc":  deque(all_candles, maxlen=200),
        }

        # 確認死叉格局成立
        from app.strategy.analysis_utils import get_4h_ema
        ema60_val = get_4h_ema(SYM, EMA_LONG_PERIOD)
        ema15_val = get_4h_ema(SYM, EMA_SHORT_PERIOD)
        assert ema15_val is not None and ema60_val is not None
        assert ema15_val < ema60_val, "測試前置條件：需確認死叉格局"

        # 壓力位設高，EMA60 設高，讓 EMA15 成為觸發基準
        enter_short_watching(SYM, high_close * 3, ELOW, TS0 / 1000)

        ts = TS0 + _4H_MS
        c = _make_rejection_candle(ts, ema15_val, baseline_vol=100.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _sst()["phase"] == ShortPhase.READY

    def test_ema15_no_death_cross_no_ready(self):
        """EMA15 >= EMA60（非死叉）時，EMA15 壓制條件不觸發 → 維持 WATCHING。"""
        close_price = 9_500.0
        _setup_ema_baseline(EMA_LONG_PERIOD + 5, close_price)
        # EMA15 == EMA60（均等收盤），非死叉

        enter_short_watching(SYM, RES * 2, ELOW, TS0 / 1000)
        ema15 = close_price   # EMA15 = close_price = EMA60 → 不成立死叉

        ts = TS0 + _4H_MS
        c = _make_rejection_candle(ts, ema15, baseline_vol=100.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        # EMA15 不死叉，靜態壓力位 (RES*2) 也未觸，EMA60 雖觸到但因先前是靜態壓力位 (=c[2]...?)
        # 實際上 EMA60 觸到了，這根 K 應觸發 C1 而非 C2
        # 重新設計：EMA15 的值高於 EMA60，確保 C2 不觸發
        # 由於均等收盤時 EMA15=EMA60，C2 條件 (ema15 < ema60) 不成立
        # 但 C1 (EMA60) 會觸發，所以此測試驗證「C2 不會在非死叉下觸發」
        # 可以透過設置 EMA60 >> reference 來排除 C1
        # 重置並用高 EMA60 重新設定
        models.short_strategy_state.clear()
        # 用極高收盤建 EMA60，使 EMA60 >> rejection_candle 的 high
        _setup_ema_baseline(EMA_LONG_PERIOD + 5, close_price * 10)
        enter_short_watching(SYM, RES * 2, ELOW, TS0 / 1000)

        # EMA15 ≈ close_price*10 >> c 的 high → C1 不觸發
        # EMA15 >= EMA60（均等）→ C2 不觸發
        # 靜態壓力位 = RES*2 >> c 的 high → A 不觸發
        c2 = _make_rejection_candle(ts + 100, close_price, baseline_vol=100.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c2)
        on_new_4h_candle(SYM, c2)
        assert _sst()["phase"] == ShortPhase.WATCHING


# ─── SHORT_READY 廢棄 ─────────────────────────────────────────────────────────

class TestShortReady:
    """SHORT_READY 狀態下的廢棄條件。"""

    def _setup_ready(self):
        """直接進入 SHORT_READY。"""
        _setup_watching()
        _setup_4h_baseline(n=14)
        ts = TS0 + _4H_MS
        c = _make_rejection_candle(ts, RES)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _sst()["phase"] == ShortPhase.READY

    def test_volume_breakout_in_ready_resets(self):
        """SHORT_READY 時帶量 4h K 實體收超壓力位 → IDLE。"""
        self._setup_ready()
        ts = TS0 + 2 * _4H_MS
        # body_high > resistance，帶量
        c = (ts, RES * 0.99, RES * 1.02, RES * 0.98, RES * 1.01, 400.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _sst()["phase"] == ShortPhase.IDLE

    def test_low_volume_breakout_in_ready_stays(self):
        """SHORT_READY 時帶量但實體未超壓力位 → 維持 READY。"""
        self._setup_ready()
        ts = TS0 + 2 * _4H_MS
        # body_high < resistance（close < resistance）
        c = (ts, RES * 0.97, RES * 1.01, RES * 0.96, RES * 0.98, 400.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _sst()["phase"] == ShortPhase.READY


# ─── Type 2 進場訊號 ─────────────────────────────────────────────────────────

class TestType2Signal:
    """SHORT_READY 時 15m 帶量跌破做空訊號。"""

    def _setup_ready_with_15m(self, base_volume=100.0):
        """進入 SHORT_READY 並填入 15m 歷史。"""
        _setup_watching()
        _setup_4h_baseline(n=14)
        ts = TS0 + _4H_MS
        c = _make_rejection_candle(ts, RES)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _sst()["phase"] == ShortPhase.READY
        models.symbol_state[SYM]["kline_15m_ohlc"] = make_15m_ohlc_deque(count=200, base_volume=base_volume)

    def test_breakout_with_volume_triggers(self):
        """close < entry_level × (1 - BREAKOUT_BODY_PCT) 且量能足夠 → 觸發 type2。"""
        self._setup_ready_with_15m(base_volume=100.0)
        # close = ELOW * 0.99 < ELOW * (1 - 0.005) = ELOW * 0.995
        ts_candle = TS0 + 2 * _4H_MS
        close_ = ELOW * 0.99
        candle = (ts_candle, ELOW * 1.0, ELOW * 1.005, close_, close_, 200.0)
        result = on_new_15m_candle(SYM, candle)
        assert result is not None
        assert result["type"]   == "type2"
        assert result["symbol"] == SYM

    def test_close_above_threshold_no_signal(self):
        """close >= entry_level × (1 - BREAKOUT_BODY_PCT) → 不觸發。"""
        self._setup_ready_with_15m(base_volume=100.0)
        ts_candle = TS0 + 2 * _4H_MS
        close_ = ELOW * 0.996   # > ELOW * 0.995（未收破門檻）
        candle = (ts_candle, ELOW, ELOW, close_, close_, 200.0)
        assert on_new_15m_candle(SYM, candle) is None

    def test_volume_insufficient_no_signal(self):
        """close 達標但量能不足 < avg × SHORT_ENTRY_VOLUME_MIN(1.0) → 不觸發。"""
        self._setup_ready_with_15m(base_volume=100.0)
        ts_candle = TS0 + 2 * _4H_MS
        close_ = ELOW * 0.99
        # volume = 50 < avg(100) * 1.0 = 100
        candle = (ts_candle, ELOW, ELOW, close_, close_, 50.0)
        assert on_new_15m_candle(SYM, candle) is None

    def test_insufficient_15m_history_no_signal(self):
        """15m 歷史 < 193 根 → 不觸發。"""
        _setup_watching()
        _setup_4h_baseline(n=14)
        ts = TS0 + _4H_MS
        c = _make_rejection_candle(ts, RES)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _sst()["phase"] == ShortPhase.READY
        models.symbol_state[SYM]["kline_15m_ohlc"] = make_15m_ohlc_deque(count=100, base_volume=100.0)
        ts_candle = TS0 + 2 * _4H_MS
        close_ = ELOW * 0.99
        candle = (ts_candle, ELOW, ELOW, close_, close_, 200.0)
        assert on_new_15m_candle(SYM, candle) is None

    def test_not_triggered_in_watching_phase(self):
        """WATCHING（非 READY）時不觸發 Type 2 訊號。"""
        _setup_watching()
        models.symbol_state[SYM] = {
            "last_price":     ELOW,
            "kline_15m_ohlc": make_15m_ohlc_deque(count=200, base_volume=100.0),
            "kline_4h_ohlc":  deque(maxlen=200),
        }
        ts_candle = TS0 + _4H_MS
        close_ = ELOW * 0.99
        candle = (ts_candle, ELOW, ELOW, close_, close_, 200.0)
        assert on_new_15m_candle(SYM, candle) is None

    def test_cooldown_prevents_repeat_signal(self):
        """Type 2 訊號觸發後，STRATEGY_COOLDOWN 冷卻期內再次觸發 → 不發出訊號。"""
        self._setup_ready_with_15m(base_volume=100.0)
        close_ = ELOW * 0.99
        candle1 = (TS0 + 2 * _4H_MS, ELOW, ELOW, close_, close_, 200.0)
        result1 = on_new_15m_candle(SYM, candle1)
        assert result1 is not None, "第一次應觸發"

        # 冷卻期內，15m 後再發同樣跌破 K
        candle2 = (TS0 + 2 * _4H_MS + 15 * 60 * 1000, ELOW, ELOW, close_, close_, 200.0)
        result2 = on_new_15m_candle(SYM, candle2)
        assert result2 is None, "冷卻期內不應重複觸發"

    def test_signal_fields_complete(self):
        """Type 2 訊號 dict 包含必要欄位。"""
        self._setup_ready_with_15m(base_volume=100.0)
        ts_candle = TS0 + 2 * _4H_MS
        close_ = ELOW * 0.99
        candle = (ts_candle, ELOW, ELOW, close_, close_, 200.0)
        result = on_new_15m_candle(SYM, candle)
        assert result is not None
        for key in ("type", "symbol", "close", "stop_loss", "entry_level",
                    "short_resistance", "vol_ratio", "candle_open_time_ms"):
            assert key in result, f"缺少欄位：{key}"
        assert result["type"]            == "type2"
        assert result["entry_level"]     == pytest.approx(ELOW, rel=1e-6)
        assert result["short_resistance"] == pytest.approx(RES,  rel=1e-6)


# ─── 協調器整合 ───────────────────────────────────────────────────────────────

class TestOrchestratorIntegration:
    """多頭廢棄事件透過 state_machine 協調器觸發空頭 WATCHING。"""

    def test_long_abandonment_triggers_short_watching(self):
        """多頭 4h K 實體廢棄 → 空頭自動進入 SHORT_WATCHING。"""
        bottom, _, _ = trigger_long_tracking(SYM, TS0, gain_pct=4.0)

        # 廢棄 K（實體低點 < bottom）
        ts_next = TS0 + _4H_MS
        open_   = bottom * 1.01
        close   = bottom * 0.98
        c = (ts_next, open_, open_ * 1.01, bottom * 0.97, close, 1000.0)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)

        # 多頭廢棄至 IDLE
        assert models.strategy_state.get(SYM, {}).get("phase") in (None, __import__("app.strategy.state_machine", fromlist=["StrategyPhase"]).StrategyPhase.IDLE)
        # 空頭進入 WATCHING
        assert _sst()["phase"] == ShortPhase.WATCHING

    def test_realtime_invalidation_does_not_trigger_short(self):
        """即時廢棄（markPrice 跌破底部）不觸發空頭策略 → 空頭維持 IDLE。"""
        from app.strategy.state_machine import check_invalidation_realtime
        from tests.conftest import setup_symbol_state

        bottom, _, _ = trigger_long_tracking(SYM, TS0, gain_pct=4.0)
        setup_symbol_state(SYM, last_price=bottom * 0.99)
        check_invalidation_realtime(SYM)

        assert _sst().get("phase", ShortPhase.IDLE) == ShortPhase.IDLE


# ─── 即時廢棄掃描 ─────────────────────────────────────────────────────────────

class TestRealtimeInvalidation:
    """check_short_invalidation_realtime() 直接驗證。"""

    def _set_last_price(self, price: float):
        if SYM not in models.symbol_state:
            models.symbol_state[SYM] = {
                "last_price":     price,
                "kline_15m_ohlc": deque(maxlen=200),
                "kline_4h_ohlc":  deque(maxlen=200),
            }
        else:
            models.symbol_state[SYM]["last_price"] = price

    def test_watching_price_above_resistance_resets(self):
        """WATCHING 狀態：markPrice > short_resistance → 重置為 IDLE，回傳 True。"""
        _setup_watching()
        self._set_last_price(RES * 1.01)
        result = check_short_invalidation_realtime(SYM)
        assert result is True
        assert _sst()["phase"] == ShortPhase.IDLE

    def test_ready_price_above_resistance_resets(self):
        """SHORT_READY 狀態：markPrice > short_resistance → 重置為 IDLE，回傳 True。"""
        _setup_watching()
        _setup_4h_baseline(n=14)
        ts = TS0 + _4H_MS
        c = _make_rejection_candle(ts, RES)
        models.symbol_state[SYM]["kline_4h_ohlc"].append(c)
        on_new_4h_candle(SYM, c)
        assert _sst()["phase"] == ShortPhase.READY

        self._set_last_price(RES * 1.01)
        result = check_short_invalidation_realtime(SYM)
        assert result is True
        assert _sst()["phase"] == ShortPhase.IDLE

    def test_price_at_or_below_resistance_no_reset(self):
        """markPrice ≤ short_resistance → 狀態不變，回傳 False。"""
        _setup_watching()
        self._set_last_price(RES * 0.99)
        result = check_short_invalidation_realtime(SYM)
        assert result is False
        assert _sst()["phase"] == ShortPhase.WATCHING
