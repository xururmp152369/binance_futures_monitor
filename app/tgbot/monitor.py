import asyncio
from ..setting.models import running, symbol_state
from ..datacenter.binance_opendata import initialize_symbols
from ..strategy.state_machine import scan_strategy
from ..extension.utils import setup_logging

log = setup_logging()


async def periodic_screen(client):
    """週期性任務：更新幣種清單 + 即時廢棄策略狀態機。

    - initialize_symbols：動態新增/移除監控幣種，並為新幣種載入歷史資料
    - scan_strategy：即時廢棄檢查（markPrice 跌破盤整底部時重置策略狀態）
    - 告警偵測已全部移至 WebSocket handler（kline 收盤時即時觸發）
    """
    while running:
        await initialize_symbols(client)
        for sym in list(symbol_state.keys()):
            scan_strategy(sym)
        await asyncio.sleep(10)
