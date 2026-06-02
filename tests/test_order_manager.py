"""市價開倉 + -1007 重試 + 多空方向邏輯測試"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.trading.order_manager import _place_orders_for_user, place_orders_for_all_users


# ─── 共用設定 ──────────────────────────────────────────────────────────────────

_CFG = {
    "API_KEY":           "test_key",
    "SECRET_KEY":        "test_secret",
    "DEV_STRATEGY":      ["long_breakout"],
    "RISK_TYPE":         0,
    "RISK_AMOUNT":       100.0,
    "RISK_LEVERAGE":     10,
    "LONG_TP_STRATEGY":  [{"RR_RATIO": 2.0, "PERCENT": 50}],
    "LONG_ORDER_LIMIT":  10,
    "ADD_SAME_SYMBOL":   False,
    "SYMBOL_BLACKLIST":  [],
    "ENABLED":           True,
}

_CFG_SHORT = {
    **_CFG,
    "DEV_STRATEGY":      ["death_cross_short"],
    "SHORT_ORDER_LIMIT": 5,
}

_SIGNAL_LONG  = {"type": "type1", "close": 100.0, "stop_loss": 95.0}
_SIGNAL_SHORT = {"type": "type3", "close": 100.0, "stop_loss": 105.0}
_SYMBOL       = "BTCUSDT"


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


def _get_order_kwargs(client, call_index: int) -> dict:
    """取得第 N 次 futures_create_order 呼叫的 kwargs。"""
    return client.futures_create_order.call_args_list[call_index].kwargs


# ─── 多頭開倉 ──────────────────────────────────────────────────────────────────

def test_normal_success():
    """正常路徑：市價開倉成功，SL 用 closePosition，單筆 TP 用 closePosition。"""
    client = _make_client(
        futures_create_order=AsyncMock(
            return_value={"avgPrice": "100.0", "orderId": 1}
        )
    )
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        run(_place_orders_for_user("test_account", _CFG, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    # 市價開倉 1 + 止損 1 + 止盈 1 = 3
    assert client.futures_create_order.call_count == 3

    entry_kwargs = _get_order_kwargs(client, 0)
    assert entry_kwargs["side"] == "BUY"

    sl_kwargs = _get_order_kwargs(client, 1)
    assert sl_kwargs["type"] == "STOP_MARKET"
    assert sl_kwargs["side"] == "SELL"
    assert sl_kwargs.get("closePosition") is True
    assert "quantity" not in sl_kwargs

    tp_kwargs = _get_order_kwargs(client, 2)
    assert tp_kwargs["type"] == "TAKE_PROFIT_MARKET"
    assert tp_kwargs["side"] == "SELL"
    assert tp_kwargs.get("closePosition") is True
    assert "quantity" not in tp_kwargs


def test_long_tp_price_above_entry():
    """多頭止盈價格應高於入場價。"""
    captured = []
    async def record_order(**kwargs):
        captured.append(kwargs)
        return {"avgPrice": "100.0", "orderId": 1}

    client = _make_client(futures_create_order=AsyncMock(side_effect=record_order))
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        run(_place_orders_for_user("test_account", _CFG, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    tp_kwargs = captured[2]
    assert tp_kwargs["stopPrice"] > 100.0


def test_multi_tp_last_is_close_position():
    """多筆 TP：非最後一筆用 quantity+reduceOnly，最後一筆用 closePosition。"""
    cfg_multi = {**_CFG, "LONG_TP_STRATEGY": [
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
        run(_place_orders_for_user("test_account", cfg_multi, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    # 市價1 + 止損1 + 止盈2 = 4
    assert client.futures_create_order.call_count == 4

    tp1_kwargs = _get_order_kwargs(client, 2)
    assert tp1_kwargs.get("reduceOnly") is True
    assert "quantity" in tp1_kwargs
    assert "closePosition" not in tp1_kwargs

    tp2_kwargs = _get_order_kwargs(client, 3)
    assert tp2_kwargs.get("closePosition") is True
    assert "quantity" not in tp2_kwargs


# ─── 空頭開倉 ──────────────────────────────────────────────────────────────────

def test_short_signal_uses_sell_entry():
    """type3 訊號 → 市價 SELL 開倉，止損/止盈用 BUY。"""
    client = _make_client(
        futures_create_order=AsyncMock(
            return_value={"avgPrice": "100.0", "orderId": 1}
        )
    )
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        run(_place_orders_for_user("test_account", _CFG_SHORT, _SYMBOL, _SIGNAL_SHORT, use_prd=False))

    assert client.futures_create_order.call_count == 3

    entry_kwargs = _get_order_kwargs(client, 0)
    assert entry_kwargs["side"] == "SELL"

    sl_kwargs = _get_order_kwargs(client, 1)
    assert sl_kwargs["side"] == "BUY"

    tp_kwargs = _get_order_kwargs(client, 2)
    assert tp_kwargs["side"] == "BUY"


def test_short_tp_price_below_entry():
    """空頭止盈價格應低於入場價（fill_price - sl_dist * rr）。"""
    captured = []
    async def record_order(**kwargs):
        captured.append(kwargs)
        return {"avgPrice": "100.0", "orderId": 1}

    client = _make_client(futures_create_order=AsyncMock(side_effect=record_order))
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        run(_place_orders_for_user("test_account", _CFG_SHORT, _SYMBOL, _SIGNAL_SHORT, use_prd=False))

    tp_kwargs = captured[2]
    assert tp_kwargs["stopPrice"] < 100.0


def test_short_uses_short_tp_strategy():
    """SHORT_TP_STRATEGY 存在時應使用它，而非 LONG_TP_STRATEGY。"""
    cfg = {
        **_CFG_SHORT,
        "LONG_TP_STRATEGY":  [{"RR_RATIO": 3.0, "PERCENT": 100}],
        "SHORT_TP_STRATEGY": [{"RR_RATIO": 1.5, "PERCENT": 100}],
    }
    captured = []
    async def record_order(**kwargs):
        captured.append(kwargs)
        return {"avgPrice": "100.0", "orderId": 1}

    client = _make_client(futures_create_order=AsyncMock(side_effect=record_order))
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        run(_place_orders_for_user("test_account", cfg, _SYMBOL, _SIGNAL_SHORT, use_prd=False))

    # sl_dist = |100 - 105| = 5，SHORT_TP rr=1.5 → tp = 100 - 5*1.5 = 92.5
    tp_price = captured[2]["stopPrice"]
    assert abs(tp_price - 92.5) < 0.01


def test_short_falls_back_to_long_tp_strategy():
    """SHORT_TP_STRATEGY 未設定時，空頭應 fallback 使用 LONG_TP_STRATEGY。"""
    cfg = {**_CFG_SHORT}  # 沒有 SHORT_TP_STRATEGY
    captured = []
    async def record_order(**kwargs):
        captured.append(kwargs)
        return {"avgPrice": "100.0", "orderId": 1}

    client = _make_client(futures_create_order=AsyncMock(side_effect=record_order))
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        run(_place_orders_for_user("test_account", cfg, _SYMBOL, _SIGNAL_SHORT, use_prd=False))

    # 應仍有止盈單（使用 LONG_TP_STRATEGY rr=2.0 → tp = 100 - 5*2 = 90.0）
    assert client.futures_create_order.call_count == 3
    tp_price = captured[2]["stopPrice"]
    assert abs(tp_price - 90.0) < 0.01


def test_short_uses_short_order_limit():
    """SHORT_ORDER_LIMIT 限制空單部位數，達上限時拒絕。"""
    cfg = {**_CFG_SHORT, "SHORT_ORDER_LIMIT": 1}
    client = _make_client(
        futures_position_information=AsyncMock(return_value=[
            {"symbol": "ETHUSDT", "positionAmt": "-1.0"},  # 已有一筆空單
        ]),
    )
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        ok, msg = run(_place_orders_for_user("test_account", cfg, _SYMBOL, _SIGNAL_SHORT, use_prd=False))

    assert ok is False
    assert "已達上限" in msg
    assert "空單" in msg


def test_short_add_on_detects_negative_position():
    """空單加倉：symbol 已有負數 positionAmt 時，視為同方向持倉加倉。"""
    cfg = {**_CFG_SHORT, "ADD_SAME_SYMBOL": True}
    client = _make_client(
        futures_position_information=AsyncMock(return_value=[
            {"symbol": _SYMBOL, "positionAmt": "-0.5"},  # 已有空單
        ]),
        futures_create_order=AsyncMock(
            return_value={"avgPrice": "100.0", "orderId": 1}
        ),
    )
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        ok, _ = run(_place_orders_for_user("test_account", cfg, _SYMBOL, _SIGNAL_SHORT, use_prd=False))

    assert ok is True


# ─── 策略比對 ──────────────────────────────────────────────────────────────────

def test_strategy_filter_in_place_orders_for_all_users():
    """DEV_STRATEGY 不含訊號策略時，place_orders_for_all_users 跳過該使用者，回傳空 dict。"""
    cfg_long_only = {**_CFG, "DEV_STRATEGY": ["long_breakout"]}

    with patch("app.trading.order_manager.get_all_trading_configs_with_chat_id",
               return_value=[("test_account", 123, cfg_long_only)]):
        results = run(place_orders_for_all_users(_SYMBOL, _SIGNAL_SHORT))

    assert results == {}


def test_prd_strategy_result_labeled_as_prd():
    """PRD_STRATEGY 含訊號策略時，結果標籤為「正式」，並使用 PRD API 金鑰（testnet=False）。"""
    cfg_prd = {
        **_CFG,
        "DEV_STRATEGY":     [],
        "PRD_STRATEGY":     ["long_breakout"],
        "PRD_API_KEY":      "prd_key",
        "PRD_SECRET_KEY":   "prd_secret",
    }
    client = _make_client(
        futures_create_order=AsyncMock(return_value={"avgPrice": "100.0", "orderId": 1})
    )
    with patch("app.trading.order_manager.get_all_trading_configs_with_chat_id",
               return_value=[("test_account", 123, cfg_prd)]):
        with patch("app.trading.order_manager.AsyncClient") as cls:
            cls.create = AsyncMock(return_value=client)
            results = run(place_orders_for_all_users(_SYMBOL, _SIGNAL_LONG))
            create_kwargs = cls.create.call_args.kwargs

    assert 123 in results
    assert results[123][0][0] == "正式"
    assert create_kwargs.get("testnet") is False
    assert create_kwargs.get("api_key") == "prd_key"


def test_unknown_signal_type_returns_none():
    """未知訊號類型 → _place_orders_for_user 回傳 None（略過）。"""
    bad_signal = {"type": "type99", "close": 100.0, "stop_loss": 95.0}
    client = _make_client()
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        result = run(_place_orders_for_user("test_account", _CFG, _SYMBOL, bad_signal, use_prd=False))

    assert result is None


# ─── -1007 重試邏輯 ────────────────────────────────────────────────────────────

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
        run(_place_orders_for_user("test_account", _CFG, _SYMBOL, _SIGNAL_LONG, use_prd=False))

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
        run(_place_orders_for_user("test_account", _CFG, _SYMBOL, _SIGNAL_LONG, use_prd=False))

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
        run(_place_orders_for_user("test_account", _CFG, _SYMBOL, _SIGNAL_LONG, use_prd=False))

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
        run(_place_orders_for_user("test_account", _CFG, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    assert client.futures_create_order.call_count == 1  # 只嘗試 1 次


# ─── 加倉例外邏輯 ──────────────────────────────────────────────────────────────

def test_add_on_bypasses_limit_when_allowed():
    """加倉：symbol 已有同方向持倉，即使達到上限仍允許開單（ADD_SAME_SYMBOL=True）。"""
    cfg_add = {**_CFG, "ADD_SAME_SYMBOL": True, "LONG_ORDER_LIMIT": 1}
    client = _make_client(
        futures_position_information=AsyncMock(return_value=[
            {"symbol": _SYMBOL, "positionAmt": "0.5"},  # 已有多單
        ]),
        futures_create_order=AsyncMock(
            return_value={"avgPrice": "100.0", "orderId": 1}
        ),
    )
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        ok, _ = run(_place_orders_for_user("test_account", cfg_add, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    assert ok is True


def test_add_on_blocked_when_not_allowed():
    """加倉：symbol 已有同方向持倉，但 ADD_SAME_SYMBOL=False → 拒絕。"""
    cfg_no_add = {**_CFG, "ADD_SAME_SYMBOL": False}
    client = _make_client(
        futures_position_information=AsyncMock(return_value=[
            {"symbol": _SYMBOL, "positionAmt": "0.5"},  # 已有多單
        ]),
    )
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        ok, msg = run(_place_orders_for_user("test_account", cfg_no_add, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    assert ok is False
    assert "不允許加倉" in msg


def test_new_position_blocked_at_limit():
    """新倉：不同 symbol 已佔滿 LONG_ORDER_LIMIT → 拒絕。"""
    cfg_limit = {**_CFG, "LONG_ORDER_LIMIT": 1}
    client = _make_client(
        futures_position_information=AsyncMock(return_value=[
            {"symbol": "ETHUSDT", "positionAmt": "1.0"},  # 不同 symbol 的多單
        ]),
    )
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        ok, msg = run(_place_orders_for_user("test_account", cfg_limit, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    assert ok is False
    assert "已達上限" in msg


def test_opposite_direction_does_not_count_as_add_on():
    """反向持倉不算加倉：symbol 已有空單，此次開多單 → 應走新倉邏輯（上限未達則允許）。"""
    cfg = {**_CFG, "LONG_ORDER_LIMIT": 3}
    client = _make_client(
        futures_position_information=AsyncMock(return_value=[
            {"symbol": _SYMBOL, "positionAmt": "-0.5"},  # 已有空單
        ]),
        futures_create_order=AsyncMock(
            return_value={"avgPrice": "100.0", "orderId": 1}
        ),
    )
    with patch("app.trading.order_manager.AsyncClient") as cls:
        cls.create = AsyncMock(return_value=client)
        ok, _ = run(_place_orders_for_user("test_account", cfg, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    assert ok is True  # 多單上限未達（0/3），應允許開倉
