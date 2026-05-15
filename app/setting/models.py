from collections import defaultdict, deque

"""專案全域共享狀態（in-memory）。

此專案採用多個 async 任務並行更新資料（WebSocket/REST/掃描告警）。
為了讓各模組共享最新狀態，會把狀態集中在此模組：

- running: 全域 loop 開關
- symbol_state: 每個幣種的即時狀態（價格/EMA 等）
- price_history: 價格歷史（供 /s 指令查詢）
- bot: Telegram bot instance（初始化後寫入）
"""

# ================== 全域狀態 ==================
running = True
symbol_state = {}
price_history = defaultdict(lambda: deque(maxlen=100))
bot = None

# ================== 策略狀態 ==================
strategy_state = {}  # symbol → 策略狀態 dict（由 state_machine 管理）