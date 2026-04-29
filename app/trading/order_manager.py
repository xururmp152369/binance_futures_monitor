import asyncio
import json
import math
import time
from pathlib import Path
from binance import AsyncClient
from ..extension.utils import setup_logging

log = setup_logging()

_DATA_DIR = Path(__file__).parent.parent.parent / "data" / "users"

# True = testnet (https://testnet.binancefuture.com)，測試完畢後改 False 切回正式
USE_TESTNET = True


def _floor_to_precision(value: float, precision: int) -> float:
    """依精度無條件捨去，避免超出 step size 上界導致下單被拒。"""
    factor = 10 ** precision
    return math.floor(value * factor) / factor


def _calc_quantity(cfg: dict, entry_price: float, stop_loss: float) -> float:
    risk_type   = cfg["RISK_TYPE"]
    risk_amount = cfg["RISK_AMOUNT"]
    leverage    = cfg["RISK_LEVERAGE"]

    if risk_type == 0:
        # 固定投入：(RISK_AMOUNT × 槓桿) / 入場價
        return (risk_amount * leverage) / entry_price
    else:
        # 固定損失：RISK_AMOUNT / 止損點差
        sl_dist = entry_price - stop_loss
        if sl_dist <= 0:
            return 0.0
        return risk_amount / sl_dist


async def _get_precisions(client: AsyncClient, symbol: str) -> tuple[int, int]:
    """回傳 (qty_precision, price_precision)，失敗時預設 (3, 2)。"""
    try:
        info = await client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                return int(s.get("quantityPrecision", 3)), int(s.get("pricePrecision", 2))
    except Exception as e:
        log.warning(f"[自動開單] 無法取得 {symbol} 精度資訊，使用預設值: {e}")
    return 3, 2


async def _get_open_positions(client: AsyncClient) -> list[dict]:
    """回傳所有有持倉的合約部位。"""
    positions = await client.futures_position_information()
    return [p for p in positions if float(p.get("positionAmt", 0)) != 0]


