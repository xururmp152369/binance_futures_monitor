"""
test_order_dev.py — 測試指定帳號在 Binance 或 BingX 模擬環境的開單流程

用法：
  python tests/test_order_dev.py --account <帳號> --symbol BTCUSDT
  python tests/test_order_dev.py --account <帳號> --symbol BTCUSDT --exchange bingx
  python tests/test_order_dev.py --account <帳號> --symbol BTCUSDT --side sell
  python tests/test_order_dev.py --account <帳號> --symbol BTCUSDT --mode prd  # 正式環境（謹慎）

流程（模擬完整開單生命週期）：
  1. 建立 adapter（Binance testnet 或 BingX Virtual Trading）
  2. 取得精度資訊
  3. 查詢現有持倉
  4. 設定保證金模式 + 槓桿
  5. 市價開倉
  6. 掛止損單（closePosition）
  7. 掛止盈單（closePosition）
  8. 查詢 closePosition 條件單（驗證掛單成功）
  9. 取消所有 closePosition 條件單
  10. 市價平倉（closePosition）

環境需求：
  - .env 需有 ENCRYPTION_KEY
  - Binance dev 模式：帳號設定需有 API_KEY / SECRET_KEY（Binance Testnet 金鑰）
  - BingX dev 模式：帳號設定需有 API_KEY / SECRET_KEY（BingX Virtual Trading 金鑰）
"""

import asyncio
import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from app.user.user_config import get_user_config
from app.trading.exchange import create_adapter
from app.trading.exchange.bingx_adapter import _binance_to_ccxt


# ─── CLI 參數 ──────────────────────────────────────────────────────────────────

def _parse_args():
    p = argparse.ArgumentParser(description="Binance / BingX 模擬開單流程測試")
    p.add_argument("--account",  required=True, help="帳號名稱（需已設定 API Key）")
    p.add_argument("--symbol",   required=True, help="幣種，例如 BTCUSDT")
    p.add_argument("--exchange", choices=["binance", "bingx"], default=None,
                   help="交易所（預設從帳號設定讀取，可手動覆蓋）")
    p.add_argument("--mode",     choices=["dev", "prd"], default="dev",
                   help="dev=模擬（預設），prd=正式（謹慎使用）")
    p.add_argument("--side",     choices=["buy", "sell"], default="buy",
                   help="buy=多頭（預設），sell=空頭")
    p.add_argument("--notional", type=float, default=10.0,
                   help="名義金額 USD（預設 10.0，實際數量依精度計算）")
    p.add_argument("--leverage", type=int, default=5,
                   help="槓桿倍數（預設 5x）")
    p.add_argument("--verbose", action="store_true",
                   help="啟用 ccxt 詳細 HTTP log（診斷用）")
    return p.parse_args()


# ─── 印出工具 ─────────────────────────────────────────────────────────────────

def ok(msg):  print(f"  ✅ {msg}")
def fail(msg): print(f"  ❌ {msg}")
def info(msg): print(f"  ℹ  {msg}")
def step(n, title): print(f"\n[步驟 {n}] {title}")


# ─── 取得即時價格（BingX 用 ccxt fetch_ticker，Binance 另行呼叫） ───────────────

async def _get_current_price(adapter, symbol: str, exchange: str) -> float:
    """透過 adapter 底層取得即時成交價。"""
    if exchange == "bingx":
        ccxt_sym = _binance_to_ccxt(symbol)
        ticker   = await adapter._exchange.fetch_ticker(ccxt_sym)
        return float(ticker["last"] or ticker["close"] or 0)
    else:
        # Binance：直接呼叫 AsyncClient
        ticker = await adapter._client.futures_symbol_ticker(symbol=symbol)
        return float(ticker["price"])


async def _ensure_binance_client(adapter):
    """Binance adapter 的 _client 需先初始化。"""
    await adapter._get_client()


