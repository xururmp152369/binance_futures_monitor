import asyncio
import time
import talib
import numpy as np
from binance import BinanceSocketManager
from ..setting.config import EXCLUDE_SYMBOLS, BATCH_SIZE
from ..setting.models import symbol_state, semaphore, running, price_history, oi_history, last_alert, strategy_state, runtime_config
from ..extension.utils import setup_logging
from collections import deque
from ..strategy.state_machine import on_new_4h_candle, on_new_1h_candle, on_new_15m_candle, on_new_15m_spike, replay_historical_4h_candles
from ..strategy.strategy_alerts import send_strategy_alert

log = setup_logging()

def _is_rate_limit_error(error_msg: str) -> bool:
    """判斷錯誤訊息是否屬於 Binance API rate limit 類型。"""
    if not error_msg:
        return False
    msg = error_msg.lower()
    return "429" in msg or "rate limit" in msg or "too many requests" in msg


async def _sleep_rate_limit_backoff(retry: int, max_retries: int):
    """依重試次數做退避等待，用於 rate limit 情境。"""
    wait_time = (retry + 1) * 5
    log.warning(f"API 限制觸發，等待 {wait_time} 秒後重試 ({retry + 1}/{max_retries})")
    await asyncio.sleep(wait_time)

# ================== 歷史資料載入函數 ==================

async def load_historical_klines(client, symbol, interval, limit=100, max_retries=3):
    """載入指定週期的歷史 K 線收盤價並計算 EMA。

    主要用於初始化新幣種時，補足 1h/4h close 序列與 EMA(15/30/45/60)。
    內含簡易重試與 API rate limit 的退避等待。

    Args:
        client: Binance AsyncClient
        symbol: 合約幣種（例如 BTCUSDT）
        interval: K 線週期（例如 "1h" / "4h"）
        limit: 取得 K 線數量
        max_retries: 最多重試次數

    Returns:
        (closes, emas):
            closes: list[float] 的收盤價
            emas: dict，包含 {15,30,45,60} 四條 EMA 的最後一個值（資料不足則為 None）
    """
    for retry in range(max_retries):
        try:
            # 獲取歷史K線
            klines = await client.futures_klines(
                symbol=symbol,
                interval=interval,
                limit=limit
            )
            
            # 提取收盤價
            closes = [float(k[4]) for k in klines]  # k[4] 是收盤價
            
            # 計算 EMA
            emas = {}
            if len(closes) >= 60:
                closes_array = np.array(closes)
                emas[15] = talib.EMA(closes_array, timeperiod=15)[-1]
                emas[30] = talib.EMA(closes_array, timeperiod=30)[-1]
                emas[45] = talib.EMA(closes_array, timeperiod=45)[-1]
                emas[60] = talib.EMA(closes_array, timeperiod=60)[-1]
            else:
                emas = {15: None, 30: None, 45: None, 60: None}
                
            return closes, emas
            
        except Exception as e:
            error_msg = str(e).lower()
            
            # 檢查是否為 API 限制錯誤
            if _is_rate_limit_error(error_msg):
                await _sleep_rate_limit_backoff(retry, max_retries)
                continue
            else:
                log.error(f"載入 {symbol} {interval} 歷史資料失敗: {e}")
                break
    
    return [], {15: None, 30: None, 45: None, 60: None}

async def load_historical_klines_ohlc(client, symbol, interval, limit=100, max_retries=3):
    """載入歷史 K 線的完整 OHLC 資料，供策略狀態機使用。

    Args:
        client: Binance AsyncClient
        symbol: 合約幣種（例如 BTCUSDT）
        interval: K 線週期（"15m" / "1h" / "4h"）
        limit: 取得 K 線根數
        max_retries: 最多重試次數

    Returns:
        list[tuple]:
            4h/1h → [(open_time_ms, open, high, low, close), ...]
            15m   → [(open_time_ms, open, high, low, close, quote_volume), ...]
        時間由舊到新排列。失敗時回傳空 list。
    """
    for retry in range(max_retries):
        try:
            klines = await client.futures_klines(
                symbol=symbol,
                interval=interval,
                limit=limit,
            )
            result = []
            for k in klines:
                t = int(k[0])  # open_time_ms
                o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
                if interval == "15m":
                    vol = float(k[7])  # quoteVolume（USDT）
                    result.append((t, o, h, l, c, vol))
                else:
                    result.append((t, o, h, l, c))
            return result

        except Exception as e:
            error_msg = str(e).lower()
            if _is_rate_limit_error(error_msg):
                await _sleep_rate_limit_backoff(retry, max_retries)
                continue
            log.error(f"載入 {symbol} {interval} OHLC 歷史資料失敗: {e}")
            break

    return []

