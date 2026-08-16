"""
Price Data Fetcher.

Downloads OHLCV data from Yahoo Finance for all NSE symbols.
Caches to Parquet files to avoid redundant API calls.
"""

import os
import logging
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from config.settings import CACHE_DIR, LOOKBACK_DAYS, PRICE_CACHE_TTL_HOURS

logger = logging.getLogger(__name__)

PRICE_CACHE_DIR = os.path.join(CACHE_DIR, "price")


def _cache_path(ticker: str) -> str:
    safe = ticker.replace(".", "_").replace("/", "_")
    return os.path.join(PRICE_CACHE_DIR, f"{safe}.parquet")


def _is_cache_valid(path: str, ttl_hours: int) -> bool:
    if not os.path.exists(path):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    return datetime.now() - mtime < timedelta(hours=ttl_hours)


def fetch_price_data(
    ticker: str,
    force_refresh: bool = False,
    period_days: int = LOOKBACK_DAYS,
    ttl_hours: int | None = None,
) -> pd.DataFrame | None:
    """
    Fetch OHLCV for a single ticker.

    Returns DataFrame with columns: [Open, High, Low, Close, Volume]
    indexed by Date. Returns None on failure.
    """
    os.makedirs(PRICE_CACHE_DIR, exist_ok=True)
    cache = _cache_path(ticker)
    cache_ttl = ttl_hours or PRICE_CACHE_TTL_HOURS

    if not force_refresh and _is_cache_valid(cache, cache_ttl):
        try:
            df = pd.read_parquet(cache)
            logger.debug(f"[cache] {ticker}: {len(df)} rows")
            return df
        except Exception as e:
            logger.warning(f"Cache read failed for {ticker}: {e}")

    try:
        end = datetime.today()
        start = end - timedelta(days=period_days)
        ticker_obj = yf.Ticker(ticker)
        df = ticker_obj.history(start=start.strftime("%Y-%m-%d"),
                                end=end.strftime("%Y-%m-%d"),
                                auto_adjust=True)
        if df.empty:
            logger.warning(f"No price data returned for {ticker}")
            return None

        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df.to_parquet(cache)
        logger.debug(f"[fetched] {ticker}: {len(df)} rows")
        return df

    except Exception as e:
        logger.warning(f"Price fetch failed for {ticker}: {e}")
        return None


def fetch_price_batch(
    tickers: list[str],
    force_refresh: bool = False,
    period_days: int = LOOKBACK_DAYS,
    ttl_hours: int | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Fetch OHLCV for a batch of tickers.

    Returns dict: { ticker -> DataFrame }
    Failed tickers are omitted.
    """
    results: dict[str, pd.DataFrame] = {}
    cache_ttl = ttl_hours or PRICE_CACHE_TTL_HOURS

    # Separate cache hits from misses
    to_download: list[str] = []
    for ticker in tickers:
        cache = _cache_path(ticker)
        if not force_refresh and _is_cache_valid(cache, cache_ttl):
            try:
                results[ticker] = pd.read_parquet(cache)
            except Exception:
                to_download.append(ticker)
        else:
            to_download.append(ticker)

    if to_download:
        logger.info(f"Downloading price data for {len(to_download)} tickers...")
        end = datetime.today()
        start = end - timedelta(days=period_days)

        # yfinance batch download (much faster than individual calls)
        try:
            raw = yf.download(
                tickers=" ".join(to_download),
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                auto_adjust=True,
                progress=False,
                threads=True,
            )
        except Exception as e:
            logger.error(f"Batch download failed: {e}")
            raw = pd.DataFrame()

        if not raw.empty:
            # yfinance batch returns MultiIndex columns: (field, ticker)
            if isinstance(raw.columns, pd.MultiIndex):
                for ticker in to_download:
                    try:
                        df = raw.xs(ticker, level=1, axis=1)[
                            ["Open", "High", "Low", "Close", "Volume"]
                        ].dropna(how="all")
                        if df.empty:
                            continue
                        df.index = pd.to_datetime(df.index).tz_localize(None)
                        os.makedirs(PRICE_CACHE_DIR, exist_ok=True)
                        df.to_parquet(_cache_path(ticker))
                        results[ticker] = df
                    except Exception as e:
                        logger.warning(f"Could not extract {ticker}: {e}")
            else:
                # Single ticker batch
                if len(to_download) == 1:
                    ticker = to_download[0]
                    df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
                    if not df.empty:
                        df.index = pd.to_datetime(df.index).tz_localize(None)
                        df.to_parquet(_cache_path(ticker))
                        results[ticker] = df

    logger.info(f"Price data ready for {len(results)}/{len(tickers)} tickers.")
    return results


def get_benchmark_data(force_refresh: bool = False, ttl_hours: int | None = None, period_days: int | None = None) -> pd.DataFrame | None:
    """Fetch Nifty 50 index as benchmark (^NSEI)."""
    return fetch_price_data(
        "^NSEI",
        force_refresh=force_refresh,
        period_days=period_days or LOOKBACK_DAYS,
        ttl_hours=ttl_hours,
    )
