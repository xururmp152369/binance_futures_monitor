import asyncio
import math
import time
from binance import AsyncClient
from ..extension.utils import setup_logging
from ..user.user_config import get_all_trading_configs_with_chat_id

log = setup_logging()

# 訊號類型 → 設定中的策略代號
_SIGNAL_TO_STRATEGY = {
    "type1": "long_breakout",
    "type3": "death_cross_short",
}


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


async def _cancel_close_position_orders(client: AsyncClient, symbol: str, exit_side: str) -> int:
    """取消指定方向的所有 closePosition 止損/止盈條件單，回傳取消數量。

    Binance 的 closePosition 條件單存在 /fapi/v1/openAlgoOrders，
    無法透過 /fapi/v1/openOrders 查詢，須改走 algo 端點取消。
    """
    try:
        raw = await client.futures_get_open_algo_orders(symbol=symbol)
        algo_orders = raw if isinstance(raw, list) else raw.get("orders", [])
        cancel_ids = [
            o["algoId"] for o in algo_orders
            if (o.get("closePosition") is True
                or str(o.get("closePosition", "")).lower() == "true")
            and o.get("side") == exit_side
        ]
        for algo_id in cancel_ids:
            try:
                await client.futures_cancel_algo_order(algoId=algo_id)
            except Exception as ce:
                log.warning(f"[自動開單] {symbol} 取消條件單 algoId={algo_id} 失敗: {ce}")
        if cancel_ids:
            await asyncio.sleep(0.5)
        return len(cancel_ids)
    except Exception as e:
        log.warning(f"[自動開單] {symbol} 取消 closePosition 條件單失敗: {e}")
        return 0


async def _get_close_position_algo_orders(client: AsyncClient, symbol: str, side: str) -> list[dict]:
    """回傳指定方向的所有 closePosition algo 條件單（止損＋止盈）。"""
    try:
        raw = await client.futures_get_open_algo_orders(symbol=symbol)
        orders = raw if isinstance(raw, list) else raw.get("orders", [])
        return [
            o for o in orders
            if (o.get("closePosition") is True
                or str(o.get("closePosition", "")).lower() == "true")
            and o.get("side") == side
        ]
    except Exception as e:
        log.warning(f"[自動開單] {symbol} 查詢 closePosition algo 條件單失敗: {e}")
        return []