async def load_historical_volume(client, symbol, limit=192, max_retries=3):
    """載入歷史15分鐘成交量資料，包含重試機制。

    注意：Binance `futures_klines` 的單次 `limit` 有上限（常見為 1500），
    因此需要用 endTime 分頁往前抓取，拼出最近的 48h（192 根 15m K）。

    Args:
        client: Binance AsyncClient
        symbol: 合約幣種（例如 BTCUSDT）
        limit: 需要的 15m 根數（例如 48h = 192）
        max_retries: 每一頁最多重試次數

    Returns:
        list[float]: 依時間由舊到新排列的 quoteVolume(USDT) 序列。
        可能少於 limit（例如幣種上市時間較短或 API 回傳不足）。
    """
    max_limit_per_request = 1500
    need = int(limit)
    if need <= 0:
        return []

    volumes = []
    end_time_ms = int(time.time() * 1000)

    while need > 0:
        req_limit = min(max_limit_per_request, need)
        for retry in range(max_retries):
            try:
                klines = await client.futures_klines(
                    symbol=symbol,
                    interval="15m",
                    limit=req_limit,
                    endTime=end_time_ms,
                )

                if not klines:
                    return volumes

                # klines: [ [open_time, o, h, l, c, v, close_time, quoteVol, ...], ... ]
                # 依時間由舊到新，這裡要把更舊的一段塞到最前面
                chunk = [float(k[7]) for k in klines]
                volumes = chunk + volumes

                need -= len(klines)
                first_open_time = int(klines[0][0])
                end_time_ms = first_open_time - 1
                break

            except Exception as e:
                error_msg = str(e).lower()

                if _is_rate_limit_error(error_msg):
                    await _sleep_rate_limit_backoff(retry, max_retries)
                    continue

                log.error(f"載入 {symbol} 成交量歷史資料失敗: {e}")
                return volumes
        else:
            return volumes

    return volumes

async def load_historical_data_batch(client, symbols):
    """批次載入新幣種的歷史資料。

    會依序載入：
    - 1h close + EMA
    - 4h close + EMA
    - 15m quoteVolume（用於成交量 vs 48h baseline）

    為避免觸發 API 限制，內部使用 `hist_semaphore` 控制並發，並在每個 API 呼叫後 sleep。

    Side effects:
        會直接填充 `symbol_state[symbol]` 內的 deque（kline_*_closes / volume_15m）與 ema_*。
    """
    if not symbols:
        return
    
    # 更保守的並發限制
    hist_semaphore = asyncio.Semaphore(3)
    
    # 分批處理，每批最多 10 個幣種
    batch_size = 10
    total_batches = len(symbols) // batch_size + (1 if len(symbols) % batch_size else 0)
    
    log.info(f"將分 {total_batches} 批處理 {len(symbols)} 個幣種的歷史資料")
    
    for batch_idx in range(total_batches):
        start_idx = batch_idx * batch_size
        end_idx = min(start_idx + batch_size, len(symbols))
        batch_symbols = symbols[start_idx:end_idx]
        
        log.info(f"處理第 {batch_idx + 1}/{total_batches} 批：{len(batch_symbols)} 個幣種")
        
        async def load_symbol_data(symbol):
            async with hist_semaphore:
                try:
                    # 載入 1 小時 K 線
                    closes_1h, emas_1h = await load_historical_klines(client, symbol, "1h", 100)
                    if closes_1h:
                        symbol_state[symbol]["kline_1h_closes"].extend(closes_1h)
                        symbol_state[symbol]["ema_1h"] = emas_1h
                    
                    # API 呼叫間隔
                    await asyncio.sleep(0.5)
                    
                    # 載入 4 小時 K 線
                    closes_4h, emas_4h = await load_historical_klines(client, symbol, "4h", 100)
                    if closes_4h:
                        symbol_state[symbol]["kline_4h_closes"].extend(closes_4h)
                        symbol_state[symbol]["ema_4h"] = emas_4h
                    
                    # API 呼叫間隔
                    await asyncio.sleep(0.5)
                    
                    # 載入 15 分鐘成交量
                    volumes = await load_historical_volume(client, symbol, 192)
                    if volumes:
                        symbol_state[symbol]["volume_15m"].extend(volumes)

                    await asyncio.sleep(0.5)

                    # 載入策略用 OHLC（4h / 1h / 15m）
                    ohlc_4h = await load_historical_klines_ohlc(client, symbol, "4h", 50)
                    if ohlc_4h:
                        symbol_state[symbol]["kline_4h_ohlc"].extend(ohlc_4h)

                    await asyncio.sleep(0.5)

                    ohlc_1h = await load_historical_klines_ohlc(client, symbol, "1h", 100)
                    if ohlc_1h:
                        symbol_state[symbol]["kline_1h_ohlc"].extend(ohlc_1h)

                    await asyncio.sleep(0.5)

                    ohlc_15m = await load_historical_klines_ohlc(client, symbol, "15m", 200)
                    if ohlc_15m:
                        symbol_state[symbol]["kline_15m_ohlc"].extend(ohlc_15m)

                    # 重播歷史 4h K 棒以恢復策略狀態
                    replay_historical_4h_candles(symbol)

                    log.info(f"✅ {symbol} 歷史資料載入完成")
                    
                except Exception as e:
                    log.error(f"❌ {symbol} 歷史資料載入失敗: {e}")
                
                # 每個幣種完成後的間隔
                await asyncio.sleep(1.0)
        
        # 並發載入當前批次
        tasks = [load_symbol_data(symbol) for symbol in batch_symbols]
        await asyncio.gather(*tasks, return_exceptions=True)
        
        # 批次間的間隔
        if batch_idx < total_batches - 1:
            log.info(f"第 {batch_idx + 1} 批完成，等待 3 秒後處理下一批...")
            await asyncio.sleep(3.0)

