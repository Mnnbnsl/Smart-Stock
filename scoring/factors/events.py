"""
Events Factor.

Applies penalties for upcoming corporate events that create uncertainty:
  - Earnings/results within next N days → penalize score
  - Ex-dividend within next 2 days → minor penalty (stock drops post-div)

Since NSE doesn't provide a free real-time events API, this module:
  1. Uses yfinance calendar for earnings dates
  2. Falls back to a neutral score (50) if no data available

Score: 0–100 (higher = fewer upcoming risk events)
"""

import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import yfinance as yf
from rich.progress import track

from config.settings import EARNINGS_PENALTY_DAYS

logger = logging.getLogger(__name__)


def _get_next_earnings(ticker: str) -> datetime | None:
    """Fetch next earnings date from yfinance calendar."""
    try:
        t = yf.Ticker(ticker)
        cal = t.calendar
        if cal is None:
            return None
        # calendar can be a dict or DataFrame depending on yfinance version
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date", [])
            if dates:
                return pd.to_datetime(dates[0]).to_pydatetime()
        elif isinstance(cal, pd.DataFrame):
            if "Earnings Date" in cal.columns:
                val = cal["Earnings Date"].iloc[0]
                return pd.to_datetime(val).to_pydatetime()
    except Exception as e:
        logger.debug(f"Earnings calendar fetch failed for {ticker}: {e}")
    return None


def compute_events_scores(tickers: list[str]) -> pd.DataFrame:
    """
    Compute events-risk scores for a list of tickers.

    Returns DataFrame indexed by ticker with columns:
    [days_to_earnings, events_score]

    Logic:
      - No upcoming earnings (>30 days or unknown): score = 70  (mild positive)
      - Earnings 8–30 days away: score = 50  (neutral)
      - Earnings 4–7 days away:  score = 30  (mild penalty)
      - Earnings ≤ EARNINGS_PENALTY_DAYS days: score = 10  (strong penalty)
    """
    rows: list[dict] = []
    today = datetime.now().date()

    def _score(ticker: str) -> dict:
        next_earn = _get_next_earnings(ticker)
        days_away = None
        if next_earn is not None:
            days_away = (next_earn.date() - today).days

        # Score assignment
        if days_away is None:
            score = 70.0  # unknown → mild positive (no imminent risk known)
        elif days_away <= 0:
            score = 50.0  # earnings just passed
        elif days_away <= EARNINGS_PENALTY_DAYS:
            score = 10.0  # imminent earnings → high uncertainty
        elif days_away <= 7:
            score = 30.0
        elif days_away <= 30:
            score = 50.0
        else:
            score = 70.0

        return {
            "ticker":           ticker,
            "days_to_earnings": days_away,
            "events_score":     score,
        }

    max_workers = min(8, len(tickers)) if tickers else 1
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {executor.submit(_score, t): t for t in tickers}
    try:
        for future in track(
            as_completed(futures),
            total=len(futures),
            description="Events",
        ):
            rows.append(future.result())
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if not rows:
        return pd.DataFrame()

    df_out = pd.DataFrame(rows).set_index("ticker")
    logger.info(f"Events: scored {len(df_out)} tickers")
    return df_out
