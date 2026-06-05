"""
test_order_prd.py — 對指定帳號的 Binance 期貨下 $1 測試單（正式 / 模擬）

用法：
  python tests/test_order_prd.py --account xururmp152369 --symbol BTCUSDT --mode prd
  python tests/test_order_prd.py --account xururmp152369 --symbol LOBSTERUSDT --mode prd

流程：
  1. 讀取帳號加密設定
  2. 設定 1x 槓桿
  3. 計算最小合法數量（≥ $1 且符合 step size）
  4. 市價買入（多頭）或市價賣出（空頭，傳入 --side sell）
  5. 立即市價平倉（reduceOnly）
  6. 印出每步驟結果

環境需求：
  - .env 需有 ENCRYPTION_KEY
  - 帳號需有對應的 API Key（prd 模式用 PRD_API_KEY/PRD_SECRET_KEY）
"""

import asyncio
import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from binance import AsyncClient
from app.user.user_config import get_user_config


def _parse_args():
    p = argparse.ArgumentParser(description="Binance 期貨 $1 測試下單")
    p.add_argument("--account", required=True, help="帳號名稱")
    p.add_argument("--symbol",  required=True, help="交易對，例如 BTCUSDT")
    p.add_argument("--mode",    choices=["dev", "prd"], default="prd",
                   help="dev=模擬帳號，prd=正式帳號（預設）")
    p.add_argument("--side",    choices=["buy", "sell"], default="buy",
                   help="buy=開多（預設），sell=開空")
    p.add_argument("--notional", type=float, default=10.0,
                   help="名義金額（USD，預設 10.0）")
    return p.parse_args()



async def main():
    args    = _parse_args()
    symbol  = args.symbol.upper()
    use_prd = args.mode == "prd"
    is_long = args.side == "buy"
    notional = args.notional

    print(f"\n{'='*60}")
    print(f" 帳號：{args.account}  |  模式：{'正式' if use_prd else '模擬'}")
    print(f" 幣種：{symbol}  |  方向：{'多頭(BUY)' if is_long else '空頭(SELL)'}  |  名義金額：${notional}")
    print(f"{'='*60}")

    cfg = get_user_config(args.account)
    if cfg is None:
        print(f"[錯誤] 找不到帳號 '{args.account}'")
        sys.exit(1)

    api_key    = cfg["PRD_API_KEY"]    if use_prd else cfg["API_KEY"]
    api_secret = cfg["PRD_SECRET_KEY"] if use_prd else cfg["SECRET_KEY"]

    client = None
    entry_order_id = None
    close_order_id = None

    try:
        client = await AsyncClient.create(
            api_key=api_key,
            api_secret=api_secret,
            testnet=not use_prd,
        )
        print("\n✅ 連線成功")

        # ── 取得精度 ─────────────────────────────────────────────────────
        info = await client.futures_exchange_info()
        sym_info = next((s for s in info["symbols"] if s["symbol"] == symbol), None)
        if sym_info is None:
            print(f"[錯誤] 找不到 {symbol}，請確認幣種名稱")
            sys.exit(1)

        qty_precision   = int(sym_info.get("quantityPrecision", 3))
        price_precision = int(sym_info.get("pricePrecision", 2))

        # step size（最小數量單位）
        lot_filter = next(
            (f for f in sym_info.get("filters", []) if f["filterType"] == "LOT_SIZE"),
            None,
        )
        step_size = float(lot_filter["stepSize"]) if lot_filter else 10 ** (-qty_precision)

        # 最低名義金額（-4164 錯誤來源）
        notional_filter = next(
            (f for f in sym_info.get("filters", []) if f["filterType"] == "MIN_NOTIONAL"),
            None,
        )
        min_notional = float(notional_filter["notional"]) if notional_filter else 5.0
        print(f"   交易所最低名義金額：${min_notional}（filter={'有' if notional_filter else '無，預設5'}）")
        if notional < min_notional:
            print(f"   ⚠️  調整名義金額：${notional} → ${min_notional}（交易所最低要求）")
            notional = min_notional

        # ── 取得即時價格 ─────────────────────────────────────────────────
        ticker = await client.futures_symbol_ticker(symbol=symbol)
        price  = float(ticker["price"])
        print(f"\n   即時價格：{price}")

        # ── 計算數量：ceiling 確保名義值 >= min_notional（floor 會差幾分錢被拒）─────
        qty = math.ceil(notional / price / step_size) * step_size
        qty = round(qty, qty_precision)
        actual_notional = qty * price
        print(f"   計算數量：{qty}（step={step_size}, min_notional=${min_notional}, 實際名義值≈${actual_notional:.4f}）")

        # ── 設定 5x 槓桿 ──────────────────────────────────────────────────
        try:
            await client.futures_change_leverage(symbol=symbol, leverage=5)
            print(f"\n✅ 槓桿設定為 5x")
        except Exception as e:
            print(f"⚠️  設定槓桿失敗（可能已是 5x）：{e}")

        # ── 市價開倉 ──────────────────────────────────────────────────────
        entry_side = "BUY" if is_long else "SELL"
        close_side = "SELL" if is_long else "BUY"

        import time
        safe_sym = symbol.encode("ascii", errors="ignore").decode("ascii")
        entry_order_id = f"cc_test_{safe_sym}_{int(time.time() * 1000)}"

        print(f"\n→ 市價{entry_side}開倉 qty={qty}，newClientOrderId={entry_order_id!r} ...")
        entry = await client.futures_create_order(
            symbol=symbol,
            side=entry_side,
            type="MARKET",
            quantity=qty,
            newClientOrderId=entry_order_id,
        )
        fill_price = float(entry.get("avgPrice") or 0) or price
        print(f"✅ 開倉成功！orderId={entry.get('orderId')}  avgPrice={fill_price:.{price_precision}f}")

        await asyncio.sleep(0.5)

        # ── 立即市價平倉 ─────────────────────────────────────────────────
        close_order_id = f"cc_close_{safe_sym}_{int(time.time() * 1000)}"
        print(f"\n→ 市價{close_side}平倉 qty={qty}，newClientOrderId={close_order_id!r} ...")
        close = await client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type="MARKET",
            quantity=qty,
            reduceOnly=True,
            newClientOrderId=close_order_id,
        )
        print(f"✅ 平倉成功！orderId={close.get('orderId')}  status={close.get('status')}")

        print(f"\n{'='*60}")
        print(" 測試完成，開倉 + 平倉均成功")
        print(f"{'='*60}\n")

    except Exception as e:
        print(f"\n❌ 失敗：{e}")
        if entry_order_id and not close_order_id:
            print(f"⚠️  開倉已送出但平倉失敗！請手動查詢 {symbol} 持倉並平倉")
        sys.exit(1)
    finally:
        if client:
            await client.close_connection()


if __name__ == "__main__":
    asyncio.run(main())
