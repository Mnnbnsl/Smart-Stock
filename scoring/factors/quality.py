"""
Quality Factor.

Measures financial health using:
  - Return on Equity (ROE)       — higher is better
  - Return on Capital Employed (ROCE, proxied via ROA)
  - Debt-to-Equity ratio         — lower is better
  - Profit margins               — higher is better

Score: 0–100 (higher = higher quality business)
"""

import logging
import numpy as np
import pandas as pd

from scoring.normalizer import safe_score

logger = logging.getLogger(__name__)

# Sub-weights within quality factor
QUALITY_SUB_WEIGHTS = {
    "roe":    0.35,
    "roa":    0.20,
    "debt":   0.30,
    "margin": 0.15,
}


def compute_quality_scores(fundamentals: pd.DataFrame) -> pd.DataFrame:
    """
    Compute quality scores from fundamental data.

    Parameters
    ----------
    fundamentals : pd.DataFrame
        Output of fundamental_fetcher.fetch_fundamentals_batch().
        Indexed by ticker.

    Returns
    -------
    pd.DataFrame
        Indexed by ticker with columns:
        [roe_score, roa_score, debt_score, margin_score, quality_score]
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

    # Composite: weighted average of sub-scores, skipping NaN components
    composite = pd.Series(0.0, index=fundamentals.index)
    weight_sum = pd.Series(0.0, index=fundamentals.index)

    for component, weight in QUALITY_SUB_WEIGHTS.items():
        if component == "roe":
            scores = roe_score
        elif component == "roa":
            scores = roa_score
        elif component == "debt":
            scores = debt_score
        elif component == "margin":
            scores = margin_score
        else:
            continue
        valid = scores.notna()
        composite[valid] += weight * scores[valid]
        weight_sum[valid] += weight

    mask = weight_sum > 0
    composite[mask] = composite[mask] / weight_sum[mask]

    out = pd.DataFrame({
        "roe_score":     roe_score,
        "roa_score":     roa_score,
        "debt_score":    debt_score,
        "margin_score":  margin_score,
        "quality_score": composite.clip(0, 100),
    }, index=fundamentals.index)

    out.index.name = "ticker"
    logger.info(f"Quality: scored {len(out)} tickers")
    return out
