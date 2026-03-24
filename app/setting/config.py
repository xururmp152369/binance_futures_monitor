import os
from dotenv import load_dotenv
load_dotenv()

"""專案設定與策略參數。

此模組負責：
- 從環境變數/`.env` 讀取 Telegram 必要參數（BOT_TOKEN/CHAT_ID）
- 集中管理告警策略門檻（價格/成交量/OI）與監控行為參數

注意：此檔的常數會被多個模組 import（conditions/monitor/binance_opendata），
調整門檻後不需要改其他程式碼。
"""
# ================== 設定 ==================
CHAT_ID = os.getenv("CHAT_ID") 
BOT_TOKEN = os.getenv("BOT_TOKEN")
EXCLUDE_SYMBOLS = {"BUSD", "USDC", "TUSD", "DAI"}

# ================== 參數 ==================
OI_THRESHOLD = 8 # 持倉量變化百分比
PRICE_THRESHOLD = 2 # 價格異動百分比
VOLUME_THRESHOLD = 6 # 成交量倍數
QUOTE_VOLUME = 6_000_000 # 24h成交量額
ALERT_COOLDOWN = 3600 # 同一幣種告警冷卻時間
BATCH_SIZE = 20 # 批次數量
RESTART_INTERVAL = 3600 # 固定重啟秒數