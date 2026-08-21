"""
Price Data Fetcher.

Downloads OHLCV data from Yahoo Finance for all NSE symbols.
Uses SQLite for incremental storage — only fetches the delta since the
last stored date per symbol.
"""

import os
import logging
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from config.settings import LOOKBACK_DAYS, PRICE_BATCH_CHUNK
from data.db import get_max_price_date, insert_prices, get_prices

logger = logging.getLogger(__name__)


def fetch_price_data(
    ticker: str,
    force_refresh: bool = False,
    period_days: int = LOOKBACK_DAYS,
) -> pd.DataFrame | None:
    """
    Fetch OHLCV for a single ticker.

    Checks the DB for the latest stored date and only fetches the delta.
    Returns DataFrame with columns: [Open, High, Low, Close, Volume]
    indexed by Date. Returns None on failure.
    """
    max_stored = get_max_price_date(ticker) if not force_refresh else None

    if max_stored and not force_refresh:
        # Data exists — fetch only the gap
        start_date = (
            datetime.strptime(max_stored, "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")
        end_date = datetime.today().strftime("%Y-%m-%d")

        if start_date > end_date:
            # Already up to date
            df = get_prices(ticker)
            logger.debug(f"[db] {ticker}: {len(df)} rows (up to date)")
            return df if not df.empty else None

        try:
            ticker_obj = yf.Ticker(ticker)
            df = ticker_obj.history(start=start_date, end=end_date, auto_adjust=True)
            if not df.empty:
                df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
                df.index = pd.to_datetime(df.index).tz_localize(None)
                insert_prices(ticker, df)
                # Return full history from DB
                full = get_prices(ticker)
                logger.debug(f"[incremental] {ticker}: +{len(df)} rows, total {len(full)}")
                return full if not full.empty else None
            else:
                # No new data but existing data is fine
                full = get_prices(ticker)
                logger.debug(f"[db] {ticker}: {len(full)} rows (no new data)")
                return full if not full.empty else None
        except Exception as e:
            logger.warning(f"Incremental price fetch failed for {ticker}: {e}")
            # Fall through to full fetch

    # Full fetch (first time or force_refresh)
    try:
        end = datetime.today()
        start = end - timedelta(days=period_days)
        ticker_obj = yf.Ticker(ticker)
        df = ticker_obj.history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            auto_adjust=True,
        )
        if df.empty:
            logger.warning(f"No price data returned for {ticker}")
            return None

        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        insert_prices(ticker, df)
        logger.debug(f"[fetched] {ticker}: {len(df)} rows")
        return df

    except Exception as e:
        logger.warning(f"Price fetch failed for {ticker}: {e}")
        return None


def _download_and_extract(
    batch: list[str],
    start: datetime,
    end: datetime,
    results: dict[str, pd.DataFrame],
) -> None:
    """Download one chunk of tickers via yfinance and extract per-ticker frames."""
    if not batch:
        return
    try:
        raw = yf.download(
            tickers=" ".join(batch),
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        logger.error(f"Batch download failed: {e}")
        raw = pd.DataFrame()

    if raw.empty:
        return

    if isinstance(raw.columns, pd.MultiIndex):
        for ticker in batch:
            try:
                df = raw.xs(ticker, level=1, axis=1)[
                    ["Open", "High", "Low", "Close", "Volume"]
                ].dropna(how="all")
                if df.empty:
                    continue
                df.index = pd.to_datetime(df.index).tz_localize(None)
                insert_prices(ticker, df)
                results[ticker] = df
            except Exception as e:
                logger.warning(f"Could not extract {ticker}: {e}")
    else:
        if len(batch) == 1:
            ticker = batch[0]
            df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
            if not df.empty:
                df.index = pd.to_datetime(df.index).tz_localize(None)
                insert_prices(ticker, df)
                results[ticker] = df


def fetch_price_batch(
    tickers: list[str],
    force_refresh: bool = False,
    period_days: int = LOOKBACK_DAYS,
    ttl_hours: int | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Fetch OHLCV for a batch of tickers.

    Uses incremental DB-backed storage:
      - Symbols with fresh data in DB are loaded from DB (no network call).
      - Symbols with stale/missing data are downloaded and stored.
      - Batch downloads are chunked (PRICE_BATCH_CHUNK per call).

    Parameters
    ----------
    ttl_hours : int | None
        Kept for API compatibility but no longer used (DB freshness is
        determined by max stored date vs today).

    Returns
    -------
    dict[str, pd.DataFrame]
        { ticker -> OHLCV DataFrame }. Failed tickers are omitted.
    """
    results: dict[str, pd.DataFrame] = {}

    # Separate: DB already has data vs needs download
    to_download: list[str] = []
    for ticker in tickers:
        if not force_refresh:
            max_date = get_max_price_date(ticker)
            if max_date:
                # Check if it covers up to today (or yesterday for weekends)
                stored = datetime.strptime(max_date, "%Y-%m-%d")
                today = datetime.today()
                # Allow 3-day slack for weekends/holidays
                if (today - stored).days <= 3:
                    df = get_prices(ticker)
                    if not df.empty:
                        results[ticker] = df
                        continue
        to_download.append(ticker)

    if to_download:
        logger.info(f"Downloading price data for {len(to_download)} tickers...")
        end = datetime.today()
        start = end - timedelta(days=period_days)

        chunk_size = PRICE_BATCH_CHUNK
        total_chunks = (len(to_download) + chunk_size - 1) // chunk_size
        for i in range(0, len(to_download), chunk_size):
            chunk = to_download[i : i + chunk_size]
            if total_chunks > 1:
                logger.info(
                    f"  price chunk {i // chunk_size + 1}/{total_chunks}: "
                    f"{len(chunk)} tickers"
                )
            _download_and_extract(chunk, start, end, results)

        # Retry leftovers individually
        missing = [t for t in to_download if t not in results]
        if missing:
            logger.info(f"Retrying {len(missing)} symbols individually...")
            for t in missing:
                df = fetch_price_data(t, force_refresh=True, period_days=period_days)
                if df is not None and not df.empty:
                    results[t] = df

    logger.info(f"Price data ready for {len(results)}/{len(tickers)} tickers.")
    return results


def get_benchmark_data(
    force_refresh: bool = False,
    ttl_hours: int | None = None,
    period_days: int | None = None,
) -> pd.DataFrame | None:
    """Fetch Nifty 50 index as benchmark (^NSEI)."""
    return fetch_price_data(
        "^NSEI",
        force_refresh=force_refresh,
        period_days=period_days or LOOKBACK_DAYS,
    )
