import asyncio
from collections import defaultdict, deque

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
"""

# ================== 全域狀態 ==================
running = True
symbol_state = {}
price_history = defaultdict(lambda: deque(maxlen=100)) # 多增加10個長度(緩衝)
oi_history = defaultdict(lambda: deque(maxlen=370)) # 多增加10個長度(緩衝)
last_alert = defaultdict(float)
bot = None
semaphore = asyncio.Semaphore(20)