# ================== 合約幣對 初始化 ==================

async def initialize_symbols(client):
    """初始化/更新監控幣種清單，並為新幣種建立 symbol_state 結構。

    流程：
    1) 透過 futures_ticker 篩選出符合：USDT 合約、QUOTE_VOLUME、且不在 EXCLUDE_SYMBOLS 的幣種
    2) 對新幣種建立 `symbol_state[s]` 的基礎欄位（價格/OI/成交量/EMA 容器）
    3) 對新幣種批次載入歷史資料（避免一開始資料不足）
    4) 清理已不符合條件的幣種（包含 history 與 cooldown state）
    """
    try:
        ticker24 = await client.futures_ticker()
        valid = set()
        for t in ticker24:
            s = t["symbol"]
            if (s.endswith("USDT") 
                and float(t["quoteVolume"]) >= runtime_config["QUOTE_VOLUME"] # 24h 成交量
                and not any(s.endswith(ex) for ex in EXCLUDE_SYMBOLS)):
                valid.add(s)

        now = time.time()
        new_symbols = []
        
        for s in valid:
            if s not in symbol_state:
                # 先建立基本結構
                symbol_state[s] = {
                    "last_price": None,
                    "last_oi": None,
                    "funding_rate": 0.0,
                    "monitor_start": now - 120,
                    "volume_15m": deque(maxlen=192),  # 48h = 192 根 15m K
                    "last_kline_close_time_15m": 0,  # 避免重複處理同一根
                    "new_15m_kline": False,  # 標記是否有新 15m K 收盤
                    "kline_1h_closes": deque(maxlen=100),
                    "ema_1h": {15: None, 30: None, 45: None, 60: None},
                    "kline_4h_closes": deque(maxlen=100),
                    "ema_4h": {15: None, 30: None, 45: None, 60: None},
                    # 策略用 OHLC deque
                    "kline_4h_ohlc":  deque(maxlen=50),
                    "kline_1h_ohlc":  deque(maxlen=100),
                    "kline_15m_ohlc": deque(maxlen=200),
                }
                new_symbols.append(s)

        # 批次載入新幣種的歷史資料
        if new_symbols:
            log.info(f"開始載入 {len(new_symbols)} 個新幣種的歷史資料...")
            await load_historical_data_batch(client, new_symbols)
            log.info(f"歷史資料載入完成")

        # 清理無效幣種
        for s in list(symbol_state):
            if s not in valid:
                log.info(f"幣種 {s} 不再符合條件，開始清理...")
                symbol_state.pop(s, None)
                price_history.pop(s, None)
                oi_history.pop(s, None)
                last_alert.pop(s, None)
                strategy_state.pop(s, None)
                log.info(f"幣種 {s} 已清理")   

    except Exception as e:
        log.error(f"初始化幣種失敗: {e}")

# ================== 合約幣對 持倉量監控 ==================

