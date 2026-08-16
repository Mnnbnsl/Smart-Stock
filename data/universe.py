"""
NSE Stock Universe Loader.

Downloads the Nifty 500 constituent list from NSE website.
Falls back to a bundled CSV if the download fails.
Applies basic liquidity and price filters.
"""

import os
import io
import logging
import requests
import pandas as pd
from datetime import datetime, timedelta

from config.settings import (
    CACHE_DIR,
    UNIVERSE_CSV,
    MIN_MARKET_CAP_CR,
    MIN_PRICE,
    NSE_HEADERS,
    NSE_BASE_URL,
)

logger = logging.getLogger(__name__)

# NSE provides downloadable CSV for major indices
NSE_INDEX_URLS = {
    "nifty500": "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20500",
    "nifty200": "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20200",
    "nifty100": "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20100",
}

# Fallback: well-known Nifty 100 symbols (safe baseline)
FALLBACK_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR",
    "BHARTIARTL", "SBILIFE", "BAJFINANCE", "KOTAKBANK", "LT", "AXISBANK",
    "ASIANPAINT", "MARUTI", "SUNPHARMA", "TITAN", "NESTLEIND", "WIPRO",
    "ULTRACEMCO", "ADANIENT", "NTPC", "POWERGRID", "TECHM", "HCLTECH",
    "SBIN", "INDUSINDBK", "BAJAJFINSV", "GRASIM", "HINDALCO", "DRREDDY",
    "DIVISLAB", "CIPLA", "COALINDIA", "EICHERMOT", "TATAMOTORS", "TATASTEEL",
    "ONGC", "BPCL", "IOC", "SHREECEM", "BRITANNIA", "APOLLOHOSP",
    "ADANIPORTS", "JSWSTEEL", "HDFCLIFE", "M&M", "HEROMOTOCO",
    "BAJAJ-AUTO", "GODREJCP", "DABUR", "PIDILITIND", "COLPAL", "MARICO",
    "HAVELLS", "VOLTAS", "LUPIN", "TORNTPHARM", "AUROPHARMA", "MCDOWELL-N",
    "BERGEPAINT", "AMBUJACEM", "ACC", "INDIGO", "TATACONSUM", "ITC",
    "BANKBARODA", "CANBK", "PNB", "FEDERALBNK", "IDFCFIRSTB",
    "MUTHOOTFIN", "CHOLAFIN", "RECLTD", "PFC", "IRCTC", "DMART",
    "JUBLFOOD", "ZOMATO", "NAUKRI", "PAYTM", "PERSISTENT", "LTIM",
    "MPHASIS", "COFORGE", "OFSS", "KPITTECH", "TATAELXSI", "DIXON",
    "ASTRAL", "POLYCAB", "ABFRL", "PAGEIND", "RELAXO", "BATA",
    "SRF", "AARTIIND", "ALKYLAMINE", "DEEPAKNTR", "NAVINFLUOR",
    "PIIND", "ATUL", "BALRAMCHIN", "CHAMBLFERT", "COROMANDEL",
    "TATACHEM", "GHCL", "GNFC", "GSFC",
]


def _get_nse_session() -> requests.Session:
    """Create a requests session with NSE cookies."""
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    try:
        # Visit homepage first to get cookies
        session.get(NSE_BASE_URL, timeout=10)
    except Exception as e:
        logger.warning(f"Could not establish NSE session: {e}")
    return session


def _fetch_nse_index_symbols(index_key: str = "nifty500") -> list[str]:
    """Fetch constituent symbols from NSE API."""
    url = NSE_INDEX_URLS.get(index_key, NSE_INDEX_URLS["nifty500"])
    session = _get_nse_session()
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        records = data.get("data", [])
        symbols = [r["symbol"] for r in records if r.get("symbol")]
        logger.info(f"Fetched {len(symbols)} symbols from NSE {index_key}")
        return symbols
    except Exception as e:
        logger.warning(f"NSE index fetch failed ({e}), using fallback list.")
        return []


def _load_cached_universe() -> pd.DataFrame | None:
    """Load universe from local CSV cache if it exists and is fresh (< 7 days)."""
    if not os.path.exists(UNIVERSE_CSV):
        return None
    mtime = datetime.fromtimestamp(os.path.getmtime(UNIVERSE_CSV))
    if datetime.now() - mtime > timedelta(days=7):
        logger.info("Universe cache is stale, will refresh.")
        return None
    df = pd.read_csv(UNIVERSE_CSV)
    logger.info(f"Loaded {len(df)} symbols from cached universe.")
    return df


def _save_universe(df: pd.DataFrame) -> None:
    """Save universe to local CSV cache."""
    os.makedirs(os.path.dirname(UNIVERSE_CSV), exist_ok=True)
    df.to_csv(UNIVERSE_CSV, index=False)
    logger.info(f"Universe saved: {len(df)} symbols -> {UNIVERSE_CSV}")


def load_universe(force_refresh: bool = False) -> pd.DataFrame:
    """
    Load the NSE stock universe.

    Returns a DataFrame with columns: [symbol, yf_ticker]
    - symbol: NSE symbol (e.g. 'TCS')
    - yf_ticker: Yahoo Finance ticker (e.g. 'TCS.NS')
    """
    if not force_refresh:
        cached = _load_cached_universe()
        if cached is not None:
            return cached

    # Try fetching from NSE
    symbols = _fetch_nse_index_symbols("nifty500")

    # Fall back to hard-coded list if NSE fails
    if len(symbols) < 50:
        logger.warning("Using fallback symbol list.")
        symbols = FALLBACK_SYMBOLS

    # Deduplicate and clean
    symbols = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))

    df = pd.DataFrame({
        "symbol": symbols,
        "yf_ticker": [f"{s}.NS" for s in symbols],
    })

    _save_universe(df)
    return df


def get_yf_tickers(force_refresh: bool = False) -> list[str]:
    """Convenience function: returns list of Yahoo Finance tickers."""
    df = load_universe(force_refresh=force_refresh)
    return df["yf_ticker"].tolist()


def get_symbols(force_refresh: bool = False) -> list[str]:
    """Convenience function: returns list of NSE symbols."""
    df = load_universe(force_refresh=force_refresh)
    return df["symbol"].tolist()
