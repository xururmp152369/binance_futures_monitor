"""死亡叉制空策略（Type 3）。

策略邏輯：
  Layer 1（日線，持續監控）：EMA50(D) < EMA200(D) → 格局確立，進入 WATCHING
  Layer 2（日線，每日確認）：close(D) < EMA200(D)，且時效與幅度檢查通過 → 進入 ALERT
  Layer 3（1H，即時監控）：ALERT 窗口 48H 內，1H 出現信號 A 或 B → 做空進場

狀態轉換：
  IDLE → WATCHING：EMA50 < EMA200
  WATCHING → ALERT：日線跌破 EMA200，時效 ≤ 48H，幅度未回漲
  ALERT → WATCHING：48H 到期 / 幅度超限 / Layer 1 失效
  ALERT / WATCHING → IDLE：EMA50 穿回 EMA200 上方
"""
from enum import Enum
import time
from ..setting import models
from ..setting.config import (
    DC_DAILY_EMA_FAST, DC_DAILY_EMA_SLOW,
    DC_1H_EMA_PERIOD, DC_1H_ATR_PERIOD,
    DC_ALERT_WINDOW_HOURS, DC_MAX_ROLLBACK_HOURS,
    DC_PRICE_RECOVERY_PCT, DC_MAX_ENTRIES_PER_ALERT,
    DC_REJECTION_BODY_PCT, DC_ENGULF_BODY_RATIO, DC_ENGULF_VOLUME_RATIO,
    STRATEGY_COOLDOWN,
)
from .analysis_utils import get_daily_ema, get_1h_ema, get_1h_atr
from ..extension.utils import setup_logging

log = setup_logging()


class DeathCrossPhase(Enum):
    IDLE     = "IDLE"     # EMA50 >= EMA200，不監控
    WATCHING = "WATCHING" # EMA50 < EMA200，等待日線跌破 EMA200
    ALERT    = "ALERT"    # 48H 監控窗口，等待 1H 進場信號


def _init_dc_state() -> dict:
    return {
        "phase":         DeathCrossPhase.IDLE,
        "alert_time":    None,   # T0 Unix 秒數（ALERT 窗口起點）
        "close_t0":      None,   # T0 當下日線收盤價（幅度保護基準）
        "entry_count":   0,      # 本 ALERT 窗口已進場次數（上限 DC_MAX_ENTRIES_PER_ALERT）
        "last_entry_ts": 0.0,    # 上次進場時間戳（冷卻用）
    }


def get_or_init_dc_state(symbol: str) -> dict:
    if symbol not in models.death_cross_state:
        models.death_cross_state[symbol] = _init_dc_state()
    return models.death_cross_state[symbol]


def _reset_to_idle(symbol: str, reason: str = "") -> None:
    st = get_or_init_dc_state(symbol)
    st["phase"]       = DeathCrossPhase.IDLE
    st["alert_time"]  = None
    st["close_t0"]    = None
    st["entry_count"] = 0
    if reason:
        log.info(f"[DC] {symbol} → IDLE ({reason})")


def _reset_to_watching(symbol: str, reason: str = "") -> None:
    st = get_or_init_dc_state(symbol)
    st["phase"]       = DeathCrossPhase.WATCHING
    st["alert_time"]  = None
    st["close_t0"]    = None
    st["entry_count"] = 0
    if reason:
        log.info(f"[DC] {symbol} → WATCHING ({reason})")


def _enter_alert(symbol: str, close_t0: float, alert_time: float) -> None:
    st = get_or_init_dc_state(symbol)
    st["phase"]       = DeathCrossPhase.ALERT
    st["alert_time"]  = alert_time
    st["close_t0"]    = close_t0
    st["entry_count"] = 0
    log.info(f"[DC] {symbol} → ALERT (Close_T0={close_t0:.6f})")


# ─── Daily K 棒處理 ────────────────────────────────────────────────────────────

