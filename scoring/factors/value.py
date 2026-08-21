"""
Value Factor.

Measures relative cheapness of a stock using sector-aware sub-weights:

  Financials:
    - P/E 50%, P/B 50% (no EV/EBITDA — meaningless for banks)

  IT / Pharma / Other:
    - P/E 45%, P/B 30%, EV/EBITDA 25%

Scoring is done relative to the full universe (percentile rank),
so a "cheap" stock gets a higher score than an expensive one.

Score: 0-100 (higher = relatively cheaper / better value)
"""

import logging
import numpy as np
import pandas as pd

from scoring.normalizer import safe_score
from scoring.factors.sectors import classify_sector

logger = logging.getLogger(__name__)

# Sector-specific sub-weights
VALUE_SUB_WEIGHTS_BY_SECTOR = {
    "financials": {
        "pe":        0.50,
        "pb":        0.50,
    },
    "it": {
        "pe":        0.45,
        "pb":        0.30,
        "ev_ebitda": 0.25,
    },
    "pharma": {
        "pe":        0.45,
        "pb":        0.30,
        "ev_ebitda": 0.25,
    },
    "other": {
        "pe":        0.45,
        "pb":        0.30,
        "ev_ebitda": 0.25,
    },
}


def compute_value_scores(fundamentals: pd.DataFrame) -> pd.DataFrame:
    """
    Compute value scores from fundamental data with sector-aware weights.

    Parameters
    ----------
    fundamentals : pd.DataFrame
        Output of fundamental_fetcher.fetch_fundamentals_batch().
        Indexed by ticker. Must contain 'sector' column for classification.

    Returns
    -------
    pd.DataFrame
        Indexed by ticker with columns:
        [pe_score, pb_score, ev_ebitda_score, value_score, sector_bucket]
    """
    if fundamentals.empty:
        return pd.DataFrame()

    df = fundamentals.copy()

    # Trailing P/E (lower = better, ascending=False)
    pe_raw = pd.to_numeric(df.get("trailingPE"), errors="coerce")
    pe_raw = pe_raw.where((pe_raw > 0) & (pe_raw < 500), other=np.nan)
    pe_score = safe_score(pe_raw, ascending=False)

    # Price-to-Book (lower = better)
    pb_raw = pd.to_numeric(df.get("priceToBook"), errors="coerce")
    pb_raw = pb_raw.where(pb_raw > 0, other=np.nan)
    pb_score = safe_score(pb_raw, ascending=False)

    # EV/EBITDA (lower = better)
    ev_raw = pd.to_numeric(df.get("enterpriseToEbitda"), errors="coerce")
    ev_raw = ev_raw.where((ev_raw > 0) & (ev_raw < 200), other=np.nan)
    ev_score = safe_score(ev_raw, ascending=False)

    # Classify sector per ticker
    sector_col = df.get("sector")
    sector_buckets = pd.Series("other", index=df.index)
    if sector_col is not None:
        sector_buckets = sector_col.apply(classify_sector)

    # Composite: sector-weighted average
    composite = pd.Series(0.0, index=df.index)
    weight_sum = pd.Series(0.0, index=df.index)

    score_map = {
        "pe":        pe_score,
        "pb":        pb_score,
        "ev_ebitda": ev_score,
    }

    for ticker in df.index:
        bucket = sector_buckets.get(ticker, "other")
        weights = VALUE_SUB_WEIGHTS_BY_SECTOR.get(bucket, VALUE_SUB_WEIGHTS_BY_SECTOR["other"])

        for component, weight in weights.items():
            scores = score_map.get(component)
            if scores is None:
                continue
            val = scores.get(ticker, np.nan) if hasattr(scores, "get") else np.nan
            if pd.notna(val):
                composite[ticker] += weight * val
                weight_sum[ticker] += weight

    mask = weight_sum > 0
    composite[mask] = composite[mask] / weight_sum[mask]

    out = pd.DataFrame({
        "pe_score":         pe_score,
        "pb_score":         pb_score,
        "ev_ebitda_score":  ev_score,
        "value_score":      composite.clip(0, 100),
        "sector_bucket":    sector_buckets,
    }, index=df.index)

    out.index.name = "ticker"
    logger.info(f"Value: scored {len(out)} tickers")
    return out
