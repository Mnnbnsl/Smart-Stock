"""
Fundamental Data Fetcher.

Retrieves key fundamental metrics from Yahoo Finance's .info dict.
Cached to Parquet (24-hour TTL since fundamentals change slowly).
"""

import os
import json
import logging
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from config.settings import CACHE_DIR, FUNDAMENTAL_CACHE_TTL_HOURS

logger = logging.getLogger(__name__)

FUND_CACHE_DIR = os.path.join(CACHE_DIR, "fundamentals")

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


def _cache_path(ticker: str) -> str:
    safe = ticker.replace(".", "_").replace("/", "_")
    return os.path.join(FUND_CACHE_DIR, f"{safe}.json")


def _is_cache_valid(path: str) -> bool:
    if not os.path.exists(path):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    return datetime.now() - mtime < timedelta(hours=FUNDAMENTAL_CACHE_TTL_HOURS)


def fetch_fundamentals(ticker: str, force_refresh: bool = False) -> dict:
    """
    Fetch fundamental data for a single ticker.

    Returns a dict of fundamental metrics. Missing fields default to None.
    """
    os.makedirs(FUND_CACHE_DIR, exist_ok=True)
    cache = _cache_path(ticker)

    if not force_refresh and _is_cache_valid(cache):
        try:
            with open(cache, "r") as f:
                data = json.load(f)
            logger.debug(f"[cache] fundamentals {ticker}")
            return data
        except Exception as e:
            logger.warning(f"Fundamental cache read failed for {ticker}: {e}")

    try:
        info = yf.Ticker(ticker).info
        data = {field: info.get(field) for field in WANTED_FIELDS}
        data["ticker"] = ticker
        data["fetched_at"] = datetime.now().isoformat()

        with open(cache, "w") as f:
            json.dump(data, f, indent=2, default=str)
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
    """
    rows: list[dict] = []
    for ticker in tickers:
        d = fetch_fundamentals(ticker, force_refresh=force_refresh)
        rows.append(d)

    df = pd.DataFrame(rows)
    if "ticker" in df.columns:
        df = df.set_index("ticker")
    return df
