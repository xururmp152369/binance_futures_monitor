"""產生 CSV 回測報告（支援基礎模式與帳戶模式）。"""
import csv
import os
from datetime import datetime, timezone, timedelta

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
TZ_TAIPEI   = timezone(timedelta(hours=8))

_STRATEGY_NAME = {
    "type1": "long_breakout",
    "type2": "short_bounce",
    "type3": "death_cross_short",
}

_DIRECTION = {
    "type1": "LONG",
    "type2": "SHORT",
    "type3": "SHORT",
}

_BASE_FIELDS = [
    "symbol", "strategy", "signal_time", "direction",
    "entry_price", "stop_loss", "risk_1r_pct",
]

# 基礎模式專屬欄位（無帳戶）
_BASE_EXTRA = ["risk_1r", "max_r_reached", "stop_hit", "eval_incomplete"]

# 帳戶模式尾部欄位
_ACCOUNT_TAIL = ["win", "pnl_usdt", "stop_hit", "eval_incomplete"]


def _signal_time_str(candle_open_time_ms: int) -> str:
    ts = datetime.fromtimestamp(candle_open_time_ms / 1000, tz=TZ_TAIPEI)
    return ts.strftime("%Y-%m-%d %H:%M")


def _today_str() -> str:
    return datetime.now(TZ_TAIPEI).strftime("%Y%m%d")


def _base_row(r: dict) -> dict:
    sig_type = r.get("type", "")
    return {
        "symbol":          r.get("symbol", ""),
        "strategy":        _STRATEGY_NAME.get(sig_type, sig_type),
        "signal_time":     _signal_time_str(r.get("candle_open_time_ms", 0)),
        "direction":       _DIRECTION.get(sig_type, ""),
        "entry_price":     f"{r.get('close', 0):.8g}",
        "stop_loss":       f"{r.get('stop_loss', 0):.8g}",
        "risk_1r_pct":     f"{r.get('risk_1r_pct', 0):.4f}",
    }


# ─── 基礎模式（無帳戶）────────────────────────────────────────────────────────

def write_report(results: list[dict], strategy_name: str) -> str:
    """基礎模式 CSV：max_r_reached 0~10。"""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"{strategy_name}_{_today_str()}.csv")
    fieldnames = _BASE_FIELDS + _BASE_EXTRA

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            row = _base_row(r)
            row.update({
                "risk_1r":       f"{r.get('risk_1r', 0):.8g}",
                "max_r_reached": r.get("max_r_reached", 0),
                "stop_hit":      r.get("stop_hit", False),
                "eval_incomplete": r.get("eval_incomplete", False),
            })
            writer.writerow(row)
    return path


def print_summary(results: list[dict], strategy_name: str) -> None:
    if not results:
        print(f"\n[{strategy_name}] 無訊號")
        return

    total    = len(results)
    complete = [r for r in results if not r.get("eval_incomplete")]
    stops    = sum(1 for r in complete if r.get("stop_hit"))
    win_rate = (1 - stops / len(complete)) * 100 if complete else 0.0

    r_counts: dict[int, int] = {}
    for r in results:
        v = r.get("max_r_reached", 0)
        r_counts[v] = r_counts.get(v, 0) + 1

    print(f"\n{'─' * 50}")
    print(f"  策略：{strategy_name}")
    print(f"  訊號：{total}（完整：{len(complete)}，不完整：{total - len(complete)}）")
    if complete:
        print(f"  止損率：{stops}/{len(complete)} = {100 - win_rate:.1f}%  未止損：{win_rate:.1f}%")
    print("  R 分佈（max_r_reached）：")
    for rv in sorted(r_counts):
        print(f"    {rv:2d}R : {r_counts[rv]:4d}  {'█' * r_counts[rv]}")
    print(f"{'─' * 50}")


def write_all_reports(results_by_strategy: dict[str, list[dict]]) -> list[str]:
    paths = []
    for strat, results in results_by_strategy.items():
        print_summary(results, strat)
        if results:
            path = write_report(results, strat)
            print(f"  → 已輸出：{path}")
            paths.append(path)
    return paths


# ─── 帳戶模式 ─────────────────────────────────────────────────────────────────

def _account_fieldnames(n_tps: int) -> list[str]:
    fields = list(_BASE_FIELDS)
    for i in range(1, n_tps + 1):
        fields += [f"tp{i}_rr", f"tp{i}_hit"]
    fields += _ACCOUNT_TAIL
    return fields


def _detect_n_tps(results: list[dict]) -> int:
    """從結果中自動偵測最大止盈組數（1~3）。"""
    n = 0
    for r in results:
        for i in range(1, 4):
            if f"tp{i}_rr" in r:
                n = max(n, i)
    return max(n, 1)


def write_account_report(
    results: list[dict],
    strategy_name: str,
    account_name: str,
) -> str:
    """帳戶模式 CSV：TP 欄位 + win + pnl_usdt。"""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    n_tps = _detect_n_tps(results)
    fieldnames = _account_fieldnames(n_tps)
    path = os.path.join(RESULTS_DIR, f"{strategy_name}_{account_name}_{_today_str()}.csv")

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            row = _base_row(r)
            for i in range(1, n_tps + 1):
                row[f"tp{i}_rr"]  = r.get(f"tp{i}_rr", "")
                row[f"tp{i}_hit"] = r.get(f"tp{i}_hit", False)
            row.update({
                "win":             r.get("win", False),
                "pnl_usdt":        f"{r.get('pnl_usdt', 0):.4f}",
                "stop_hit":        r.get("stop_hit", False),
                "eval_incomplete": r.get("eval_incomplete", False),
            })
            writer.writerow(row)
    return path


def print_account_summary(
    results: list[dict],
    strategy_name: str,
    account_name: str,
) -> None:
    print(f"\n{'─' * 50}")
    print(f"  策略：{strategy_name}  帳戶：{account_name}")

    if not results:
        print("  無訊號")
        print(f"{'─' * 50}")
        return

    total    = len(results)
    complete = [r for r in results if not r.get("eval_incomplete")]
    wins     = sum(1 for r in complete if r.get("win"))
    win_rate = wins / len(complete) * 100 if complete else 0.0
    total_pnl = sum(r.get("pnl_usdt", 0) for r in complete)
    avg_pnl   = total_pnl / len(complete) if complete else 0.0

    print(f"  訊號：{total}（完整：{len(complete)}，不完整：{total - len(complete)}）")
    if complete:
        print(f"  勝率：{wins}/{len(complete)} = {win_rate:.1f}%")
        print(f"  總損益：{total_pnl:+.2f} USDT  每筆均：{avg_pnl:+.2f} USDT")

    # TP 命中率分佈
    n_tps = _detect_n_tps(results)
    for i in range(1, n_tps + 1):
        rr   = next((r.get(f"tp{i}_rr") for r in results if f"tp{i}_rr" in r), "?")
        hits = sum(1 for r in complete if r.get(f"tp{i}_hit"))
        pct  = hits / len(complete) * 100 if complete else 0
        print(f"  TP{i}（{rr}R）命中：{hits}/{len(complete)} = {pct:.1f}%")
    print(f"{'─' * 50}")


def write_all_account_reports(
    results_by_strategy: dict[str, list[dict]],
    account_name: str,
) -> list[str]:
    paths = []
    for strat, results in results_by_strategy.items():
        print_account_summary(results, strat, account_name)
        if results:
            path = write_account_report(results, strat, account_name)
            print(f"  → 已輸出：{path}")
            paths.append(path)
    return paths
