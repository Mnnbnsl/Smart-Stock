"""
SQLite Persistence Layer.

Single source of truth for all cached market data, scored runs, and backtest
results. Replaces the previous Parquet/JSON file caches.

Design principles:
  - prices table is append-only (INSERT OR IGNORE); daily jobs fetch only the
    delta since the last stored date per symbol.
  - fundamentals table keeps history (one row per fetch) instead of overwriting;
    this enables per-stock ratio-reliability scoring in the future.
  - scores and backtest_runs store every run for queryable history.
  - WAL mode + proper indices keep queries sub-second at Nifty-500 scale.
"""

import os
import json
import sqlite3
import logging
import threading
from datetime import datetime

import pandas as pd

from config.settings import DB_PATH

logger = logging.getLogger(__name__)

_local = threading.local()


def get_connection() -> sqlite3.Connection:
    """
    Return a thread-local SQLite connection (created on first call per thread).

    Worker threads (e.g. ThreadPoolExecutor in fundamental_fetcher) each get
    their own connection; WAL mode allows concurrent readers alongside writers.
    """
    conn: sqlite3.Connection | None = getattr(_local, "connection", None)
    if conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _local.connection = conn
    return conn


def close_connection() -> None:
    """Close and discard this thread's DB connection (if any)."""
    conn: sqlite3.Connection | None = getattr(_local, "connection", None)
    if conn is not None:
        conn.close()
        _local.connection = None


def init_schema() -> None:
    """Create all tables if they don't exist."""
    conn = get_connection()
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    logger.info("SQLite schema initialized.")


# ─────────────────────────────────────────────────────────────────────
# SQL Schema
# ─────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
-- Universe (slowly changing, track membership over time)
CREATE TABLE IF NOT EXISTS universe (
    symbol      TEXT NOT NULL,
    added_date  TEXT NOT NULL,
    removed_date TEXT,
    source      TEXT,
    PRIMARY KEY (symbol, added_date)
);

-- Daily OHLCV, append-only, one row per (symbol, date)
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT    NOT NULL,
    date   TEXT    NOT NULL,
    open   REAL,
    high   REAL,
    low    REAL,
    close  REAL,
    volume INTEGER,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_symbol_date ON prices(symbol, date);

-- Fundamentals snapshot, one row per (symbol, fetch_date) — keep history
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol           TEXT NOT NULL,
    fetch_date       TEXT NOT NULL,
    trailing_pe      REAL,
    forward_pe       REAL,
    price_to_book    REAL,
    ev_ebitda        REAL,
    ev_revenue       REAL,
    roe              REAL,
    roa              REAL,
    debt_to_equity   REAL,
    operating_margin REAL,
    profit_margin    REAL,
    gross_margin     REAL,
    earnings_growth  REAL,
    revenue_growth   REAL,
    market_cap       REAL,
    enterprise_value REAL,
    current_ratio    REAL,
    quick_ratio      REAL,
    dividend_yield   REAL,
    beta             REAL,
    sector           TEXT,
    industry         TEXT,
    long_name        TEXT,
    raw_json         TEXT,
    PRIMARY KEY (symbol, fetch_date)
);

-- Delisted/dropped tracking
CREATE TABLE IF NOT EXISTS delisted (
    symbol      TEXT PRIMARY KEY,
    marked_date TEXT NOT NULL,
    reason      TEXT
);

-- One row per (symbol, run) with full factor breakdown
CREATE TABLE IF NOT EXISTS scores (
    run_id           TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    quant_score      REAL,
    rank             INTEGER,
    shortlisted      INTEGER,
    momentum_score   REAL,
    liquidity_score  REAL,
    quality_score    REAL,
    value_score      REAL,
    technical_score  REAL,
    events_score     REAL,
    data_completeness REAL,
    flags            TEXT,
    PRIMARY KEY (run_id, symbol)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    run_at        TEXT NOT NULL,
    total_scored  INTEGER,
    shortlist_size INTEGER,
    weights_json  TEXT
);

-- Backtest results
CREATE TABLE IF NOT EXISTS backtest_runs (
    backtest_id TEXT PRIMARY KEY,
    start_date  TEXT,
    end_date    TEXT,
    rebalance   TEXT,
    top_n       INTEGER,
    fee         REAL,
    cagr        REAL,
    sharpe      REAL,
    sortino     REAL,
    max_dd      REAL,
    alpha       REAL,
    beta        REAL,
    win_rate    REAL,
    params_json TEXT
);

