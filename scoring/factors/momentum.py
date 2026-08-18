"""
Momentum Factor.

Measures relative price performance vs Nifty 50 benchmark
over 1M, 3M, 6M, and 12M periods.

Score: 0–100 (higher = stronger relative momentum)
"""

import logging
import numpy as np
import pandas as pd

from scoring.normalizer import safe_score
from config.settings import MOMENTUM_PERIOD_WEIGHTS

logger = logging.getLogger(__name__)

# Trading day approximations
PERIOD_DAYS = {
    "1m":  21,
    "3m":  63,
    "6m":  126,
    "12m": 252,
}


def _return(prices: pd.Series, days: int) -> float:
    """Compute price return over the last N trading days."""
    if len(prices) < days + 1:
        return np.nan
    return (prices.iloc[-1] / prices.iloc[-days - 1]) - 1


def compute_momentum_scores(
    price_data: dict[str, pd.DataFrame],
    benchmark_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Compute momentum scores for all tickers.

    Parameters
    ----------
    price_data : dict
        { ticker -> OHLCV DataFrame }
    benchmark_df : pd.DataFrame | None
        Nifty 50 OHLCV data. If None, absolute returns are used.

    Returns
    -------
    pd.DataFrame
        Indexed by ticker with columns:
        [ret_1m, ret_3m, ret_6m, ret_12m,
         rel_1m, rel_3m, rel_6m, rel_12m,
         momentum_score]
    """
    rows: list[dict] = []

    # Benchmark returns
    bench_returns = {}
    if benchmark_df is not None and not benchmark_df.empty:
        bench_close = benchmark_df["Close"]
        for period, days in PERIOD_DAYS.items():
            bench_returns[period] = _return(bench_close, days)

    for ticker, df in price_data.items():
        if df is None or df.empty or len(df) < 22:
            continue
        close = df["Close"]
        row: dict = {"ticker": ticker}

        for period, days in PERIOD_DAYS.items():
            ret = _return(close, days)
            bench_ret = bench_returns.get(period, 0.0) or 0.0
            row[f"ret_{period}"] = ret
            # Relative return vs benchmark
            row[f"rel_{period}"] = ret - bench_ret if not np.isnan(ret) else np.nan

        rows.append(row)

    if not rows:
        return pd.DataFrame()

    df_out = pd.DataFrame(rows).set_index("ticker")

    # Composite momentum score: weighted sum of period percentile scores
    # NaN periods are skipped and weights renormalized per-stock so that
    # missing data (e.g.12m with insufficient lookback) does not poison
    # the entire composite.
    composite = pd.Series(0.0, index=df_out.index)
    weight_sum = pd.Series(0.0, index=df_out.index)
    for period, weight in MOMENTUM_PERIOD_WEIGHTS.items():
        col = f"rel_{period}"
        if col in df_out.columns:
            period_score = safe_score(df_out[col], ascending=True)
            valid = period_score.notna()
            composite[valid] += weight * period_score[valid]
            weight_sum[valid] += weight

    mask = weight_sum > 0
    composite[mask] = composite[mask] / weight_sum[mask]

    df_out["momentum_score"] = composite.clip(0, 100)
    logger.info(f"Momentum: scored {len(df_out)} tickers")
    return df_out
