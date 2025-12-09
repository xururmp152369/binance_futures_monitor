import asyncio
from collections import defaultdict, deque

# ================== 全域狀態 ==================
running = True
symbol_state = {}
price_history = defaultdict(lambda: deque(maxlen=100)) # 多增加10個長度(緩衝)
oi_history = defaultdict(lambda: deque(maxlen=370)) # 多增加10個長度(緩衝)
last_alert = defaultdict(float)
bot = None
semaphore = asyncio.Semaphore(20)