CREATE TABLE IF NOT EXISTS backtest_equity (
    backtest_id       TEXT NOT NULL,
    date              TEXT NOT NULL,
    strategy_equity   REAL,
    benchmark_equity  REAL,
    PRIMARY KEY (backtest_id, date)
);
"""


# ─────────────────────────────────────────────────────────────────────
# Prices CRUD
# ─────────────────────────────────────────────────────────────────────

def get_max_price_date(symbol: str) -> str | None:
    """Return the most recent date stored for a symbol, or None."""
    conn = get_connection()
    row = conn.execute(
        "SELECT MAX(date) FROM prices WHERE symbol = ?", (symbol,)
    ).fetchone()
    return row[0] if row and row[0] else None


def insert_prices(symbol: str, df: pd.DataFrame) -> int:
    """
    Insert OHLCV rows for a symbol. Uses INSERT OR IGNORE so existing
    rows are not overwritten. Returns the number of rows inserted.
    """
    if df is None or df.empty:
        return 0
    conn = get_connection()
    records = []
    for idx, row in df.iterrows():
        date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
        records.append((
            symbol,
            date_str,
            float(row.get("Open", 0)),
            float(row.get("High", 0)),
            float(row.get("Low", 0)),
            float(row.get("Close", 0)),
            int(row.get("Volume", 0)),
        ))
    conn.executemany(
        "INSERT OR IGNORE INTO prices (symbol, date, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        records,
    )
    conn.commit()
    return len(records)


def get_prices(symbol: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """
    Read prices for a symbol from DB.
    Returns a DataFrame indexed by date with columns [Open, High, Low, Close, Volume].
    """
    conn = get_connection()
    sql = "SELECT date, open, high, low, close, volume FROM prices WHERE symbol = ?"
    params: list = [symbol]
    if start:
        sql += " AND date >= ?"
        params.append(start)
    if end:
        sql += " AND date <= ?"
        params.append(end)
    sql += " ORDER BY date"
    df = pd.read_sql_query(sql, conn, params=params)
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    df.columns = ["Open", "High", "Low", "Close", "Volume"]
    return df


def get_all_stored_symbols() -> set[str]:
    """Return the set of symbols that have any price data in the DB."""
    conn = get_connection()
    rows = conn.execute("SELECT DISTINCT symbol FROM prices").fetchall()
    return {row[0] for row in rows}


# ─────────────────────────────────────────────────────────────────────
# Fundamentals CRUD
# ─────────────────────────────────────────────────────────────────────

def get_latest_fundamentals(symbol: str) -> dict | None:
    """Return the most recent fundamentals row for a symbol, or None."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM fundamentals WHERE symbol = ? ORDER BY fetch_date DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def get_fundamentals_as_of(symbol: str, as_of_date: str) -> dict | None:
    """
    Return the most recent fundamentals row for a symbol that was fetched
    on or before as_of_date. Used for point-in-time backtesting.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM fundamentals WHERE symbol = ? AND fetch_date <= ? "
        "ORDER BY fetch_date DESC LIMIT 1",
        (symbol, as_of_date),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def insert_fundamentals(symbol: str, data: dict) -> None:
    """
    Insert a fundamentals snapshot. Always appends a new row (never overwrites)
    so we build up history for ratio-reliability scoring.
    """
    conn = get_connection()
    fetch_date = data.get("fetched_at", datetime.now().isoformat())

    def _num(key):
        v = data.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    conn.execute(
        """INSERT OR REPLACE INTO fundamentals
           (symbol, fetch_date, trailing_pe, forward_pe, price_to_book,
            ev_ebitda, ev_revenue, roe, roa, debt_to_equity, operating_margin,
            profit_margin, gross_margin, earnings_growth, revenue_growth,
            market_cap, enterprise_value, current_ratio, quick_ratio,
            dividend_yield, beta, sector, industry, long_name, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            symbol, fetch_date,
            _num("trailingPE"), _num("forwardPE"), _num("priceToBook"),
            _num("enterpriseToEbitda"), _num("enterpriseToRevenue"),
            _num("returnOnEquity"), _num("returnOnAssets"),
            _num("debtToEquity"), _num("operatingMargins"),
            _num("profitMargins"), _num("grossMargins"),
            _num("earningsGrowth"), _num("revenueGrowth"),
            _num("marketCap"), _num("enterpriseValue"),
            _num("currentRatio"), _num("quickRatio"),
            _num("dividendYield"), _num("beta"),
            data.get("sector"), data.get("industry"),
            data.get("longName"),
            json.dumps(data, default=str),
        ),
    )
    conn.commit()


