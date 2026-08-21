"""
Stock Scoring Engine — Main Entry Point.

Usage:
    # Full run on Nifty 500 universe
    python main.py

    # Quick dry run on 20 stocks (faster for testing)
    python main.py --dry-run

    # Force refresh all cached data
    python main.py --refresh

    # Limit universe size
    python main.py --limit 50
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

from config.settings import LOGS_DIR, DRY_RUN_SYMBOLS
from data.db import init_schema
from data.universe import load_universe
from scoring.engine import ScoringEngine
from output.final_ranker import save_results

# ── Logging setup ─────────────────────────────────────────────
os.makedirs(LOGS_DIR, exist_ok=True)
_log_file = os.path.join(LOGS_DIR, "engine.log")
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

# Silence yfinance's internal error spam (delisted-symbol warnings etc.).
# Our own modules log their own warnings at the appropriate level.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def parse_args():
    parser = argparse.ArgumentParser(
        description="NSE Stock Quantitative Scoring Engine"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run on a small set of 20 well-known stocks (fast test mode)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force refresh all cached data (ignore cache)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit universe to N symbols (e.g. --limit 100 for faster runs)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=25,
        help="Number of stocks to shortlist (default: 25)",
    )
    return parser.parse_args()


def run_pipeline(
    dry_run: bool = False,
    force_refresh: bool = False,
    limit: int | None = None,
    top_n: int = 25,
) -> dict:
    """
    Execute the full scoring pipeline.

    Returns dict of output file paths.
    """
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    console.rule(f"[bold cyan]SCORING ENGINE RUN -- {run_ts}")

    # ── Initialize SQLite DB ───────────────────────────────────
    init_schema()

    # ── Load universe ────────────────────────────────────────────
    if dry_run:
        symbols = DRY_RUN_SYMBOLS
        console.print(f"[yellow]DRY RUN MODE - {len(symbols)} symbols[/]")
    else:
        universe_df = load_universe(force_refresh=force_refresh)
        symbols = universe_df["symbol"].tolist()
        if limit:
            symbols = symbols[:limit]
            console.print(f"[yellow]Limited to {limit} symbols[/]")

    tickers = [f"{s}.NS" for s in symbols]
    console.print(f"[bold]Universe:[/] {len(tickers)} tickers\n")

    # ── Run engine ───────────────────────────────────────────────
    engine = ScoringEngine(top_n=top_n)
    results_df = engine.run(
        tickers=tickers,
        nse_symbols=symbols,
        force_refresh=force_refresh,
    )

    if results_df.empty:
        console.print("[bold red]Scoring failed - no results produced.[/]")
        return {}

    # ── Save outputs ─────────────────────────────────────────────
    paths = save_results(results_df, run_ts=run_ts)

    # ── Print shortlist ──────────────────────────────────────────
    shortlist = results_df[results_df["shortlisted"] == True]
    console.rule("[bold green]SHORTLIST -- READY FOR STAGE 2 AGENT RESEARCH")
    for _, row in shortlist.iterrows():
        console.print(
            f"  [cyan]#{int(row['rank']):>3}[/]  "
            f"[bold white]{row['symbol']:<12}[/]  "
            f"Score: [green]{row['quant_score']:.1f}[/]"
        )

    console.print(f"\n[bold]Reports saved to:[/]")
    for key, path in paths.items():
        console.print(f"   {key}: [dim]{path}[/]")

    console.rule("[bold cyan]DONE")
    return paths


if __name__ == "__main__":
    args = parse_args()
    try:
        run_pipeline(
            dry_run=args.dry_run,
            force_refresh=args.refresh,
            limit=args.limit,
            top_n=args.top_n,
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted - run aborted. No partial results saved.[/]")
        sys.exit(130)
