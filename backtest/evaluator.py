"""交易評估：計算每筆訊號的最大 R 倍數，或依帳戶止盈策略計算盈虧。"""

MAX_R = 50
EVAL_WINDOW_CANDLES = 672  # 7 天 × 96 根/天（15m）


# ─── 基礎 R 倍數評估（無帳戶設定）────────────────────────────────────────────

def evaluate_trade(
    signal: dict,
    subsequent_15m_candles: list,
    max_r: int = MAX_R,
) -> dict:
    """計算訊號止損前最高到達的整數 R（0~max_r）。

    保守原則：同一根 K 棒先檢查止損，止損命中則停止。
    """
    entry   = signal["close"]
    stop    = signal["stop_loss"]
    is_long = signal.get("type") == "type1"
    risk_1r = abs(entry - stop)

    if risk_1r == 0:
        return {
            "max_r_reached": 0, "stop_hit": False,
            "risk_1r": 0.0, "risk_1r_pct": 0.0, "eval_incomplete": False,
        }

    max_r_reached = 0
    stop_hit      = False

    for candle in subsequent_15m_candles[:EVAL_WINDOW_CANDLES]:
        _, _, high, low, _close, _ = candle

        if is_long:
            if low <= stop:
                stop_hit = True; break
            for r in range(max_r_reached + 1, max_r + 1):
                if high >= entry + r * risk_1r:
                    max_r_reached = r
                else:
                    break
        else:
            if high >= stop:
                stop_hit = True; break
            for r in range(max_r_reached + 1, max_r + 1):
                if low <= entry - r * risk_1r:
                    max_r_reached = r
                else:
                    break

        if max_r_reached >= max_r:
            break

    eval_incomplete = (
        len(subsequent_15m_candles) < EVAL_WINDOW_CANDLES
        and not stop_hit
        and max_r_reached < max_r
    )
    return {
        "max_r_reached":   max_r_reached,
        "stop_hit":        stop_hit,
        "risk_1r":         risk_1r,
        "risk_1r_pct":     risk_1r / entry * 100,
        "eval_incomplete": eval_incomplete,
    }


def evaluate_all(
    signals: list[dict],
    all_15m_data: dict[str, list],
) -> list[dict]:
    """對所有訊號進行 R 倍數評估。"""
    results = []
    for signal in signals:
        symbol     = signal["symbol"]
        open_ms    = signal["candle_open_time_ms"]
        subsequent = [c for c in all_15m_data.get(symbol, []) if c[0] > open_ms]
        results.append({**signal, **evaluate_trade(signal, subsequent)})
    return results


# ─── 帳戶止盈策略評估 ─────────────────────────────────────────────────────────

def compute_risk_per_1r(
    risk_1r: float,
    entry: float,
    risk_type: int,
    risk_amount: float,
    risk_leverage: int,
) -> float:
    """計算每 1R 的美元損益。

    RISK_TYPE=0（固定倉位）: 1R = risk_1r/entry × notional
    RISK_TYPE=1（固定損失）: 1R = RISK_AMOUNT（固定）
    """
    if entry == 0 or risk_1r == 0:
        return 0.0
    if risk_type == 0:
        return (risk_1r / entry) * risk_amount * risk_leverage
    return float(risk_amount)


