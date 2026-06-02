"""
test_orders_diag.py - 查詢 / 取消指定帳號的掛單（本機測試用）

此腳本不是自動化測試，需要手動執行（需要真實 API Key 與網路連線至 Binance）：

  python tests/test_orders_diag.py --account <帳號名> --symbol <幣種> [--mode dev|prd] [--cancel]

  --account  帳號名稱（對應 app/user/configs/ 目錄下的加密設定檔）
  --symbol   交易對，例如 BTCUSDT
  --mode     dev（模擬，預設）| prd（正式）
  --cancel   取消所有 closePosition 止損止盈條件單
"""
import asyncio
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from binance import AsyncClient
from app.user.user_config import get_user_config


def _parse_args():
    parser = argparse.ArgumentParser(description="查詢 / 取消 Binance 期貨掛單（本機測試用）")
    parser.add_argument("--account", required=True, help="帳號名稱")
    parser.add_argument("--symbol",  required=True, help="交易對（例如 BTCUSDT）")
    parser.add_argument(
        "--mode", choices=["dev", "prd"], default="dev",
        help="dev=模擬帳號（預設），prd=正式帳號"
    )
    parser.add_argument("--cancel", action="store_true", help="取消所有 closePosition 條件單")
    parser.add_argument("--try-sl", metavar="PRICE", type=float,
                        help="試著掛一筆止損（指定止損價），成功後自動取消，用來診斷下單錯誤")
    return parser.parse_args()


def _is_close_position(order: dict) -> bool:
    return (
        order.get("closePosition") is True
        or str(order.get("closePosition", "")).lower() == "true"
    )


