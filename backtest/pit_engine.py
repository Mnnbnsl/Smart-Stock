"""
Point-in-Time Factor Scoring Engine for Backtesting.

Prevents lookahead bias by strictly slicing price & factor data up to cutoff_date.
"""

import logging
from datetime import datetime
import pandas as pd

from scoring.factors.momentum import compute_momentum_scores
from scoring.factors.liquidity import compute_liquidity_scores
from scoring.factors.technical import compute_technical_scores
from scoring.factors.events import compute_events_scores
from scoring.normalizer import safe_score
from config.settings import FACTOR_WEIGHTS

logger = logging.getLogger(__name__)

# Price-derived factors available point-in-time (fundamental/events data is a
# current snapshot and would leak lookahead information into the backtest).
PRICE_FACTOR_COLS = {
    "momentum":  "momentum_score",
    "technical": "technical_score",
    "liquidity": "liquidity_score",
}


def _price_factor_weights() -> dict[str, float]:
    """
    Renormalize the live engine's FACTOR_WEIGHTS over the price-only factor
    subset so the backtest ranks stocks with the SAME relative factor emphasis
    as the live engine (momentum 44.8% / technical 27.6% / liquidity 27.6%).
    """
    total = sum(FACTOR_WEIGHTS.get(f, 0.0) for f in PRICE_FACTOR_COLS)
    if total <= 0:
        equal = 1.0 / len(PRICE_FACTOR_COLS)
        return {col: equal for col in PRICE_FACTOR_COLS.values()}
    return {
        col: FACTOR_WEIGHTS[f] / total
        for f, col in PRICE_FACTOR_COLS.items()
    }


def score_point_in_time(
    price_data: dict[str, pd.DataFrame],
    cutoff_date: pd.Timestamp,
    benchmark_df: pd.DataFrame | None = None,
    top_n: int = 15,
) -> pd.DataFrame:
    """
    Score all stocks at point-in-time cutoff_date.

    Parameters
    ----------
    price_data : dict[str, pd.DataFrame]
        Full price dataset.
    cutoff_date : pd.Timestamp
        Historical evaluation date. Only data up to cutoff_date is used.
    benchmark_df : pd.DataFrame | None
        Nifty 50 benchmark DataFrame.
    top_n : int
        Number of top ranked stocks to shortlist.

    Returns
    -------
    pd.DataFrame
        Ranked DataFrame with top_n shortlisted stocks as of cutoff_date.
    """
    # ── Step 1: Slice data up to cutoff_date ─────────────────────
    pit_price_data: dict[str, pd.DataFrame] = {}
    for ticker, df in price_data.items():
        if df is None or df.empty:
            continue
        sliced = df[df.index <= cutoff_date]
        if len(sliced) >= 30:  # Require minimum 30 trading days
            pit_price_data[ticker] = sliced

    if not pit_price_data:
        return pd.DataFrame()

    pit_benchmark = None
    if benchmark_df is not None and not benchmark_df.empty:
        pit_benchmark = benchmark_df[benchmark_df.index <= cutoff_date]

    # ── Step 2: Compute Point-in-Time Factors ─────────────────────
    factor_frames: dict[str, pd.DataFrame] = {}

    # Momentum
    mom = compute_momentum_scores(pit_price_data, pit_benchmark)
    if not mom.empty:
        factor_frames["momentum"] = mom[["momentum_score"]]

    # Liquidity
    liq = compute_liquidity_scores(pit_price_data)
    if not liq.empty:
        factor_frames["liquidity"] = liq[["liquidity_score"]]

    # Technical
    tech = compute_technical_scores(pit_price_data)
    if not tech.empty:
        factor_frames["technical"] = tech[["technical_score"]]

    if not factor_frames:
        return pd.DataFrame()

    # Merge factor frames
    merged = pd.concat(factor_frames.values(), axis=1, join="outer")

    # Weights for the price-only backtest subset, renormalized from the live
    # engine's FACTOR_WEIGHTS (keeps relative emphasis identical to live).
    sub_weights = _price_factor_weights()

    quant_score = pd.Series(0.0, index=merged.index)
    for col, w in sub_weights.items():
        if col in merged.columns:
            quant_score += w * merged[col].fillna(50.0)
        else:
            quant_score += w * 50.0

    merged["quant_score"] = quant_score.clip(0, 100).round(2)
    merged["symbol"] = merged.index.map(lambda t: t.replace(".NS", ""))
    merged["rank"] = merged["quant_score"].rank(ascending=False, method="min").astype(int)

    ranked = merged.sort_values("quant_score", ascending=False)
    shortlisted = ranked.head(top_n).copy()
    shortlisted["shortlisted"] = True
    shortlisted.attrs["factors_used"] = {
        f: round(w, 4) for f, w in sub_weights.items()
    }

    return shortlisted
