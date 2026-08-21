"""
Quality Factor.

Measures financial health using sector-aware sub-weights:

  Financials (banks/NBFCs/insurance):
    - Drop D/E (leverage IS their business model)
    - ROE 40%, ROA 30%, Operating Margin 30%

  IT / Other:
    - ROE 35%, ROA 20%, D/E 30%, Operating Margin 15%

  Pharma:
    - ROE 35%, ROA 20%, D/E 25%, Operating Margin 20%

Score: 0-100 (higher = higher quality business)
"""

import logging
import numpy as np
import pandas as pd

from scoring.normalizer import safe_score
from scoring.factors.sectors import classify_sector

logger = logging.getLogger(__name__)

# Sector-specific sub-weights (must each sum to 1.0)
QUALITY_SUB_WEIGHTS_BY_SECTOR = {
    "financials": {
        "roe":    0.40,
        "roa":    0.30,
        "margin": 0.30,
    },
    "it": {
        "roe":    0.35,
        "roa":    0.20,
        "debt":   0.30,
        "margin": 0.15,
    },
    "pharma": {
        "roe":    0.35,
        "roa":    0.20,
        "debt":   0.25,
        "margin": 0.20,
    },
    "other": {
        "roe":    0.35,
        "roa":    0.20,
        "debt":   0.30,
        "margin": 0.15,
    },
}

# All possible sub-weight keys
ALL_COMPONENTS = ("roe", "roa", "debt", "margin")


def compute_quality_scores(fundamentals: pd.DataFrame) -> pd.DataFrame:
    """
    Compute quality scores from fundamental data with sector-aware weights.

    Parameters
    ----------
    fundamentals : pd.DataFrame
        Output of fundamental_fetcher.fetch_fundamentals_batch().
        Indexed by ticker. Must contain 'sector' column for classification.

    Returns
    -------
    pd.DataFrame
        Indexed by ticker with columns:
        [roe_score, roa_score, debt_score, margin_score, quality_score,
         sector_bucket]
    """
    if fundamentals.empty:
        return pd.DataFrame()

    df = fundamentals.copy()

    # ROE: Return on Equity (higher = better)
    roe_raw = pd.to_numeric(df.get("returnOnEquity"), errors="coerce") * 100
    roe_score = safe_score(roe_raw, ascending=True)

    # ROA: Return on Assets (higher = better, proxy for ROCE)
    roa_raw = pd.to_numeric(df.get("returnOnAssets"), errors="coerce") * 100
    roa_score = safe_score(roa_raw, ascending=True)

    # Debt-to-Equity: lower = better (invert)
    debt_raw = pd.to_numeric(df.get("debtToEquity"), errors="coerce")
    debt_score = safe_score(debt_raw, ascending=False)

    # Operating margin: higher = better
    margin_raw = pd.to_numeric(df.get("operatingMargins"), errors="coerce") * 100
    margin_score = safe_score(margin_raw, ascending=True)

    # Classify sector per ticker
    sector_col = df.get("sector")
    sector_buckets = pd.Series("other", index=df.index)
    if sector_col is not None:
        sector_buckets = sector_col.apply(classify_sector)

    # Composite: sector-weighted average of sub-scores, skipping NaN components
    composite = pd.Series(0.0, index=df.index)
    weight_sum = pd.Series(0.0, index=df.index)

    for ticker in df.index:
        bucket = sector_buckets.get(ticker, "other")
        weights = QUALITY_SUB_WEIGHTS_BY_SECTOR.get(bucket, QUALITY_SUB_WEIGHTS_BY_SECTOR["other"])

        score_map = {
            "roe":    roe_score,
            "roa":    roa_score,
            "debt":   debt_score,
            "margin": margin_score,
        }

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
        "roe_score":       roe_score,
        "roa_score":       roa_score,
        "debt_score":      debt_score,
        "margin_score":    margin_score,
        "quality_score":   composite.clip(0, 100),
        "sector_bucket":   sector_buckets,
    }, index=df.index)

    out.index.name = "ticker"
    logger.info(f"Quality: scored {len(out)} tickers (sectors: {sector_buckets.value_counts().to_dict()})")
    return out