async def _place_orders_for_user(
    account_name: str, cfg: dict, symbol: str, signal: dict, *, use_prd: bool
) -> tuple[bool, str] | None:
    """對單一使用者執行自動下單。

    回傳：
      None          → 略過（未知訊號類型 / 黑名單），不發通知
      (True,  msg)  → 開倉成功
      (False, msg)  → 開倉失敗，msg 含錯誤說明
    """
    # 未知訊號類型 → 略過
    raw_type = signal["type"].lower()
    if _SIGNAL_TO_STRATEGY.get(raw_type) is None:
        return None

    # 黑名單檢查 → None = 略過（不通知）
    blacklist = [s.upper() for s in cfg.get("SYMBOL_BLACKLIST", [])]
    if symbol.upper() in blacklist:
        log.info(f"[自動開單] {symbol} 在黑名單，略過 ({account_name})")
        return None

    # 方向判斷
    is_long       = raw_type == "type1"
    entry_side    = "BUY"  if is_long else "SELL"
    exit_side     = "SELL" if is_long else "BUY"
    direction_str = "多頭" if is_long else "空頭"
    side_label    = "多單" if is_long else "空單"

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
        if is_long:
            side_positions = [p for p in open_positions if float(p.get("positionAmt", 0)) > 0]
            order_limit    = cfg["LONG_ORDER_LIMIT"]
        else:
            side_positions = [p for p in open_positions if float(p.get("positionAmt", 0)) < 0]
            order_limit    = cfg.get("SHORT_ORDER_LIMIT") or cfg["LONG_ORDER_LIMIT"]

        # 判斷是否為加倉（該 symbol 已有同方向持倉）
        is_add_on = any(p["symbol"] == symbol for p in side_positions)

        if is_add_on:
            if not cfg.get("ADD_SAME_SYMBOL", False):
                msg = f"{symbol} 已有{side_label}持倉，且設定不允許加倉"
                log.info(f"[自動開單] {msg} ({account_name})")
                return (False, msg)
        else:
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

        if not is_add_on:
            # 首次開倉：清除所有殘留條件單（一般掛單 + closePosition algo 單），重新設置 SL/TP
            try:
                await client.futures_cancel_all_algo_open_orders(symbol=symbol)
                log.info(f"[自動開單] {symbol} 已取消所有一般掛單")
                await asyncio.sleep(0.3)
            except Exception as e:
                log.warning(f"[自動開單] {symbol} 取消一般掛單失敗（忽略）: {e}")
            cancelled_b = await _cancel_close_position_orders(client, symbol, "BUY")
            cancelled_s = await _cancel_close_position_orders(client, symbol, "SELL")
            total = cancelled_b + cancelled_s
            if total > 0:
                log.info(f"[自動開單] {symbol} 已清除 {total} 個舊 closePosition 條件單")
        # 加倉：保留量化止盈掛單，開倉成交後統一更新 closePosition 條件單

        # 計算下單量
        entry_price                    = signal["close"]
        stop_loss                      = signal["stop_loss"]
        qty_precision, price_precision = await _get_precisions(client, symbol)
        raw_qty                        = _calc_quantity(cfg, entry_price, stop_loss)
        qty                            = _floor_to_precision(raw_qty, qty_precision)

        if qty <= 0:
            msg = f"下單量計算異常（raw={raw_qty:.6f}），請確認風險設定"
            log.error(f"[自動開單] {symbol} {msg} ({account_name})")
            return (False, msg)

        # 市價開倉（確認倉位成立後才設置 SL/TP，最多重試 5 次）
        MAX_RETRIES = 5
        fill_price  = None
        filled_qty  = qty
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

        # 加倉：先記錄現有止盈價，取消舊 closePosition 條件單，再重新設置
        # 非 closePosition 的分批止盈（有數量的）不受影響，保留原止盈點位
        existing_close_tp_price: float | None = None
        if is_add_on:
            existing_orders = await _get_close_position_algo_orders(client, symbol, exit_side)
            tp_order = next(
                (o for o in existing_orders if o.get("algoType") == "TAKE_PROFIT_MARKET"),
                None,
            )
            if tp_order:
                raw_tp = float(tp_order.get("triggerPrice") or tp_order.get("stopPrice") or 0)
                if raw_tp > 0:
                    existing_close_tp_price = raw_tp
                    log.info(f"[自動開單] {symbol} 加倉：記錄現有止盈價 {existing_close_tp_price:.6f}")

            cancelled = await _cancel_close_position_orders(client, symbol, exit_side)
            if cancelled > 0:
                log.info(f"[自動開單] {symbol} 加倉：已取消 {cancelled} 個舊 closePosition 條件單")

        warnings: list[str] = []
        sl_dist = abs(fill_price - stop_loss)

        # 止損單（-4130 時再次清除後重試一次）
        sl_placed = False
        for sl_attempt in range(2):
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
                sl_placed = True
                break
            except Exception as e:
                if "-4130" in str(e) and sl_attempt == 0:
                    log.warning(f"[自動開單] {symbol} 止損設置遇到 -4130，再次清除後重試")
                    await _cancel_close_position_orders(client, symbol, exit_side)
                    continue
                log.error(f"[自動開單] {symbol} 止損掛出失敗: {e} ({account_name})")
                warnings.append(f"⚠️ 止損設置失敗，請手動設置\n{e}")
                break

        # 止盈策略（空頭若未設定則 fallback 到多頭止盈）
        if is_long:
            tp_strategy = cfg.get("LONG_TP_STRATEGY", [])
        else:
            tp_strategy = cfg.get("SHORT_TP_STRATEGY") or cfg.get("LONG_TP_STRATEGY", [])

        last_idx  = len(tp_strategy) - 1
        tp_failed = False
        for i, tp_entry in enumerate(tp_strategy):
            rr       = tp_entry["RR_RATIO"]
            pct      = tp_entry["PERCENT"]
            # 多頭止盈往上，空頭止盈往下
            tp_price = fill_price + sl_dist * rr if is_long else fill_price - sl_dist * rr
            is_last  = (i == last_idx)

            if is_last:
                # 加倉時保留對持有者更有利的止盈價
                if existing_close_tp_price is not None:
                    tp_price = max(tp_price, existing_close_tp_price) if is_long \
                               else min(tp_price, existing_close_tp_price)
                    log.info(f"[自動開單] {symbol} 加倉止盈調整後 stopPrice={tp_price:.6f}")
                # closePosition 最後一筆止盈：-4130 時再次清除後重試一次
                for tp_attempt in range(2):
                    try:
                        await client.futures_create_order(
                            symbol=symbol,
                            side=exit_side,
                            type="TAKE_PROFIT_MARKET",
                            stopPrice=round(tp_price, price_precision),
                            closePosition=True,
                            workingType="MARK_PRICE",
                        )
                        log.info(f"[自動開單] {symbol} 止盈{i + 1} stopPrice={tp_price:.6f} (closePosition)")
                        break
                    except Exception as e:
                        if "-4130" in str(e) and tp_attempt == 0:
                            log.warning(f"[自動開單] {symbol} 止盈{i + 1} 遇到 -4130，再次清除後重試")
                            await _cancel_close_position_orders(client, symbol, exit_side)
                            continue
                        log.error(f"[自動開單] {symbol} 止盈{i + 1} 掛出失敗: {e} ({account_name})")
                        tp_failed = True
                        break
            else:
                try:
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

        fmt_price   = f"{fill_price:,.6f}".rstrip("0").rstrip(".")
        margin_val  = filled_qty * fill_price / leverage
        fmt_margin  = f"{margin_val:,.2f}"
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


