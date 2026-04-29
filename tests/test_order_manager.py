"""市價開倉 + -1007 重試邏輯測試"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.trading.order_manager import _place_orders_for_user


# ─── 共用設定 ──────────────────────────────────────────────────────────────────

_CFG = {
    "API_KEY":          "test_key",
    "SECRET_KEY":       "test_secret",
    "STRATEGY":         ["TYPE1"],
    "RISK_TYPE":        0,
    "RISK_AMOUNT":      100.0,
    "RISK_LEVERAGE":    10,
    "TP_STRATEGY":      [{"RR_RATIO": 2.0, "PERCENT": 50}],
    "ORDER_LIMIT":      10,
    "ADD_SAME_SYMBOL":  False,
    "SYMBOL_BLACKLIST": [],
    "ENABLED":          True,
}

_SIGNAL = {"type": "type1", "close": 100.0, "stop_loss": 95.0}
_SYMBOL = "BTCUSDT"


@pytest.fixture(autouse=True)
def skip_sleep():
    """跳過 asyncio.sleep，避免測試等待。"""
    with patch("asyncio.sleep", new=AsyncMock()):
        yield


def _make_client(**overrides) -> AsyncMock:
    client = AsyncMock()
    client.futures_exchange_info.return_value = {
        "symbols": [{"symbol": _SYMBOL, "quantityPrecision": 3}]
    }
    client.futures_position_information.return_value = []
    client.futures_change_leverage.return_value = {}
    client.close_connection.return_value = None
    for attr, val in overrides.items():
        setattr(client, attr, val)
    return client


def run(coro):
    return asyncio.run(coro)


# ─── 測試案例 ──────────────────────────────────────────────────────────────────

def _get_order_kwargs(client, call_index: int) -> dict:
    """取得第 N 次 futures_create_order 呼叫的 kwargs。"""
    return client.futures_create_order.call_args_list[call_index].kwargs


def test_normal_success():
    """正常路徑：市價開倉成功，SL 用 closePosition，單筆 TP 用 closePosition。"""
    client = _make_client(
        futures_create_order=AsyncMock(
            return_value={"avgPrice": "100.0", "orderId": 1}
        )
    )
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        run(_place_orders_for_user(_CFG, _SYMBOL, _SIGNAL))

    # 市價開倉 1 + 止損 1 + 止盈 1 = 3
    assert client.futures_create_order.call_count == 3

    sl_kwargs = _get_order_kwargs(client, 1)
    assert sl_kwargs["type"] == "STOP_MARKET"
    assert sl_kwargs.get("closePosition") is True
    assert "quantity" not in sl_kwargs

    tp_kwargs = _get_order_kwargs(client, 2)
    assert tp_kwargs["type"] == "TAKE_PROFIT_MARKET"
    assert tp_kwargs.get("closePosition") is True
    assert "quantity" not in tp_kwargs


def test_multi_tp_last_is_close_position():
    """多筆 TP：非最後一筆用 quantity+reduceOnly，最後一筆用 closePosition。"""
    cfg_multi = {**_CFG, "TP_STRATEGY": [
        {"RR_RATIO": 1.0, "PERCENT": 50},
        {"RR_RATIO": 2.0, "PERCENT": 50},
    ]}
    client = _make_client(
        futures_create_order=AsyncMock(
            return_value={"avgPrice": "100.0", "orderId": 1}
        )
    )
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        run(_place_orders_for_user(cfg_multi, _SYMBOL, _SIGNAL))

    # 市價1 + 止損1 + 止盈2 = 4
    assert client.futures_create_order.call_count == 4

    tp1_kwargs = _get_order_kwargs(client, 2)
    assert tp1_kwargs.get("reduceOnly") is True
    assert "quantity" in tp1_kwargs
    assert "closePosition" not in tp1_kwargs

    tp2_kwargs = _get_order_kwargs(client, 3)
    assert tp2_kwargs.get("closePosition") is True
    assert "quantity" not in tp2_kwargs


def test_1007_query_shows_filled():
    """-1007 逾時後查詢訂單 = FILLED，不重新開倉，繼續設 SL/TP。"""
    client = _make_client(
        futures_create_order=AsyncMock(
            side_effect=[
                Exception("APIError(code=-1007): Timeout"),  # 開倉逾時
                {"avgPrice": "100.0"},                        # 止損
                {"avgPrice": "100.0"},                        # 止盈
            ]
        ),
        futures_get_order=AsyncMock(
            return_value={"status": "FILLED", "avgPrice": "100.0"}
        ),
    )
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        run(_place_orders_for_user(_CFG, _SYMBOL, _SIGNAL))

    assert client.futures_create_order.call_count == 3  # 開倉1 + 止損1 + 止盈1
    assert client.futures_get_order.call_count == 1


def test_1007_retry_succeeds_on_second_attempt():
    """-1007 且訂單未成立，第 2 次重試開倉成功，正常設 SL/TP。"""
    client = _make_client(
        futures_create_order=AsyncMock(
            side_effect=[
                Exception("APIError(code=-1007): Timeout"),  # 第1次：逾時
                {"avgPrice": "100.0", "orderId": 2},          # 第2次：成功
                {"avgPrice": "100.0"},                        # 止損
                {"avgPrice": "100.0"},                        # 止盈
            ]
        ),
        futures_get_order=AsyncMock(
            return_value={"status": "NEW", "avgPrice": "0"}  # 訂單未成立
        ),
    )
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        run(_place_orders_for_user(_CFG, _SYMBOL, _SIGNAL))

    assert client.futures_create_order.call_count == 4  # 開倉2 + 止損1 + 止盈1
    assert client.futures_get_order.call_count == 1


def test_1007_all_5_retries_exhausted():
    """-1007 重試 5 次全部失敗，放棄開倉，不設 SL/TP。"""
    client = _make_client(
        futures_create_order=AsyncMock(
            side_effect=Exception("APIError(code=-1007): Timeout")
        ),
        futures_get_order=AsyncMock(
            return_value={"status": "NEW", "avgPrice": "0"}
        ),
    )
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        run(_place_orders_for_user(_CFG, _SYMBOL, _SIGNAL))

    assert client.futures_create_order.call_count == 5
    assert client.futures_get_order.call_count == 5


def test_other_error_aborts_immediately():
    """非 -1007 錯誤不重試，立即放棄，不設 SL/TP。"""
    client = _make_client(
        futures_create_order=AsyncMock(
            side_effect=Exception("APIError(code=-2019): Margin is insufficient")
        ),
    )
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        run(_place_orders_for_user(_CFG, _SYMBOL, _SIGNAL))

    assert client.futures_create_order.call_count == 1  # 只嘗試 1 次


# ─── 實際下單整合測試（打 testnet，需要有效 API Key） ──────────────────────────

_USER_CONFIG_PATH = Path(__file__).parent.parent / "data" / "users" / "5746757471.json"


@pytest.mark.skipif(not _USER_CONFIG_PATH.exists(), reason="使用者設定檔不存在")
def test_real_btcusdt_order():
    """實際下單：讀 5746757471.json，BTCUSDT 市價開倉，SL = 市價 × 95%。"""
    cfg = json.loads(_USER_CONFIG_PATH.read_text(encoding="utf-8"))

    async def _run():
        from binance import AsyncClient as _Client
        # 取得 BTCUSDT 當前標記價格（公開資料，不需 API Key）
        pub = await _Client.create(testnet=True)
        try:
            ticker = await pub.futures_mark_price(symbol="BTCUSDT")
            mark_price = float(ticker["markPrice"])
        finally:
            await pub.close_connection()

        stop_loss = round(mark_price * 0.95, 2)
        signal = {"type": "type1", "close": mark_price, "stop_loss": stop_loss}
        print(f"\n  mark_price={mark_price:.2f}  stop_loss={stop_loss:.2f}")

        await _place_orders_for_user(cfg, "BTCUSDT", signal)

    run(_run())
