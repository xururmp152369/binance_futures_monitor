"""
驗證腳本：從 Binance exchangeInfo 取出所有非 COIN 合約（美股 TRADIFI_PERPETUAL / 指數等），
輸出到 scripts/equity_symbols.txt 供人工確認。

執行方式：
    python scripts/check_equity_symbols.py
"""

import asyncio
import aiohttp
from pathlib import Path
from datetime import datetime

BASE_URL = "https://fapi.binance.com"
OUTPUT_FILE = Path(__file__).parent / "equity_symbols.txt"

# 納入所有可能的加密貨幣永續合約類型
CRYPTO_CONTRACT_TYPES = {"PERPETUAL", "CURRENT_QUARTER", "NEXT_QUARTER"}


async def fetch_equity_symbols() -> tuple[list[dict], list[dict]]:
    """回傳 (coin_contracts, non_coin_contracts) 兩份清單。"""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/fapi/v1/exchangeInfo") as resp:
            resp.raise_for_status()
            info = await resp.json()

    coin, non_coin = [], []
    for s in info["symbols"]:
        if s["quoteAsset"] != "USDT" or s["status"] != "TRADING":
            continue
        entry = {
            "symbol": s["symbol"],
            "baseAsset": s["baseAsset"],
            "contractType": s.get("contractType", "N/A"),
            "underlyingType": s.get("underlyingType", "N/A"),
            "underlyingSubType": s.get("underlyingSubType", []),
        }
        if s.get("underlyingType") == "COIN":
            coin.append(entry)
        else:
            non_coin.append(entry)

    return sorted(coin, key=lambda x: x["symbol"]), sorted(non_coin, key=lambda x: x["symbol"])


def write_report(coin: list[dict], non_coin: list[dict]) -> None:
    lines = [
        f"Binance USDT 合約 — underlyingType 分類報告",
        f"產生時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"",
        f"{'='*70}",
        f"【被排除的非 COIN 合約】（共 {len(non_coin)} 筆）",
        f"{'='*70}",
    ]
    if non_coin:
        for s in non_coin:
            sub = ", ".join(s["underlyingSubType"]) if s["underlyingSubType"] else "-"
            lines.append(
                f"  {s['symbol']:<22}  contractType={s['contractType']:<22}  "
                f"underlyingType={s['underlyingType']:<10}  subType={sub}"
            )
    else:
        lines.append("  （無）")

    lines += [
        f"",
        f"{'='*70}",
        f"【保留的 COIN 合約】（共 {len(coin)} 筆）",
        f"{'='*70}",
    ]
    for s in coin:
        sub = ", ".join(s["underlyingSubType"]) if s["underlyingSubType"] else "-"
        lines.append(f"  {s['symbol']:<22}  subType={sub}")

    OUTPUT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"報告已寫入：{OUTPUT_FILE}")
    print(f"  被排除合約：{len(non_coin)} 筆")
    print(f"  保留合約  ：{len(coin)} 筆")
    print("\n被排除清單預覽（前 20 筆）：")
    for s in non_coin[:20]:
        print(f"  {s['symbol']:<22}  {s['contractType']}")
    if len(non_coin) > 20:
        print(f"  ... 另有 {len(non_coin) - 20} 筆，請查看 {OUTPUT_FILE}")


async def main() -> None:
    print("正在從 Binance 取得 exchangeInfo ...")
    coin, non_coin = await fetch_equity_symbols()
    write_report(coin, non_coin)


if __name__ == "__main__":
    asyncio.run(main())