async def place_orders_for_all_users(symbol: str, signal: dict) -> dict[int, list[tuple[str, bool, str]]]:
    """遍歷所有使用者設定，對符合條件的使用者在 Binance 期貨自動下單。

    回傳 {chat_id: [(env_label, success, message), ...]}，
    同一 chat_id 可能同時有正式與模擬兩筆結果。
    """
    raw_type     = signal.get("type", "").lower()
    strategy_key = _SIGNAL_TO_STRATEGY.get(raw_type)
    if strategy_key is None:
        return {}

    tasks: list = []
    keys:  list[tuple[int, str]] = []  # (chat_id, env_label)

    for account_name, chat_id, cfg in get_all_trading_configs_with_chat_id():
        if not cfg.get("ENABLED"):
            continue
        if chat_id is None:
            continue
        if strategy_key in [s.lower() for s in cfg.get("PRD_STRATEGY", [])]:
            tasks.append(_place_orders_for_user(account_name, cfg, symbol, signal, use_prd=True))
            keys.append((chat_id, "正式"))
        if strategy_key in [s.lower() for s in cfg.get("DEV_STRATEGY", [])]:
            tasks.append(_place_orders_for_user(account_name, cfg, symbol, signal, use_prd=False))
            keys.append((chat_id, "模擬"))

    if not tasks:
        return {}

    results_raw = await asyncio.gather(*tasks, return_exceptions=True)
    results: dict[int, list[tuple[str, bool, str]]] = {}
    for (chat_id, env_label), r in zip(keys, results_raw):
        if isinstance(r, Exception):
            log.error(f"[自動開單] 使用者任務發生未捕捉例外: {r}")
            entry: tuple[str, bool, str] = (env_label, False, f"未預期錯誤: {r}")
        elif r is None:
            continue
        else:
            entry = (env_label, r[0], r[1])
        results.setdefault(chat_id, []).append(entry)
    return results