def fundamentals_to_dataframe(tickers: list[str]) -> pd.DataFrame:
    """
    Load the latest fundamentals for a list of tickers into a DataFrame
    indexed by ticker. This is the drop-in replacement for the old
    fetch_fundamentals_batch() return value.

    Columns are renamed from the snake_case DB schema back to the camelCase
    yfinance field names that all scoring consumers expect.
    """
    rows: list[dict] = []
    for ticker in tickers:
        row = get_latest_fundamentals(ticker)
        if row is not None:
            rows.append(row)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)

    column_map = {
        "trailing_pe": "trailingPE",
        "forward_pe": "forwardPE",
        "price_to_book": "priceToBook",
        "ev_ebitda": "enterpriseToEbitda",
        "ev_revenue": "enterpriseToRevenue",
        "roe": "returnOnEquity",
        "roa": "returnOnAssets",
        "debt_to_equity": "debtToEquity",
        "operating_margin": "operatingMargins",
        "profit_margin": "profitMargins",
        "gross_margin": "grossMargins",
        "earnings_growth": "earningsGrowth",
        "revenue_growth": "revenueGrowth",
        "market_cap": "marketCap",
        "enterprise_value": "enterpriseValue",
        "current_ratio": "currentRatio",
        "quick_ratio": "quickRatio",
        "dividend_yield": "dividendYield",
        "long_name": "longName",
    }
    df = df.rename(columns=column_map)

    if "symbol" in df.columns:
        df = df.rename(columns={"symbol": "ticker"})
        df = df.set_index("ticker")
    elif "ticker" in df.columns:
        df = df.set_index("ticker")
    return df


def count_fundamentals_rows(symbol: str) -> int:
    """Count how many historical fundamentals rows exist for a symbol."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) FROM fundamentals WHERE symbol = ?", (symbol,)
    ).fetchone()
    return row[0] if row else 0


# ─────────────────────────────────────────────────────────────────────
# Universe CRUD
# ─────────────────────────────────────────────────────────────────────

def upsert_universe(symbol: str, added_date: str, source: str) -> None:
    """Insert or update universe membership for a symbol."""
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO universe (symbol, added_date, source) VALUES (?, ?, ?)",
        (symbol, added_date, source),
    )
    conn.commit()


def get_universe_symbols() -> set[str]:
    """Return symbols currently in the universe (not removed)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT symbol FROM universe WHERE removed_date IS NULL"
    ).fetchall()
    return {row[0] for row in rows}


# ─────────────────────────────────────────────────────────────────────
# Delisted CRUD
# ─────────────────────────────────────────────────────────────────────

def mark_delisted_db(symbol: str, reason: str = "no_price_data") -> None:
    """Record a symbol as delisted in the DB."""
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO delisted (symbol, marked_date, reason) VALUES (?, ?, ?)",
        (symbol, datetime.now().strftime("%Y-%m-%d"), reason),
    )
    conn.commit()


def get_delisted_symbols() -> set[str]:
    """Return all symbols marked as delisted."""
    conn = get_connection()
    rows = conn.execute("SELECT symbol FROM delisted").fetchall()
    return {row[0] for row in rows}


def is_delisted(symbol: str) -> bool:
    """Check if a symbol is marked as delisted."""
    conn = get_connection()
    row = conn.execute(
        "SELECT 1 FROM delisted WHERE symbol = ?", (symbol,)
    ).fetchone()
    return row is not None


# ─────────────────────────────────────────────────────────────────────
# Scores CRUD
# ─────────────────────────────────────────────────────────────────────

