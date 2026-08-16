"""
Liquidity Factor.

Measures how tradable a stock is based on:
  - 20-day average daily volume (ADV)
  - 20-day average daily turnover (ADT) in rupees

Score: 0–100 (higher = more liquid)
"""

import logging
import numpy as np
import pandas as pd

from scoring.normalizer import safe_score

logger = logging.getLogger(__name__)


def compute_liquidity_scores(
    price_data: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """
    Compute liquidity scores for all tickers.

    Returns DataFrame indexed by ticker with columns:
    [adv_20, adt_20_cr, liquidity_score]
    """
    rows: list[dict] = []

    for ticker, df in price_data.items():
        if df is None or df.empty or len(df) < 5:
            continue

        recent = df.tail(20)
        adv = recent["Volume"].mean()
        avg_price = recent["Close"].mean()
        adt_cr = (adv * avg_price) / 1e7  # convert to crores

        rows.append({
            "ticker":   ticker,
            "adv_20":   adv,
            "adt_20_cr": adt_cr,
        })

    if not rows:
        return pd.DataFrame()

    df_out = pd.DataFrame(rows).set_index("ticker")

    # Score both components and average them
    adv_score = safe_score(df_out["adv_20"],    ascending=True)
    adt_score = safe_score(df_out["adt_20_cr"], ascending=True)

    df_out["liquidity_score"] = ((adv_score * 0.5) + (adt_score * 0.5)).clip(0, 100)
    logger.info(f"Liquidity: scored {len(df_out)} tickers")
    return df_out
