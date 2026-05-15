import asyncio
import time
from binance import BinanceSocketManager
from ..setting.config import EXCLUDE_SYMBOLS, BATCH_SIZE, QUOTE_VOLUME
from ..setting.models import symbol_state, running, price_history, strategy_state
from ..extension.utils import setup_logging
from collections import deque
from ..strategy.state_machine import (
    on_new_4h_candle,
    on_new_15m_candle,
    replay_historical_4h_candles,
)
from ..strategy.strategy_alerts import send_strategy_alert, send_order_results
from ..trading.order_manager import place_orders_for_all_users

log = setup_logging()


async def _fire_signal(sym: str, signal: dict) -> None:
    """發送策略訊號、執行自動下單、對每位使用者回報開單結果。"""
    await send_strategy_alert(sym, signal)
    results = await place_orders_for_all_users(sym, signal)
    await send_order_results(sym, results)


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

async def _fetch_klines(client, symbol: str, interval: str, limit: int, max_retries: int = 3) -> list:
    """從 Binance futures API 取得原始 K 線資料，含重試與 rate limit 退避。"""
    for retry in range(max_retries):
        try:
            return await client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        except Exception as e:
            if _is_rate_limit_error(str(e)):
                await _sleep_rate_limit_backoff(retry, max_retries)
                continue
            log.error(f"載入 {symbol} {interval} K 線失敗: {e}")
            break
    return []


async def load_historical_klines_ohlc(client, symbol: str, interval: str, limit: int = 100) -> list:
    """載入歷史 OHLC 供策略狀態機使用。

    4h/15m → [(open_time_ms, open, high, low, close, quote_volume), ...]
    """
    klines = await _fetch_klines(client, symbol, interval, limit)
    return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[7])) for k in klines]

async def load_historical_data_batch(client, symbols):
    """批次載入新幣種的歷史資料。

    會依序載入：1h/4h close + EMA，以及策略用 15m/1h/4h OHLC。

    為避免觸發 API 限制，內部使用 `hist_semaphore` 控制並發，並在每個 API 呼叫後 sleep。

    Side effects:
        會直接填充 `symbol_state[symbol]` 內的 deque（kline_*_closes / kline_*_ohlc）與 ema_*。
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
                    # 載入策略用 OHLC（4h / 15m）
                    ohlc_4h = await load_historical_klines_ohlc(client, symbol, "4h", 50)
                    if ohlc_4h:
                        symbol_state[symbol]["kline_4h_ohlc"].extend(ohlc_4h)

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
                and float(t["quoteVolume"]) >= QUOTE_VOLUME # 24h 成交量
                and not any(s.endswith(ex) for ex in EXCLUDE_SYMBOLS)):
                valid.add(s)

        new_symbols = []
        
        for s in valid:
            if s not in symbol_state:
                # 先建立基本結構
                symbol_state[s] = {
                    "last_price": None,
                    "funding_rate": 0.0,
                    "last_kline_close_time_15m": 0,
                    "kline_4h_ohlc":  deque(maxlen=50),
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
                strategy_state.pop(s, None)
                log.info(f"幣種 {s} 已清理")   

    except Exception as e:
        log.error(f"初始化幣種失敗: {e}")

# ================== WebSocket Stream 處理函式 ==================

def _handle_mark_price(data: dict) -> None:
    """更新即時價格、資金費率與 price_history。"""
    sym = data["s"].upper()
    if sym not in symbol_state:
        return
    state = symbol_state[sym]
    state["last_price"]   = float(data["p"])
    state["funding_rate"] = float(data["r"]) * 100
    now  = time.time()
    hist = price_history[sym]
    if not hist or now - hist[-1][0] >= 10:
        hist.append((now, state["last_price"]))


def _handle_kline_15m(data: dict) -> tuple | None:
    """收盤 15m K 棒：去重、存 OHLC。回傳 (sym, candle) 或 None。"""
    k   = data["k"]
    sym = k["s"]
    if sym not in symbol_state or not k["x"]:
        return None
    state      = symbol_state[sym]
    close_time = k["T"] // 1000
    if close_time <= state["last_kline_close_time_15m"]:
        return None
    state["last_kline_close_time_15m"] = close_time
    candle = (int(k["t"]), float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["q"]))
    state["kline_15m_ohlc"].append(candle)
    return sym, candle


def _handle_kline_4h(data: dict) -> None:
    """收盤 4h K 棒：存 OHLC、驅動策略狀態機。"""
    k   = data["k"]
    sym = k["s"]
    if sym not in symbol_state or not k["x"]:
        return
    state  = symbol_state[sym]
    candle = (int(k["t"]), float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["q"]))
    state["kline_4h_ohlc"].append(candle)
    on_new_4h_candle(sym, candle)


# ================== 合約幣對 價格K棒監控 ==================

async def handle_price_websocket(client, batch_symbols):
    """建立並維持一個 WebSocket multiplex 連線，接收一批 symbols 的即時資料。

    訂閱 stream：
    - markPrice：更新最新價格、資金費率、並寫入 price_history
    - kline_15m：K 線收盤後儲存 OHLC，觸發 Type1 突破檢查
    - kline_4h：K 線收盤後儲存 OHLC，驅動策略狀態機

    斷線後（Binance 每 24h 關閉、網路中斷）會自動重連，直到 running=False 為止。
    """
    streams = []
    for sym in batch_symbols:
        s = sym.lower()
        streams.append(f"{s}@markPrice")
        streams.append(f"{s}@kline_15m")
        streams.append(f"{s}@kline_4h")

    reconnect_delay = 5  # 初始重連等待秒數

    while running:
        bm = BinanceSocketManager(client, user_timeout=60)
        try:
            async with bm.futures_multiplex_socket(streams) as stream:
                reconnect_delay = 5  # 連線成功後重置退避
                last_stream_name = None
                last_symbol      = None
                last_interval    = None
                while running:
                    try:
                        msg = await stream.recv()
                        if not msg or "data" not in msg:
                            continue

                        stream_name      = msg["stream"]
                        last_stream_name = stream_name
                        data             = msg["data"]

                        if stream_name.endswith("@markPrice"):
                            last_symbol = data["s"].upper()
                            _handle_mark_price(data)

                        elif stream_name.endswith("@kline_15m"):
                            last_symbol, last_interval = data["k"]["s"], "15m"
                            result = _handle_kline_15m(data)
                            if result:
                                sym, candle = result
                                signal = on_new_15m_candle(sym, candle)
                                if signal:
                                    asyncio.create_task(_fire_signal(sym, signal))

                        elif stream_name.endswith("@kline_4h"):
                            last_symbol, last_interval = data["k"]["s"], "4h"
                            _handle_kline_4h(data)

                    except asyncio.CancelledError:
                        log.info(f"批次 WebSocket 收到取消信號 | batch_symbols={batch_symbols[:3]}...")
                        return
                    except Exception as e:
                        log.error(
                            f"接收訊息時發生錯誤（繼續運行）: {e} | "
                            f"last_stream={last_stream_name} last_symbol={last_symbol} last_interval={last_interval}"
                        )
                        await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            log.info(f"批次 WebSocket 外層被取消 | batch_symbols={batch_symbols[:3]}...")
            raise
        except Exception as e:
            log.error(f"Price WebSocket 連線中斷: {e} | batch_symbols={batch_symbols[:3]}...")

        if running:
            log.info(f"批次 WebSocket 將於 {reconnect_delay} 秒後重連 | batch_symbols={batch_symbols[:3]}...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)  # 指數退避，上限 60 秒

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
