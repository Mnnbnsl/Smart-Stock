"""
F&O Sentiment Factor.

Uses Put-Call Ratio (PCR) and OI trend from the F&O data fetcher.

PCR interpretation:
  - PCR > 1.2: Contrarian bullish (heavy put writing = market expects rise)
  - PCR 0.8–1.2: Neutral
  - PCR < 0.7: Bearish (call dominance = bearish bets)

Score: 0–100 (higher = more bullish F&O sentiment)
"""

import logging
import numpy as np
import pandas as pd

from scoring.normalizer import safe_score

logger = logging.getLogger(__name__)


def _pcr_to_score(pcr: float | None) -> float:
    """
    Map Put-Call Ratio to a 0–100 bullish sentiment score.
    Uses a heuristic piecewise mapping.
    """
    if pcr is None or np.isnan(pcr):
        return 50.0  # neutral default

    # Very high PCR (>2.0) = extreme fear = potentially very bullish contrarian
    if pcr >= 2.0:
        return 95.0
    elif pcr >= 1.5:
        return 80.0
    elif pcr >= 1.2:
        return 70.0
    elif pcr >= 0.8:
        return 50.0  # neutral
    elif pcr >= 0.5:
        return 30.0
    else:
        return 15.0  # extreme call dominance = bearish


_OI_TREND_SCORES = {
    "bullish":  75.0,
    "neutral":  50.0,
    "bearish":  25.0,
}


def compute_fno_scores(fno_data: pd.DataFrame) -> pd.DataFrame:
    """
    Compute F&O sentiment scores.

    Parameters
    ----------
    fno_data : pd.DataFrame
        Output of fno_fetcher.fetch_fno_batch(). Indexed by symbol.
        Expected columns: [pcr, oi_trend]

    Returns
    -------
    pd.DataFrame
        Indexed by symbol with columns: [pcr_score, oi_score, fno_score]
    """
    if fno_data is None or fno_data.empty:
        return pd.DataFrame()

    df = fno_data.copy()

    # PCR score
    pcr_col = df.get("pcr", pd.Series(dtype=float))
    pcr_score = pd.to_numeric(pcr_col, errors="coerce").apply(_pcr_to_score)

    # OI trend score
    oi_col = df.get("oi_trend", pd.Series(dtype=str))
    oi_score = oi_col.map(lambda x: _OI_TREND_SCORES.get(str(x).lower(), 50.0))

    composite = (pcr_score * 0.6 + oi_score * 0.4).clip(0, 100)

    out = pd.DataFrame({
        "pcr_score":  pcr_score,
        "oi_score":   oi_score,
        "fno_score":  composite,
    }, index=df.index)

    out.index.name = "ticker"
    logger.info(f"F&O Sentiment: scored {len(out)} tickers")
    return out
