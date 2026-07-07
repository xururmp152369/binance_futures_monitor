from .base import ExchangeAdapter, ExchangeConflictError, ExchangeOrderTimeout
from .binance_adapter import BinanceAdapter
from .bingx_adapter import BingXAdapter

__all__ = [
    "ExchangeAdapter",
    "ExchangeOrderTimeout",
    "ExchangeConflictError",
    "create_adapter",
]


def create_adapter(exchange: str, api_key: str, secret: str, use_prd: bool) -> ExchangeAdapter:
    """工廠函數：依 exchange 名稱建立對應的 adapter 實例。"""
    if exchange == "bingx":
        return BingXAdapter(api_key, secret, use_prd)
    return BinanceAdapter(api_key, secret, use_prd)
