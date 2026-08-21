"""
Backtest Data Loader.

Fetches and caches multi-year OHLCV price history for the backtest universe
plus the Nifty 50 benchmark. Uses SQLite for storage — same incremental
approach as the live engine's price_fetcher.
"""

import logging
from datetime import datetime, timedelta

import pandas as pd

from config.settings import BACKTEST_LOOKBACK_DAYS
from data.fetchers.price_fetcher import fetch_price_batch, fetch_price_data, get_benchmark_data

logger = logging.getLogger(__name__)

# Minimum trading days required per ticker to be usable in the backtest
MIN_TRADING_DAYS = 60


def _required_start() -> datetime:
    """Earliest date the cached price history must cover."""
    return datetime.now() - timedelta(days=BACKTEST_LOOKBACK_DAYS)


def load_backtest_price_data(
    tickers: list[str],
    force_refresh: bool = False,
) -> dict[str, pd.DataFrame]:
    """
    Load multi-year OHLCV history for all backtest tickers.

    Uses the same SQLite-backed incremental fetcher as the live engine.
    Tickes with insufficient history are re-fetched; thin tickers are dropped.

    Returns dict: { ticker -> OHLCV DataFrame } with at least MIN_TRADING_DAYS.
    """
    price_data = fetch_price_batch(
        tickers,
        force_refresh=force_refresh,
        period_days=BACKTEST_LOOKBACK_DAYS,
    )

    # Verify actual coverage and re-fetch if needed
    required_start = _required_start()
    insufficient = [
        t for t, df in price_data.items()
        if df is not None and not df.empty and df.index.min() > required_start
    ]
    if insufficient:
        logger.info(
            f"Re-fetching {len(insufficient)} tickers with insufficient history "
            f"(need data back to {required_start.date()})..."
        )
        for t in insufficient:
            df = fetch_price_data(
                t,
                force_refresh=True,
                period_days=BACKTEST_LOOKBACK_DAYS,
            )
            if df is not None and not df.empty:
                price_data[t] = df
            else:
                price_data.pop(t, None)

    thin = [t for t, df in price_data.items() if df is None or len(df) < MIN_TRADING_DAYS]
    if thin:
        logger.warning(
            f"Dropping {len(thin)} tickers with < {MIN_TRADING_DAYS} trading days: "
            f"{thin[:10]}{'...' if len(thin) > 10 else ''}"
        )
        for t in thin:
            price_data.pop(t, None)

    logger.info(f"Backtest price data ready for {len(price_data)}/{len(tickers)} tickers.")
    return price_data


def load_backtest_benchmark(
    force_refresh: bool = False,
) -> pd.DataFrame | None:
    """Load Nifty 50 (^NSEI) OHLCV history as the backtest benchmark."""
    df = get_benchmark_data(
        force_refresh=force_refresh,
        period_days=BACKTEST_LOOKBACK_DAYS,
    )
    if df is not None and not df.empty and df.index.min() > _required_start():
        logger.info("Benchmark cache too short - re-fetching full history...")
        df = get_benchmark_data(
            force_refresh=True,
            period_days=BACKTEST_LOOKBACK_DAYS,
        )
    return df
