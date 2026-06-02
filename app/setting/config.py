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

# ================== 策略參數 ==================
# ── 觸發 K 棒（單根 4h 帶量陽線） ──────────────────────────────────────────────
PUMP_THRESHOLD            = 3      # 觸發 K 單根漲幅門檻（%）：(close-open)/open × 100
TRIGGER_VOLUME_MULT       = 3      # 觸發 K 量能倍數（當根量需 > 前 N 根均量 × 此值）
TRIGGER_VOLUME_BASELINE_N = 12     # 觸發量能基準根數（前 N 根 4h K 棒，排除當根）
# ── 盤整 ────────────────────────────────────────────────────────────────────────
CONSOLIDATION_MIN_HOURS      = 12   # 最低盤整時數（從最後一次創新高起算）→ READY
METHOD_B_GAIN_ADVANTAGE      = 10.0 # Method B：新觸發 K 漲幅需超過前觸發 K 漲幅 × (1 + N/100)，即比例優勢 N%
METHOD_B_RELAXED_THRESHOLD   = 10.0 # 原始觸發 K 漲幅超過此值時，Method B 不需 N+1%，滿足觸發條件即完整重置
# ── Type 1 進場訊號（15m 帶量突破） ────────────────────────────────────────────
BREAKOUT_VOLUME_MULT      = 3.5    # Type1 量能倍數（相對前 192 根 15m 均量）
BREAKOUT_BODY_PCT         = 0.005  # Type1 實體超頂幅度（0.5%）：close > top × (1 + N)
LOOKBACK_VOLUME_MULT      = 2.5    # Type1 回掃止損放量門檻（低於此倍數即停止往回）
# ── 冷卻 ────────────────────────────────────────────────────────────────────────
STRATEGY_COOLDOWN         = 14400  # 策略告警冷卻秒數（4 小時）

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