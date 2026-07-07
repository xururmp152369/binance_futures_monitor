import math
import re
import ccxt.async_support as ccxt_async
from ...extension.utils import setup_logging
from .base import ExchangeAdapter, ExchangeConflictError, ExchangeOrderTimeout

log = setup_logging()

_LIVE_HOST  = "open-api.bingx.com"
_DEMO_HOST  = "open-api-vst.bingx.com"

# BingX 逾時 / 網路錯誤碼（字串比對）
_TIMEOUT_CODES = ("-1007", "timeout", "Timeout", "RequestTimeout")
# BingX 條件單衝突（等同 Binance -4130）
_CONFLICT_CODES = ("80014", "Position side does not match")


def _tick_to_dp(tick) -> int:
    """ccxt TICK_SIZE 格式 → 小數位數：0.001→3, 0.1→1, 1→0"""
    f = float(tick)
    if f <= 0 or f >= 1:
        return 0
    return max(0, -int(math.floor(math.log10(f))))


def _binance_to_ccxt(symbol: str) -> str:
    """BTCUSDT → BTC/USDT:USDT（ccxt 永續合約格式）。"""
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}/USDT:USDT"
    # 其他報價幣種：盡量轉換，末尾加 :報價幣
    m = re.match(r"^(.+?)(BTC|ETH|BNB|BUSD)$", symbol)
    if m:
        return f"{m.group(1)}/{m.group(2)}:{m.group(2)}"
    return symbol


def _ccxt_to_binance(ccxt_symbol: str) -> str:
    """BTC/USDT:USDT → BTCUSDT。"""
    base = ccxt_symbol.split("/")[0]
    quote = ccxt_symbol.split("/")[1].split(":")[0]
    return f"{base}{quote}"


def _has_code(err: Exception, codes: tuple) -> bool:
    msg = str(err)
    return any(c in msg for c in codes)


