from abc import ABC, abstractmethod


class ExchangeOrderTimeout(Exception):
    """開單請求逾時，訂單狀態未知，應查詢後決定是否重試。"""


class ExchangeConflictError(Exception):
    """止損/止盈單與現有條件單衝突（Binance -4130），清除舊單後重試。"""


class ExchangeAdapter(ABC):
    """交易所 adapter 介面。

    所有方法的 symbol 參數皆使用 Binance 格式（如 BTCUSDT），
    各 adapter 自行處理內部格式轉換。

    positionAmt 正數 = 多單、負數 = 空單（統一格式）。
    """

    @abstractmethod
    async def get_precisions(self, symbol: str) -> tuple[int, int]:
        """回傳 (qty_precision, price_precision)。"""

    @abstractmethod
    async def get_open_positions(self) -> list[dict]:
        """回傳所有有持倉的合約，格式：[{"symbol": "BTCUSDT", "positionAmt": float}]。"""

    @abstractmethod
    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        """設定保證金模式（CROSSED / ISOLATED）。已是目標模式時靜默忽略。"""

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """設定槓桿倍數。"""

    @abstractmethod
    async def cancel_all_open_orders(self, symbol: str) -> None:
        """取消所有一般掛單（非 closePosition 條件單）。"""

    @abstractmethod
    async def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        client_order_id: str,
        *,
        reduce_only: bool = False,
    ) -> dict:
        """市價開倉或平倉。回傳 {"avgPrice": float, "executedQty": float}。

        reduce_only=True 表示平倉（BingX hedge mode 需反轉 positionSide）。
        逾時時拋出 ExchangeOrderTimeout。
        """

    @abstractmethod
    async def get_order(self, symbol: str, client_order_id: str) -> dict:
        """查詢訂單狀態。回傳 {"status": str, "avgPrice": float, "executedQty": float}。"""

    @abstractmethod
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
        """掛止損單（STOP_MARKET）。衝突時拋出 ExchangeConflictError。"""

    @abstractmethod
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
        """掛止盈單（TAKE_PROFIT_MARKET）。衝突時拋出 ExchangeConflictError。"""

    @abstractmethod
    async def get_close_position_orders(self, symbol: str, side: str) -> list[dict]:
        """回傳指定方向的所有 closePosition 止損/止盈條件單。
        每筆包含：{"orderId": str, "type": "STOP_MARKET"|"TAKE_PROFIT_MARKET", "stopPrice": float}
        """

    @abstractmethod
    async def cancel_close_position_orders(self, symbol: str, side: str) -> int:
        """取消指定方向所有 closePosition 條件單，回傳取消數量。"""

    @abstractmethod
    async def close(self) -> None:
        """釋放 HTTP session / 連線資源。"""
