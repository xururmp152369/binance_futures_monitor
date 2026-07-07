import asyncio
from binance import AsyncClient
from ...extension.utils import setup_logging
from .base import ExchangeAdapter, ExchangeConflictError, ExchangeOrderTimeout

log = setup_logging()


class BinanceAdapter(ExchangeAdapter):
    """封裝 python-binance AsyncClient，實作 ExchangeAdapter 介面。"""

    def __init__(self, api_key: str, secret: str, use_prd: bool) -> None:
        self._api_key  = api_key
        self._secret   = secret
        self._use_prd  = use_prd
        self._client: AsyncClient | None = None

    async def _get_client(self) -> AsyncClient:
        if self._client is None:
            self._client = await AsyncClient.create(
                api_key=self._api_key,
                api_secret=self._secret,
                testnet=not self._use_prd,
            )
        return self._client

    # ── 查詢 ──────────────────────────────────────────────────────────────────

    async def get_precisions(self, symbol: str) -> tuple[int, int]:
        client = await self._get_client()
        try:
            info = await client.futures_exchange_info()
            for s in info["symbols"]:
                if s["symbol"] == symbol:
                    return int(s.get("quantityPrecision", 3)), int(s.get("pricePrecision", 2))
        except Exception as e:
            log.warning(f"[Binance] 無法取得 {symbol} 精度資訊，使用預設值: {e}")
        return 3, 2

    async def get_open_positions(self) -> list[dict]:
        client = await self._get_client()
        positions = await client.futures_position_information()
        return [
            {"symbol": p["symbol"], "positionAmt": float(p.get("positionAmt", 0))}
            for p in positions
            if float(p.get("positionAmt", 0)) != 0
        ]

    # ── 設定 ──────────────────────────────────────────────────────────────────

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        client = await self._get_client()
        try:
            await client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
            log.info(f"[Binance] {symbol} 保證金模式設為 {margin_type}")
        except Exception as e:
            if "-4046" not in str(e):
                log.warning(f"[Binance] {symbol} 設定保證金模式失敗（忽略）: {e}")

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        client = await self._get_client()
        await client.futures_change_leverage(symbol=symbol, leverage=leverage)
        log.info(f"[Binance] {symbol} 槓桿設為 {leverage}x")

    # ── 掛單管理 ──────────────────────────────────────────────────────────────

    async def cancel_all_open_orders(self, symbol: str) -> None:
        client = await self._get_client()
        try:
            await client.futures_cancel_all_algo_open_orders(symbol=symbol)
            log.info(f"[Binance] {symbol} 已取消所有一般掛單")
            await asyncio.sleep(0.3)
        except Exception as e:
            log.warning(f"[Binance] {symbol} 取消一般掛單失敗（忽略）: {e}")

    async def get_close_position_orders(self, symbol: str, side: str) -> list[dict]:
        client = await self._get_client()
        try:
            raw = await client.futures_get_open_algo_orders(symbol=symbol)
            orders = raw if isinstance(raw, list) else raw.get("orders", [])
            result = []
            for o in orders:
                is_close = (
                    o.get("closePosition") is True
                    or str(o.get("closePosition", "")).lower() == "true"
                )
                if is_close and o.get("side") == side:
                    result.append({
                        "orderId":   str(o.get("algoId", "")),
                        "type":      o.get("algoType", ""),
                        "stopPrice": float(o.get("triggerPrice") or o.get("stopPrice") or 0),
                    })
            return result
        except Exception as e:
            log.warning(f"[Binance] {symbol} 查詢 closePosition 條件單失敗: {e}")
            return []

    async def cancel_close_position_orders(self, symbol: str, side: str) -> int:
        client = await self._get_client()
        try:
            raw = await client.futures_get_open_algo_orders(symbol=symbol)
            algo_orders = raw if isinstance(raw, list) else raw.get("orders", [])
            cancel_ids = [
                o["algoId"] for o in algo_orders
                if (o.get("closePosition") is True
                    or str(o.get("closePosition", "")).lower() == "true")
                and o.get("side") == side
            ]
            for algo_id in cancel_ids:
                try:
                    await client.futures_cancel_algo_order(algoId=algo_id)
                except Exception as ce:
                    log.warning(f"[Binance] {symbol} 取消條件單 algoId={algo_id} 失敗: {ce}")
            if cancel_ids:
                await asyncio.sleep(0.5)
            return len(cancel_ids)
        except Exception as e:
            log.warning(f"[Binance] {symbol} 取消 closePosition 條件單失敗: {e}")
            return 0

    # ── 開倉 / 查詢訂單 ───────────────────────────────────────────────────────

    async def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        client_order_id: str,
        *,
        reduce_only: bool = False,
    ) -> dict:
        client = await self._get_client()
        kwargs: dict = dict(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=quantity,
            newClientOrderId=client_order_id,
        )
        if reduce_only:
            kwargs["reduceOnly"] = True
        try:
            order = await client.futures_create_order(**kwargs)
            return {
                "avgPrice":    float(order.get("avgPrice") or 0),
                "executedQty": float(order.get("executedQty") or 0),
            }
        except Exception as e:
            if "-1007" in str(e):
                raise ExchangeOrderTimeout(str(e)) from e
            raise

    async def get_order(self, symbol: str, client_order_id: str) -> dict:
        client = await self._get_client()
        order = await client.futures_get_order(
            symbol=symbol, origClientOrderId=client_order_id
        )
        return {
            "status":      order.get("status", ""),
            "avgPrice":    float(order.get("avgPrice") or 0),
            "executedQty": float(order.get("executedQty") or 0),
        }

    # ── 止損 / 止盈 ───────────────────────────────────────────────────────────

    async def create_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        *,
        close_position: bool = True,
        quantity: float = 0.0,
        reduce_only: bool = False,
    ) -> dict:
        client = await self._get_client()
        kwargs: dict = dict(
            symbol=symbol,
            side=side,
            type="STOP_MARKET",
            stopPrice=stop_price,
            workingType="MARK_PRICE",
        )
        if close_position:
            kwargs["closePosition"] = True
        else:
            kwargs["quantity"]   = quantity
            kwargs["reduceOnly"] = reduce_only
        try:
            return await client.futures_create_order(**kwargs)
        except Exception as e:
            if "-4130" in str(e):
                raise ExchangeConflictError(str(e)) from e
            raise

    async def create_take_profit_market_order(
        self,
        symbol: str,
        side: str,
        tp_price: float,
        *,
        close_position: bool = True,
        quantity: float = 0.0,
        reduce_only: bool = False,
    ) -> dict:
        client = await self._get_client()
        kwargs: dict = dict(
            symbol=symbol,
            side=side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=tp_price,
            workingType="MARK_PRICE",
        )
        if close_position:
            kwargs["closePosition"] = True
        else:
            kwargs["quantity"]   = quantity
            kwargs["reduceOnly"] = reduce_only
        try:
            return await client.futures_create_order(**kwargs)
        except Exception as e:
            if "-4130" in str(e):
                raise ExchangeConflictError(str(e)) from e
            raise

    # ── 清理 ──────────────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._client:
            await self._client.close_connection()
            self._client = None