async def update_open_interest(client):
    """週期性更新所有監控幣種的持倉量（Open Interest）。

    - 會分批（每批 50）併發呼叫 `fetch_oi`
    - 每輪完成後 sleep 60 秒
    """
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
    """抓取單一幣種的 Open Interest 並更新到 state 與 history。

    Side effects:
        - `symbol_state[sym]["last_oi"]` 會被更新
        - `oi_history[sym]` 會以節流方式 append (timestamp, oi)
    """
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
    """建立並維持一個 WebSocket multiplex 連線，接收一批 symbols 的即時資料。

    訂閱 stream：
    - markPrice：更新最新價格、資金費率、並寫入 price_history
    - kline_15m：K 線收盤後寫入 volume_15m 並設定 new_15m_kline flag
    - kline_1h / kline_4h：K 線收盤後更新 close deque 並計算 EMA

    發生接收例外時會記錄最後一筆 stream/symbol/interval，方便排查。
    """
    bm = BinanceSocketManager(client, user_timeout=60)
    # 同時訂閱 markPrice + kline_15m + kline_1h/4h
    streams = []
    for sym in batch_symbols:
        s = sym.lower()
        streams.append(f"{s}@markPrice") # 價格
        streams.append(f"{s}@kline_15m") # 15分K棒
        streams.append(f"{s}@kline_1h") # 1小K棒
        streams.append(f"{s}@kline_4h") # 4小K棒
    try:
        async with bm.futures_multiplex_socket(streams) as stream:
            last_stream_name = None
            last_symbol = None
            last_interval = None
            while running:
                try:
                    msg = await stream.recv()
                    if not msg or "data" not in msg:
                        continue

                    stream_name = msg["stream"]
                    last_stream_name = stream_name
                    data = msg["data"]

                    if stream_name.endswith("@markPrice"):
                        sym = data["s"].upper()
                        last_symbol = sym
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
                        # === 處理 15m K線（成交量 + OHLC + 策略 Type1）===
                    elif stream_name.endswith("@kline_15m"):
                        k = data["k"]
                        sym = k["s"]
                        last_symbol = sym
                        last_interval = k.get("i")
                        if sym not in symbol_state:
                            continue

                        # 只有收盤的K才處理（x=True）
                        if not k["x"]:
                            continue

                        close_time = k["T"] // 1000  # 毫秒 → 秒
                        state = symbol_state[sym]

                        # 避免重複處理同一根K（Binance 會重發）
                        if close_time <= state["last_kline_close_time_15m"]:
                            continue

                        quote_vol = float(k["q"])  # quoteVolume（USDT量）
                        state["volume_15m"].append(quote_vol)
                        state["last_kline_close_time_15m"] = close_time
                        state["new_15m_kline"] = True  # 標記有新 15m K 收盤

                        # 策略：儲存 OHLC 並檢查 Type1 突破
                        candle_15m = (
                            int(k["t"]),       # open_time_ms（統一用開盤時間）
                            float(k["o"]),
                            float(k["h"]),
                            float(k["l"]),
                            float(k["c"]),
                            quote_vol,
                        )
                        state["kline_15m_ohlc"].append(candle_15m)

                        # Type 0：量價異動（原突破告警）
                        signal_t0 = on_new_15m_spike(sym, candle_15m)
                        if signal_t0:
                            asyncio.create_task(send_strategy_alert(sym, signal_t0))

                        # Type 1：帶量突破盤整頂部
                        signal_15m = on_new_15m_candle(sym, candle_15m)
                        if signal_15m:
                            asyncio.create_task(send_strategy_alert(sym, signal_15m))

                    # === 處理 4h K線（EMA + OHLC + 策略狀態機）===
                    elif stream_name.endswith("@kline_4h"):
                        k = data["k"]
                        sym = k["s"]
                        last_symbol = sym
                        last_interval = k.get("i")
                        if sym not in symbol_state:
                            continue
                        if not k["x"]:
                            continue

                        close_price = float(k["c"])
                        state = symbol_state[sym]
                        state["kline_4h_closes"].append(close_price)

                        closes = np.array(state["kline_4h_closes"])
                        if len(closes) >= 60:
                            state["ema_4h"][15] = talib.EMA(closes, timeperiod=15)[-1]
                            state["ema_4h"][30] = talib.EMA(closes, timeperiod=30)[-1]
                            state["ema_4h"][45] = talib.EMA(closes, timeperiod=45)[-1]
                            state["ema_4h"][60] = talib.EMA(closes, timeperiod=60)[-1]

                        # 策略：儲存 OHLC 並驅動狀態機
                        candle_4h = (
                            int(k["t"]),       # open_time_ms
                            float(k["o"]),
                            float(k["h"]),
                            float(k["l"]),
                            close_price,
                        )
                        state["kline_4h_ohlc"].append(candle_4h)
                        on_new_4h_candle(sym, candle_4h)

                    # === 處理 1h K線（EMA + OHLC + 策略 Type2）===
                    elif stream_name.endswith("@kline_1h"):
                        k = data["k"]
                        sym = k["s"]
                        last_symbol = sym
                        last_interval = k.get("i")
                        if sym not in symbol_state:
                            continue
                        if not k["x"]:
                            continue

                        close_price = float(k["c"])
                        state = symbol_state[sym]
                        state["kline_1h_closes"].append(close_price)

                        closes = np.array(state["kline_1h_closes"])
                        if len(closes) >= 60:
                            state["ema_1h"][15] = talib.EMA(closes, timeperiod=15)[-1]
                            state["ema_1h"][30] = talib.EMA(closes, timeperiod=30)[-1]
                            state["ema_1h"][45] = talib.EMA(closes, timeperiod=45)[-1]
                            state["ema_1h"][60] = talib.EMA(closes, timeperiod=60)[-1]

                        # 策略：儲存 OHLC 並檢查 Type2 反彈
                        candle_1h = (
                            int(k["t"]),       # open_time_ms
                            float(k["o"]),
                            float(k["h"]),
                            float(k["l"]),
                            close_price,
                        )
                        state["kline_1h_ohlc"].append(candle_1h)
                        signal_1h = on_new_1h_candle(sym, candle_1h)
                        if signal_1h:
                            asyncio.create_task(send_strategy_alert(sym, signal_1h))
                except asyncio.CancelledError:
                    log.info(f"批次 WebSocket 收到取消信號 | batch_symbols={batch_symbols[:3]}...")
                    break
                except Exception as e:
                    # 記錄錯誤但不退出，繼續嘗試接收下一筆訊息
                    log.error(
                        f"接收訊息時發生錯誤（繼續運行）: {e} | last_stream={last_stream_name} last_symbol={last_symbol} last_interval={last_interval}"
                    )
                    # 短暫延遲避免錯誤循環
                    await asyncio.sleep(0.1)
            # 退出 while 循環後，準備關閉 WebSocket
            log.info(f"批次 WebSocket 退出接收循環，準備關閉連線 | batch_symbols={batch_symbols[:3]}...")
    except asyncio.CancelledError:
        log.info(f"批次 WebSocket 外層被取消 | batch_symbols={batch_symbols[:3]}...")
        raise
    except Exception as e:
        log.exception(f"Price WebSocket 連線失敗: {e} | batch_symbols={batch_symbols}")
    finally:
        log.info(f"批次 WebSocket 已完全退出 | batch_symbols={batch_symbols[:3]}...")

