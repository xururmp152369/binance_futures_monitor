"""回測 CLI 入口。

用法：
    python backtest/run.py --strategy long_breakout
    python backtest/run.py --strategy death_cross_short
    python backtest/run.py --strategy fibonacci_long
    python backtest/run.py --strategy fibonacci_short
    python backtest/run.py --strategy all
    python backtest/run.py --strategy long_breakout --days 30 --no-cache
    python backtest/run.py --strategy all --symbols BTCUSDT,ETHUSDT
    python backtest/run.py --strategy long_breakout --account xururmp152369
    python backtest/run.py --strategy all --account xururmp152369 --days 30

    # 指定區間回測
    python backtest/run.py --strategy all --start 2025-06-04 --end 2026-06-04
    python backtest/run.py --strategy all --start 2025-01-01  # --end 預設今天

注意：
  - 同一天多次執行會使用快取資料。若要取得最新 K 棒（如當日訊號），請加 --no-cache。
  - 使用 --account 時需在環境中設定 ENCRYPTION_KEY（與 bot 相同）。
  - --start/--end 優先於 --days；指定區間時系統自動追加 260 天暖機資料。
"""
import argparse
import asyncio
import sys
import os
from datetime import datetime, timedelta, timezone

# 暖機天數：EMA200 日線需要 200 根，加 60 天緩衝
_WARM_UP_DAYS = 260
# 評估窗口緩衝（需與 data_fetcher._EVAL_BUFFER 保持一致）
_EVAL_BUFFER  = 14


def _parse_date(s: str) -> datetime:
    """將 YYYY-MM-DD 解析為 UTC 午夜 datetime。"""
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backtest.data_fetcher import fetch_usdt_symbols, fetch_all_symbols
from backtest.engine import run_backtest
from backtest.evaluator import evaluate_all, evaluate_all_with_account
from backtest.reporter import write_all_reports, write_all_account_reports
from app.strategy.long_breakout import print_diag_stats  # [DIAG]

ALL_STRATEGIES = {"long_breakout", "death_cross_short", "fibonacci_long", "fibonacci_short"}