async def _get_usdt_balance(adapter, exchange: str) -> tuple[float, dict]:
    """查詢帳戶可用 USDT 餘額（永續合約錢包）。
    回傳 (amount, raw_balance_dict)，方便診斷。
    """
    if exchange == "bingx":
        raw = await adapter._exchange.fetch_balance()
        # 1. 標準 ccxt 格式
        free = float((raw.get("USDT") or {}).get("free") or 0)
        if free > 0:
            return free, raw
        # 2. ccxt free dict
        free = float((raw.get("free") or {}).get("USDT") or 0)
        if free > 0:
            return free, raw
        # 3. BingX 原生 info.data.balance（availableMargin）
        bal = ((raw.get("info") or {}).get("data") or {}).get("balance") or {}
        free = float(bal.get("availableMargin") or bal.get("balance") or 0)
        return free, raw
    else:
        balances = await adapter._client.futures_account_balance()
        for b in balances:
            if b["asset"] == "USDT":
                return float(b.get("withdrawAvailable") or b.get("balance") or 0), {}
        return 0.0, {}


# ─── 主流程 ───────────────────────────────────────────────────────────────────

async def main():
    args     = _parse_args()
    symbol   = args.symbol.upper()
    use_prd  = (args.mode == "prd")
    is_long  = (args.side == "buy")
    entry_side = "BUY"  if is_long else "SELL"
    exit_side  = "SELL" if is_long else "BUY"

    # ── 讀取帳號設定 ────────────────────────────────────────────────────────
    cfg = get_user_config(args.account)
    if cfg is None:
        fail(f"找不到帳號 '{args.account}'，請先用 /register 建立並設定 API Key")
        sys.exit(1)

    exchange = args.exchange or cfg.get("EXCHANGE", "binance").lower()
    api_key    = cfg["PRD_API_KEY"]    if use_prd else cfg.get("API_KEY", "")
    api_secret = cfg["PRD_SECRET_KEY"] if use_prd else cfg.get("SECRET_KEY", "")

    if not api_key or not api_secret:
        key_name = ("PRD_API_KEY/PRD_SECRET_KEY" if use_prd else "API_KEY/SECRET_KEY")
        fail(f"帳號設定缺少 {key_name}，請先在 Telegram 設定")
        sys.exit(1)

    print(f"\n{'='*65}")
    print(f"  帳號：{args.account}  |  交易所：{exchange.upper()}  |  模式：{'正式⚠️' if use_prd else '模擬✅'}")
    print(f"  幣種：{symbol}  |  方向：{'多頭(BUY)' if is_long else '空頭(SELL)'}  |  名義金額：${args.notional}")
    print(f"{'='*65}")

    if use_prd:
        print("\n  ⚠️  正式模式：會動用真實資金，5 秒後開始...")
        await asyncio.sleep(5)

    # ── 建立 Adapter ─────────────────────────────────────────────────────────
    step(1, f"建立 {exchange.upper()} Adapter（{'正式' if use_prd else '模擬'}環境）")
    adapter = create_adapter(exchange, api_key, api_secret, use_prd)
    try:
        # 讓 Binance adapter 初始化連線
        if exchange == "binance":
            await _ensure_binance_client(adapter)
        if exchange == "bingx":
            if args.verbose:
                adapter._exchange.verbose = True
                info("ccxt verbose mode 已啟用")
            # 印出實際使用的 API 端點確認 VST/PRD
            api_urls = adapter._exchange.urls.get("api", {})
            hosts = set()
            for v in api_urls.values():
                if isinstance(v, str) and "bingx.com" in v:
                    from urllib.parse import urlparse
                    hosts.add(urlparse(v).netloc)
            info(f"BingX API host：{hosts or api_urls}")
        ok(f"Adapter 建立成功（{'Binance Testnet' if not use_prd and exchange == 'binance' else 'BingX Virtual Trading' if not use_prd and exchange == 'bingx' else '正式環境'}）")
    except Exception as e:
        fail(f"Adapter 建立失敗：{e}")
        sys.exit(1)

    try:
        # ── BingX：確認 Hedge Mode（雙向持倉）────────────────────────────────
        if exchange == "bingx":
            step("1.5", "確認 BingX 帳戶為 Hedge Mode（雙向持倉）")
            try:
                await adapter._exchange.set_position_mode(True)
                ok("帳戶已設為 Hedge Mode")
            except Exception as e:
                msg = str(e).lower()
                if any(k in msg for k in ("no need", "already", "same", "not change", "position mode")):
                    ok("帳戶已在 Hedge Mode")
                else:
                    info(f"Hedge Mode 設定回應（繼續）：{e}")
        # ── 取得精度 ─────────────────────────────────────────────────────────
        step(2, f"取得 {symbol} 精度資訊")
        qty_p, price_p = await adapter.get_precisions(symbol)
        ok(f"qty_precision={qty_p}  price_precision={price_p}")

        # ── 查詢現有持倉 + 帳戶餘額 ─────────────────────────────────────────
        step(3, "查詢帳戶狀態（持倉 + USDT 餘額）")
        positions = await adapter.get_open_positions()
        same = [p for p in positions if p["symbol"] == symbol]
        if same:
            info(f"已有 {symbol} 持倉：{same}")
        else:
            ok(f"無 {symbol} 持倉（共 {len(positions)} 筆持倉）")
        try:
            usdt_free, raw_bal = await _get_usdt_balance(adapter, exchange)
            if usdt_free < 5:
                # 印出原始結構幫助診斷
                if exchange == "bingx" and raw_bal:
                    top = {k: raw_bal[k] for k in raw_bal if k != "info"}
                    info(f"  [診斷] 頂層鍵值：{list(top.keys())}")
                    info(f"  [診斷] USDT 項：{raw_bal.get('USDT')}")
                    info(f"  [診斷] free 項：{raw_bal.get('free')}")
                    bal_info = ((raw_bal.get("info") or {}).get("data") or {}).get("balance")
                    info(f"  [診斷] info.data.balance：{bal_info}")
                fail(f"USDT 可用餘額：{usdt_free:.2f}（若帳戶確有資金，請將診斷資訊回報）")
            else:
                ok(f"USDT 可用餘額：{usdt_free:.2f}")
        except Exception as e:
            info(f"查詢 USDT 餘額失敗（繼續）：{e}")

        # ── 取得即時價格 ─────────────────────────────────────────────────────
        step(4, "取得即時市場價格")
        try:
            price = await _get_current_price(adapter, symbol, exchange)
            ok(f"即時價格：{price:.{price_p}f}")
        except Exception as e:
            fail(f"取得價格失敗：{e}")
            sys.exit(1)

        # ── 計算下單量（ceiling 確保名義值達標） ─────────────────────────────
        step_size = 10 ** (-qty_p)
        qty = math.ceil(args.notional / price / step_size) * step_size
        qty = round(qty, qty_p)
        actual_notional = qty * price
        info(f"計算數量：{qty}（step={step_size}，實際名義值≈${actual_notional:.2f}）")

        # ── 設定保證金模式 ────────────────────────────────────────────────────
        step(5, "設定保證金模式（ISOLATED）")
        try:
            await adapter.set_margin_type(symbol, "ISOLATED")
            ok("保證金模式設定完成")
        except Exception as e:
            info(f"設定保證金模式失敗（忽略）：{e}")

        # ── 設定槓桿 ─────────────────────────────────────────────────────────
        step(6, f"設定槓桿 {args.leverage}x")
        try:
            await adapter.set_leverage(symbol, args.leverage)
            ok(f"槓桿設為 {args.leverage}x")
        except Exception as e:
            fail(f"設定槓桿失敗：{e}")

        # ── 清除殘留條件單 ───────────────────────────────────────────────────
        step(7, "清除殘留掛單")
        try:
            await adapter.cancel_all_open_orders(symbol)
            cancelled_b = await adapter.cancel_close_position_orders(symbol, "BUY")
            cancelled_s = await adapter.cancel_close_position_orders(symbol, "SELL")
            total = cancelled_b + cancelled_s
            ok(f"清除完成（取消 {total} 個舊 closePosition 條件單）")
        except Exception as e:
            info(f"清除掛單時發生警告（繼續）：{e}")

        # ── 市價開倉 ─────────────────────────────────────────────────────────
        step(8, f"市價{entry_side}開倉 qty={qty}")
        safe_sym   = symbol.encode("ascii", errors="ignore").decode("ascii")
        order_id   = f"cc_test_{safe_sym}_{int(time.time() * 1000)}"
        fill_price = None
        try:
            result = await adapter.create_market_order(symbol, entry_side, qty, order_id)
            fill_price = result["avgPrice"] or price
            filled_qty = result["executedQty"] or qty
            ok(f"開倉成功  avgPrice={fill_price:.{price_p}f}  filledQty={filled_qty}")
        except Exception as e:
            fail(f"開倉失敗：{e}")
            sys.exit(1)

        await asyncio.sleep(1)

        # ── 掛止損單 ─────────────────────────────────────────────────────────
        sl_dist  = fill_price * 0.02            # 入場價 ±2% 作為測試止損距離
        sl_price = fill_price - sl_dist if is_long else fill_price + sl_dist
        sl_price = round(sl_price, price_p)

        step(9, f"掛止損單 stopPrice={sl_price:.{price_p}f}（closePosition）")
        try:
            await adapter.create_stop_market_order(
                symbol, exit_side, sl_price, close_position=True
            )
            ok(f"止損掛出成功")
        except Exception as e:
            fail(f"止損掛出失敗：{e}")

        # ── 掛止盈單 ─────────────────────────────────────────────────────────
        tp_price = fill_price + sl_dist * 2 if is_long else fill_price - sl_dist * 2
        tp_price = round(tp_price, price_p)

        step(10, f"掛止盈單 stopPrice={tp_price:.{price_p}f}（closePosition）")
        try:
            await adapter.create_take_profit_market_order(
                symbol, exit_side, tp_price, close_position=True
            )
            ok(f"止盈掛出成功")
        except Exception as e:
            fail(f"止盈掛出失敗：{e}")

        await asyncio.sleep(1)

        # ── 查詢 closePosition 條件單 ────────────────────────────────────────
        step(11, "查詢已掛的 closePosition 條件單")
        try:
            close_orders = await adapter.get_close_position_orders(symbol, exit_side)
            if close_orders:
                ok(f"找到 {len(close_orders)} 個條件單：")
                for o in close_orders:
                    print(f"       orderId={o['orderId']}  type={o['type']}  stopPrice={o['stopPrice']}")
            else:
                info("未查到條件單（可能交易所有延遲，屬正常）")
        except Exception as e:
            fail(f"查詢條件單失敗：{e}")

        # ── 取消所有 closePosition 條件單 ────────────────────────────────────
        step(12, "取消所有 closePosition 條件單")
        try:
            cancelled = await adapter.cancel_close_position_orders(symbol, exit_side)
            ok(f"已取消 {cancelled} 個條件單")
        except Exception as e:
            fail(f"取消條件單失敗：{e}")

        await asyncio.sleep(0.5)

        # ── 市價平倉（reduce_only） ───────────────────────────────────────────
        step(13, f"市價{exit_side}平倉（reduce_only）")
        close_price = None
        try:
            close_id = f"cc_close_{safe_sym}_{int(time.time() * 1000)}"
            close_result = await adapter.create_market_order(
                symbol, exit_side, qty, close_id, reduce_only=True
            )
            close_price = close_result["avgPrice"] or price
            ok(f"平倉成功  avgPrice={close_price:.{price_p}f}")
        except Exception as e:
            fail(f"平倉失敗：{e}")
            print(f"\n  ⚠️  請手動登入 {'Binance Testnet' if exchange == 'binance' else 'BingX Virtual Trading'} 平掉 {symbol} 持倉！")

        # ── 結果摘要 ─────────────────────────────────────────────────────────
        print(f"\n{'='*65}")
        print(f"  測試完成！")
        if fill_price and close_price:
            pnl_pct = (close_price - fill_price) / fill_price * 100 * (1 if is_long else -1)
            print(f"  開倉價：{fill_price:.{price_p}f}  平倉價：{close_price:.{price_p}f}  模擬損益：{pnl_pct:+.3f}%")
        print(f"{'='*65}\n")

    except KeyboardInterrupt:
        print("\n\n⚠️  使用者中斷！請確認是否有未平倉的模擬持倉")
        sys.exit(1)
    finally:
        await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