async def main():
    args    = _parse_args()
    symbol  = args.symbol.upper()
    use_prd = args.mode == "prd"

    cfg = get_user_config(args.account)
    if cfg is None:
        print(f"[錯誤] 找不到帳號 '{args.account}'，請確認 app/user/configs/ 目錄下有對應的加密設定檔")
        sys.exit(1)

    try:
        api_key    = cfg["PRD_API_KEY"]    if use_prd else cfg["API_KEY"]
        api_secret = cfg["PRD_SECRET_KEY"] if use_prd else cfg["SECRET_KEY"]
    except KeyError as e:
        mode_label = "正式" if use_prd else "模擬"
        print(f"[錯誤] 帳號設定缺少欄位 {e}（模式：{mode_label}），請確認帳號已設定對應的 API key")
        sys.exit(1)

    mode_label = "正式" if use_prd else "模擬"
    print(f"\n{'='*62}")
    print(f" 帳號：{args.account}  |  幣種：{symbol}  |  模式：{mode_label}")
    print(f"{'='*62}")

    client = None
    try:
        client = await AsyncClient.create(
            api_key=api_key,
            api_secret=api_secret,
            testnet=not use_prd,
        )

        # ── 當前持倉 ────────────────────────────────────────────────────
        all_positions = await client.futures_position_information()
        active = [p for p in all_positions if float(p.get("positionAmt", 0)) != 0]
        print(f"\n【當前持倉】共 {len(active)} 個")
        for p in active:
            print(f"  {p['symbol']:>15}  positionAmt={p['positionAmt']:>12}  entryPrice={p['entryPrice']}")

        symbol_pos = [p for p in active if p["symbol"] == symbol]
        if not symbol_pos:
            print(f"\n  ⚠️  帳號在此交易所沒有 {symbol} 的持倉，請確認 --mode 是否正確")

        # ── openAlgoOrders：查詢 closePosition 條件單 ───────────────────
        # futures_get_open_orders 查不到這類單，須走 /fapi/v1/openAlgoOrders
        raw = await client.futures_get_open_algo_orders(symbol=symbol)
        algo_orders = raw if isinstance(raw, list) else raw.get("orders", [])

        print(f"\n【openAlgoOrders(symbol={symbol})】共 {len(algo_orders)} 筆")
        if algo_orders:
            print(f"  {'algoId':>10}  {'algoType':>22}  {'side':>5}  {'triggerPrice':>14}  {'closePos':>8}  {'orderId':>15}")
            print(f"  {'-'*10}  {'-'*22}  {'-'*5}  {'-'*14}  {'-'*8}  {'-'*15}")
            for o in algo_orders:
                close_pos = "是" if _is_close_position(o) else "否"
                trigger   = o.get("triggerPrice") or o.get("stopPrice") or 0
                print(
                    f"  {str(o.get('algoId','')):>10}"
                    f"  {str(o.get('algoType','')):>22}"
                    f"  {str(o.get('side','')):>5}"
                    f"  {float(trigger):>14.4f}"
                    f"  {close_pos:>8}"
                    f"  {str(o.get('orderId','')):>15}"
                )
        else:
            print("  （無條件單）")

        # ── 試掛止損（診斷用）──────────────────────────────────────────
        if args.try_sl is not None:
            stop_price = args.try_sl
            pos = next((p for p in active if p["symbol"] == symbol), None)
            if pos is None:
                print(f"\n【試掛止損】⚠️ 帳號沒有 {symbol} 持倉，無法試掛")
            else:
                pos_amt   = float(pos["positionAmt"])
                exit_side = "SELL" if pos_amt > 0 else "BUY"
                print(f"\n【試掛止損】{symbol}  持倉方向={'多頭' if pos_amt > 0 else '空頭'}  exit_side={exit_side}  stopPrice={stop_price}")

                print(f"  → 先清除 {symbol} 所有 closePosition 條件單...")
                close_orders = [o for o in algo_orders if _is_close_position(o)]
                cancelled = 0
                for o in close_orders:
                    try:
                        await client.futures_cancel_algo_order(algoId=o["algoId"])
                        cancelled += 1
                    except Exception as e:
                        print(f"  ✗ 取消失敗 algoId={o['algoId']}：{e}")
                print(f"  ✓ 已取消 {cancelled} 筆 closePosition 條件單")
                await asyncio.sleep(1)

                placed_id = None
                try:
                    result = await client.futures_create_order(
                        symbol=symbol,
                        side=exit_side,
                        type="STOP_MARKET",
                        stopPrice=stop_price,
                        closePosition=True,
                        workingType="MARK_PRICE",
                    )
                    placed_id = result.get("orderId")
                    print(f"  ✓ 止損掛單成功！orderId={placed_id}  status={result.get('status')}")
                except Exception as e:
                    print(f"  ✗ 掛單仍然失敗，完整錯誤：{e}")

                if placed_id:
                    await asyncio.sleep(0.3)
                    try:
                        await client.futures_cancel_order(symbol=symbol, orderId=placed_id)
                        print(f"  ✓ 測試單已自動取消")
                    except Exception as e:
                        print(f"  ⚠️ 自動取消失敗，請手動取消 orderId={placed_id}：{e}")

        # ── 取消 closePosition 訂單 ────────────────────────────────────
        if args.cancel:
            print(f"\n【取消 closePosition 條件單】")
            targets = [o for o in algo_orders if _is_close_position(o)]

            if not targets:
                print("  （無 closePosition 條件單可取消）")
            else:
                for o in targets:
                    try:
                        await client.futures_cancel_algo_order(algoId=o["algoId"])
                        trigger = o.get("triggerPrice") or o.get("stopPrice") or 0
                        print(
                            f"  ✓ 取消成功"
                            f"  algoId={o['algoId']}"
                            f"  algoType={o.get('algoType')}"
                            f"  side={o.get('side')}"
                            f"  triggerPrice={float(trigger):.4f}"
                        )
                    except Exception as e:
                        print(f"  ✗ 取消失敗  algoId={o.get('algoId')}  錯誤：{e}")

                await asyncio.sleep(0.5)

                # 確認剩餘
                raw2 = await client.futures_get_open_algo_orders(symbol=symbol)
                remaining = raw2 if isinstance(raw2, list) else raw2.get("orders", [])
                remaining_close = [o for o in remaining if _is_close_position(o)]
                print(f"\n  取消後剩餘 closePosition 條件單：{len(remaining_close)} 筆")

    except Exception as e:
        print(f"[錯誤] {e}")
        sys.exit(1)
    finally:
        if client:
            await client.close_connection()


if __name__ == "__main__":
    asyncio.run(main())