_TYPE_TO_STRATEGY = {
    "type1":           "long_breakout",
    "type3":           "death_cross_short",
    "fibonacci_long":  "fibonacci_long",
    "fibonacci_short": "fibonacci_short",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Binance 期貨策略回測")
    parser.add_argument(
        "--strategy", "-s",
        required=True,
        choices=[*sorted(ALL_STRATEGIES), "all"],
        help="要回測的策略，或 all",
    )
    parser.add_argument(
        "--days", "-d",
        type=int, default=30,
        help="回測天數（預設 30）",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="忽略快取，強制重新下載（當日訊號不見時使用）",
    )
    parser.add_argument(
        "--symbols",
        type=str, default="",
        help="指定幣種（逗號分隔），預設下載全部 USDT 永續合約",
    )
    parser.add_argument(
        "--account", "-a",
        type=str, default="",
        help="帳戶名稱，依該帳戶止盈策略計算盈虧（需設定 ENCRYPTION_KEY）",
    )
    parser.add_argument(
        "--start",
        type=str, default="",
        help="回測起始日期 YYYY-MM-DD（與 --end 搭配；優先於 --days）",
    )
    parser.add_argument(
        "--end",
        type=str, default="",
        help="回測結束日期 YYYY-MM-DD（預設今天）",
    )
    return parser.parse_args()


def load_account_config(account_name: str) -> dict | None:
    """讀取並解密帳戶設定，失敗時印出原因並回傳 None。"""
    try:
        from app.user.user_config import get_user_config
        cfg = get_user_config(account_name)
        if cfg is None:
            print(f"[run] 帳戶 '{account_name}' 不存在或 ENCRYPTION_KEY 錯誤")
        return cfg
    except Exception as exc:
        print(f"[run] 帳戶設定讀取失敗：{exc}")
        return None


def _group_by_strategy(
    results: list[dict],
    strategies: set[str],
) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {s: [] for s in strategies}
    for r in results:
        strat = _TYPE_TO_STRATEGY.get(r.get("type", ""), "")
        if strat in grouped:
            grouped[strat].append(r)
    return grouped


async def main() -> None:
    args = parse_args()
    strategies: set[str] = ALL_STRATEGIES if args.strategy == "all" else {args.strategy}

    # 帳戶模式：讀取設定
    account_cfg: dict | None = None
    if args.account:
        account_cfg = load_account_config(args.account)
        if account_cfg is None:
            sys.exit(1)
        print(f"[run] 帳戶模式：{args.account}  "
              f"RISK_TYPE={account_cfg.get('RISK_TYPE')}  "
              f"RISK_AMOUNT={account_cfg.get('RISK_AMOUNT')}  "
              f"RISK_LEVERAGE={account_cfg.get('RISK_LEVERAGE')}×")

    # ── 計算回測時間邊界 ──────────────────────────────────────────────────────
    if args.start:
        start_dt = _parse_date(args.start)
        end_dt   = (
            _parse_date(args.end)
            if args.end
            else datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        )
        if start_dt >= end_dt:
            print("[run] 錯誤：--start 必須早於 --end")
            sys.exit(1)

        range_days        = (end_dt - start_dt).days
        # 下載天數 = 回測區間 + 暖機期（EMA200）；_calc_pages 會再加 _EVAL_BUFFER
        backtest_days     = range_days + _WARM_UP_DAYS
        backtest_start_ms = int(start_dt.timestamp() * 1000)
        backtest_end_ms   = int(end_dt.timestamp() * 1000)
        # 抓取結束點往後多 14 天，供區間尾端訊號的評估窗口使用
        fetch_end_time_ms = int((end_dt + timedelta(days=_EVAL_BUFFER)).timestamp() * 1000)

        end_label = args.end or end_dt.strftime("%Y-%m-%d")
        print(f"[run] 回測區間：{args.start} → {end_label}（{range_days} 天，含 {_WARM_UP_DAYS} 天暖機）")
    else:
        backtest_days     = args.days
        backtest_start_ms = None
        backtest_end_ms   = None
        fetch_end_time_ms = None

    # ── 取得幣種清單 ──────────────────────────────────────────────────────────
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        print(f"[run] 指定幣種：{symbols}")
    else:
        print("[run] 取得 USDT 永續合約清單 …")
        symbols = await fetch_usdt_symbols()
        print(f"[run] 共 {len(symbols)} 個幣種")

    # 下載資料
    print(f"[run] 開始下載歷史資料（no_cache={args.no_cache}）…")
    all_data = await fetch_all_symbols(
        symbols,
        backtest_days=backtest_days,
        no_cache=args.no_cache,
        fetch_end_time_ms=fetch_end_time_ms,
    )
    print(f"[run] 下載完成，共 {len(all_data)} 個幣種有資料")

    # 執行回測
    range_label = (
        f"{args.start} → {args.end or datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        if args.start else f"最近 {args.days} 天"
    )
    print(f"[run] 開始回測（策略={strategies}，{range_label}）…")
    signals = run_backtest(
        all_data, strategies,
        backtest_days=backtest_days,
        backtest_start_ms=backtest_start_ms,
        backtest_end_ms=backtest_end_ms,
    )
    print(f"[run] 收集到 {len(signals)} 筆訊號")
    print_diag_stats()  # [DIAG]

    all_15m = {sym: data.get("15m", []) for sym, data in all_data.items()}

    if account_cfg:
        # 帳戶模式：依止盈策略計算盈虧
        print("[run] 依帳戶止盈策略評估 …")
        results = evaluate_all_with_account(signals, all_15m, account_cfg)
        grouped = _group_by_strategy(results, strategies)
        write_all_account_reports(grouped, args.account)
    else:
        # 基礎模式：計算最大 R
        print("[run] 評估最大 R 倍數 …")
        results = evaluate_all(signals, all_15m)
        grouped = _group_by_strategy(results, strategies)
        write_all_reports(grouped)

    print("\n[run] 完成！")


if __name__ == "__main__":
    asyncio.run(main())
