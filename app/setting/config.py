import os
from dotenv import load_dotenv
load_dotenv()

"""專案設定與策略參數。

此模組負責：
- 從環境變數/`.env` 讀取 Telegram 必要參數（BOT_TOKEN/CHAT_ID）
- 集中管理告警策略門檻（價格/成交量/OI）與監控行為參數
"""
# ================== 設定 ==================
CHAT_ID        = int(os.getenv("CHAT_ID")) if os.getenv("CHAT_ID") else None
BOT_TOKEN      = os.getenv("BOT_TOKEN")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
EXCLUDE_SYMBOLS = {"USDC"}

# ================== 參數 ==================
QUOTE_VOLUME = 0 # 24h成交量額（USDT）
BATCH_SIZE = 20 # WebSocket 批次數量

# ================== 多頭策略參數（long_breakout / Type 1）==================
# ── 觸發 K 棒（單根 4h 帶量陽線） ──────────────────────────────────────────────
# PUMP_THRESHOLD 依 BTC 24h 漲幅動態選擇
PUMP_THRESHOLD_BULL        = 3.5   # BTC 牛市（24h > BTC_BULL_THRESHOLD）時的觸發漲幅門檻（%）
PUMP_THRESHOLD_NORMAL      = 3.0   # BTC 震盪市時的觸發漲幅門檻（%）
PUMP_THRESHOLD_BEAR        = 2.5   # BTC 熊市（24h < BTC_BEAR_THRESHOLD）時的觸發漲幅門檻（%）
BTC_BULL_THRESHOLD         = 3.0   # 判定牛市：BTC 1d K 漲幅超過此值（%）
BTC_BEAR_THRESHOLD         = -3.0  # 判定熊市：BTC 1d K 跌幅低於此值（%）
TRIGGER_VOLUME_MULT        = 3     # 觸發 K 量能倍數（當根量需 > 前 N 根均量 × 此值，嚴格 >）
TRIGGER_VOLUME_BASELINE_N  = 12    # 觸發量能基準根數（前 N 根 4h K 棒，排除當根）
PUMP_BODY_RATIO            = 0.75  # 觸發 K 實體佔幅：body/(high-low) ≥ 此值
# ── 盤整 ────────────────────────────────────────────────────────────────────────
CONSOLIDATION_MIN_HOURS      = 12   # 最低盤整時數（從最後一次創新高起算）→ READY
METHOD_B_RELAXED_THRESHOLD   = 10.0 # 前觸發 K 漲幅超過此值時，Method B 直接完整重置（不比較漲幅）
METHOD_B_VOLUME_RATIO        = 0.8  # Method B 體量驗證：新 K volume / 前觸發 K volume 最低比例
# ── Type 1 進場訊號（15m 帶量突破）─────────────────────────────────────────────
BREAKOUT_VOLUME_MULT      = 3.5    # 突破量能倍數（相對前 192 根 15m 均量，>=）
BREAKOUT_BODY_PCT         = 0.005  # 實體超頂幅度（0.5%）：close > top × (1 + N)
BREAKOUT_BODY_RATIO       = 0.75   # 進場 K 實體強度：(close-open)/(high-low) ≥ 此值
BREAKOUT_ATR_PERIOD       = 14     # ATR 週期（4h K 棒根數）
BREAKOUT_ATR_RATIO        = 0.30   # 突破 ATR 力度：close - top ≥ ATR × 此值
LOOKBACK_VOLUME_MULT      = 2.5    # 止損回掃放量門檻倍數（非連續，回掃所有放量 K）
BREAKOUT_RISK_PCT_MIN     = 1.0    # 止損距離下限（%），過窄訊號止損易被掃
BREAKOUT_RISK_PCT_MAX     = 15.0    # 止損距離上限（%），過寬訊號盈虧比差
# ── 集體觸發過濾 ──────────────────────────────────────────────────────────────
BATCH_SIGNAL_LIMIT        = 3      # 同一 15m 視窗觸發 ≥ 此數筆，整批進入冷卻
# ── 冷卻（三層機制） ──────────────────────────────────────────────────────────
STRATEGY_COOLDOWN                = 14400  # 全局告警冷卻秒數（4h），三層冷卻第二層
LIQUIDATION_BUFFER_CONFIRM_COUNT = 3      # 即時廢棄連續幾次掃描確認才執行（每次 10 秒）

# ================== 死亡叉策略參數（Type 3）==================
# ── Layer 1/2 日線均線 ───────────────────────────────────────────────────────────
DC_DAILY_EMA_FAST        = 50      # Layer 1：短均線週期（EMA50，日線）
DC_DAILY_EMA_SLOW        = 200     # Layer 1/2：長均線週期（EMA200，日線）
# ── Layer 3 1H 均線與 ATR ────────────────────────────────────────────────────────
DC_1H_EMA_PERIOD         = 200     # 1H 壓制均線週期（EMA200，1H）
DC_1H_ATR_PERIOD         = 14      # 止損 ATR 週期（1H）
# ── 時效性與幅度保護 ─────────────────────────────────────────────────────────────
DC_ALERT_WINDOW_HOURS    = 48      # 48H 監控窗口（日線觸發後有效時間）
DC_MAX_ROLLBACK_HOURS    = 48      # 時效性：距上次 close > EMA200 不超過此小時數
DC_PRICE_RECOVERY_PCT    = 1.10    # 幅度保護：日線收盤超過 Close_T0 × 此值則廢棄信號
# ── 進場條件 ────────────────────────────────────────────────────────────────────
DC_MAX_ENTRIES_PER_ALERT = 2       # 每 ALERT 窗口最大進場次數
DC_REJECTION_BODY_PCT    = 0.005   # 信號 A：收盤低於 EMA200(1H) 至少 0.5%
DC_ENGULF_BODY_RATIO     = 1.5     # 信號 B：當根實體需 > 前根實體 × 此值
DC_ENGULF_VOLUME_RATIO   = 1.5     # 信號 B：量能需 > 前根 × 此值
# ── 止損距離過濾 ────────────────────────────────────────────────────────────────
DC_RISK_PCT_MIN          = 3.0     # 止損距離下限（%），過窄的訊號勝率低
DC_RISK_PCT_MAX          = 12.0    # 止損距離上限（%），過寬的訊號 EV 差

# ================== Fibonacci 策略參數（fibonacci_long / fibonacci_short）==================
FIB_K_INTERVAL    = "4h"   # K 線週期（"15m" / "1h" / "4h"）
FIB_EMA_PERIOD    = 100     # EMA 週期（barA/barB 收盤需在 EMA 同側）
FIB_CONFIRM_LEVEL = 1.73   # bar5/bar8 影線確認層級（Fib 倍數）
FIB_TP1_LEVEL     = 6.92   # TP1 止盈層級（Fib 倍數）
FIB_MAX_SL_PCT    = 5.0    # 止損距離上限（%），超過此值的形態不發訊號