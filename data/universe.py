"""
NSE Stock Universe Loader.

Downloads the Nifty 500 constituent list from NSE website.
Falls back to a bundled CSV if the download fails.
Applies basic liquidity and price filters.
"""

import os
import io
import json
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

# NSE provides downloadable CSV for major indices (archive subdomain works
# without session cookies; the live API requires cookies and pagination).
NSE_INDEX_URLS = {
    "nifty500": "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20500",
    "nifty200": "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20200",
    "nifty100": "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20100",
}

NSE_INDEX_CSV_URLS = {
    "nifty500": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "nifty200": "https://archives.nseindia.com/content/indices/ind_nifty200list.csv",
    "nifty100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
}

NSE_INDEX_PAGE_SIZE = 100

# Bump when the cache format changes so old caches are rebuilt automatically.
UNIVERSE_CACHE_VERSION = 2

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

# Symbols that no longer resolve on Yahoo (delisted / renamed) — persisted so
# later runs skip them at load instead of wasting time on throttled retries.
DELISTED_CACHE = os.path.join(CACHE_DIR, "delisted.json")


def _load_delisted() -> set[str]:
    """Load the set of NSE symbols previously found to have no price data."""
    try:
        with open(DELISTED_CACHE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_delisted(symbols: set[str]) -> None:
    """Persist the delisted-symbol set to cache."""
    os.makedirs(os.path.dirname(DELISTED_CACHE), exist_ok=True)
    with open(DELISTED_CACHE, "w") as f:
        json.dump(sorted(symbols), f, indent=2)


def mark_delisted(symbols: list[str]) -> None:
    """Record symbols with no price data so future runs skip them."""
    delisted = _load_delisted()
    delisted.update(symbols)
    _save_delisted(delisted)


def _drop_delisted(df: pd.DataFrame) -> pd.DataFrame:
    """Filter a universe DataFrame (symbol column) to drop known-dead symbols."""
    delisted = _load_delisted()
    if not delisted:
        return df
    dropped = [s for s in df["symbol"] if s in delisted]
    if dropped:
        logger.info(f"Skipping {len(dropped)} delisted symbols: {sorted(set(dropped))}")
    return df[~df["symbol"].isin(delisted)].reset_index(drop=True)


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


def _fetch_nse_index_csv(index_key: str = "nifty500") -> list[str]:
    """Fetch constituent symbols from NSE's archives CSV (no cookies needed)."""
    url = NSE_INDEX_CSV_URLS.get(index_key, NSE_INDEX_CSV_URLS["nifty500"])
    try:
        resp = requests.get(url, headers=NSE_HEADERS, timeout=20)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        if "Symbol" not in df.columns:
            logger.warning(f"NSE CSV for {index_key} missing Symbol column: {list(df.columns)}")
            return []
        symbols = [
            str(s).strip().upper() for s in df["Symbol"] if str(s).strip()
        ]
        logger.info(f"Fetched {len(symbols)} symbols from NSE {index_key} CSV")
        return symbols
    except Exception as e:
        logger.warning(f"NSE CSV fetch failed ({e}).")
        return []


def _fetch_nse_index_api(index_key: str = "nifty500") -> list[str]:
    """Fetch constituent symbols from NSE API, paginated to cover the full list."""
    base = NSE_INDEX_URLS.get(index_key, NSE_INDEX_URLS["nifty500"])
    session = _get_nse_session()
    symbols: list[str] = []
    offset = 0
    total: int | None = None
    try:
        while True:
            sep = "&" if "?" in base else "?"
            resp = session.get(
                f"{base}{sep}limit={NSE_INDEX_PAGE_SIZE}&offset={offset}",
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            records = data.get("data", [])
            symbols += [r["symbol"] for r in records if r.get("symbol")]
            if total is None:
                total = data.get("total") or len(records)
            if total is None or len(symbols) >= total or not records:
                break
            offset += len(records)
        logger.info(f"Fetched {len(symbols)} symbols from NSE {index_key} API")
        return symbols
    except Exception as e:
        logger.warning(f"NSE index API fetch failed ({e}).")
        return []


def _fetch_nse_index_symbols(index_key: str = "nifty500") -> list[str]:
    """Fetch constituent symbols: archives CSV first, then the paginated API."""
    symbols = _fetch_nse_index_csv(index_key)
    if symbols:
        return symbols
    return _fetch_nse_index_api(index_key)


def _load_cached_universe() -> pd.DataFrame | None:
    """Load universe from local CSV cache if it exists, is fresh (< 7 days),
    and matches the current cache format version."""
    if not os.path.exists(UNIVERSE_CSV):
        return None
    mtime = datetime.fromtimestamp(os.path.getmtime(UNIVERSE_CSV))
    if datetime.now() - mtime > timedelta(days=7):
        logger.info("Universe cache is stale, will refresh.")
        return None
    try:
        df = pd.read_csv(UNIVERSE_CSV)
    except Exception as e:
        logger.warning(f"Universe cache unreadable ({e}), will rebuild.")
        return None
    version = int(df.get("source_version", pd.Series([0])).iloc[0] or 0)
    if "source" not in df.columns or "yf_ticker" not in df.columns \
            or version < UNIVERSE_CACHE_VERSION:
        logger.info("Universe cache format is outdated, will rebuild.")
        return None
    df = df.drop(columns=["source_version", "source"])
    logger.info(f"Loaded {len(df)} symbols from cached universe.")
    return df


def _save_universe(df: pd.DataFrame, source: str = "nse-archives") -> None:
    """Save universe to local CSV cache with a format version marker."""
    os.makedirs(os.path.dirname(UNIVERSE_CSV), exist_ok=True)
    out = df.copy()
    out["source"] = source
    out["source_version"] = UNIVERSE_CACHE_VERSION
    out.to_csv(UNIVERSE_CSV, index=False)
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
            return _drop_delisted(cached)

    # Try fetching from NSE (archives CSV, then paginated API)
    symbols = _fetch_nse_index_symbols("nifty500")
    source = "nse-archives"

    # Fall back to hard-coded list if NSE fails
    if len(symbols) < 50:
        logger.warning("Using fallback symbol list.")
        symbols = FALLBACK_SYMBOLS
        source = "fallback"

    # Deduplicate and clean
    symbols = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))

    df = pd.DataFrame({
        "symbol": symbols,
        "yf_ticker": [f"{s}.NS" for s in symbols],
    })

    _save_universe(df, source=source)
    return _drop_delisted(df)


def get_yf_tickers(force_refresh: bool = False) -> list[str]:
    """Convenience function: returns list of Yahoo Finance tickers."""
    df = load_universe(force_refresh=force_refresh)
    return df["yf_ticker"].tolist()


def get_symbols(force_refresh: bool = False) -> list[str]:
    """Convenience function: returns list of NSE symbols."""
    df = load_universe(force_refresh=force_refresh)
    return df["symbol"].tolist()