async def monitor_price_websocket(client):
    """啟動並監控所有 symbols 的 WebSocket 任務。

    行為：
    - 依 `BATCH_SIZE` 分批啟動多個 `handle_price_websocket` task
    - 持續運行直到程式結束（running=False）
    - 若某個批次異常退出，會記錄錯誤並等待其他批次
    """
    log.info("啟動 Price WebSocket 監控...")
    
    symbols = list(symbol_state.keys())
    if not symbols:
        log.error("symbol_state 為空，無法啟動 WebSocket 監控")
        return

    batches = [symbols[i:i + BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    log.info(f"🚀 啟動 {len(batches)} 個 Price WebSocket 批次（共 {len(symbols)} 幣）")

    tasks = []
    for idx, batch in enumerate(batches):
        tasks.append(asyncio.create_task(handle_price_websocket(client, batch)))

    log.info(f"✅ 所有 {len(tasks)} 個批次已啟動，持續監控中...")

    try:
        # 等待所有任務完成（正常情況下會持續運行直到程式結束）
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 檢查是否有異常
        exception_count = 0
        for idx, r in enumerate(results):
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                exception_count += 1
                log.error(f"批次 {idx} 異常退出: {type(r).__name__}: {r}")
        
        if exception_count > 0:
            log.warning(f"⚠️ 共 {exception_count} 個批次異常退出")
        else:
            log.info("所有 WebSocket 批次已正常結束")
            
    except Exception as e:
        log.exception(f"Price WebSocket 監控發生錯誤: {e}")
