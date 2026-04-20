import os
from dotenv import load_dotenv
load_dotenv()

"""專案設定與策略參數。

此模組負責：
- 從環境變數/`.env` 讀取 Telegram 必要參數（BOT_TOKEN/CHAT_ID）
- 集中管理告警策略門檻（價格/成交量/OI）與監控行為參數

重要邏輯說明:
- 成交量監控：使用 15m K 線，檢查最新一根是否高於 48h 平均 × VOLUME_THRESHOLD
- 觸發時機：只在 15m K 收盤時（00/15/30/45 分）檢查條件，避免中途誤判
- 冷卻機制：基於 15m K 收盤時間間隔，確保同一幣種至少間隔 N 根 K 線才再次告警

注意：此檔的常數會被多個模組 import（conditions/monitor/binance_opendata），調整門檻後不需要改其他程式碼。
"""
# ================== 設定 ==================
CHAT_ID = os.getenv("CHAT_ID") 
BOT_TOKEN = os.getenv("BOT_TOKEN")
EXCLUDE_SYMBOLS = {"USDC"}

# ================== 參數 ==================
OI_THRESHOLD = 8 # 持倉量變化百分比
PRICE_THRESHOLD = 3 # 價格異動百分比
VOLUME_THRESHOLD = 7 # 成交量倍數
QUOTE_VOLUME = 0 # 24h成交量額
ALERT_COOLDOWN = 7200 # 同一幣種告警冷卻時間（秒）
BATCH_SIZE = 20 # WebSocket 批次數量

# ================== 策略參數 ==================
PUMP_THRESHOLD          = 8      # 4h K 棒漲幅門檻（%），單根 (close-open)/open >= N%
CONSOLIDATION_MIN_HOURS = 12     # 最低盤整時數，滿足後進入 READY 狀態
BREAKOUT_VOLUME_MULT    = 3      # Type1 突破量能倍數（相對 48h 平均）
EMA_TOUCH_THRESHOLD     = 0.5   # Type2 EMA 觸碰容忍距離（%），low <= EMA × (1 + N%)
WICK_THRESHOLD          = 2      # Type2 有效收針：close > low × (1 + N%)
STRATEGY_RR_MIN         = 1.0   # Type2 最低盈虧比
STRATEGY_COOLDOWN       = 14400  # 策略告警冷卻秒數（4 小時）