"""歷史 OHLC 資料下載與快取（Binance 公開 REST API，無需 API Key）。

資料量根據 backtest_days 自動計算：
  1d  : 1 頁（最多 1500 根，覆蓋 EMA200 暖機 + 任意天數）
  4h  : ceil((days+14)*6  + 200) / 1500) 頁（200 根暖機緩衝）
  1h  : ceil((days+14)*24 + 250) / 1500) 頁（250 根 EMA200 暖機）
  15m : ceil((days+14)*96 / 1500)         頁（14天評估窗口緩衝）

快取 key 含 backtest_days，避免不同天數的快取互相污染。
"""
import asyncio
import math
import os
import pickle
from collections import deque
from datetime import datetime, timezone

import aiohttp

CACHE_DIR    = os.path.join(os.path.dirname(__file__), "cache")
BASE_URL     = "https://fapi.binance.com"
RATE_LIMIT_SLEEP = 0.3   # 每次 API 呼叫後等待秒數
_EVAL_BUFFER = 14        # 評估窗口最多 7 天，加緩衝


# ─── 動態頁數計算 ─────────────────────────────────────────────────────────────

def _calc_pages(backtest_days: int) -> dict[str, int]:
    """各時框需要的 API 頁數（每頁最多 1500 根）。"""
    d = backtest_days + _EVAL_BUFFER
    return {
        "1d":  1,                                            # 1500根 ≥ 1250天，永遠夠
        "4h":  max(1, math.ceil((d * 6  + 200) / 1500)),    # 200根暖機
        "1h":  max(1, math.ceil((d * 24 + 250) / 1500)),    # EMA200暖機
        "15m": max(1, math.ceil(d * 96 / 1500)),
    }


# ─── 快取 ─────────────────────────────────────────────────────────────────────

def _cache_path(symbol: str, interval: str, backtest_days: int, end_date_str: str | None = None) -> str:
    # 區間模式用結束日期作 key（資料固定，永不過期）；一般模式用今天（每日更新）
    date_key = end_date_str if end_date_str else datetime.now(timezone.utc).strftime("%Y%m%d")
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{symbol}_{interval}_{backtest_days}d_{date_key}.pkl")


def _load_cache(symbol: str, interval: str, backtest_days: int, end_date_str: str | None = None) -> list | None:
    path = _cache_path(symbol, interval, backtest_days, end_date_str)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return pickle.load(f)
    return None


def _save_cache(symbol: str, interval: str, backtest_days: int, data: list, end_date_str: str | None = None) -> None:
    path = _cache_path(symbol, interval, backtest_days, end_date_str)
    with open(path, "wb") as f:
        pickle.dump(data, f)


# ─── API 呼叫 ─────────────────────────────────────────────────────────────────

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

    # k[7]=quote_vol, k[10]=takerBuyQuoteAssetVolume（統一 7 欄，與正式系統格式對齊）
    return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[7]), float(k[10])) for k in raw]


async def _fetch_paginated(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    n_pages: int,
    fetch_end_time_ms: int | None = None,
) -> list:
    """分頁抓取任意時框的 K 棒（由新到舊翻頁，最後排序回正向）。

    fetch_end_time_ms: 抓取的時間上限（ms）；None 表示抓到最新。
    """
    if n_pages == 1:
        candles = await _fetch_klines(session, symbol, interval, limit=1500, end_time_ms=fetch_end_time_ms)
        await asyncio.sleep(RATE_LIMIT_SLEEP)
        return candles

    pages: list[list] = []
    end_time_ms: int | None = fetch_end_time_ms  # 從指定結束點往前翻頁

    for _ in range(n_pages):
        page = await _fetch_klines(session, symbol, interval, limit=1500, end_time_ms=end_time_ms)
        await asyncio.sleep(RATE_LIMIT_SLEEP)
        if not page:
            break
        pages.insert(0, page)
        end_time_ms = page[0][0] - 1

    if not pages:
        return []

    seen: set[int] = set()
    combined: list = []
    for page in pages:
        for c in page:
            if c[0] not in seen:
                seen.add(c[0])
                combined.append(c)

    combined.sort(key=lambda x: x[0])
    return combined


# ─── 單一幣種下載 ─────────────────────────────────────────────────────────────

async def fetch_symbol_data(
    session: aiohttp.ClientSession,
    symbol: str,
    backtest_days: int = 30,
    no_cache: bool = False,
    fetch_end_time_ms: int | None = None,
) -> tuple[dict[str, list], bool]:
    """下載一個幣種的所有時框資料。

    回傳 (data, from_cache)：
      data       = {interval: candles}
      from_cache = True 表示所有時框都命中 cache，無 API 呼叫
    """
    pages = _calc_pages(backtest_days)
    end_date_str = (
        datetime.fromtimestamp(fetch_end_time_ms / 1000, tz=timezone.utc).strftime("%Y%m%d")
        if fetch_end_time_ms else None
    )
    result    = {}
    api_called = False

    for interval in ("1d", "4h", "1h", "15m"):
        if not no_cache:
            cached = _load_cache(symbol, interval, backtest_days, end_date_str)
            if cached is not None:
                result[interval] = cached
                continue

        api_called = True
        candles = await _fetch_paginated(session, symbol, interval, pages[interval], fetch_end_time_ms)
        _save_cache(symbol, interval, backtest_days, candles, end_date_str)
        result[interval] = candles

    return result, not api_called


# ─── 批次下載 ─────────────────────────────────────────────────────────────────

async def fetch_all_symbols(
    symbols: list[str],
    backtest_days: int = 30,
    no_cache: bool = False,
    batch_sleep: float = 3.0,
    symbol_sleep: float = 0.5,
    fetch_end_time_ms: int | None = None,
) -> dict[str, dict[str, list]]:
    """批次下載所有幣種資料。回傳 {symbol: {interval: candles}}。

    cache 命中時跳過所有 sleep，只有真正呼叫 API 時才等待。
    """
    pages = _calc_pages(backtest_days)
    api_calls_per_symbol = sum(pages.values())
    est_minutes = len(symbols) * (api_calls_per_symbol * RATE_LIMIT_SLEEP + symbol_sleep) / 60
    print(f"[data_fetcher] 每幣種 {api_calls_per_symbol} 頁，預估下載時間 ~{est_minutes:.0f} 分鐘（全 cache 則秒完）")

    result: dict[str, dict[str, list]] = {}
    batch_had_api_call = False

    async with aiohttp.ClientSession() as session:
        for i, symbol in enumerate(symbols):
            try:
                data, from_cache = await fetch_symbol_data(
                    session, symbol,
                    backtest_days=backtest_days,
                    no_cache=no_cache,
                    fetch_end_time_ms=fetch_end_time_ms,
                )
                result[symbol] = data
                if not from_cache:
                    batch_had_api_call = True
                    await asyncio.sleep(symbol_sleep)   # 只有 API 呼叫後才等待
            except Exception as exc:
                print(f"[data_fetcher] {symbol} 下載失敗：{exc}")

            if (i + 1) % 20 == 0:
                if batch_had_api_call:
                    print(f"[data_fetcher] 已處理 {i + 1}/{len(symbols)} 個幣種，暫停 {batch_sleep}s …")
                    await asyncio.sleep(batch_sleep)
                else:
                    print(f"[data_fetcher] 已處理 {i + 1}/{len(symbols)} 個幣種（全 cache）")
                batch_had_api_call = False

    return result


# ─── 幣種清單 ─────────────────────────────────────────────────────────────────

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
