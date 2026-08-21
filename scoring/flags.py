"""
Stock Flags — Transparency & Debugging.

Collects human-readable flag strings for each stock so the output JSON
is self-documenting: consumers can see exactly what adjustments or data
gaps affected the score.

No black boxes — every penalty, imputation, and sector classification
is surfaced as a flag.
"""

import numpy as np
import pandas as pd

from scoring.factors.sectors import classify_sector

# ─────────────────────────────────────────────────────────────────────
# Flag constants (used as string identifiers in output JSON)
# ─────────────────────────────────────────────────────────────────────

# Events flags
FLAG_EARNINGS_WITHIN_3D = "earnings_within_3d"
FLAG_EARNINGS_WITHIN_7D = "earnings_within_7d"

# Fundamental missing data flags
FLAG_PE_NEGATIVE_EXCLUDED = "pe_negative_excluded"
FLAG_PE_MISSING = "pe_missing"
FLAG_PB_MISSING = "pb_missing"
FLAG_EV_EBITDA_MISSING = "ev_ebitda_missing"
FLAG_ROE_MISSING = "roe_missing"
FLAG_DE_MISSING = "de_missing"
FLAG_OPERATING_MARGIN_MISSING = "operating_margin_missing"

# Imputation flags
FLAG_MISSING_FUNDAMENTALS_IMPUTED = "missing_fundamentals_imputed_neutral"

# Sector flags
FLAG_SECTOR_FINANCIALS = "sector_financials"
FLAG_SECTOR_IT = "sector_it"
FLAG_SECTOR_PHARMA = "sector_pharma"

# Completeness flags
FLAG_LOW_DATA_COMPLETENITY = "low_data_completeness"


def collect_flags(
    fundamentals_row: dict | None,
    quality_score: float,
    value_score: float,
    quality_was_imputed: bool,
    value_was_imputed: bool,
    earnings_days: int | None,
    pe_raw: float | None,
    data_completeness: float,
    min_completeness: float,
) -> list[str]:
    """
    Collect transparency flags for a single stock.

    Parameters
    ----------
    fundamentals_row : dict | None
        The raw fundamentals dict for this ticker (from yfinance .info).
    quality_score : float
        Computed quality score (50.0 if imputed).
    value_score : float
        Computed value score (50.0 if imputed).
    quality_was_imputed : bool
        True if quality_score was set to neutral 50 because no fundamental
        data was available.
    value_was_imputed : bool
        True if value_score was set to neutral 50 because no fundamental
        data was available.
    earnings_days : int | None
        Days to next earnings, or None if unknown.
    pe_raw : float | None
        Raw trailing P/E before scoring (to detect negative/high exclusion).
    data_completeness : float
        Fraction of factors with real (non-imputed) scores [0.0, 1.0].
    min_completeness : float
        Threshold below which to flag low_data_completeness.

    Returns
    -------
    list[str]
        List of flag strings.
    """
    flags: list[str] = []

    # ── Sector flag ─────────────────────────────────────────────────
    if fundamentals_row:
        bucket = classify_sector(fundamentals_row.get("sector"))
        if bucket == "financials":
            flags.append(FLAG_SECTOR_FINANCIALS)
        elif bucket == "it":
            flags.append(FLAG_SECTOR_IT)
        elif bucket == "pharma":
            flags.append(FLAG_SECTOR_PHARMA)

    # ── Earnings proximity flags ────────────────────────────────────
    if earnings_days is not None:
        if earnings_days <= 3:
            flags.append(FLAG_EARNINGS_WITHIN_3D)
        elif earnings_days <= 7:
            flags.append(FLAG_EARNINGS_WITHIN_7D)

    # ── Fundamental missing data flags ──────────────────────────────
    if fundamentals_row:
        # P/E
        pe = fundamentals_row.get("trailingPE")
        if pe is None or (isinstance(pe, str) and not pe):
            flags.append(FLAG_PE_MISSING)
        else:
            try:
                pe_val = float(pe)
                if pe_val <= 0 or pe_val > 500:
                    flags.append(FLAG_PE_NEGATIVE_EXCLUDED)
            except (ValueError, TypeError):
                flags.append(FLAG_PE_MISSING)

        # P/B
        pb = fundamentals_row.get("priceToBook")
        if pb is None or (isinstance(pb, str) and not pb):
            flags.append(FLAG_PB_MISSING)

        # EV/EBITDA
        ev = fundamentals_row.get("enterpriseToEbitda")
        if ev is None or (isinstance(ev, str) and not ev):
            flags.append(FLAG_EV_EBITDA_MISSING)

        # ROE
        roe = fundamentals_row.get("returnOnEquity")
        if roe is None or (isinstance(roe, str) and not roe):
            flags.append(FLAG_ROE_MISSING)

        # D/E
        de = fundamentals_row.get("debtToEquity")
        if de is None or (isinstance(de, str) and not de):
            flags.append(FLAG_DE_MISSING)

        # Operating margin
        margin = fundamentals_row.get("operatingMargins")
        if margin is None or (isinstance(margin, str) and not margin):
            flags.append(FLAG_OPERATING_MARGIN_MISSING)
    else:
        # No fundamentals at all
        flags.append(FLAG_PE_MISSING)
        flags.append(FLAG_PB_MISSING)
        flags.append(FLAG_EV_EBITDA_MISSING)
        flags.append(FLAG_ROE_MISSING)
        flags.append(FLAG_DE_MISSING)
        flags.append(FLAG_OPERATING_MARGIN_MISSING)

    # ── Imputation flags ────────────────────────────────────────────
    if quality_was_imputed and value_was_imputed:
        flags.append(FLAG_MISSING_FUNDAMENTALS_IMPUTED)

    # ── Completeness flag ───────────────────────────────────────────
    if data_completeness < min_completeness:
        flags.append(FLAG_LOW_DATA_COMPLETENITY)

    return flags
