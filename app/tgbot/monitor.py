import asyncio
import time
from ..setting.models import running, symbol_state
from ..datacenter.binance_opendata import initialize_symbols
from ..strategy.state_machine import scan_strategy
from ..user.user_config import check_expired_sessions
from ..extension.utils import setup_logging

log = setup_logging()

_SESSION_CHECK_INTERVAL = 3600  # 每小時檢查一次 session 過期


async def periodic_screen(client):
    """週期性任務：更新幣種清單 + 即時廢棄策略狀態機 + session 過期檢查。

    - initialize_symbols：動態新增/移除監控幣種，並為新幣種載入歷史資料
    - scan_strategy：即時廢棄檢查（markPrice 跌破盤整底部時重置策略狀態）
    - check_expired_sessions：每小時掃描 session 過期帳號，停用自動開單並通知
    """
    last_session_check = time.time()

    while running:
        await initialize_symbols(client)
        for sym in list(symbol_state.keys()):
            scan_strategy(sym)

        if time.time() - last_session_check >= _SESSION_CHECK_INTERVAL:
            last_session_check = time.time()
            from ..setting import models
            if models.bot:
                try:
                    await check_expired_sessions(models.bot)
                except Exception as e:
                    log.error(f"Session 過期檢查失敗: {e}")

        await asyncio.sleep(10)
