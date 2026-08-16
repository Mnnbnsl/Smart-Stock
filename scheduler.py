"""
Daily Scheduler.

Runs the scoring pipeline every day at the configured time (default: 08:30 IST).
Designed to be left running in the background.

Usage:
    python scheduler.py

Press Ctrl+C to stop.
"""

import sys
import os
import logging
import time
from datetime import datetime

import schedule
from rich.console import Console

# ── Ensure project root is on sys.path ──────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import DAILY_RUN_TIME
from main import run_pipeline

console = Console()
logger = logging.getLogger(__name__)


def scheduled_job():
    """Job that runs the scoring pipeline once."""
    console.rule(f"[bold cyan]SCHEDULED RUN -- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        run_pipeline(dry_run=False, force_refresh=True)
    except Exception as e:
        logger.error(f"Scheduled run failed: {e}", exc_info=True)
        console.print(f"[bold red]Scheduled run failed: {e}[/]")


def main():
    console.print(f"[bold green]Scheduler started.[/]")
    console.print(f"   Daily run at: [cyan]{DAILY_RUN_TIME} IST[/]")
    console.print(f"   Press [bold]Ctrl+C[/] to stop.\n")

    schedule.every().day.at(DAILY_RUN_TIME).do(scheduled_job)

    # Show time until next run
    next_run = schedule.next_run()
    if next_run:
        console.print(f"   Next run: [yellow]{next_run.strftime('%Y-%m-%d %H:%M:%S')}[/]\n")

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Scheduler stopped.[/]")
