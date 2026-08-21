"""
Fundamental Data Fetcher.

Retrieves key fundamental metrics from Yahoo Finance's .info dict.
Uses SQLite for storage: one row per fetch per ticker (history-preserving).
TTL-gated: only fetches if the latest DB row is older than 24h.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import yfinance as yf
from rich.progress import track

from config.settings import FUNDAMENTAL_WORKERS
from data.db import (
    get_latest_fundamentals,
    insert_fundamentals,
    fundamentals_to_dataframe,
)

logger = logging.getLogger(__name__)

# TTL for freshness check (hours)
FUNDAMENTAL_TTL_HOURS = 24

# Fields we care about from yfinance .info
WANTED_FIELDS = [
    # Valuation
    "trailingPE",
    "forwardPE",
    "priceToBook",
    "enterpriseToEbitda",
    "enterpriseToRevenue",
    # Profitability
    "returnOnEquity",
    "returnOnAssets",
    "profitMargins",
    "grossMargins",
    "operatingMargins",
    # Growth
    "earningsGrowth",
    "revenueGrowth",
    "earningsQuarterlyGrowth",
    # Financial health
    "debtToEquity",
    "currentRatio",
    "quickRatio",
    "totalCashPerShare",
    # Size
    "marketCap",
    "enterpriseValue",
    "sharesOutstanding",
    # Dividends
    "dividendYield",
    "payoutRatio",
    # Ownership
    "heldPercentInsiders",
    "heldPercentInstitutions",
    # Other
    "beta",
    "trailingEps",
    "forwardEps",
    "bookValue",
    "sector",
    "industry",
    "longName",
]


def _is_fresh(symbol: str) -> bool:
    """Check if the latest fundamentals row for a symbol is within TTL."""
    row = get_latest_fundamentals(symbol)
    if row is None:
        return False
    fetch_date_str = row.get("fetch_date", "")
    if not fetch_date_str:
        return False
    try:
        fetch_date = datetime.fromisoformat(fetch_date_str)
        return datetime.now() - fetch_date < timedelta(hours=FUNDAMENTAL_TTL_HOURS)
    except (ValueError, TypeError):
        return False


def fetch_fundamentals(ticker: str, force_refresh: bool = False) -> dict:
    """
    Fetch fundamental data for a single ticker.

    Returns a dict of fundamental metrics. Missing fields default to None.
    Checks DB freshness first; only hits yfinance if data is stale.
    """
    if not force_refresh and _is_fresh(ticker):
        row = get_latest_fundamentals(ticker)
        if row:
            logger.debug(f"[db] fundamentals {ticker}")
            return dict(row)

    try:
        info = yf.Ticker(ticker).info
        data = {field: info.get(field) for field in WANTED_FIELDS}
        data["ticker"] = ticker
        data["fetched_at"] = datetime.now().isoformat()

        insert_fundamentals(ticker, data)
        logger.debug(f"[fetched] fundamentals {ticker}")
        return data

    except Exception as e:
        logger.warning(f"Fundamental fetch failed for {ticker}: {e}")
        return {"ticker": ticker}


def fetch_fundamentals_batch(
    tickers: list[str],
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Fetch fundamentals for all tickers.

    Returns a DataFrame indexed by ticker with all fundamental columns.
    Only fetches tickers whose DB data is stale (>24h old or missing).
    """
    # Determine which tickers need fetching
    to_fetch: list[str] = []
    cached_count = 0
    for ticker in tickers:
        if not force_refresh and _is_fresh(ticker):
            cached_count += 1
        else:
            to_fetch.append(ticker)

    if cached_count:
        logger.info(f"Fundamentals: {cached_count} tickers from DB cache, {len(to_fetch)} to fetch.")

    # Fetch stale tickers from yfinance
    fetched_rows: list[dict] = []
    if to_fetch:
        max_workers = min(FUNDAMENTAL_WORKERS, len(to_fetch))
        executor = ThreadPoolExecutor(max_workers=max_workers)
        futures = {executor.submit(fetch_fundamentals, t, force_refresh): t for t in to_fetch}
        try:
            for future in track(
                as_completed(futures),
                total=len(futures),
                description="Fundamentals",
            ):
                fetched_rows.append(future.result())
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    # Load all tickers from DB (includes both cached and just-fetched)
    df = fundamentals_to_dataframe(tickers)
    if df.empty:
        logger.warning("No fundamental data available for any ticker.")
    else:
        logger.info(f"Fundamentals loaded: {len(df)} tickers.")
    return df
