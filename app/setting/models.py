import asyncio
from collections import defaultdict, deque
from .config import (
    OI_THRESHOLD, PRICE_THRESHOLD, VOLUME_THRESHOLD, ALERT_COOLDOWN,
    PUMP_THRESHOLD, CONSOLIDATION_MIN_HOURS, BREAKOUT_VOLUME_MULT,
    EMA_TOUCH_THRESHOLD, WICK_THRESHOLD, STRATEGY_RR_MIN, STRATEGY_COOLDOWN,
    QUOTE_VOLUME,
)

"""專案全域共享狀態（in-memory）。

此專案採用多個 async 任務並行更新資料（WebSocket/REST/掃描告警）。
為了讓各模組共享最新狀態，會把狀態集中在此模組：

- running: 全域 loop 開關
- symbol_state: 每個幣種的即時狀態（價格/OI/成交量/EMA 等）
- price_history: 價格歷史（用於 15 分鐘漲跌判斷）
- oi_history: OI 歷史（用於 1 小時變化判斷）
- last_alert: 上次告警時間（冷卻）
- bot: Telegram bot instance（初始化後寫入）
- semaphore: 限制 REST API 併發
- runtime_config: 可在執行期透過 /config 指令動態調整的參數
"""

# ================== 全域狀態 ==================
running = True
symbol_state = {}
price_history = defaultdict(lambda: deque(maxlen=100)) # 多增加10個長度(緩衝)
oi_history = defaultdict(lambda: deque(maxlen=370)) # 多增加10個長度(緩衝)
last_alert = defaultdict(float)
bot = None
semaphore = asyncio.Semaphore(20)

# ================== 策略狀態 ==================
strategy_state = {}                       # symbol → 策略狀態 dict（由 state_machine 管理）
strategy_last_alert = defaultdict(float)  # 備用冷卻追蹤

# ================== 執行期可調整參數 ==================
# 從 config.py 初始化；可透過 /config set PARAM VALUE 動態修改，重啟後還原預設值
runtime_config: dict = {
    # --- 量價異動告警（Type 0）---
    "OI_THRESHOLD":            OI_THRESHOLD,            # OI 1小時變化門檻 (%)
    "PRICE_THRESHOLD":         PRICE_THRESHOLD,          # 15m 價格異動門檻 (%)
    "VOLUME_THRESHOLD":        VOLUME_THRESHOLD,         # 成交量倍數門檻 (×均值)
    "ALERT_COOLDOWN":          ALERT_COOLDOWN,           # 量價告警冷卻 (秒)
    # --- 幣種篩選 ---
    "QUOTE_VOLUME":            QUOTE_VOLUME,             # 24h 最低成交量 (USDT)
    # --- 策略狀態機 ---
    "PUMP_THRESHOLD":          PUMP_THRESHOLD,           # 4h 拉漲門檻 (%)
    "CONSOLIDATION_MIN_HOURS": CONSOLIDATION_MIN_HOURS,  # 最低盤整時數 (h)
    "BREAKOUT_VOLUME_MULT":    BREAKOUT_VOLUME_MULT,     # Type1 突破量能倍數
    "EMA_TOUCH_THRESHOLD":     EMA_TOUCH_THRESHOLD,      # Type2 EMA 觸碰容忍 (%)
    "WICK_THRESHOLD":          WICK_THRESHOLD,           # Type2 有效收針 (%)
    "STRATEGY_RR_MIN":         STRATEGY_RR_MIN,          # Type2 最低盈虧比
    "STRATEGY_COOLDOWN":       STRATEGY_COOLDOWN,        # 策略告警冷卻 (秒)
}