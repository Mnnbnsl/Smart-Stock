"""
Technical Factor.

Computes technical analysis signals using the `ta` library:
  - RSI (14-day):     Ideal range 40–70 → higher score
  - SMA trend:        Price above 50-day SMA is bullish
  - MACD signal:      MACD line above signal line is bullish
  - 52-week position: Price near 52-week high signals strength

Score: 0–100 (higher = stronger technical setup)
"""

import logging
import numpy as np
import pandas as pd

try:
    import ta
    TA_AVAILABLE = True
except ImportError:
    TA_AVAILABLE = False
    logging.warning("'ta' library not installed. Technical scores will be neutral.")

from scoring.normalizer import safe_score, percentile_rank
from config.settings import RSI_IDEAL_LOW, RSI_IDEAL_HIGH

logger = logging.getLogger(__name__)

TECHNICAL_SUB_WEIGHTS = {
    "rsi":        0.25,
    "sma_trend":  0.30,
    "macd":       0.25,
    "week52":     0.20,
}


def _rsi_score(rsi_value: float) -> float:
    """
    Score RSI based on ideal range [40, 70].
    - 40–70: Full score (100)
    - <30 or >80: Penalized (overbought/oversold)
    """
    if np.isnan(rsi_value):
        return 50.0
    if RSI_IDEAL_LOW <= rsi_value <= RSI_IDEAL_HIGH:
        return 100.0
    elif rsi_value < 30:
        return 20.0  # oversold / breakdown
    elif rsi_value > 80:
        return 30.0  # overbought
    elif rsi_value < RSI_IDEAL_LOW:
        # Linearly interpolate from 20 → 100 as RSI goes 30 → 40
        return 20.0 + (rsi_value - 30) / (RSI_IDEAL_LOW - 30) * 80.0
    else:
        # Linearly interpolate from 100 → 30 as RSI goes 70 → 80
        return 100.0 - (rsi_value - RSI_IDEAL_HIGH) / (80 - RSI_IDEAL_HIGH) * 70.0


def compute_technical_scores(
    price_data: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """
    Compute technical scores for all tickers.

    Returns DataFrame indexed by ticker with columns:
    [rsi, sma50_pct, macd_signal, week52_pct, technical_score]
    """
    rows: list[dict] = []

    for ticker, df in price_data.items():
        if df is None or df.empty or len(df) < 30:
            continue

        close = df["Close"]
        row: dict = {"ticker": ticker}

        # ── RSI ─────────────────────────────────────────
        rsi_val = np.nan
        if TA_AVAILABLE and len(close) >= 15:
            try:
                rsi_series = ta.momentum.RSIIndicator(close, window=14).rsi()
                rsi_val = rsi_series.iloc[-1]
            except Exception:
                pass
        row["rsi"] = rsi_val
        row["rsi_score"] = _rsi_score(rsi_val)

        # ── SMA Trend ────────────────────────────────────
        sma50_pct = np.nan
        if len(close) >= 50:
            sma50 = close.rolling(50).mean().iloc[-1]
            if sma50 and sma50 > 0:
                sma50_pct = (close.iloc[-1] / sma50 - 1) * 100
        row["sma50_pct"] = sma50_pct

        # ── MACD ─────────────────────────────────────────
        macd_diff = np.nan
        if TA_AVAILABLE and len(close) >= 35:
            try:
                macd_ind = ta.trend.MACD(close)
                macd_line   = macd_ind.macd().iloc[-1]
                signal_line = macd_ind.macd_signal().iloc[-1]
                macd_diff = macd_line - signal_line  # positive = bullish
            except Exception:
                pass
        row["macd_diff"] = macd_diff

        # ── 52-week position ─────────────────────────────
        week52_pct = np.nan
        if len(close) >= 252:
            high_52 = close.tail(252).max()
            if high_52 > 0:
                week52_pct = (close.iloc[-1] / high_52) * 100  # 100 = at 52w high
        row["week52_pct"] = week52_pct

        rows.append(row)

    if not rows:
        return pd.DataFrame()

    df_out = pd.DataFrame(rows).set_index("ticker")

    # Normalize each sub-component
    rsi_score    = pd.to_numeric(df_out["rsi_score"], errors="coerce").fillna(50.0)
    sma_score    = safe_score(df_out["sma50_pct"],  ascending=True)
    macd_score   = safe_score(df_out["macd_diff"],  ascending=True)
    week52_score = safe_score(df_out["week52_pct"], ascending=True)

    # Composite: weighted average of sub-scores, skipping NaN components
    # so that missing data (e.g. no ta library for MACD) does not poison
    # the entire score.
    composite = pd.Series(0.0, index=df_out.index)
    weight_sum = pd.Series(0.0, index=df_out.index)

    for component, weight in TECHNICAL_SUB_WEIGHTS.items():
        if component == "rsi":
            scores = rsi_score
        elif component == "sma_trend":
            scores = sma_score
        elif component == "macd":
            scores = macd_score
        elif component == "week52":
            scores = week52_score
        else:
            continue
        valid = scores.notna()
        composite[valid] += weight * scores[valid]
        weight_sum[valid] += weight

    mask = weight_sum > 0
    composite[mask] = composite[mask] / weight_sum[mask]

    df_out["technical_score"] = composite.clip(0, 100)
    logger.info(f"Technical: scored {len(df_out)} tickers")
    return df_out
