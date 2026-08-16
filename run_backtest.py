"""
Quantitative Backtesting CLI — Entry Point.

Usage:
    # 3-year full universe backtest (default)
    python run_backtest.py

    # Quick 6-month dry run on 20 well-known stocks
    python run_backtest.py --start 2024-01-01 --end 2024-06-01 --dry-run

    # Custom parameters
    python run_backtest.py --start 2023-01-01 --end 2026-01-01 --top-n 15 --rebalance monthly

    # Force re-download all historical price data
    python run_backtest.py --force-refresh
"""

import os
import sys
import logging
import argparse
from datetime import datetime

from rich.console import Console
from rich.logging import RichHandler

# ── Ensure project root is on sys.path ──────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import OUTPUT_DIR, DRY_RUN_SYMBOLS
from backtest.runner import BacktestRunner

# ── Logging setup ─────────────────────────────────────────────
os.makedirs(OUTPUT_DIR, exist_ok=True)
_log_file = os.path.join(OUTPUT_DIR, "backtest.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        RichHandler(rich_tracebacks=True, markup=True),
        logging.FileHandler(_log_file, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)
console = Console()


def parse_args():
    parser = argparse.ArgumentParser(
        description="NSE Quantitative Backtester (point-in-time, no lookahead bias)"
    )
    parser.add_argument(
        "--start",
        type=str,
        default="2023-01-01",
        help="Backtest start date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end",
        type=str,
        default="2026-01-01",
        help="Backtest end date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--rebalance",
        type=str,
        default="monthly",
        choices=["monthly", "biweekly", "weekly"],
        help="Rebalancing frequency (default: monthly)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=15,
        help="Number of stocks in portfolio (default: 15)",
    )
    parser.add_argument(
        "--fee",
        type=float,
        default=0.001,
        help="Transaction/slippage fee per trade, e.g. 0.001 = 0.1%% (default: 0.001)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=100_000.0,
        help="Initial capital in INR (default: 100000)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run on a small set of 20 well-known stocks (fast test mode)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit universe to N symbols (e.g. --limit 100 for faster runs)",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Force re-download all cached historical price data",
    )
    return parser.parse_args()


def run_backtest(args) -> dict:
    console.rule(f"[bold cyan]BACKTEST RUN -- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    symbols = None
    if args.dry_run:
        symbols = DRY_RUN_SYMBOLS
        console.print(f"[yellow]DRY RUN MODE - {len(symbols)} symbols[/]")
    elif args.limit:
        from data.universe import load_universe
        universe_df = load_universe()
        symbols = universe_df["symbol"].tolist()[: args.limit]
        console.print(f"[yellow]Limited to {len(symbols)} symbols[/]")

    runner = BacktestRunner(
        start_date=args.start,
        end_date=args.end,
        freq=args.rebalance,
        top_n=args.top_n,
        fee_pct=args.fee,
        initial_capital=args.capital,
        symbols=symbols,
    )

    result = runner.run(force_refresh=args.force_refresh)
    if not result:
        console.print("[bold red]Backtest failed - no results produced.[/]")
        return {}

    console.print("\n[bold]Backtest outputs:[/]")
    for key, path in result["paths"].items():
        console.print(f"   {key}: [dim]{path}[/]")

    console.rule("[bold cyan]DONE")
    return result


if __name__ == "__main__":
    args = parse_args()
    run_backtest(args)
