"""
One-Day Validation — Did yesterday's picks perform today?

Scores all stocks as of the last completed trading day and checks
next-day returns for the shortlisted stocks vs Nifty 50 benchmark.

Usage:
    python validate_one_day.py                # Auto-detect last 2 trading days
    python validate_one_day.py --scoring-date 2026-08-14 --check-date 2026-08-17
    python validate_one_day.py --full         # Full Nifty 500 universe
    python validate_one_day.py --top-n 15     # Check top 15 picks
"""

import os
import sys
import logging
import argparse
from datetime import datetime

import pandas as pd
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import LOGS_DIR, DRY_RUN_SYMBOLS, TOP_N_SHORTLIST
from data.db import init_schema
from data.fetchers.price_fetcher import fetch_price_batch, get_benchmark_data
from backtest.pit_engine import score_point_in_time

os.makedirs(LOGS_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        RichHandler(rich_tracebacks=True, markup=True),
        logging.FileHandler(os.path.join(LOGS_DIR, "validate.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)
console = Console()
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def parse_args():
    parser = argparse.ArgumentParser(description="One-day validation of scoring picks")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Run on 20 well-known stocks (default)")
    parser.add_argument("--full", action="store_true",
                        help="Run on full Nifty 500 universe")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit universe to N symbols")
    parser.add_argument("--top-n", type=int, default=TOP_N_SHORTLIST,
                        help=f"Number of top picks to check (default: {TOP_N_SHORTLIST})")
    parser.add_argument("--scoring-date", type=str, default=None,
                        help="Date to score stocks as of (YYYY-MM-DD). Default: auto-detect")
    parser.add_argument("--check-date", type=str, default=None,
                        help="Date to check returns on (YYYY-MM-DD). Default: next trading day after scoring-date")
    parser.add_argument("--refresh", action="store_true",
                        help="Force re-download price data")
    return parser.parse_args()


def run_validate(args):
    init_schema()
    console.rule("[bold cyan]ONE-DAY VALIDATION")

    # ── Load universe ────────────────────────────────────────────
    if args.full:
        from data.universe import load_universe
        universe_df = load_universe()
        symbols = universe_df["symbol"].tolist()
        if args.limit:
            symbols = symbols[:args.limit]
    else:
        symbols = DRY_RUN_SYMBOLS

    tickers = [f"{s}.NS" for s in symbols]
    console.print(f"Universe: [cyan]{len(tickers)}[/] tickers\n")

    # ── Fetch price data (short window for speed) ────────────────
    console.print("[bold yellow]Fetching price data...[/]")
    price_data = fetch_price_batch(tickers, period_days=30, force_refresh=args.refresh)
    benchmark_raw = get_benchmark_data(period_days=30, force_refresh=args.refresh)

    if benchmark_raw is None or benchmark_raw.empty:
        console.print("[bold red]Failed to fetch Nifty 50 benchmark.[/]")
        return

    # ── Determine scoring and check dates ─────────────────────────
    all_dates = set()
    for df in price_data.values():
        if df is not None and not df.empty:
            all_dates.update(df.index.tolist())
    trading_days = sorted(all_dates)

    if len(trading_days) < 2:
        console.print("[bold red]Need at least 2 trading days in data.[/]")
        return

    if args.scoring_date:
        scoring_date = pd.Timestamp(args.scoring_date)
        if scoring_date not in trading_days:
            console.print(f"[bold red]{args.scoring_date} is not a trading day in the data.[/]")
            console.print(f"Available: {[d.strftime('%Y-%m-%d') for d in trading_days[-10:]]}")
            return
    else:
        scoring_date = trading_days[-2]

    if args.check_date:
        check_date = pd.Timestamp(args.check_date)
        if check_date not in trading_days:
            console.print(f"[bold red]{args.check_date} is not a trading day in the data.[/]")
            console.print(f"Available: {[d.strftime('%Y-%m-%d') for d in trading_days[-10:]]}")
            return
    else:
        after = [d for d in trading_days if d > scoring_date]
        if not after:
            console.print("[bold red]No trading day found after scoring date.[/]")
            return
        check_date = after[0]

    console.print(f"Scoring as of: [cyan]{scoring_date.strftime('%Y-%m-%d')}[/] (last close)")
    console.print(f"Checking returns on: [cyan]{check_date.strftime('%Y-%m-%d')}[/]\n")

    # ── Benchmark return ─────────────────────────────────────────
    bench_close = benchmark_raw["Close"]
    if check_date in bench_close.index and scoring_date in bench_close.index:
        bench_ret = ((bench_close[check_date] / bench_close[scoring_date]) - 1.0) * 100
    else:
        bench_ret = 0.0

    # ── Score as of scoring_date ─────────────────────────────────
    console.print("[bold yellow]Scoring stocks (point-in-time)...[/]")
    shortlist_df = score_point_in_time(
        price_data=price_data,
        cutoff_date=scoring_date,
        benchmark_df=benchmark_raw,
        top_n=args.top_n,
    )

    if shortlist_df.empty:
        console.print("[bold red]No stocks scored. Check price data coverage.[/]")
        return

    # ── Compute next-day returns ─────────────────────────────────
    results = []
    for ticker in shortlist_df.index:
        score = shortlist_df.loc[ticker, "quant_score"]
        symbol = shortlist_df.loc[ticker, "symbol"]

        if ticker in price_data and price_data[ticker] is not None:
            df = price_data[ticker]
            if scoring_date in df.index and check_date in df.index:
                close_y = df.loc[scoring_date, "Close"]
                close_t = df.loc[check_date, "Close"]
                ret = ((close_t / close_y) - 1.0) * 100
            else:
                ret = None
        else:
            ret = None

        results.append({"symbol": symbol, "score": score, "return": ret})

    # ── Print results table ──────────────────────────────────────
    table = Table(
        title=f"NEXT-DAY PERFORMANCE: {scoring_date.strftime('%Y-%m-%d')} -> {check_date.strftime('%Y-%m-%d')}",
        show_header=True, header_style="bold magenta",
    )
    table.add_column("Rank", style="bold cyan", width=5)
    table.add_column("Symbol", style="bold white", width=12)
    table.add_column("Score", style="bold green", width=8)
    table.add_column("1D Return", width=12)
    table.add_column("vs Benchmark", width=14)

    returns = []
    for i, r in enumerate(results, 1):
        if r["return"] is not None:
            ret_str = f"{r['return']:+.2f}%"
            diff = r["return"] - bench_ret
            diff_str = f"{diff:+.2f}%"
            color = "green" if r["return"] > 0 else "red"
            table.add_row(
                str(i), r["symbol"], f"{r['score']:.1f}",
                f"[{color}]{ret_str}[/]", diff_str,
            )
            returns.append(r["return"])
        else:
            table.add_row(
                str(i), r["symbol"], f"{r['score']:.1f}",
                "[dim]N/A[/]", "-",
            )

    console.print(table)

    # ── Summary ──────────────────────────────────────────────────
    if returns:
        avg_ret = sum(returns) / len(returns)
        winners = sum(1 for r in returns if r > 0)
        outperformed = sum(1 for r in returns if r > bench_ret)

        console.print(f"\n[bold]Portfolio avg return:[/]  [{'green' if avg_ret > 0 else 'red'}]{avg_ret:+.2f}%[/]")
        console.print(f"[bold]Nifty 50 benchmark:[/]    {bench_ret:+.2f}%")
        console.print(f"[bold]Winners:[/]               {winners}/{len(returns)}")
        console.print(f"[bold]Beat benchmark:[/]        {outperformed}/{len(returns)}")
    else:
        console.print("\n[dim]No returns computed (missing price data for both days).[/]")

    console.rule("[bold cyan]DONE")


if __name__ == "__main__":
    args = parse_args()
    try:
        run_validate(args)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/]")
        sys.exit(130)
