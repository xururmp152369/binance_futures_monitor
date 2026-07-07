"""市價開倉 + 逾時重試 + 多空方向邏輯測試。

Mock 層：app.trading.order_manager.create_adapter 回傳 AsyncMock adapter，
不依賴任何交易所 SDK，適用於 Binance 與 BingX 路徑。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.trading.order_manager import _place_orders_for_user, place_orders_for_all_users
from app.trading.exchange.base import ExchangeConflictError, ExchangeOrderTimeout


# ─── 共用設定 ──────────────────────────────────────────────────────────────────

_CFG = {
    "EXCHANGE":          "binance",
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

_CFG_BINGX = {**_CFG, "EXCHANGE": "bingx"}

_SIGNAL_LONG  = {"type": "type1", "close": 100.0, "stop_loss": 95.0}
_SIGNAL_SHORT = {"type": "type3", "close": 100.0, "stop_loss": 105.0}
_SYMBOL       = "BTCUSDT"


@pytest.fixture(autouse=True)
def skip_sleep():
    with patch("asyncio.sleep", new=AsyncMock()):
        yield


def _make_adapter(**overrides) -> AsyncMock:
    """建立標準 adapter mock，可透過 overrides 覆蓋特定方法。"""
    adapter = AsyncMock()
    adapter.get_precisions.return_value          = (3, 2)
    adapter.get_open_positions.return_value      = []
    adapter.set_margin_type.return_value         = None
    adapter.set_leverage.return_value            = None
    adapter.cancel_all_open_orders.return_value  = None
    adapter.cancel_close_position_orders.return_value = 0
    adapter.get_close_position_orders.return_value    = []
    adapter.create_market_order.return_value     = {"avgPrice": 100.0, "executedQty": 0.0}
    adapter.create_stop_market_order.return_value      = {}
    adapter.create_take_profit_market_order.return_value = {}
    adapter.close.return_value = None
    for attr, val in overrides.items():
        setattr(adapter, attr, val)
    return adapter


def _patch_adapter(adapter):
    """回傳 patch context，讓 create_adapter 固定回傳指定 adapter。"""
    return patch(
        "app.trading.order_manager.create_adapter",
        return_value=adapter,
    )


def run(coro):
    return asyncio.run(coro)


def _market_kwargs(adapter, call_index: int) -> dict:
    """取得第 N 次 create_market_order 的 kwargs。"""
    return adapter.create_market_order.call_args_list[call_index].kwargs or \
           dict(zip(
               ("symbol", "side", "quantity", "client_order_id"),
               adapter.create_market_order.call_args_list[call_index].args,
           ))


# ─── 多頭開倉 ──────────────────────────────────────────────────────────────────

def test_normal_success():
    """正常路徑：市價開倉成功，SL 用 closePosition，單筆 TP 用 closePosition。"""
    adapter = _make_adapter()
    with _patch_adapter(adapter):
        ok, _ = run(_place_orders_for_user("acct", _CFG, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    assert ok is True
    adapter.create_market_order.assert_called_once()
    adapter.create_stop_market_order.assert_called_once()
    adapter.create_take_profit_market_order.assert_called_once()

    sl_call = adapter.create_stop_market_order.call_args
    assert sl_call.args[1] == "SELL"           # side
    assert sl_call.kwargs["close_position"] is True

    tp_call = adapter.create_take_profit_market_order.call_args
    assert tp_call.args[1] == "SELL"           # side
    assert tp_call.kwargs["close_position"] is True


def test_long_tp_price_above_entry():
    """多頭止盈價格應高於入場價。"""
    adapter = _make_adapter()
    with _patch_adapter(adapter):
        run(_place_orders_for_user("acct", _CFG, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    tp_price = adapter.create_take_profit_market_order.call_args.args[2]  # stop_price positional
    assert tp_price > 100.0


def test_multi_tp_last_is_close_position():
    """多筆 TP：非最後一筆帶 quantity+reduce_only，最後一筆用 closePosition。"""
    cfg_multi = {**_CFG, "LONG_TP_STRATEGY": [
        {"RR_RATIO": 1.0, "PERCENT": 50},
        {"RR_RATIO": 2.0, "PERCENT": 50},
    ]}
    adapter = _make_adapter()
    with _patch_adapter(adapter):
        run(_place_orders_for_user("acct", cfg_multi, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    assert adapter.create_take_profit_market_order.call_count == 2

    tp1_kw = adapter.create_take_profit_market_order.call_args_list[0].kwargs
    assert tp1_kw["reduce_only"] is True
    assert tp1_kw["quantity"] > 0
    assert tp1_kw["close_position"] is False

    tp2_kw = adapter.create_take_profit_market_order.call_args_list[1].kwargs
    assert tp2_kw["close_position"] is True


# ─── 空頭開倉 ──────────────────────────────────────────────────────────────────

def test_short_signal_uses_sell_entry():
    """type3 訊號 → 市價 SELL 開倉，止損/止盈用 BUY。"""
    adapter = _make_adapter()
    with _patch_adapter(adapter):
        ok, _ = run(
            _place_orders_for_user("acct", _CFG_SHORT, _SYMBOL, _SIGNAL_SHORT, use_prd=False)
        )

    assert ok is True
    args = adapter.create_market_order.call_args
    side = args.kwargs.get("side") or args.args[1]
    assert side == "SELL"

    assert adapter.create_stop_market_order.call_args.args[1] == "BUY"
    assert adapter.create_take_profit_market_order.call_args.args[1] == "BUY"


def test_short_tp_price_below_entry():
    """空頭止盈價格應低於入場價。"""
    adapter = _make_adapter()
    with _patch_adapter(adapter):
        run(
            _place_orders_for_user("acct", _CFG_SHORT, _SYMBOL, _SIGNAL_SHORT, use_prd=False)
        )

    tp_price = adapter.create_take_profit_market_order.call_args.args[2]
    assert tp_price < 100.0


def test_short_uses_short_tp_strategy():
    """SHORT_TP_STRATEGY 存在時應使用它，而非 LONG_TP_STRATEGY。"""
    cfg = {
        **_CFG_SHORT,
        "LONG_TP_STRATEGY":  [{"RR_RATIO": 3.0, "PERCENT": 100}],
        "SHORT_TP_STRATEGY": [{"RR_RATIO": 1.5, "PERCENT": 100}],
    }
    adapter = _make_adapter()
    with _patch_adapter(adapter):
        run(_place_orders_for_user("acct", cfg, _SYMBOL, _SIGNAL_SHORT, use_prd=False))

    # sl_dist = |100 - 105| = 5，SHORT_TP rr=1.5 → tp = 100 - 5*1.5 = 92.5
    tp_price = adapter.create_take_profit_market_order.call_args.args[2]
    assert abs(tp_price - 92.5) < 0.01


def test_short_falls_back_to_long_tp_strategy():
    """SHORT_TP_STRATEGY 未設定時，空頭應 fallback 使用 LONG_TP_STRATEGY。"""
    cfg = {**_CFG_SHORT}
    adapter = _make_adapter()
    with _patch_adapter(adapter):
        run(_place_orders_for_user("acct", cfg, _SYMBOL, _SIGNAL_SHORT, use_prd=False))

    # LONG_TP rr=2.0 → tp = 100 - 5*2 = 90.0
    tp_price = adapter.create_take_profit_market_order.call_args.args[2]
    assert abs(tp_price - 90.0) < 0.01


def test_short_uses_short_order_limit():
    """SHORT_ORDER_LIMIT 限制空單部位數，達上限時拒絕。"""
    cfg = {**_CFG_SHORT, "SHORT_ORDER_LIMIT": 1}
    adapter = _make_adapter(
        get_open_positions=AsyncMock(return_value=[
            {"symbol": "ETHUSDT", "positionAmt": -1.0},
        ])
    )
    with _patch_adapter(adapter):
        ok, msg = run(
            _place_orders_for_user("acct", cfg, _SYMBOL, _SIGNAL_SHORT, use_prd=False)
        )

    assert ok is False
    assert "已達上限" in msg
    assert "空單" in msg


def test_short_add_on_detects_negative_position():
    """空單加倉：symbol 已有負數 positionAmt 時，視為同方向持倉加倉。"""
    cfg = {**_CFG_SHORT, "ADD_SAME_SYMBOL": True}
    adapter = _make_adapter(
        get_open_positions=AsyncMock(return_value=[
            {"symbol": _SYMBOL, "positionAmt": -0.5},
        ])
    )
    with _patch_adapter(adapter):
        ok, _ = run(
            _place_orders_for_user("acct", cfg, _SYMBOL, _SIGNAL_SHORT, use_prd=False)
        )

    assert ok is True


# ─── 策略比對 ──────────────────────────────────────────────────────────────────

def test_strategy_filter_in_place_orders_for_all_users():
    """DEV_STRATEGY 不含訊號策略時跳過該使用者，回傳空 dict。"""
    cfg_long_only = {**_CFG, "DEV_STRATEGY": ["long_breakout"]}
    with patch(
        "app.trading.order_manager.get_all_trading_configs_with_chat_id",
        return_value=[("acct", 123, cfg_long_only)],
    ):
        results = run(place_orders_for_all_users(_SYMBOL, _SIGNAL_SHORT))

    assert results == {}


def test_prd_strategy_result_labeled_as_prd():
    """PRD_STRATEGY 含訊號策略時，結果標籤為「正式」，且使用 PRD API 金鑰。"""
    cfg_prd = {
        **_CFG,
        "DEV_STRATEGY":   [],
        "PRD_STRATEGY":   ["long_breakout"],
        "PRD_API_KEY":    "prd_key",
        "PRD_SECRET_KEY": "prd_secret",
    }
    adapter = _make_adapter()
    captured_args: list = []

    def factory(exchange, api_key, secret, use_prd):
        captured_args.append((exchange, api_key, secret, use_prd))
        return adapter

    with patch(
        "app.trading.order_manager.get_all_trading_configs_with_chat_id",
        return_value=[("acct", 123, cfg_prd)],
    ):
        with patch("app.trading.order_manager.create_adapter", side_effect=factory):
            results = run(place_orders_for_all_users(_SYMBOL, _SIGNAL_LONG))

    assert 123 in results
    assert results[123][0][0] == "正式"
    assert captured_args[0][1] == "prd_key"   # api_key
    assert captured_args[0][3] is True         # use_prd


def test_unknown_signal_type_returns_none():
    """未知訊號類型 → _place_orders_for_user 回傳 None（略過）。"""
    bad_signal = {"type": "type99", "close": 100.0, "stop_loss": 95.0}
    adapter = _make_adapter()
    with _patch_adapter(adapter):
        result = run(_place_orders_for_user("acct", _CFG, _SYMBOL, bad_signal, use_prd=False))

    assert result is None


# ─── 逾時重試邏輯 ──────────────────────────────────────────────────────────────

def test_timeout_query_shows_filled():
    """逾時後查詢訂單 = FILLED，不重新開倉，繼續設 SL/TP。"""
    adapter = _make_adapter(
        create_market_order=AsyncMock(side_effect=ExchangeOrderTimeout("timeout")),
        get_order=AsyncMock(return_value={"status": "FILLED", "avgPrice": 100.0, "executedQty": 0.0}),
    )
    with _patch_adapter(adapter):
        ok, _ = run(_place_orders_for_user("acct", _CFG, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    assert ok is True
    assert adapter.create_market_order.call_count == 1
    assert adapter.get_order.call_count == 1
    adapter.create_stop_market_order.assert_called_once()


def test_timeout_retry_succeeds_on_second_attempt():
    """逾時且訂單未成立，第 2 次重試開倉成功，正常設 SL/TP。"""
    adapter = _make_adapter(
        create_market_order=AsyncMock(side_effect=[
            ExchangeOrderTimeout("timeout"),
            {"avgPrice": 100.0, "executedQty": 0.0},
        ]),
        get_order=AsyncMock(return_value={"status": "NEW", "avgPrice": 0.0, "executedQty": 0.0}),
    )
    with _patch_adapter(adapter):
        ok, _ = run(_place_orders_for_user("acct", _CFG, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    assert ok is True
    assert adapter.create_market_order.call_count == 2
    assert adapter.get_order.call_count == 1


def test_timeout_all_5_retries_exhausted():
    """逾時重試 5 次全部失敗，放棄開倉，不設 SL/TP。"""
    adapter = _make_adapter(
        create_market_order=AsyncMock(side_effect=ExchangeOrderTimeout("timeout")),
        get_order=AsyncMock(return_value={"status": "NEW", "avgPrice": 0.0, "executedQty": 0.0}),
    )
    with _patch_adapter(adapter):
        ok, msg = run(_place_orders_for_user("acct", _CFG, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    assert ok is False
    assert adapter.create_market_order.call_count == 5
    assert adapter.get_order.call_count == 5


def test_other_error_aborts_immediately():
    """非逾時錯誤不重試，立即放棄，不設 SL/TP。"""
    adapter = _make_adapter(
        create_market_order=AsyncMock(side_effect=Exception("Margin is insufficient")),
    )
    with _patch_adapter(adapter):
        ok, _ = run(_place_orders_for_user("acct", _CFG, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    assert ok is False
    assert adapter.create_market_order.call_count == 1
    adapter.create_stop_market_order.assert_not_called()


# ─── 加倉例外邏輯 ──────────────────────────────────────────────────────────────

def test_add_on_bypasses_limit_when_allowed():
    """加倉：symbol 已有同方向持倉，即使達到上限仍允許開單（ADD_SAME_SYMBOL=True）。"""
    cfg_add = {**_CFG, "ADD_SAME_SYMBOL": True, "LONG_ORDER_LIMIT": 1}
    adapter = _make_adapter(
        get_open_positions=AsyncMock(return_value=[
            {"symbol": _SYMBOL, "positionAmt": 0.5},
        ])
    )
    with _patch_adapter(adapter):
        ok, _ = run(_place_orders_for_user("acct", cfg_add, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    assert ok is True


def test_add_on_blocked_when_not_allowed():
    """加倉：symbol 已有同方向持倉，但 ADD_SAME_SYMBOL=False → 拒絕。"""
    cfg_no_add = {**_CFG, "ADD_SAME_SYMBOL": False}
    adapter = _make_adapter(
        get_open_positions=AsyncMock(return_value=[
            {"symbol": _SYMBOL, "positionAmt": 0.5},
        ])
    )
    with _patch_adapter(adapter):
        ok, msg = run(
            _place_orders_for_user("acct", cfg_no_add, _SYMBOL, _SIGNAL_LONG, use_prd=False)
        )

    assert ok is False
    assert "不允許加倉" in msg


def test_new_position_blocked_at_limit():
    """新倉：不同 symbol 已佔滿 LONG_ORDER_LIMIT → 拒絕。"""
    cfg_limit = {**_CFG, "LONG_ORDER_LIMIT": 1}
    adapter = _make_adapter(
        get_open_positions=AsyncMock(return_value=[
            {"symbol": "ETHUSDT", "positionAmt": 1.0},
        ])
    )
    with _patch_adapter(adapter):
        ok, msg = run(
            _place_orders_for_user("acct", cfg_limit, _SYMBOL, _SIGNAL_LONG, use_prd=False)
        )

    assert ok is False
    assert "已達上限" in msg


def test_opposite_direction_does_not_count_as_add_on():
    """反向持倉不算加倉：symbol 已有空單，此次開多單 → 走新倉邏輯。"""
    cfg = {**_CFG, "LONG_ORDER_LIMIT": 3}
    adapter = _make_adapter(
        get_open_positions=AsyncMock(return_value=[
            {"symbol": _SYMBOL, "positionAmt": -0.5},
        ])
    )
    with _patch_adapter(adapter):
        ok, _ = run(_place_orders_for_user("acct", cfg, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    assert ok is True


# ─── 中文 symbol → client_order_id ASCII 防護 ────────────────────────────────

def test_chinese_symbol_order_id_is_ascii():
    """中文幣種名稱 → client_order_id 應只含 ASCII（避免簽名不符）。"""
    chinese_symbol = "龍蝦USDT"
    captured_ids: list = []

    async def record_order(symbol, side, quantity, client_order_id):
        captured_ids.append(client_order_id)
        return {"avgPrice": 100.0, "executedQty": 0.0}

    adapter = _make_adapter(create_market_order=AsyncMock(side_effect=record_order))
    with _patch_adapter(adapter):
        run(_place_orders_for_user("acct", _CFG, chinese_symbol, _SIGNAL_LONG, use_prd=False))

    assert len(captured_ids) >= 1
    assert captured_ids[0].isascii(), f"client_order_id 含非 ASCII：{captured_ids[0]!r}"


# ─── BingX 路由 ───────────────────────────────────────────────────────────────

def test_bingx_config_routes_to_bingx_adapter():
    """EXCHANGE=bingx 時，create_adapter 應收到 exchange='bingx'。"""
    captured: list = []

    def factory(exchange, api_key, secret, use_prd):
        captured.append(exchange)
        return _make_adapter()

    with patch("app.trading.order_manager.create_adapter", side_effect=factory):
        run(_place_orders_for_user("acct", _CFG_BINGX, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    assert captured[0] == "bingx"


def test_default_exchange_is_binance():
    """未設定 EXCHANGE 時預設路由到 binance。"""
    cfg_no_exchange = {k: v for k, v in _CFG.items() if k != "EXCHANGE"}
    captured: list = []

    def factory(exchange, api_key, secret, use_prd):
        captured.append(exchange)
        return _make_adapter()

    with patch("app.trading.order_manager.create_adapter", side_effect=factory):
        run(_place_orders_for_user("acct", cfg_no_exchange, _SYMBOL, _SIGNAL_LONG, use_prd=False))

    assert captured[0] == "binance"
