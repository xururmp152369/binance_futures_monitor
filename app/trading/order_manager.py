import asyncio
import math
import time
from binance import AsyncClient
from ..extension.utils import setup_logging
from ..user.user_config import get_all_trading_configs_with_chat_id

log = setup_logging()



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
        # 固定損失：RISK_AMOUNT / 止損點差（abs 支援多空兩方向）
        sl_dist = abs(entry_price - stop_loss)
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


async def _place_orders_for_user(
    account_name: str, cfg: dict, symbol: str, signal: dict
) -> tuple[bool, str] | None:
    """對單一使用者執行自動下單。

    回傳：
      None          → 略過（策略不符 / 黑名單），不發通知
      (True,  msg)  → 開倉成功
      (False, msg)  → 開倉失敗，msg 含錯誤說明
    """
    # 策略類型比對 → None = 略過（不通知）
    signal_type = signal["type"].upper()
    if signal_type not in [s.upper() for s in cfg.get("STRATEGY", [])]:
        return None

    # 黑名單檢查 → None = 略過（不通知）
    blacklist = [s.upper() for s in cfg.get("SYMBOL_BLACKLIST", [])]
    if symbol.upper() in blacklist:
        log.info(f"[自動開單] {symbol} 在黑名單，略過 ({account_name})")
        return None

    use_prd = cfg.get("ORDER_MODE", "DEV") == "PRD"
    api_key    = cfg["PRD_API_KEY"]    if use_prd else cfg["API_KEY"]
    api_secret = cfg["PRD_SECRET_KEY"] if use_prd else cfg["SECRET_KEY"]

    client = None
    try:
        client = await AsyncClient.create(
            api_key=api_key,
            api_secret=api_secret,
            testnet=not use_prd,
        )
        log.info(f"[自動開單] {symbol} 模式={'正式' if use_prd else '模擬'} ({account_name})")

        # 部位上限 & 加倉檢查
        open_positions = await _get_open_positions(client)
        side_positions = [p for p in open_positions if float(p.get("positionAmt", 0)) > 0]
        order_limit    = cfg["LONG_ORDER_LIMIT"]
        side_label     = "多單"

        # 判斷是否為加倉（該 symbol 已有同方向持倉）
        is_add_on = any(p["symbol"] == symbol for p in side_positions)

        if is_add_on:
            # 加倉：跳過持倉上限，但需確認設定允許加倉
            if not cfg.get("ADD_SAME_SYMBOL", False):
                msg = f"{symbol} 已有{side_label}持倉，且設定不允許加倉"
                log.info(f"[自動開單] {msg} ({account_name})")
                return (False, msg)
        else:
            # 新倉：檢查持倉上限
            if len(side_positions) >= order_limit:
                msg = f"{side_label}部位已達上限（{len(side_positions)}/{order_limit}），略過 {symbol}"
                log.info(f"[自動開單] {msg} ({account_name})")
                return (False, msg)

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
        if not is_add_on:
            try:
                await client.futures_cancel_all_algo_open_orders(symbol=symbol)
                log.info(f"[自動開單] {symbol} 已清除舊條件單")
            except Exception as e:
                log.warning(f"[自動開單] {symbol} 清除舊條件單失敗（忽略）: {e}")

        entry_side    = "BUY"
        exit_side     = "SELL"
        direction_str = "多頭"

        # 計算下單量
        entry_price                   = signal["close"]
        stop_loss                     = signal["stop_loss"]
        qty_precision, price_precision = await _get_precisions(client, symbol)
        raw_qty                        = _calc_quantity(cfg, entry_price, stop_loss)
        qty                            = _floor_to_precision(raw_qty, qty_precision)

        if qty <= 0:
            msg = f"下單量計算異常（raw={raw_qty:.6f}），請確認風險設定"
            log.error(f"[自動開單] {symbol} {msg} ({account_name})")
            return (False, msg)

        # 市價開倉（確認倉位成立後才設置 SL/TP，最多重試 5 次）
        MAX_RETRIES = 5
        fill_price = None
        filled_qty = qty
        for attempt in range(1, MAX_RETRIES + 1):
            order_id = f"cc_{symbol}_{int(time.time() * 1000)}"
            try:
                order = await client.futures_create_order(
                    symbol=symbol,
                    side=entry_side,
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
                    f"[自動開單] {symbol} 市價{direction_str}開倉成功 attempt={attempt}"
                    f" filled_qty={filled_qty} fill={fill_price:.6f} ({account_name})"
                )
                break
            except Exception as order_err:
                if "-1007" not in str(order_err):
                    log.error(f"[自動開單] {symbol} 開倉失敗: {order_err} ({account_name})")
                    return (False, str(order_err))
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
            msg = f"重試 {MAX_RETRIES} 次後仍無法確認倉位，已放棄"
            log.error(f"[自動開單] {symbol} {msg} ({account_name})")
            return (False, msg)

        await asyncio.sleep(1)

        warnings: list[str] = []
        sl_dist = abs(fill_price - stop_loss)

        # 止損單
        try:
            await client.futures_create_order(
                symbol=symbol,
                side=exit_side,
                type="STOP_MARKET",
                stopPrice=round(stop_loss, price_precision),
                closePosition=True,
                workingType="MARK_PRICE",
            )
            log.info(f"[自動開單] {symbol} 止損掛出 stopPrice={stop_loss:.6f} (closePosition)")
        except Exception as e:
            log.error(f"[自動開單] {symbol} 止損掛出失敗: {e} ({account_name})")
            warnings.append(f"⚠️ 止損設置失敗，請手動設置\n{e}")

        tp_strategy = cfg.get("TP_STRATEGY", [])
        last_idx  = len(tp_strategy) - 1
        tp_failed = False
        for i, tp_entry in enumerate(tp_strategy):
            rr      = tp_entry["RR_RATIO"]
            pct     = tp_entry["PERCENT"]
            tp_price = fill_price + sl_dist * rr
            is_last  = (i == last_idx)

            try:
                if is_last:
                    await client.futures_create_order(
                        symbol=symbol,
                        side=exit_side,
                        type="TAKE_PROFIT_MARKET",
                        stopPrice=round(tp_price, price_precision),
                        closePosition=True,
                        workingType="MARK_PRICE",
                    )
                    log.info(f"[自動開單] {symbol} 止盈{i + 1} stopPrice={tp_price:.6f} (closePosition)")
                else:
                    tp_qty = _floor_to_precision(filled_qty * pct / 100, qty_precision)
                    if tp_qty <= 0:
                        continue
                    await client.futures_create_order(
                        symbol=symbol,
                        side=exit_side,
                        type="TAKE_PROFIT_MARKET",
                        stopPrice=round(tp_price, price_precision),
                        quantity=tp_qty,
                        reduceOnly=True,
                        workingType="MARK_PRICE",
                    )
                    log.info(f"[自動開單] {symbol} 止盈{i + 1} stopPrice={tp_price:.6f} qty={tp_qty}")
            except Exception as e:
                log.error(f"[自動開單] {symbol} 止盈{i + 1} 掛出失敗: {e} ({account_name})")
                tp_failed = True

        if tp_failed:
            warnings.append("⚠️ 部分止盈設置失敗，請手動確認")

        fmt_price  = f"{fill_price:,.6f}".rstrip("0").rstrip(".")
        margin_val = filled_qty * fill_price / leverage
        fmt_margin = f"{margin_val:,.2f}"
        success_msg = f"市價 {fmt_price} 已開，倉位價值 {fmt_margin} USDT"
        if warnings:
            success_msg += "\n" + "\n".join(warnings)
        return (True, success_msg)

    except Exception as e:
        log.error(f"[自動開單] {symbol} 開單失敗: {e} ({account_name})")
        return (False, str(e))
    finally:
        if client:
            await client.close_connection()


async def place_orders_for_all_users(symbol: str, signal: dict) -> dict[int, tuple[bool, str]]:
    """遍歷所有使用者設定，對符合條件的使用者在 Binance 期貨自動下單。

    回傳 {chat_id: (success, message)}，供後續對每位使用者發送開單結果通知。
    """
    tasks: list = []
    chat_ids: list[int] = []
    for account_name, chat_id, cfg in get_all_trading_configs_with_chat_id():
        if not cfg.get("ENABLED"):
            continue
        if chat_id is None:
            continue
        tasks.append(_place_orders_for_user(account_name, cfg, symbol, signal))
        chat_ids.append(chat_id)

    if not tasks:
        return {}

    results_raw = await asyncio.gather(*tasks, return_exceptions=True)
    results: dict[int, tuple[bool, str]] = {}
    for chat_id, r in zip(chat_ids, results_raw):
        if isinstance(r, Exception):
            log.error(f"[自動開單] 使用者任務發生未捕捉例外: {r}")
            results[chat_id] = (False, f"未預期錯誤: {r}")
        elif r is not None:
            results[chat_id] = r
    return results