def on_new_daily_candle(symbol: str, candle: tuple) -> None:
    """處理每根 Daily 收盤 K 棒，驅動 Layer 1/2 狀態轉換。

    candle = (open_time_ms, open, high, low, close, quote_volume)
    """
    st    = get_or_init_dc_state(symbol)
    phase = st["phase"]

    ema50  = get_daily_ema(symbol, DC_DAILY_EMA_FAST)
    ema200 = get_daily_ema(symbol, DC_DAILY_EMA_SLOW)

    # 資料不足，無法判斷
    if ema50 is None or ema200 is None:
        return

    # Layer 1 檢查：EMA50 < EMA200？
    layer1_ok = ema50 < ema200

    if not layer1_ok:
        if phase != DeathCrossPhase.IDLE:
            _reset_to_idle(symbol, "EMA50 穿越 EMA200 上方，格局失效")
        return

    # Layer 1 成立
    if phase == DeathCrossPhase.IDLE:
        _reset_to_watching(symbol, "EMA50 < EMA200 格局確立")
        phase = DeathCrossPhase.WATCHING

    close        = candle[4]
    candle_ts    = candle[0] / 1000  # ms → 秒

    if phase == DeathCrossPhase.WATCHING:
        # Layer 2 觸發：日線收盤跌破 EMA200？
        if close < ema200:
            _try_enter_alert(symbol, candle, ema200, candle_ts)

    elif phase == DeathCrossPhase.ALERT:
        close_t0   = st["close_t0"]
        alert_time = st["alert_time"]

        # 48H 到期？
        if candle_ts - alert_time > DC_ALERT_WINDOW_HOURS * 3600:
            _reset_to_watching(symbol, "48H 監控窗口到期")
            return

        # 幅度超限：日線收盤超過 Close_T0 × DC_PRICE_RECOVERY_PCT？
        if close_t0 and close > close_t0 * DC_PRICE_RECOVERY_PCT:
            _reset_to_watching(symbol, f"日線回漲超過 {(DC_PRICE_RECOVERY_PCT - 1) * 100:.0f}%")


def _try_enter_alert(symbol: str, candle: tuple, ema200: float, candle_ts: float) -> None:
    """檢查 Layer 2 的時效性條件，通過則進入 ALERT 窗口。"""
    ohlc = models.symbol_state.get(symbol, {}).get("kline_daily_ohlc")
    if not ohlc:
        return

    candles_list = list(ohlc)

    # 檢查 A：尋找距今最近一根 close > EMA200 的日線 K 棒（排除當根）
    last_above_ts = None
    for c in reversed(candles_list[:-1]):
        if c[4] > ema200:
            last_above_ts = c[0] / 1000  # ms → 秒
            break

    if last_above_ts is None:
        log.debug(f"[DC] {symbol} Layer2 時效檢查：找不到近期高於 EMA200 的日線，略過")
        return

    time_diff_h = (candle_ts - last_above_ts) / 3600
    if time_diff_h > DC_MAX_ROLLBACK_HOURS:
        # log.info(f"[DC] {symbol} Layer2 時效檢查失敗：距上次 close > EMA200 已 {time_diff_h:.1f}H")
        return

    # 檢查 B：幅度（T0 當下為起點，回漲 0%，永遠通過；後續由 on_new_daily_candle 持續監控）
    close = candle[4]
    _enter_alert(symbol, close, candle_ts)
    log.info(f"[DC] {symbol} Layer2 通過，距上次高於 EMA200: {time_diff_h:.1f}H")


# ─── 1H K 棒處理 ──────────────────────────────────────────────────────────────

def on_new_1h_candle(symbol: str, candle: tuple) -> dict | None:
    """處理每根 1H 收盤 K 棒，在 ALERT 狀態下偵測 Type 3 進場信號。

    candle = (open_time_ms, open, high, low, close, quote_volume)
    回傳訊號 dict 或 None。
    """
    st = get_or_init_dc_state(symbol)

    if st["phase"] != DeathCrossPhase.ALERT:
        return None

    alert_time = st["alert_time"]
    close_t0   = st["close_t0"]
    candle_ts  = candle[0] / 1000  # ms → 秒
    close      = candle[4]

    # 48H 窗口到期？
    if candle_ts - alert_time > DC_ALERT_WINDOW_HOURS * 3600:
        _reset_to_watching(symbol, "48H 監控窗口到期（1H 觸發）")
        return None

    # 幅度超限？
    if close_t0 and close > close_t0 * DC_PRICE_RECOVERY_PCT:
        _reset_to_watching(symbol, f"價格回漲超限（1H 觸發）")
        return None

    # 進場次數上限？
    if st["entry_count"] >= DC_MAX_ENTRIES_PER_ALERT:
        return None

    # 冷卻中？
    if candle_ts - st["last_entry_ts"] < STRATEGY_COOLDOWN:
        return None

    # 取 1H EMA200
    ema200_1h = get_1h_ema(symbol, DC_1H_EMA_PERIOD)
    if ema200_1h is None:
        return None

    # 信號偵測（優先 A，其次 B）
    signal_meta = _check_signal_a(symbol, candle, ema200_1h)
    if signal_meta is None:
        signal_meta = _check_signal_b(symbol, candle, ema200_1h)

    if signal_meta is None:
        return None

    # ATR 止損計算
    atr_14_1h = get_1h_atr(symbol, DC_1H_ATR_PERIOD)
    if atr_14_1h is None:
        log.warning(f"[DC] {symbol} 1H ATR 資料不足，略過此信號")
        return None

    stop_loss = ema200_1h + atr_14_1h

    # 更新狀態
    st["entry_count"]   += 1
    st["last_entry_ts"]  = candle_ts

    signal = {
        "type":               "type3",
        "strategy":           "death_cross_short",
        "symbol":             symbol,
        "close":              close,
        "stop_loss":          stop_loss,
        "signal_type":        signal_meta["signal_type"],
        "ema200_1h":          ema200_1h,
        "atr_14_1h":          atr_14_1h,
        "close_t0":           close_t0,
        "vol_ratio":          signal_meta["vol_ratio"],
        "candle_open_time_ms": candle[0],
    }
    log.info(
        f"[DC] {symbol} Type3 信號 ({signal_meta['signal_type']}) "
        f"進場 #{st['entry_count']} close={close:.6f} SL={stop_loss:.6f}"
    )
    return signal