def evaluate_trade_with_account(
    signal: dict,
    subsequent_15m_candles: list,
    tp_strategy: list,
    risk_per_1r: float,
) -> dict:
    """依帳戶止盈策略評估交易。

    tp_strategy: [{"RR_RATIO": float, "PERCENT": float}, ...]（最多 3 組，需已按 RR_RATIO 升序）
    risk_per_1r: 每 1R 美元損益

    保守原則：同一根 K 棒先檢查止損，才檢查止盈目標。

    回傳 dict：
        tp1_rr, tp1_hit [, tp2_rr, tp2_hit, tp3_rr, tp3_hit]
        win            - 是否勝利（任一止盈被觸發）
        pnl_usdt       - 已實現損益（USDT）
        stop_hit       - 是否觸及止損
        eval_incomplete - 評估窗口不足
    """
    entry   = signal["close"]
    stop    = signal["stop_loss"]
    is_long = signal.get("type") == "type1"
    risk_1r = abs(entry - stop)

    empty = {"win": False, "pnl_usdt": 0.0, "stop_hit": False, "eval_incomplete": False}

    if not tp_strategy or risk_1r == 0 or risk_per_1r == 0:
        for i, tp in enumerate(sorted(tp_strategy, key=lambda x: x["RR_RATIO"]), 1):
            empty[f"tp{i}_rr"]  = tp["RR_RATIO"]
            empty[f"tp{i}_hit"] = False
        return empty

    tps           = sorted(tp_strategy, key=lambda x: x["RR_RATIO"])
    tp_hits       = [False] * len(tps)
    stop_hit      = False
    remaining_pct = 100.0
    pnl           = 0.0

    for candle in subsequent_15m_candles[:EVAL_WINDOW_CANDLES]:
        _, _, high, low, _close, _ = candle

        # 先檢查止損
        if is_long:
            if low <= stop:
                stop_hit = True
                pnl -= risk_per_1r * remaining_pct / 100
                break
        else:
            if high >= stop:
                stop_hit = True
                pnl -= risk_per_1r * remaining_pct / 100
                break

        # 檢查各止盈目標（由低到高）
        for i, tp in enumerate(tps):
            if tp_hits[i]:
                continue
            rr  = tp["RR_RATIO"]
            pct = tp["PERCENT"]
            target = entry + rr * risk_1r if is_long else entry - rr * risk_1r
            if (high >= target if is_long else low <= target):
                tp_hits[i]     = True
                pnl           += rr * risk_per_1r * pct / 100
                remaining_pct -= pct

    win = any(tp_hits)
    eval_incomplete = (
        len(subsequent_15m_candles) < EVAL_WINDOW_CANDLES
        and not stop_hit
        and not all(tp_hits)
    )

    result: dict = {
        "win":             win,
        "pnl_usdt":        round(pnl, 4),
        "stop_hit":        stop_hit,
        "eval_incomplete": eval_incomplete,
    }
    for i, tp in enumerate(tps, 1):
        result[f"tp{i}_rr"]  = tp["RR_RATIO"]
        result[f"tp{i}_hit"] = tp_hits[i - 1]

    return result


def evaluate_all_with_account(
    signals: list[dict],
    all_15m_data: dict[str, list],
    account_cfg: dict,
) -> list[dict]:
    """依帳戶設定評估所有訊號，回傳帶 P&L 的結果 list。"""
    risk_type     = account_cfg.get("RISK_TYPE", 1)
    risk_amount   = account_cfg.get("RISK_AMOUNT", 0.0)
    risk_leverage = account_cfg.get("RISK_LEVERAGE", 1)
    long_tp       = account_cfg.get("LONG_TP_STRATEGY") or []
    short_tp      = account_cfg.get("SHORT_TP_STRATEGY") or long_tp

    results = []
    for signal in signals:
        is_long    = signal.get("type") == "type1"
        tp_strat   = long_tp if is_long else short_tp
        entry      = signal["close"]
        stop       = signal["stop_loss"]
        risk_1r    = abs(entry - stop)
        r_per_1r   = compute_risk_per_1r(risk_1r, entry, risk_type, risk_amount, risk_leverage)

        open_ms    = signal["candle_open_time_ms"]
        subsequent = [c for c in all_15m_data.get(signal["symbol"], []) if c[0] > open_ms]

        eval_r = evaluate_trade_with_account(signal, subsequent, tp_strat, r_per_1r)
        results.append({
            **signal,
            "risk_1r_pct": risk_1r / entry * 100 if entry else 0,
            **eval_r,
        })

    return results
