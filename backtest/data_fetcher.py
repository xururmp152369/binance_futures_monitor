"""歷史 OHLC 資料下載與快取（Binance 公開 REST API，無需 API Key）。"""
import asyncio
import os
import pickle
from collections import deque
from datetime import datetime, timezone

import aiohttp

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
BASE_URL = "https://fapi.binance.com"

# 每個時框下載的 K 棒總數（暖機 + 回測 + 評估緩衝）
FETCH_LIMITS = {
    "1d":  500,   # EMA200 暖機 200 根 + 充裕緩衝
    "4h":  500,
    "1h":  1500,  # EMA200 暖機 200 根 + ~54 天
    "15m": 4500,  # 分 3 頁，每頁 1500 根，~47 天
}

RATE_LIMIT_SLEEP = 0.3   # 每次 API 呼叫後等待秒數


def _cache_path(symbol: str, interval: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{symbol}_{interval}_{today}.pkl")


def _load_cache(symbol: str, interval: str) -> list | None:
    path = _cache_path(symbol, interval)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return pickle.load(f)
    return None


def _save_cache(symbol: str, interval: str, data: list) -> None:
    path = _cache_path(symbol, interval)
    with open(path, "wb") as f:
        pickle.dump(data, f)


async def _fetch_klines(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    limit: int = 1500,
    end_time_ms: int | None = None,
) -> list:
    """呼叫 Binance /fapi/v1/klines，回傳 (open_time_ms, o, h, l, c, quote_vol) tuple list。"""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_time_ms is not None:
        params["endTime"] = end_time_ms

    async with session.get(f"{BASE_URL}/fapi/v1/klines", params=params) as resp:
        resp.raise_for_status()
        raw = await resp.json()

    return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[7])) for k in raw]


async def fetch_symbol_data(
    session: aiohttp.ClientSession,
    symbol: str,
    no_cache: bool = False,
) -> dict[str, list]:
    """下載一個幣種的所有時框資料，回傳 {interval: [(open_time_ms, o, h, l, c, qvol), ...]}。"""
    result = {}

    for interval in ("1d", "4h", "1h"):
        if not no_cache:
            cached = _load_cache(symbol, interval)
            if cached is not None:
                result[interval] = cached
                continue

        candles = await _fetch_klines(session, symbol, interval, limit=FETCH_LIMITS[interval])
        await asyncio.sleep(RATE_LIMIT_SLEEP)
        _save_cache(symbol, interval, candles)
        result[interval] = candles

    # 15m 分 3 頁向前抓
    if not no_cache:
        cached_15m = _load_cache(symbol, "15m")
        if cached_15m is not None:
            result["15m"] = cached_15m
        else:
            result["15m"] = await _fetch_15m_paginated(session, symbol)
    else:
        result["15m"] = await _fetch_15m_paginated(session, symbol)

    return result


async def _fetch_15m_paginated(session: aiohttp.ClientSession, symbol: str) -> list:
    """分 3 頁抓取 4500 根 15m K 棒（由新到舊，最後合併排序）。"""
    pages: list[list] = []
    end_time_ms: int | None = None

    for _ in range(3):
        page = await _fetch_klines(session, symbol, "15m", limit=1500, end_time_ms=end_time_ms)
        await asyncio.sleep(RATE_LIMIT_SLEEP)
        if not page:
            break
        pages.insert(0, page)            # 最舊的排在前面
        end_time_ms = page[0][0] - 1     # 往前推一根

    if not pages:
        return []

    combined = []
    seen: set[int] = set()
    for page in pages:
        for c in page:
            if c[0] not in seen:
                seen.add(c[0])
                combined.append(c)

    combined.sort(key=lambda x: x[0])
    _save_cache(symbol, "15m", combined)
    return combined


async def fetch_all_symbols(
    symbols: list[str],
    no_cache: bool = False,
    batch_sleep: float = 3.0,
    symbol_sleep: float = 0.5,
) -> dict[str, dict[str, list]]:
    """批次下載所有幣種資料。回傳 {symbol: {interval: candles}}。"""
    result = {}
    async with aiohttp.ClientSession() as session:
        for i, symbol in enumerate(symbols):
            try:
                result[symbol] = await fetch_symbol_data(session, symbol, no_cache=no_cache)
                await asyncio.sleep(symbol_sleep)
            except Exception as exc:
                print(f"[data_fetcher] {symbol} 下載失敗：{exc}")

            if (i + 1) % 20 == 0:
                print(f"[data_fetcher] 已下載 {i + 1}/{len(symbols)} 個幣種，暫停 {batch_sleep}s …")
                await asyncio.sleep(batch_sleep)

    return result


async def fetch_usdt_symbols() -> list[str]:
    """取得 Binance USDT 永續合約幣種清單。"""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/fapi/v1/exchangeInfo") as resp:
            resp.raise_for_status()
            info = await resp.json()

    symbols = [
        s["symbol"]
        for s in info["symbols"]
        if s["quoteAsset"] == "USDT"
        and s["contractType"] == "PERPETUAL"
        and s["status"] == "TRADING"
    ]
    return sorted(symbols)


def to_deque(candles: list, maxlen: int) -> deque:
    """將 list 轉為固定長度 deque（取最後 maxlen 根）。"""
    d: deque = deque(maxlen=maxlen)
    d.extend(candles[-maxlen:] if len(candles) > maxlen else candles)
    return d
