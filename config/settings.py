"""
Central configuration for the Stock Scoring Engine.
All weights, thresholds, and API settings live here.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data", "cache")
OUTPUT_DIR = os.path.join(BASE_DIR, "output", "reports")
RUNS_DIR = os.path.join(OUTPUT_DIR, "runs")
LOGS_DIR = os.path.join(BASE_DIR, "output", "logs")
UNIVERSE_CSV = os.path.join(BASE_DIR, "data", "nse_universe.csv")

# ─────────────────────────────────────────────
# UNIVERSE FILTERS
# ─────────────────────────────────────────────
MIN_MARKET_CAP_CR = 500          # Minimum market cap in crores
MIN_AVG_DAILY_VOLUME = 100_000   # Minimum average daily shares traded
MIN_PRICE = 10.0                 # Penny stock filter
LOOKBACK_DAYS = 365              # Historical data window

# ─────────────────────────────────────────────
# SCORING ENGINE WEIGHTS  (must sum to 1.0)
# ─────────────────────────────────────────────
FACTOR_WEIGHTS = {
    "momentum":   0.25,
    "liquidity":  0.15,
    "quality":    0.20,
    "value":      0.15,
    "technical":  0.15,
    "fno":        0.05,
    "events":     0.05,
}

# ─────────────────────────────────────────────
# MOMENTUM FACTOR SUB-WEIGHTS
# ─────────────────────────────────────────────
MOMENTUM_PERIOD_WEIGHTS = {
    "1m":  0.30,
    "3m":  0.30,
    "6m":  0.20,
    "12m": 0.20,
}

# ─────────────────────────────────────────────
# TECHNICAL FACTOR
# ─────────────────────────────────────────────
RSI_IDEAL_LOW  = 40
RSI_IDEAL_HIGH = 70

# ─────────────────────────────────────────────
# EVENTS PENALTY
# ─────────────────────────────────────────────
EARNINGS_PENALTY_DAYS = 3    # Penalize if earnings within N days

# ─────────────────────────────────────────────
# SHORTLIST
# ─────────────────────────────────────────────
TOP_N_SHORTLIST = 25         # How many stocks pass to Stage 2 (agent)

# ─────────────────────────────────────────────
# BACKTEST
# ─────────────────────────────────────────────
BACKTEST_LOOKBACK_DAYS = 1650           # ~4.5 years buffer (covers 12m momentum at start date)
BACKTEST_PRICE_CACHE_TTL_HOURS = 168    # 1 week — historical price data is immutable

# ─────────────────────────────────────────────
# DRY RUN SYMBOLS (quick test universe)
# ─────────────────────────────────────────────
DRY_RUN_SYMBOLS = [
    "TCS", "INFY", "HDFCBANK", "RELIANCE", "ICICIBANK",
    "BAJFINANCE", "TITAN", "MARUTI", "WIPRO", "AXISBANK",
    "SUNPHARMA", "ADANIENT", "LT", "NTPC", "SBIN",
    "HCLTECH", "KOTAKBANK", "ASIANPAINT", "NESTLEIND", "DRREDDY",
]

# ─────────────────────────────────────────────
# CACHE TTL (hours)
# ─────────────────────────────────────────────
PRICE_CACHE_TTL_HOURS = 6
FUNDAMENTAL_CACHE_TTL_HOURS = 24
FNO_CACHE_TTL_HOURS = 2

# ─────────────────────────────────────────────
# THROUGHPUT (large-universe runs)
# ─────────────────────────────────────────────
FUNDAMENTAL_WORKERS = 4       # parallel threads for Yahoo .info fetches
PRICE_BATCH_CHUNK = 100       # tickers per yf.download call

# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────
DAILY_RUN_TIME = "08:30"     # Run at 8:30 AM IST (before market open)

# ─────────────────────────────────────────────
# NSE HEADERS (required for NSE API calls)
# ─────────────────────────────────────────────
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

NSE_BASE_URL = "https://www.nseindia.com"
