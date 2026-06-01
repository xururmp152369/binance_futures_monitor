"""回測 CLI 入口。

用法：
    python backtest/run.py --strategy long_breakout
    python backtest/run.py --strategy short_bounce
    python backtest/run.py --strategy death_cross_short
    python backtest/run.py --strategy all
    python backtest/run.py --strategy long_breakout --days 30 --no-cache
    python backtest/run.py --strategy all --symbols BTCUSDT,ETHUSDT
    python backtest/run.py --strategy long_breakout --account xururmp152369
    python backtest/run.py --strategy all --account xururmp152369 --days 30

注意：
  - 同一天多次執行會使用快取資料。若要取得最新 K 棒（如當日訊號），請加 --no-cache。
  - 使用 --account 時需在環境中設定 ENCRYPTION_KEY（與 bot 相同）。
"""
import argparse
import asyncio
import sys
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backtest.data_fetcher import fetch_usdt_symbols, fetch_all_symbols
from backtest.engine import run_backtest
from backtest.evaluator import evaluate_all, evaluate_all_with_account
from backtest.reporter import write_all_reports, write_all_account_reports

ALL_STRATEGIES = {"long_breakout", "short_bounce", "death_cross_short"}

_TYPE_TO_STRATEGY = {
    "type1": "long_breakout",
    "type2": "short_bounce",
    "type3": "death_cross_short",
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

    # 取得幣種清單
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        print(f"[run] 指定幣種：{symbols}")
    else:
        print("[run] 取得 USDT 永續合約清單 …")
        symbols = await fetch_usdt_symbols()
        print(f"[run] 共 {len(symbols)} 個幣種")

    # 下載資料
    print(f"[run] 開始下載歷史資料（no_cache={args.no_cache}）…")
    all_data = await fetch_all_symbols(symbols, no_cache=args.no_cache)
    print(f"[run] 下載完成，共 {len(all_data)} 個幣種有資料")

    # 執行回測
    print(f"[run] 開始回測（策略={strategies}，回測天數={args.days}）…")
    signals = run_backtest(all_data, strategies, backtest_days=args.days)
    print(f"[run] 收集到 {len(signals)} 筆訊號")

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
