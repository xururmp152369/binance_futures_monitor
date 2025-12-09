import asyncio
import time
import talib
import numpy as np
from binance import BinanceSocketManager
from config import EXCLUDE_SYMBOLS, BATCH_SIZE, RESTART_INTERVAL, QUOTE_VOLUME
from models import symbol_state, semaphore, running, price_history, oi_history, last_alert
from utils import setup_logging
from collections import deque

log = setup_logging()

# ================== 合約幣對 初始化 ==================

async def initialize_symbols(client):
    try:
        ticker24 = await client.futures_ticker()
        valid = set()
        for t in ticker24:
            s = t["symbol"]
            if (s.endswith("USDT") 
                and float(t["quoteVolume"]) >= QUOTE_VOLUME # 24h 成交量
                and not any(ex in s for ex in EXCLUDE_SYMBOLS)):
                valid.add(s)

        now = time.time()
        for s in valid:
            if s not in symbol_state:
                symbol_state[s] = {
                    "last_price": None,
                    "last_oi": None,
                    "funding_rate": 0.0,
                    "monitor_start": now - 120,
                    "volume_5m": deque(maxlen=240),
                    "last_kline_close_time": 0,  # 避免重複處理同一根
                    "kline_1h_closes": deque(maxlen=100),
                    "ema_1h": {15: None, 30: None, 45: None, 60: None},
                    "kline_4h_closes": deque(maxlen=100),
                    "ema_4h": {15: None, 30: None, 45: None, 60: None},
                }

        for s in list(symbol_state):
            if s not in valid:
                symbol_state.pop(s, None)
                price_history.pop(s, None)
                oi_history.pop(s, None)
                last_alert.pop(s, None)

    except:
        pass

# ================== 合約幣對 持倉量監控 ==================

async def update_open_interest(client):
    while running:
        symbols = list(symbol_state.keys())
        if not symbols:
            await asyncio.sleep(60)
            continue

        for i in range(0, len(symbols), 50):
            batch = symbols[i:i+50]
            tasks = [fetch_oi(client, sym) for sym in batch]
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(1)
        await asyncio.sleep(60)

async def fetch_oi(client, sym):
    async with semaphore:
        data = await client.futures_open_interest(symbol=sym)
        oi = float(data["openInterest"])
        now = time.time()
        state = symbol_state[sym]
        state["last_oi"] = oi

        hist = oi_history[sym]
        if not hist or now - hist[-1][0] >= 10:
            hist.append((now, oi))

# ================== 合約幣對 價格K棒監控 ==================

async def handle_price_websocket(client, batch_symbols):
    bm = BinanceSocketManager(client, user_timeout=60)
    # 同時訂閱 markPrice + kline_1m
    streams = []
    for sym in batch_symbols:
        s = sym.lower()
        streams.append(f"{s}@markPrice") # 價格
        streams.append(f"{s}@kline_5m") # 5分K棒
        streams.append(f"{s}@kline_1h") # 1小K棒
        streams.append(f"{s}@kline_4h") # 4小K棒
    try:
        async with bm.futures_multiplex_socket(streams) as stream:
            while running:
                try:
                    msg = await stream.recv()
                    if not msg or "data" not in msg:
                        continue

                    stream_name = msg["stream"]
                    data = msg["data"]

                    if stream_name.endswith("@markPrice"):
                        sym = data["s"].upper()
                        if sym not in symbol_state:
                            continue

                        price = float(data["p"])
                        fund = float(data["r"]) * 100
                        state = symbol_state[sym]
                        state["last_price"] = price
                        state["funding_rate"] = fund

                        now = time.time()
                        hist = price_history[sym]
                        if not hist or now - hist[-1][0] >= 10:
                            hist.append((now, price))
                        # === 處理 5m K線（成交量）===
                    elif stream_name.endswith("@kline_5m"):
                        k = data["k"]
                        sym = k["s"]
                        if sym not in symbol_state:
                            continue

                        # 只有收盤的K才處理（x=True）
                        if not k["x"]:
                            continue

                        close_time = k["T"] // 1000  # 毫秒 → 秒
                        state = symbol_state[sym]

                        # 避免重複處理同一根K（Binance 會重發）
                        if close_time <= state["last_kline_close_time"]:
                            continue

                        quote_vol = float(k["q"])  # quoteVolume（USDT量）
                        state["volume_5m"].append(quote_vol)
                        state["last_kline_close_time"] = close_time
                    elif stream_name.endswith("@kline_4h") or stream_name.endswith("@kline_1h"):
                        k = data["k"]
                        sym = k["s"]
                        interval = k["i"]
                        if sym not in symbol_state: continue
                        if not k["x"]: continue  # 只處理收盤

                        close_price = float(k["c"])
                        state = symbol_state[sym]

                        # 超簡單一行：自動丟最舊
                        state[f"kline_{interval}_closes"].append(close_price)

                        # 轉成 numpy array（talib 必備）
                        closes = np.array(state[f"kline_{interval}_closes"])

                        # 一行算出所有 EMA
                        if len(closes) >= 60:  # 至少要有 60 根才算 EMA60
                            state[f"ema_{interval}"][15] = talib.EMA(closes, timeperiod=15)[-1]
                            state[f"ema_{interval}"][30] = talib.EMA(closes, timeperiod=30)[-1]
                            state[f"ema_{interval}"][45] = talib.EMA(closes, timeperiod=45)[-1]
                            state[f"ema_{interval}"][60] = talib.EMA(closes, timeperiod=60)[-1]
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.error(f"接收錯誤: {e}")
                    break
    except Exception as e:
        log.error(f"Price WebSocket 連線失敗: {e}")

async def monitor_price_websocket(client):
    log.info("啟動 Price WebSocket 監控...")
    while running:
        try:
            symbols = list(symbol_state.keys())
            if not symbols:
                log.warning("symbol_state 為空，10 秒後重試...")
                await asyncio.sleep(10)
                continue

            batches = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
            log.info(f"🚀 啟動 {len(batches)} 個 Price WebSocket 批次（共 {len(symbols)} 幣）")

            tasks = []
            for idx, batch in enumerate(batches):
                tasks.append(asyncio.create_task(handle_price_websocket(client, batch)))

            log.info(f"✅ 所有 {len(tasks)} 個批次已啟動，持續監控中...")

            # 定期重啟（例如每15分鐘）
            await asyncio.sleep(RESTART_INTERVAL)

            log.info("♻️ 開始重啟所有 Price WebSocket 連線...")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info("🔄 所有批次已結束，準備重新啟動...")

        except Exception as e:
            log.error(f"Price WebSocket 總錯誤: {e}")
            await asyncio.sleep(10)
