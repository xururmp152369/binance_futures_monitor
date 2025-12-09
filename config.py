
# ================== 設定 ==================
CHAT_ID = 5746757471
BOT_TOKEN = "8327041039:AAH3dt2gWAlJ3e82iM9AM4fHzw6C8Rej3eQ"
EXCLUDE_SYMBOLS = {"BUSD", "USDC", "TUSD", "DAI"}

# ================== 參數 ==================
OI_THRESHOLD = 8 # 持倉量變化百分比
PRICE_THRESHOLD = 6 # 價格異動百分比
VOLUME_THRESHOLD = 5 # 成交量倍數
QUOTE_VOLUME = 8_000_000 # 24h成交量額
ALERT_COOLDOWN = 3600 # 同一幣種告警冷卻時間
BATCH_SIZE = 20 # 批次數量
RESTART_INTERVAL = 900 # 固定重啟秒數