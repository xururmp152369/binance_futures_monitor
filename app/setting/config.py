import os
from dotenv import load_dotenv
load_dotenv()

"""專案設定與策略參數。

此模組負責：
- 從環境變數/`.env` 讀取 Telegram 必要參數（BOT_TOKEN/CHAT_ID）
- 集中管理告警策略門檻（價格/成交量/OI）與監控行為參數
"""
# ================== 設定 ==================
CHAT_ID        = os.getenv("CHAT_ID")
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
CONSOLIDATION_MIN_HOURS   = 12     # 最低盤整時數（從最後一次創新高起算）→ READY
METHOD_B_GAIN_ADVANTAGE   = 1.0    # Method B：新觸發 K 漲幅需超過前觸發 K 漲幅 + N%
# ── Type 1 進場訊號（15m 帶量突破） ────────────────────────────────────────────
BREAKOUT_VOLUME_MULT      = 4.5    # Type1 量能倍數（相對前 192 根 15m 均量）
BREAKOUT_BODY_PCT         = 0.005  # Type1 實體超頂幅度（0.5%）：close > top × (1 + N)
LOOKBACK_VOLUME_MULT      = 3      # Type1 回掃止損放量門檻（低於此倍數即停止往回）
# ── 冷卻 ────────────────────────────────────────────────────────────────────────
STRATEGY_COOLDOWN         = 14400  # 策略告警冷卻秒數（4 小時）