import time
import asyncio
from ..setting.config import EXCLUDE_SYMBOLS, QUOTE_VOLUME, ALERT_COOLDOWN
from ..setting.models import running, symbol_state
from ..datacenter.binance_opendata import initialize_symbols
from .conditions import check_conditions
from .telegram_bot import send_alert
from ..extension.utils import setup_logging

log = setup_logging()

# ================== 監控條件邏輯 ==================

async def screen_and_alert(client):
    """掃描所有符合監控條件的幣種並發送告警。

    流程：
    1) 透過 futures_ticker 篩出符合 QUOTE_VOLUME / EXCLUDE_SYMBOLS 的合約
    2) 過濾出本地已初始化且資料齊全（price/oi）的幣種
    3) **只檢查有新 15m K 收盤的幣種**（避免中途誤判）
    4) 併發執行 check_conditions
    5) 對回傳有結果者呼叫 send_alert
    6) 檢查後重置 new_15m_kline flag
    """
    try:
        ticker24 = await client.futures_ticker()
        valid_symbols = [
            t["symbol"] for t in ticker24
            if t["symbol"].endswith("USDT")
            and float(t["quoteVolume"]) > QUOTE_VOLUME
            and not any(ex in t["symbol"] for ex in EXCLUDE_SYMBOLS)
        ]

        # 只檢查有新 15m K 收盤的幣種
        monitored = [
            s for s in valid_symbols
            if s in symbol_state
            and symbol_state[s]["last_price"] is not None
            and symbol_state[s]["last_oi"] is not None
            and time.time() - symbol_state[s]["monitor_start"] >= 60
            and symbol_state[s].get("new_15m_kline", False)  # 必須有新 15m K 收盤
        ]
        
        if not monitored:
            return  # 沒有新 K 收盤，直接返回

        tasks = [check_conditions(client, s) for s in monitored]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        alerted = 0
        for sym, res in zip(monitored, results):
            # 檢查後重置 flag
            symbol_state[sym]["new_15m_kline"] = False
            
            if isinstance(res, Exception):
                continue
            if res:
                if await send_alert(sym, res):
                    alerted += 1
        if alerted:
            log.info(f"結果：發送 {alerted} 則告警")

    except Exception as e:
        log.info(f"[篩選錯誤] {e}")

async def periodic_screen(client):
    """週期性執行 initialize_symbols + screen_and_alert。

    - initialize_symbols：動態更新監控清單並載入新幣種歷史資料
    - screen_and_alert：只在有新 15m K 收盤時檢查（00/15/30/45 分收盤後）
    - 每 10 秒掃描一次，但只有 15m K 收盤時才真正檢查條件
    """
    while running:
        await initialize_symbols(client)
        await screen_and_alert(client)
        await asyncio.sleep(10)