def _check_signal_a(symbol: str, candle: tuple, ema200_1h: float) -> dict | None:
    """信號 A：拒絕蠟燭（Rejection Candle）。

    條件：
      1. high > EMA200(1H)       — 上影線刺穿 EMA200
      2. close < EMA200(1H)      — 收盤壓回 EMA200 下方
      3. (EMA200 - close) / EMA200 >= DC_REJECTION_BODY_PCT  — 至少 0.5% 壓制幅度
      4. close < open            — 陰線實體向下
    """
    open_, high, _, close, vol = candle[1], candle[2], candle[3], candle[4], candle[5]

    if not (
        high > ema200_1h
        and close < ema200_1h
        and (ema200_1h - close) / ema200_1h >= DC_REJECTION_BODY_PCT
        and close < open_
    ):
        return None

    # 計算相對前根的量能倍數（供訊號顯示用）
    vol_ratio = _prev_vol_ratio(symbol, vol)
    return {"signal_type": "rejection", "vol_ratio": vol_ratio}


def _check_signal_b(symbol: str, candle: tuple, ema200_1h: float) -> dict | None:
    """信號 B：吞噬型態（Engulfing Pattern）。

    條件：
      1. close < EMA200(1H)
      2. open > close(prev)                           — 跳空高開
      3. close < close(prev)                          — 收盤低於前根收盤
      4. |close - open| > |close_prev - open_prev| × DC_ENGULF_BODY_RATIO  — 實體吞噬
      5. volume > volume_prev × DC_ENGULF_VOLUME_RATIO                      — 帶量
    """
    ohlc = models.symbol_state.get(symbol, {}).get("kline_1h_ohlc")
    if not ohlc or len(ohlc) < 2:
        return None

    prev = list(ohlc)[-2]

    open_curr,  close_curr  = candle[1], candle[4]
    open_prev,  close_prev  = prev[1],   prev[4]
    vol_curr,   vol_prev    = candle[5], prev[5]

    curr_body = abs(close_curr - open_curr)
    prev_body = abs(close_prev - open_prev)

    if prev_body == 0 or vol_prev == 0:
        return None

    if not (
        close_curr < ema200_1h
        and open_curr > close_prev
        and close_curr < close_prev
        and curr_body > prev_body * DC_ENGULF_BODY_RATIO
        and vol_curr > vol_prev * DC_ENGULF_VOLUME_RATIO
    ):
        return None

    return {"signal_type": "engulfing", "vol_ratio": vol_curr / vol_prev}


def _prev_vol_ratio(symbol: str, current_vol: float) -> float:
    """計算當根相對前根的量能倍數，僅供顯示。"""
    ohlc = models.symbol_state.get(symbol, {}).get("kline_1h_ohlc")
    if not ohlc or len(ohlc) < 2:
        return 0.0
    prev_vol = list(ohlc)[-2][5]
    if prev_vol <= 0:
        return 0.0
    return current_vol / prev_vol


# ─── 歷史回播 ─────────────────────────────────────────────────────────────────

def replay_historical_daily_candles(symbol: str) -> None:
    """啟動時依序重播歷史 Daily K 棒，恢復死亡叉策略狀態。"""
    ohlc = models.symbol_state.get(symbol, {}).get("kline_daily_ohlc")
    if not ohlc:
        return
    for candle in ohlc:
        on_new_daily_candle(symbol, candle)
    phase = models.death_cross_state.get(symbol, {}).get("phase", DeathCrossPhase.IDLE)
    log.info(f"[DC] {symbol} 日線歷史回播完成，當前狀態: {phase.value}")
