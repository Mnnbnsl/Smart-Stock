"""
Sector Classification.

Maps yfinance sector/industry strings to simplified buckets used for
sector-aware scoring. Four buckets:

  financials  — Financial Services (banks, NBFCs, insurance, AMCs, brokers)
  it          — Technology (IT services, software)
  pharma      — Healthcare (pharmaceutical companies)
  other       — Everything else
"""

import logging

logger = logging.getLogger(__name__)

# The four sector buckets
SECTOR_BUCKETS = ("financials", "it", "pharma", "other")


def classify_sector(sector: str | None) -> str:
    """
    Classify a yfinance sector string into one of four scoring buckets.

    Parameters
    ----------
    sector : str | None
        The 'sector' field from yfinance .info (e.g. 'Financial Services').

    Returns
    -------
    str
        One of: 'financials', 'it', 'pharma', 'other'.
    """
    if not sector:
        return "other"

    s = sector.strip()

    if s == "Financial Services":
        return "financials"
    elif s == "Technology":
        return "it"
    elif s == "Healthcare":
        return "pharma"
    else:
        return "other"


def classify_from_fundamentals(fundamentals_row: dict | None) -> str:
    """
    Classify sector from a fundamentals dict (as stored in DB / yfinance .info).

    Parameters
    ----------
    fundamentals_row : dict
        Must contain 'sector' key (from yfinance).

    Returns
    -------
    str
        Sector bucket name.
    """
    if fundamentals_row is None:
        return "other"
    return classify_sector(fundamentals_row.get("sector"))
