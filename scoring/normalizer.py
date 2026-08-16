"""
Score Normalizer.

Converts raw factor values into normalized 0–100 scores using:
  - Percentile rank (primary): rank within the universe
  - Z-score clamp: alternative for symmetric distributions

All output scores are in [0, 100] range. Higher = better.
"""

import numpy as np
import pandas as pd


def percentile_rank(series: pd.Series, ascending: bool = True) -> pd.Series:
    """
    Rank values as percentiles within the series.

    ascending=True:  higher raw value → higher score (e.g. momentum)
    ascending=False: lower raw value → higher score (e.g. P/E ratio)
    """
    if ascending:
        ranked = series.rank(pct=True, ascending=True, na_option="bottom")
    else:
        ranked = series.rank(pct=True, ascending=False, na_option="bottom")
    return (ranked * 100).clip(0, 100)


def zscore_normalize(series: pd.Series, ascending: bool = True) -> pd.Series:
    """
    Z-score normalization clamped to [0, 100].

    Converts z-scores to 0–100 scale using sigmoid-like clamp at ±3σ.
    """
    mean = series.mean()
    std = series.std()
    if std == 0 or pd.isna(std):
        return pd.Series(50.0, index=series.index)

    z = (series - mean) / std
    if not ascending:
        z = -z

    # Clamp z to [-3, 3] then map to [0, 100]
    z_clipped = z.clip(-3, 3)
    score = (z_clipped + 3) / 6 * 100
    return score.fillna(50.0)


def winsorize(series: pd.Series, lower: float = 0.02, upper: float = 0.98) -> pd.Series:
    """Winsorize a series at the given percentile bounds to handle outliers."""
    lo = series.quantile(lower)
    hi = series.quantile(upper)
    return series.clip(lo, hi)


def safe_score(
    series: pd.Series,
    ascending: bool = True,
    method: str = "percentile",
    winsorize_bounds: tuple[float, float] | None = (0.02, 0.98),
) -> pd.Series:
    """
    Full normalization pipeline:
    1. Winsorize outliers
    2. Normalize to 0–100

    Parameters
    ----------
    series : pd.Series
        Raw factor values.
    ascending : bool
        True if higher raw value = better score.
    method : str
        'percentile' or 'zscore'
    winsorize_bounds : tuple or None
        Winsorization bounds. Set to None to skip.
    """
    s = series.copy().astype(float)

    # Replace inf with NaN
    s = s.replace([np.inf, -np.inf], np.nan)

    # Winsorize to handle outliers
    if winsorize_bounds and s.notna().sum() > 5:
        lo, hi = winsorize_bounds
        s = winsorize(s, lower=lo, upper=hi)

    if method == "percentile":
        return percentile_rank(s, ascending=ascending)
    elif method == "zscore":
        return zscore_normalize(s, ascending=ascending)
    else:
        raise ValueError(f"Unknown normalization method: {method}")