async def _place_orders_for_user(cfg: dict, symbol: str, signal: dict) -> None:
    # 策略類型比對（signal type: "type1"/"type2"，config STRATEGY: ["TYPE1","TYPE2"]）
    signal_type = signal["type"].upper()
    if signal_type not in [s.upper() for s in cfg.get("STRATEGY", [])]:
        return

    # 黑名單檢查
    blacklist = [s.upper() for s in cfg.get("SYMBOL_BLACKLIST", [])]
    if symbol.upper() in blacklist:
        log.info(f"[自動開單] {symbol} 在黑名單，略過")
        return

    client = None
    try:
        client = await AsyncClient.create(
            api_key=cfg["API_KEY"],
            api_secret=cfg["SECRET_KEY"],
            testnet=USE_TESTNET,
        )

        # 部位上限 & 加倉檢查
        open_positions = await _get_open_positions(client)
        open_symbols   = {p["symbol"] for p in open_positions}

        if len(open_positions) >= cfg["ORDER_LIMIT"]:
            log.info(
                f"[自動開單] 部位已達上限 ({len(open_positions)}/{cfg['ORDER_LIMIT']})，略過 {symbol}"
            )
            return

        if symbol in open_symbols and not cfg.get("ADD_SAME_SYMBOL", False):
            log.info(f"[自動開單] {symbol} 已有持倉且不允許加倉，略過")
            return

        # 設定保證金模式（逐倉 ISOLATED / 全倉 CROSSED，預設全倉）
        margin_type = cfg.get("MARGIN_TYPE", "CROSSED").upper()
        try:
            await client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
            log.info(f"[自動開單] {symbol} 保證金模式設為 {margin_type}")
        except Exception as e:
            if "-4046" not in str(e):  # -4046 = 已是目標模式，無需修改
                log.warning(f"[自動開單] {symbol} 設定保證金模式失敗（忽略）: {e}")

        # 設定槓桿
        leverage = cfg["RISK_LEVERAGE"]
        await client.futures_change_leverage(symbol=symbol, leverage=leverage)
        log.info(f"[自動開單] {symbol} 槓桿設為 {leverage}x")

        # 無現有持倉才清除殘留條件單（加倉時保留原有 SL/TP，不中斷保護）
        if symbol not in open_symbols:
            try:
                await client.futures_cancel_all_algo_open_orders(symbol=symbol)
                log.info(f"[自動開單] {symbol} 已清除舊條件單")
            except Exception as e:
                log.warning(f"[自動開單] {symbol} 清除舊條件單失敗（忽略）: {e}")

        # 計算下單量
        entry_price                   = signal["close"]
        stop_loss                     = signal["stop_loss"]
        qty_precision, price_precision = await _get_precisions(client, symbol)
        raw_qty                        = _calc_quantity(cfg, entry_price, stop_loss)
        qty                            = _floor_to_precision(raw_qty, qty_precision)

        if qty <= 0:
            log.error(f"[自動開單] {symbol} 計算下單量無效 raw={raw_qty:.6f}，略過")
            return

        # 市價開多（確認倉位成立後才設置 SL/TP，最多重試 5 次）
        MAX_RETRIES = 5
        fill_price = None
        filled_qty = qty  # 預設用計算量，成交後以 executedQty 覆蓋
        for attempt in range(1, MAX_RETRIES + 1):
            order_id = f"cc_{symbol}_{int(time.time() * 1000)}"
            try:
                order = await client.futures_create_order(
                    symbol=symbol,
                    side="BUY",
                    type="MARKET",
                    quantity=qty,
                    newClientOrderId=order_id,
                )
                fill_price = float(order.get("avgPrice") or 0) or entry_price
                _exec = float(order.get("executedQty") or 0)
                filled_qty = _floor_to_precision(
                    _exec if _exec > 0 else qty, qty_precision
                )
                log.info(
                    f"[自動開單] {symbol} 市價開倉成功 attempt={attempt}"
                    f" filled_qty={filled_qty} fill={fill_price:.6f}"
                )
                break
            except Exception as order_err:
                if "-1007" not in str(order_err):
                    log.error(f"[自動開單] {symbol} 開倉失敗: {order_err}")
                    return
                # -1007：逾時，用 newClientOrderId 查詢訂單確認真實狀態
                log.warning(
                    f"[自動開單] {symbol} 開倉逾時 (-1007) attempt={attempt}/{MAX_RETRIES}，查詢訂單..."
                )
                await asyncio.sleep(2)
                try:
                    queried = await client.futures_get_order(
                        symbol=symbol, origClientOrderId=order_id
                    )
                    if queried.get("status") == "FILLED":
                        fill_price = float(queried.get("avgPrice") or 0) or entry_price
                        _exec = float(queried.get("executedQty") or 0)
                        filled_qty = _floor_to_precision(
                            _exec if _exec > 0 else qty, qty_precision
                        )
                        log.warning(
                            f"[自動開單] {symbol} 逾時但訂單已成立"
                            f" filled_qty={filled_qty} fill={fill_price:.6f}，繼續設 SL/TP"
                        )
                        break
                    log.warning(
                        f"[自動開單] {symbol} 訂單未成立 status={queried.get('status')}，重試開倉..."
                    )
                except Exception:
                    log.warning(f"[自動開單] {symbol} 查詢訂單失敗，重試開倉...")

        if fill_price is None:
            log.error(f"[自動開單] {symbol} 重試 {MAX_RETRIES} 次後仍無法確認倉位，放棄")
            return

        # 等待倉位入帳後再掛條件單，避免 reduceOnly 因倉位尚未反映而被拒
        await asyncio.sleep(1)

        # 止損單：closePosition=True，觸價時平全倉，不依賴數量精度
        try:
            await client.futures_create_order(
                symbol=symbol,
                side="SELL",
                type="STOP_MARKET",
                stopPrice=round(stop_loss, price_precision),
                closePosition=True,
                workingType="MARK_PRICE",
            )
            log.info(f"[自動開單] {symbol} 止損掛出 stopPrice={stop_loss:.6f} (closePosition)")
        except Exception as e:
            log.error(f"[自動開單] {symbol} 止損掛出失敗: {e}")

        # 止盈單（分批）
        # 最後一筆用 closePosition=True 確保完全平倉；其餘用數量單 + reduceOnly
        tp_strategy = cfg.get("TP_STRATEGY", [])
        last_idx = len(tp_strategy) - 1
        for i, tp_entry in enumerate(tp_strategy):
            rr       = tp_entry["RR_RATIO"]
            pct      = tp_entry["PERCENT"]
            tp_price = fill_price + (fill_price - stop_loss) * rr
            is_last  = (i == last_idx)

            try:
                if is_last:
                    await client.futures_create_order(
                        symbol=symbol,
                        side="SELL",
                        type="TAKE_PROFIT_MARKET",
                        stopPrice=round(tp_price, price_precision),
                        closePosition=True,
                        workingType="MARK_PRICE",
                    )
                    log.info(
                        f"[自動開單] {symbol} 止盈{i + 1} stopPrice={tp_price:.6f} (closePosition)"
                    )
                else:
                    tp_qty = _floor_to_precision(filled_qty * pct / 100, qty_precision)
                    if tp_qty <= 0:
                        continue
                    await client.futures_create_order(
                        symbol=symbol,
                        side="SELL",
                        type="TAKE_PROFIT_MARKET",
                        stopPrice=round(tp_price, price_precision),
                        quantity=tp_qty,
                        reduceOnly=True,
                        workingType="MARK_PRICE",
                    )
                    log.info(
                        f"[自動開單] {symbol} 止盈{i + 1} stopPrice={tp_price:.6f} qty={tp_qty}"
                    )
            except Exception as e:
                log.error(f"[自動開單] {symbol} 止盈{i + 1} 掛出失敗: {e}")

    except Exception as e:
        log.error(f"[自動開單] {symbol} 開單失敗: {e}")
    finally:
        if client:
            await client.close_connection()


async def place_orders_for_all_users(symbol: str, signal: dict) -> None:
    """遍歷所有使用者設定，對符合條件的使用者在 Binance 期貨自動下單。"""
    if not _DATA_DIR.exists():
        return

    tasks = []
    for user_file in _DATA_DIR.glob("*.json"):
        try:
            cfg = json.loads(user_file.read_text(encoding="utf-8"))
        except Exception as e:
            log.error(f"[自動開單] 讀取設定失敗 {user_file.name}: {e}")
            continue

        if not cfg.get("ENABLED"):
            continue

        tasks.append(_place_orders_for_user(cfg, symbol, signal))

    if not tasks:
        return

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            log.error(f"[自動開單] 使用者任務 {i} 發生未捕捉例外: {r}")