def insert_run(
    run_id: str,
    results_df: pd.DataFrame,
    weights: dict,
) -> None:
    """
    Persist a scored run: one row in `runs` + one row per stock in `scores`.
    """
    conn = get_connection()

    shortlisted_count = int(results_df.get("shortlisted", pd.Series(False, index=results_df.index)).sum()) if "shortlisted" in results_df.columns else 0

    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, run_at, total_scored, shortlist_size, weights_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            run_id,
            datetime.now().isoformat(),
            len(results_df),
            shortlisted_count,
            json.dumps(weights),
        ),
    )

    for ticker, row in results_df.iterrows():
        symbol = row.get("symbol", ticker.replace(".NS", ""))
        flags = row.get("flags", [])
        flags_json = json.dumps(flags) if isinstance(flags, list) else (flags if isinstance(flags, str) else "[]")

        conn.execute(
            "INSERT OR REPLACE INTO scores "
            "(run_id, symbol, quant_score, rank, shortlisted, "
            "momentum_score, liquidity_score, quality_score, value_score, "
            "technical_score, events_score, data_completeness, flags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, symbol,
                float(row.get("quant_score", 0)),
                int(row.get("rank", 999)),
                1 if row.get("shortlisted", False) else 0,
                float(row.get("momentum_score", 50)),
                float(row.get("liquidity_score", 50)),
                float(row.get("quality_score", 50)),
                float(row.get("value_score", 50)),
                float(row.get("technical_score", 50)),
                float(row.get("events_score", 50)),
                float(row.get("data_completeness", 1.0)),
                flags_json,
            ),
        )
    conn.commit()
    logger.info(f"Run {run_id}: saved {len(results_df)} scores to DB.")


def get_run_scores(run_id: str) -> pd.DataFrame:
    """Load all scores for a given run."""
    conn = get_connection()
    return pd.read_sql_query(
        "SELECT * FROM scores WHERE run_id = ?", conn, params=(run_id,)
    )


def list_runs(limit: int = 20) -> pd.DataFrame:
    """List recent runs."""
    conn = get_connection()
    return pd.read_sql_query(
        "SELECT * FROM runs ORDER BY run_at DESC LIMIT ?", conn, params=(limit,)
    )


# ─────────────────────────────────────────────────────────────────────
# Backtest CRUD
# ─────────────────────────────────────────────────────────────────────

def insert_backtest(
    backtest_id: str,
    params: dict,
    metrics: dict,
    equity_curve: pd.DataFrame,
) -> None:
    """
    Persist a backtest: summary in backtest_runs + daily equity in backtest_equity.
    equity_curve should have columns [strategy_equity, benchmark_equity] indexed by date.
    """
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO backtest_runs "
        "(backtest_id, start_date, end_date, rebalance, top_n, fee, "
        "cagr, sharpe, sortino, max_dd, alpha, beta, win_rate, params_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            backtest_id,
            params.get("start_date"),
            params.get("end_date"),
            params.get("freq"),
            params.get("top_n"),
            params.get("fee_pct"),
            metrics.get("cagr"),
            metrics.get("sharpe_ratio"),
            metrics.get("sortino_ratio"),
            metrics.get("max_drawdown"),
            metrics.get("alpha"),
            metrics.get("beta"),
            metrics.get("win_rate"),
            json.dumps(params, default=str),
        ),
    )

    records = []
    for date_idx, row in equity_curve.iterrows():
        date_str = date_idx.strftime("%Y-%m-%d") if hasattr(date_idx, "strftime") else str(date_idx)
        records.append((
            backtest_id,
            date_str,
            float(row.get("strategy_equity", 0)),
            float(row.get("benchmark_equity", 0)),
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO backtest_equity "
        "(backtest_id, date, strategy_equity, benchmark_equity) "
        "VALUES (?, ?, ?, ?)",
        records,
    )
    conn.commit()
    logger.info(f"Backtest {backtest_id}: saved {len(records)} equity rows to DB.")


def get_backtest_equity(backtest_id: str) -> pd.DataFrame:
    """Load equity curve for a backtest."""
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT date, strategy_equity, benchmark_equity "
        "FROM backtest_equity WHERE backtest_id = ? ORDER BY date",
        conn, params=(backtest_id,),
    )
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    return df


def list_backtests(limit: int = 20) -> pd.DataFrame:
    """List recent backtests."""
    conn = get_connection()
    return pd.read_sql_query(
        "SELECT * FROM backtest_runs ORDER BY start_date DESC LIMIT ?",
        conn, params=(limit,),
    )