class BingXAdapter(ExchangeAdapter):
    """BingX 永續合約 adapter，使用 ccxt async_support。

    DEV 模式（use_prd=False）：連線至 BingX Virtual Trading (open-api-vst.bingx.com)。
    PRD 模式（use_prd=True）：連線至 BingX 正式環境 (open-api.bingx.com)。

    BingX 使用 Hedge Mode，每個 position 有明確的 positionSide（LONG/SHORT）。
    side=BUY  + positionSide=LONG  → 開多 / 平空
    side=SELL + positionSide=LONG  → 平多（SL/TP）
    side=SELL + positionSide=SHORT → 開空 / 平多
    side=BUY  + positionSide=SHORT → 平空（SL/TP）
    """

    def __init__(self, api_key: str, secret: str, use_prd: bool) -> None:
        self._exchange = ccxt_async.bingx({
            "apiKey": api_key,
            "secret": secret,
            "options": {"defaultType": "swap"},
        })
        if not use_prd:
            # ccxt BingX URL 為模板格式：https://open-api.{hostname}/openApi
            # 直接將模板替換為 VST 完整 host，使 ccxt 無需再解析 {hostname}
            for key in list(self._exchange.urls.get("api", {}).keys()):
                url = self._exchange.urls["api"][key]
                if isinstance(url, str):
                    self._exchange.urls["api"][key] = url.replace(
                        "open-api.{hostname}", _DEMO_HOST
                    )

    def _position_side(self, side: str) -> str:
        """BUY → LONG，SELL → SHORT（開倉方向映射到 positionSide）。"""
        return "LONG" if side == "BUY" else "SHORT"

    def _exit_position_side(self, exit_side: str) -> str:
        """平倉方向 SELL → 平多頭(LONG position)，BUY → 平空頭(SHORT position)。"""
        return "LONG" if exit_side == "SELL" else "SHORT"

    # ── 查詢 ──────────────────────────────────────────────────────────────────

    async def get_precisions(self, symbol: str) -> tuple[int, int]:
        ccxt_sym = _binance_to_ccxt(symbol)
        try:
            markets = await self._exchange.load_markets()
            m = markets.get(ccxt_sym)
            if m:
                precision = m.get("precision", {})
                qty_raw   = precision.get("amount")
                price_raw = precision.get("price")
                # ccxt BingX 使用 TICK_SIZE 格式（0.001 表示最小單位 0.001 BTC，對應 3 位小數）
                qty_p   = _tick_to_dp(qty_raw)   if qty_raw   is not None else 3
                price_p = _tick_to_dp(price_raw) if price_raw is not None else 2
                return qty_p, price_p
        except Exception as e:
            log.warning(f"[BingX] 無法取得 {symbol} 精度資訊，使用預設值: {e}")
        return 3, 2

    async def get_open_positions(self) -> list[dict]:
        try:
            positions = await self._exchange.fetch_positions()
            result = []
            for p in positions:
                contracts = float(p.get("contracts") or 0)
                if contracts == 0:
                    continue
                pos_side = p.get("side", "long")
                # 統一格式：多頭正數，空頭負數
                amt = contracts if pos_side == "long" else -contracts
                raw_sym = p.get("symbol", "")
                binance_sym = _ccxt_to_binance(raw_sym)
                result.append({"symbol": binance_sym, "positionAmt": amt})
            return result
        except Exception as e:
            log.warning(f"[BingX] 查詢持倉失敗: {e}")
            return []

    # ── 設定 ──────────────────────────────────────────────────────────────────

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        ccxt_sym = _binance_to_ccxt(symbol)
        mode = "isolated" if margin_type.upper() == "ISOLATED" else "cross"
        try:
            await self._exchange.set_margin_mode(mode, ccxt_sym)
            log.info(f"[BingX] {symbol} 保證金模式設為 {margin_type}")
        except Exception as e:
            # 已是目標模式時靜默忽略
            if "already" not in str(e).lower() and "same" not in str(e).lower():
                log.warning(f"[BingX] {symbol} 設定保證金模式失敗（忽略）: {e}")

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        ccxt_sym = _binance_to_ccxt(symbol)
        # BingX hedge mode 需對 LONG / SHORT 各設一次（API 要求大寫）
        for side in ("LONG", "SHORT"):
            try:
                await self._exchange.set_leverage(leverage, ccxt_sym, params={"side": side})
            except Exception as e:
                log.warning(f"[BingX] {symbol} 設定 {side} 槓桿失敗（忽略）: {e}")
        log.info(f"[BingX] {symbol} 槓桿設為 {leverage}x")

    # ── 掛單管理 ──────────────────────────────────────────────────────────────

    async def cancel_all_open_orders(self, symbol: str) -> None:
        ccxt_sym = _binance_to_ccxt(symbol)
        try:
            await self._exchange.cancel_all_orders(ccxt_sym)
            log.info(f"[BingX] {symbol} 已取消所有掛單")
        except Exception as e:
            log.warning(f"[BingX] {symbol} 取消掛單失敗（忽略）: {e}")

    async def get_close_position_orders(self, symbol: str, side: str) -> list[dict]:
        ccxt_sym  = _binance_to_ccxt(symbol)
        pos_side  = self._exit_position_side(side)
        try:
            orders = await self._exchange.fetch_open_orders(ccxt_sym)
            result = []
            for o in orders:
                info = o.get("info", {})
                if str(info.get("closePosition", "")).lower() != "true":
                    continue
                if info.get("positionSide", "") != pos_side:
                    continue
                order_type = info.get("type", "")
                result.append({
                    "orderId":   str(o.get("id", "")),
                    "type":      order_type,
                    "stopPrice": float(info.get("stopPrice") or o.get("stopPrice") or 0),
                })
            return result
        except Exception as e:
            log.warning(f"[BingX] {symbol} 查詢 closePosition 條件單失敗: {e}")
            return []

    async def cancel_close_position_orders(self, symbol: str, side: str) -> int:
        orders = await self.get_close_position_orders(symbol, side)
        ccxt_sym = _binance_to_ccxt(symbol)
        cancelled = 0
        for o in orders:
            try:
                await self._exchange.cancel_order(o["orderId"], ccxt_sym)
                cancelled += 1
            except Exception as ce:
                log.warning(f"[BingX] {symbol} 取消條件單 {o['orderId']} 失敗: {ce}")
        return cancelled

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
        ccxt_sym = _binance_to_ccxt(symbol)
        # 開倉：BUY→LONG / SELL→SHORT；平倉（reduce_only）：SELL→LONG / BUY→SHORT
        pos_side = self._exit_position_side(side) if reduce_only else self._position_side(side)
        try:
            order = await self._exchange.create_order(
                ccxt_sym,
                "market",
                side.lower(),
                quantity,
                params={
                    "positionSide":  pos_side,
                    "clientOrderID": client_order_id,
                },
            )
            avg = float(order.get("average") or order.get("price") or 0)
            qty = float(order.get("filled") or 0)
            return {"avgPrice": avg, "executedQty": qty}
        except Exception as e:
            if _has_code(e, _TIMEOUT_CODES):
                raise ExchangeOrderTimeout(str(e)) from e
            raise

    async def get_order(self, symbol: str, client_order_id: str) -> dict:
        ccxt_sym = _binance_to_ccxt(symbol)
        # ccxt BingX: fetch_order 支援 clientOrderId via params
        order = await self._exchange.fetch_order(
            None, ccxt_sym, params={"clientOrderID": client_order_id}
        )
        status_map = {"closed": "FILLED", "open": "NEW", "canceled": "CANCELED"}
        status = status_map.get(order.get("status", ""), order.get("status", ""))
        return {
            "status":      status,
            "avgPrice":    float(order.get("average") or 0),
            "executedQty": float(order.get("filled") or 0),
        }

    async def _min_amount(self, ccxt_sym: str) -> float:
        """查詢商品最小下單量（load_markets 由 ccxt 內部快取，不重複呼叫 API）。"""
        try:
            markets = await self._exchange.load_markets()
            m = markets.get(ccxt_sym, {})
            val = float(((m.get("limits") or {}).get("amount") or {}).get("min") or 0)
            return val if val > 0 else 0.0001
        except Exception:
            return 0.0001

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
        ccxt_sym  = _binance_to_ccxt(symbol)
        pos_side  = self._exit_position_side(side)
        params: dict = {
            "stopPrice":    stop_price,
            "positionSide": pos_side,
            "workingType":  "MARK_PRICE",
        }
        if close_position:
            params["closePosition"] = True
            # ccxt 要求 amount > 0；BingX closePosition=True 時會忽略此值
            amount = quantity if quantity > 0 else await self._min_amount(ccxt_sym)
        else:
            amount = quantity
            params["reduceOnly"] = reduce_only
        try:
            return await self._exchange.create_order(
                ccxt_sym, "stop_market", side.lower(), amount, None, params
            )
        except Exception as e:
            if _has_code(e, _CONFLICT_CODES):
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
        ccxt_sym  = _binance_to_ccxt(symbol)
        pos_side  = self._exit_position_side(side)
        params: dict = {
            "stopPrice":    tp_price,
            "positionSide": pos_side,
            "workingType":  "MARK_PRICE",
        }
        if close_position:
            params["closePosition"] = True
            # ccxt 要求 amount > 0；BingX closePosition=True 時會忽略此值
            amount = quantity if quantity > 0 else await self._min_amount(ccxt_sym)
        else:
            amount = quantity
            params["reduceOnly"] = reduce_only
        try:
            return await self._exchange.create_order(
                ccxt_sym, "take_profit_market", side.lower(), amount, None, params
            )
        except Exception as e:
            if _has_code(e, _CONFLICT_CODES):
                raise ExchangeConflictError(str(e)) from e
            raise

    # ── 清理 ──────────────────────────────────────────────────────────────────

    async def close(self) -> None:
        try:
            await self._exchange.close()
        except Exception:
            pass
