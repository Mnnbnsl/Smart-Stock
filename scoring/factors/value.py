"""
Value Factor.

Measures relative cheapness of a stock using:
  - Trailing P/E ratio      — lower is better
  - Price-to-Book (P/B)     — lower is better
  - EV/EBITDA               — lower is better

Scoring is done relative to the full universe (percentile rank),
so a "cheap" stock gets a higher score than an expensive one.

Score: 0–100 (higher = relatively cheaper / better value)
"""

import logging
import numpy as np
import pandas as pd

from scoring.normalizer import safe_score

logger = logging.getLogger(__name__)

VALUE_SUB_WEIGHTS = {
    "pe":       0.45,
    "pb":       0.30,
    "ev_ebitda": 0.25,
}


def compute_value_scores(fundamentals: pd.DataFrame) -> pd.DataFrame:
    """
    Compute value scores from fundamental data.

    Parameters
    ----------
    fundamentals : pd.DataFrame
        Output of fundamental_fetcher.fetch_fundamentals_batch().
        Indexed by ticker.

    Returns
    -------
    pd.DataFrame
        Indexed by ticker with columns:
        [pe_score, pb_score, ev_ebitda_score, value_score]
    """
    if fundamentals.empty:
        return pd.DataFrame()

    df = fundamentals.copy()

    # Trailing P/E (lower = better → ascending=False)
    pe_raw = pd.to_numeric(df.get("trailingPE"), errors="coerce")
    # Cap extremely high P/E as meaningless (e.g. negative earnings)
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

    composite = (
        VALUE_SUB_WEIGHTS["pe"]        * pe_score
        + VALUE_SUB_WEIGHTS["pb"]        * pb_score
        + VALUE_SUB_WEIGHTS["ev_ebitda"] * ev_score
    )

    out = pd.DataFrame({
        "pe_score":       pe_score,
        "pb_score":       pb_score,
        "ev_ebitda_score": ev_score,
        "value_score":    composite.clip(0, 100),
    }, index=fundamentals.index)

    out.index.name = "ticker"
    logger.info(f"Value: scored {len(out)} tickers")
    return out
