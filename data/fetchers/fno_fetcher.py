"""
F&O (Futures & Options) Data Fetcher.

Fetches Put-Call Ratio and OI data from NSE public APIs.
NSE requires session cookies established via a homepage visit first.
"""

import os
import json
import logging
import time
from datetime import datetime, timedelta

import requests
import pandas as pd

from config.settings import CACHE_DIR, FNO_CACHE_TTL_HOURS, NSE_HEADERS, NSE_BASE_URL

logger = logging.getLogger(__name__)

FNO_CACHE_DIR = os.path.join(CACHE_DIR, "fno")

# NSE option chain endpoint
NSE_OPTION_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-equities"
NSE_OI_SPURTS_URL = f"{NSE_BASE_URL}/api/live-analysis-oi-spurts-contracts"
NSE_PCR_URL = f"{NSE_BASE_URL}/api/option-chain-indices?symbol=NIFTY"


def _get_nse_session() -> requests.Session:
    """Create a session with NSE cookies."""
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    try:
        session.get(NSE_BASE_URL, timeout=10)
        time.sleep(0.5)  # polite delay
    except Exception as e:
        logger.warning(f"NSE session setup failed: {e}")
    return session


def _cache_path(symbol: str) -> str:
    safe = symbol.replace(".", "_").replace("/", "_").replace("&", "_")
    return os.path.join(FNO_CACHE_DIR, f"{safe}_fno.json")


def _is_cache_valid(path: str) -> bool:
    if not os.path.exists(path):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    return datetime.now() - mtime < timedelta(hours=FNO_CACHE_TTL_HOURS)


def fetch_option_chain(symbol: str, session: requests.Session | None = None) -> dict:
    """
    Fetch option chain data for a single F&O stock from NSE.

    Returns dict with keys:
      - pcr: Put-Call Ratio (total OI)
      - total_call_oi: Total call open interest
      - total_put_oi: Total put open interest
      - max_pain: Strike with max OI
      - oi_trend: 'buildup' | 'unwinding' | 'neutral'
    """
    os.makedirs(FNO_CACHE_DIR, exist_ok=True)
    cache = _cache_path(symbol)

    if _is_cache_valid(cache):
        try:
            with open(cache, "r") as f:
                return json.load(f)
        except Exception:
            pass

    result = {
        "symbol": symbol,
        "pcr": None,
        "total_call_oi": None,
        "total_put_oi": None,
        "max_pain": None,
        "oi_trend": "neutral",
        "fetched_at": datetime.now().isoformat(),
    }

    try:
        if session is None:
            session = _get_nse_session()

        url = f"{NSE_OPTION_CHAIN_URL}?symbol={symbol}"
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        records = data.get("records", {}).get("data", [])
        if not records:
            return result

        total_call_oi = 0
        total_put_oi = 0
        pain_map: dict[float, float] = {}

        for rec in records:
            strike = rec.get("strikePrice", 0)
            call_oi = rec.get("CE", {}).get("openInterest", 0) or 0
            put_oi = rec.get("PE", {}).get("openInterest", 0) or 0
            total_call_oi += call_oi
            total_put_oi += put_oi
            pain_map[strike] = pain_map.get(strike, 0) + call_oi + put_oi

        pcr = round(total_put_oi / total_call_oi, 4) if total_call_oi > 0 else None
        max_pain = max(pain_map, key=pain_map.get) if pain_map else None

        result.update({
            "pcr": pcr,
            "total_call_oi": total_call_oi,
            "total_put_oi": total_put_oi,
            "max_pain": max_pain,
            "oi_trend": _classify_oi_trend(pcr),
        })

        with open(cache, "w") as f:
            json.dump(result, f, indent=2, default=str)

    except Exception as e:
        logger.warning(f"F&O fetch failed for {symbol}: {e}")

    return result


def _classify_oi_trend(pcr: float | None) -> str:
    """Classify OI sentiment from PCR value."""
    if pcr is None:
        return "neutral"
    if pcr > 1.2:
        return "bullish"
    elif pcr < 0.7:
        return "bearish"
    else:
        return "neutral"


def fetch_fno_batch(symbols: list[str]) -> pd.DataFrame:
    """
    Fetch F&O data for a list of NSE symbols.

    Returns DataFrame indexed by symbol with fno columns.
    """
    session = _get_nse_session()
    rows: list[dict] = []

    for symbol in symbols:
        data = fetch_option_chain(symbol, session=session)
        rows.append(data)
        time.sleep(0.3)  # polite delay between requests

    df = pd.DataFrame(rows)
    if "symbol" in df.columns:
        df = df.set_index("symbol")
    return df
