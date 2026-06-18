"""
對比腳本：舊邏輯（全量）vs 新邏輯（只保留 COIN），輸出兩份清單供人工對比。

執行方式：
    python scripts/check_old_vs_new.py

產生：
    scripts/old_all_symbols.txt  — 舊邏輯全部包含的合約
    scripts/new_excluded.txt     — 新邏輯新排除掉的合約（差集）
"""

import asyncio
import aiohttp
from pathlib import Path
from datetime import datetime

BASE_URL = "https://fapi.binance.com"
OLD_FILE = Path(__file__).parent / "old_all_symbols.txt"
DIFF_FILE = Path(__file__).parent / "new_excluded.txt"


async def fetch_all() -> tuple[list[dict], list[dict]]:
    """回傳 (old_list, new_excluded_list)"""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/fapi/v1/exchangeInfo") as resp:
            resp.raise_for_status()
            info = await resp.json()

    old_all, excluded = [], []
    for s in info["symbols"]:
        if s["quoteAsset"] != "USDT" or s["contractType"] != "PERPETUAL" or s["status"] != "TRADING":
            continue
        entry = {
            "symbol": s["symbol"],
            "baseAsset": s["baseAsset"],
            "underlyingType": s.get("underlyingType", "N/A"),
            "underlyingSubType": ", ".join(s.get("underlyingSubType", [])) or "-",
        }
        old_all.append(entry)
        if s.get("underlyingType") != "COIN":
            excluded.append(entry)

    old_all.sort(key=lambda x: x["symbol"])
    excluded.sort(key=lambda x: x["symbol"])
    return old_all, excluded


def write_old(old_all: list[dict]) -> None:
    lines = [
        f"【舊邏輯】USDT 永續合約完整清單（共 {len(old_all)} 筆）",
        f"產生時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"",
        f"{'symbol':<24}  {'underlyingType':<14}  subType",
        f"{'-'*60}",
    ]
    for s in old_all:
        lines.append(f"  {s['symbol']:<22}  {s['underlyingType']:<14}  {s['underlyingSubType']}")
    OLD_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"舊邏輯清單已寫入：{OLD_FILE}  ({len(old_all)} 筆)")


def write_diff(excluded: list[dict]) -> None:
    lines = [
        f"【新邏輯排除的合約】underlyingType != COIN（共 {len(excluded)} 筆）",
        f"產生時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"",
        f"{'symbol':<24}  {'underlyingType':<14}  subType",
        f"{'-'*60}",
    ]
    for s in excluded:
        lines.append(f"  {s['symbol']:<22}  {s['underlyingType']:<14}  {s['underlyingSubType']}")
    DIFF_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"新增排除清單已寫入：{DIFF_FILE}  ({len(excluded)} 筆)")


async def main() -> None:
    print("正在從 Binance 取得 exchangeInfo ...")
    old_all, excluded = await fetch_all()
    write_old(old_all)
    write_diff(excluded)
    print("\n你說的美股合約（如 MUUSDT、MRVLUSDT）如在 old_all_symbols.txt 找不到，")
    print("代表 Binance 已下架；若找得到，看 underlyingType 欄位是否為 COIN。")


if __name__ == "__main__":
    asyncio.run(main())
