import os
from dotenv import load_dotenv
load_dotenv()

"""專案設定與策略參數。

此模組負責：
- 從環境變數/`.env` 讀取 Telegram 必要參數（BOT_TOKEN/CHAT_ID）
- 集中管理告警策略門檻（價格/成交量/OI）與監控行為參數

注意：此檔的常數透過 models.runtime_config 供各模組共享，可在執行期透過 /config 指令動態修改。
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
PUMP_THRESHOLD          = 8      # 4h K 棒漲幅門檻（%），單根 (close-open)/open >= N%
CONSOLIDATION_MIN_HOURS = 16     # 最低盤整時數，滿足後進入 READY 狀態
BREAKOUT_VOLUME_MULT    = 4.5    # Type1 突破量能倍數（相對 48h 平均）
LOOKBACK_VOLUME_MULT    = 3.5    # Type1 回推放量序列門檻（低於此門檻即停止往回）
EMA_TOUCH_THRESHOLD     = 0.5   # Type2 EMA 觸碰容忍距離（%），low <= EMA × (1 + N%)
WICK_THRESHOLD          = 3      # Type2 有效收針：close > low × (1 + N%)
STRATEGY_RR_MIN         = 1.0   # Type2 最低盈虧比
STRATEGY_COOLDOWN       = 14400  # 策略告警冷卻秒數（4 小時）