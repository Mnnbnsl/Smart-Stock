# Stock Scoring Engine

A two-stage NSE stock selection system. **Stage 1** (this repo) is a quantitative
scoring engine that ranks the Nifty 500 universe every day, plus a point-in-time
backtester that validates the strategy against the Nifty 50 benchmark.

- **Live scoring** (`main.py`) — fetches market data, scores every stock 0–100
  across 6 factors, and shortlists the top N.
- **Backtesting** (`run_backtest.py`) — replays the same scoring logic
  historically with **no lookahead bias** to measure whether the strategy
  actually beats the market.

---

## System Architecture

```
ALL NSE STOCKS (Nifty 500 constituents)
        |
        v
[1] Universe Builder          -> data/universe.py
    NSE index CSV / API / fallback -> nse_universe.csv (7-day cache)
    Delisted-symbol cache -> SQLite (delisted table)
        |
        v
[2] Data Fetchers             -> data/fetchers/
    |--> price_fetcher.py         OHLCV via yfinance          -> SQLite (incremental)
    |--> fundamental_fetcher.py   P/E, ROE, D/E, margins etc.  -> SQLite (append-only)
        |
        v
[3] Factor Computation        -> scoring/factors/
    |--> sectors.py        Sector classification (4 buckets)      -> sector_bucket
    |--> momentum.py       (1M/3M/6M/12M return vs Nifty)      -> momentum_score
    |--> liquidity.py      (20-day ADV & turnover)             -> liquidity_score
    |--> quality.py        (ROE, ROA, D/E, margins) [sector-aware] -> quality_score
    |--> value.py          (P/E, P/B, EV/EBITDA) [sector-aware]   -> value_score
    |--> technical.py      (RSI, SMA50, MACD, 52w position)    -> technical_score
    |--> events.py         (Earnings-date proximity)           -> events_score
        |
        v
[4] Scoring Engine            -> scoring/engine.py
    Weighted composite (FACTOR_WEIGHTS) -> quant_score 0-100 -> Top-N shortlist
    + Data completeness tracking
    + Transparent flags per stock (no black boxes)
        |
        v
[5] Output                    -> output/
    |--> final_ranker.py         JSON + CSV (latest.* + runs/ archive)
    |--> reports/  |  logs/
    |--> SQLite                  Every run persisted for queryable history
        |
        v
[6] Backtester (validation)   -> backtest/
    |--> data_loader.py     multi-year price history + SQLite
    |--> pit_engine.py      point-in-time scoring (no lookahead)
    |--> runner.py          rolling rebalance simulation
    |--> metrics.py         CAGR, Sharpe, Sortino, MaxDD, Alpha, Beta
    |--> run_backtest.py    CLI entry point
```

### Directory Map

```
main.py               Live scoring CLI entry point
run_backtest.py       Backtesting CLI entry point
validate_one_day.py   One-day forward validation
scheduler.py          Daily scheduler (runs the scoring pipeline at 08:30 IST)
config/settings.py    Central configuration (weights, paths, NSE headers)
data/
  db.py               SQLite persistence layer (schema + CRUD)
  universe.py         Nifty 500 universe loader + delisted-symbol DB
  nse_universe.csv    Cached universe list (refreshed weekly)
  stock_scores.db     SQLite database (prices, fundamentals, scores, backtests)
  fetchers/           price / fundamental data fetchers (DB-backed)
  cache/              Legacy Parquet/JSON caches (migrated to SQLite)
scoring/
  engine.py           Orchestrates fetch -> factor -> composite -> rank -> flags
  normalizer.py       Winsorize + percentile/z-score normalization
  flags.py            Transparency flag collection (no black boxes)
  factors/            One module per factor
    sectors.py        Sector classification (Financials/IT/Pharma/Other)
    quality.py        Sector-aware quality scoring
    value.py          Sector-aware value scoring
output/
  final_ranker.py     Saves JSON + CSV results + persists to SQLite
  reports/            latest.* files + timestamped runs/ archive
  logs/               engine.log, backtest.log
backtest/
  data_loader.py      Extended-history price loading via SQLite
  pit_engine.py       Point-in-time factor scoring
  runner.py           Rolling rebalance simulation loop
  metrics.py          Performance & risk metrics
```

---

## 1. Data Pipeline — What Is Fetched and How

All fetching is SQLite-backed with incremental updates. The database lives at
`data/stock_scores.db` and contains all historical prices, fundamentals, scored
runs, and backtest results.

### 1.1 Universe Builder (`data/universe.py`)

Defines the investable set. Resolution order:

1. **Local cache** — `data/nse_universe.csv` is used if it exists, is younger
   than **7 days**, and matches the current format version (`UNIVERSE_CACHE_VERSION = 2`).
2. **NSE archives CSV** — the primary live source
   (`https://archives.nseindia.com/content/indices/ind_nifty500list.csv`), which
   works without session cookies.
3. **NSE paginated API** — used only if the CSV fetch fails
   (`/api/equity-stockIndices?index=NIFTY 500`, paginated in chunks of 100).
4. **Bundled fallback** — a hard-coded ~110-symbol list if NSE returns fewer
   than 50 symbols.

Each symbol is deduplicated and upper-cased; the result is cached back to
`nse_universe.csv` with `source` and `source_version` marker columns, and also
persisted to the `universe` SQLite table for tracking membership over time.

**Delisted handling** — symbols that fail to return price data (delisted,
renamed, or temporarily throttled) are recorded in the `delisted` SQLite table.
Future runs drop these symbols at load time so the engine stops wasting time on
throttled retries. Legacy `delisted.json` files are automatically migrated to
the DB on first run.

### 1.2 Price Fetcher (`data/fetchers/price_fetcher.py`)

- **Source:** Yahoo Finance (`yfinance`) — OHLCV daily bars with
  `auto_adjust=True` (splits/dividends adjusted).
- **Storage:** SQLite `prices` table (append-only, one row per symbol per date).
- **Incremental fetch:** Checks `MAX(date)` per symbol in DB. If data is fresh
  (within 3 days of today), loads from DB. If stale, fetches only the delta
  from yfinance. On first run, fetches the full `LOOKBACK_DAYS` window.
- **Batching:** `fetch_price_batch` downloads in chunks of `PRICE_BATCH_CHUNK = 100`
  tickers per `yf.download` call. Failed tickers are retried individually.
- **Benchmark:** `get_benchmark_data()` fetches the Nifty 50 index (`^NSEI`) using
  the same fetcher.

### 1.3 Fundamental Fetcher (`data/fetchers/fundamental_fetcher.py`)

- **Source:** `yfinance` `Ticker.info` (a single snapshot dict per stock).
- **Storage:** SQLite `fundamentals` table (one row per fetch per ticker —
  history-preserving, never overwrites). This enables per-stock ratio-reliability
  scoring in the future.
- **TTL:** Only fetches if the latest DB row is older than 24 hours.
- **Extracted fields:** ~29 fields including sector, industry, valuation,
  profitability, growth, financial health, size, dividends, and ownership.
- **Concurrency:** `FUNDAMENTAL_WORKERS = 4` threads via `ThreadPoolExecutor`.

### 1.4 Cache & Storage Reference

| Data        | Storage | Location                     | Freshness | Concurrency |
|-------------|---------|------------------------------|-----------|-------------|
| Universe    | CSV + SQLite | `nse_universe.csv` + `universe` table | 7 days | — |
| Delisted    | SQLite  | `delisted` table             | Forever   | — |
| Prices      | SQLite  | `prices` table               | Incremental (3-day slack) | 100/chunk |
| Fundamentals| SQLite  | `fundamentals` table         | 24h TTL   | 4 threads   |
| Events      | —       | (none, fetched fresh)        | —         | 8 threads   |
| Scores      | SQLite  | `scores` + `runs` tables     | Every run | — |
| Backtests   | SQLite  | `backtest_runs` + `backtest_equity` | Every backtest | — |

---

## 2. Scoring — From Raw Data to 0–100

### 2.1 Normalization (`scoring/normalizer.py`)

Every factor reduces raw values to a common 0–100 scale where **higher = better**:

1. **Winsorize** — extreme values are clipped at the 2nd / 98th percentile so a
   single outlier does not distort ranks (skipped if fewer than ~6 non-null rows).
2. **Normalize** (default `percentile`):
   - **Percentile rank** — each value's rank within the universe mapped to
     [0, 100]. `ascending=True` means a higher raw value scores higher (e.g.
     momentum); `ascending=False` inverts it (e.g. a lower P/E scores higher).
   - **Z-score** (alternative) — maps ±3σ around the mean to [0, 100].
3. Missing/`inf` values become `NaN` and fall back to the neutral 50 default at
   the composite stage.

### 2.2 Sector Classification (`scoring/factors/sectors.py`)

Stocks are classified into 4 buckets based on yfinance's `sector` field:

| Bucket | yfinance sector | Examples |
|--------|----------------|----------|
| **financials** | Financial Services | HDFCBANK, ICICIBANK, SBIN, BAJFINANCE |
| **it** | Technology | TCS, INFY, WIPRO, HCLTECH |
| **pharma** | Healthcare | SUNPHARMA, DRREDDY, CIPLA, LUPIN |
| **other** | Everything else | RELIANCE, TITAN, NESTLEIND, LT |

This classification drives **sector-aware sub-weights** for Quality and Value
factors (see below).

### 2.3 The Six Factors

Each factor module produces a `*_score` column for every ticker.

**Momentum** (`momentum.py`) — *"is this stock outperforming the market?"*
Relative return vs the Nifty 50 over four horizons, then percentile-scored per
horizon and blended:

| Horizon | Window (days) | Weight |
|---------|--------------|--------|
| 1M      | 21           | 30%    |
| 3M      | 63           | 30%    |
| 6M      | 126          | 20%    |
| 12M     | 252          | 20%    |

`relative_return = stock_return - benchmark_return`; requires >= 22 rows of price
history.

**Liquidity** (`liquidity.py`) — *"can I get in and out cheaply?"*
Equal 50/50 blend of two 20-day averages, each percentile-scored:
- 20-day **average daily volume** (shares)
- 20-day **average daily turnover** = ADV x avg price, in INR crores

**Quality** (`quality.py`) — *"is this a financially healthy business?"*

Sector-aware sub-weights:

| Signal | Financials | IT | Pharma | Other |
|--------|-----------|-----|--------|-------|
| ROE    | 40%       | 35% | 35%    | 35%   |
| ROA    | 30%       | 20% | 20%    | 20%   |
| D/E    | — (dropped) | 30% | 25% | 30% |
| Margin | 30%       | 15% | 20%    | 15%   |

Key: D/E is **dropped** for Financials because leverage is their business model.
Operating margin weight is higher for Pharma (20%) since margins differentiate
pharma companies significantly.

**Value** (`value.py`) — *"is this stock relatively cheap?"*

Sector-aware sub-weights:

| Signal | Financials | IT / Pharma / Other |
|--------|-----------|---------------------|
| P/E    | 50%       | 45%                 |
| P/B    | 50%       | 30%                 |
| EV/EBITDA | — (dropped) | 25%           |

Key: EV/EBITDA is **dropped** for Financials because it's meaningless for banks.

**Technical** (`technical.py`) — *"is the price chart setup constructive?"*
Uses the `ta` library (RSI, MACD); falls back to neutral if unavailable.

| Signal        | Calculation                          | Score logic                        | Weight |
|---------------|--------------------------------------|------------------------------------|--------|
| RSI (14)      | `ta.momentum.RSIIndicator`           | ideal band 40-70 -> 100; linear ramps between | 25% |
| SMA trend     | % price above its 50-day SMA         | percentile (higher = better)       | 30% |
| MACD          | `macd_line - signal_line` (last)     | percentile (positive = bullish)    | 25% |
| 52-week position | current close / 52-week high       | percentile (near high = strength)  | 20% |

**Events** (`events.py`) — *"is any earnings surprise imminent?"*
Penalizes stocks approaching an earnings date (uncertainty):

| Days to earnings      | Score |
|-----------------------|-------|
| Unknown (no data)     | 70    |
| <= 0 (just passed)    | 50    |
| <= 3 (`EARNINGS_PENALTY_DAYS`) | 10 (strong penalty) |
| 4-7                   | 30    |
| 8-30                  | 50    |
| > 30                  | 70    |

---

## 3. Final Ranking (`scoring/engine.py`)

The engine runs in four steps:

1. **Fetch** — prices + benchmark, then fundamentals **only for tickers
   with valid price data** (skips delisted symbols).
2. **Compute factors** — each factor module returns a DataFrame indexed by ticker
   with its `*_score` column. The engine tracks which factors were actually
   computed vs imputed as neutral 50.
3. **Composite** — weighted sum with `FACTOR_WEIGHTS`, clipped to [0, 100].
   Data completeness is computed as the fraction of factors with real scores.
4. **Flags** — transparent flag collection per stock (see below).

### Factor Weights (config/settings.py)

| Factor    | Weight |
|-----------|--------|
| Momentum  | 26%    |
| Quality   | 21%    |
| Value     | 16%    |
| Technical | 16%    |
| Liquidity | 16%    |
| Events    | 5%     |

### Transparency Flags

Every stock in the output includes a `flags` list and `data_completeness` score:

| Flag | Meaning |
|------|---------|
| `sector_financials` | Stock classified as Financial Services |
| `sector_it` | Stock classified as Technology |
| `sector_pharma` | Stock classified as Healthcare |
| `earnings_within_3d` | Earnings announcement within 3 days |
| `earnings_within_7d` | Earnings announcement within 7 days |
| `pe_negative_excluded` | P/E was negative or >500 (excluded from scoring) |
| `pe_missing` | P/E not available |
| `pb_missing` | P/B not available |
| `ev_ebitda_missing` | EV/EBITDA not available |
| `roe_missing` | ROE not available |
| `de_missing` | D/E not available |
| `operating_margin_missing` | Operating margin not available |
| `missing_fundamentals_imputed_neutral` | All fundamentals missing (quality+value set to neutral 50) |
| `low_data_completeness` | Below `MIN_DATA_COMPLETENITY` threshold (default 0.5) |

**Data completeness** = fraction of the 6 factor scores that were actually
computed (not imputed as neutral 50). A stock with all price data but no
fundamentals would have completeness = 0.5 (3 of 6 factors real).

---

## 4. Output Layer (`output/final_ranker.py`)

Every scoring run writes **JSON + CSV** and persists to **SQLite**:

```
output/reports/
|--> latest.json                     # Machine-readable scored results (overwritten each run)
|--> latest.csv                      # Same results, tabular form
|--> latest_backtest.json            # Latest backtest summary + equity curve
|--> latest_backtest.csv             # Equity curve (date, strategy, benchmark)
|--> latest_backtest_rebalance.csv   # Trade/rebalance log
|--> runs/YYYYMMDD_HHMMSS/           # Timestamped archive per run
    |--> scores_YYYYMMDD_HHMMSS.json
    |--> scores_YYYYMMDD_HHMMSS.csv
    |--> backtest_YYYYMMDD_HHMMSS.json
    |--> backtest_YYYYMMDD_HHMMSS.csv
    |--> backtest_YYYYMMDD_HHMMSS_rebalance.csv
```

The JSON schema is:
```json
{
  "run_at": "20260817_083000",
  "total_scored": 450,
  "shortlist_size": 25,
  "stocks": [
    {
      "ticker": "HDFCBANK.NS",
      "symbol": "HDFCBANK",
      "rank": 5,
      "quant_score": 72.3,
      "shortlisted": true,
      "data_completeness": 0.83,
      "flags": ["sector_financials", "ev_ebitda_missing"],
      "factor_scores": {
        "momentum": 68.5,
        "liquidity": 85.2,
        "quality": 71.3,
        "value": 62.1,
        "technical": 55.8,
        "events": 70.0
      }
    }
  ]
}
```

### SQLite Schema

The `data/stock_scores.db` database contains:

| Table | Purpose |
|-------|---------|
| `universe` | Slowly-changing universe membership tracking |
| `prices` | Append-only daily OHLCV (one row per symbol per date) |
| `fundamentals` | History-preserving fundamentals snapshots (one row per fetch) |
| `delisted` | Symbols with no price data |
| `runs` | Every scoring run metadata |
| `scores` | Per-stock scores + flags for every run |
| `backtest_runs` | Backtest summary metrics |
| `backtest_equity` | Daily equity curves for backtests |

---

## 5. Point-in-Time Backtester

The backtester answers: *"If I had run the scoring engine every month over the
past 3 years and held the top 15 stocks, would I have beaten the Nifty 50?"*

### 5.1 Data Loading (`backtest/data_loader.py`)

- Requests **~4.5 years** of daily OHLCV per ticker
  (`BACKTEST_LOOKBACK_DAYS = 1650` — enough to compute the 12-month momentum at
  the earliest rebalance date).
- Uses the same SQLite-backed incremental storage as the live engine.
- **Coverage check:** any ticker whose history does not reach back far
  enough is re-downloaded individually.
- **Thin-data filter:** tickers with fewer than `MIN_TRADING_DAYS = 60` rows are
  dropped.

### 5.2 Point-in-Time Scoring (`backtest/pit_engine.py`)

The **no-lookahead rule**: at rebalance date `t`, the scorer slices every price
series to `df[df.index <= t]` (minimum 30 rows) and only computes factors from
that slice — nothing after `t` is visible.

**Fidelity adaptation** — only price-derived factors can be computed
historically. Quality/Value/Events are excluded because the current
fundamentals snapshot would leak future information. The live `FACTOR_WEIGHTS`
are **renormalized over the price-only subset** so the backtest keeps the same
*relative* emphasis as the live engine:

| Price-only factor | Live weight | Renormalized |
|-------------------|------------|--------------|
| Momentum          | 26%        | 44.8%        |
| Technical         | 16%        | 27.6%        |
| Liquidity         | 16%        | 27.6%        |

### 5.3 Simulation Loop (`backtest/runner.py`)

1. **Generate rebalance dates** — `monthly` (month-start), `biweekly`
   (`2W-MON`), or `weekly` (`W-MON`).
2. **Per rebalance date:**
   - Snap to the nearest actual trading day in the benchmark series.
   - Run `score_point_in_time` -> select the top-N portfolio.
   - Charge **turnover fees**: `capital x (% of portfolio changed) x fee`.
   - **Buy at that day's close; returns accrue from the next trading day.**
   - Compound capital daily until the next rebalance.
3. **Benchmark** — Nifty 50 (`^NSEI`) buy-and-hold, normalized to the same
   starting capital.
4. Results are persisted to SQLite `backtest_runs` + `backtest_equity` tables.

### 5.4 Metrics (`backtest/metrics.py`)

All computed from the daily strategy equity curve vs the benchmark:

- **Total return** — strategy & benchmark, over the full period.
- **CAGR** — compound annual growth rate, using 365.25-day years.
- **Annualized volatility** — daily std x sqrt(252).
- **Sharpe ratio** — (annualized return - 6% risk-free) / annualized vol.
- **Sortino ratio** — same numerator / downside deviation only.
- **Max drawdown** — deepest peak-to-trough decline (%), plus its duration in days.
- **Alpha & Beta** — CAPM regression of strategy returns on benchmark returns.
- **Win rate** — % of rebalance periods with positive return.
- **Monthly matrix** — year x month return table with a `Year_Total` column.

---

## 6. Usage

### Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Test with 20 well-known stocks (fast dry run)
python main.py --dry-run

# 3. Full Nifty 500 run
python main.py

# 4. Start the daily scheduler (runs at 08:30 IST, force-refreshes caches)
python scheduler.py
```

### `main.py` — Live Scoring

| Option        | Default | Description                              |
|---------------|---------|------------------------------------------|
| `--dry-run`   | off     | Score the 20 `DRY_RUN_SYMBOLS` (fast test) |
| `--refresh`   | off     | Bypass all caches and re-fetch            |
| `--limit N`   | all     | Cap the universe to the first N symbols   |
| `--top-n N`   | 25      | How many stocks to shortlist              |

### `run_backtest.py` — Backtesting

```bash
# Quick 6-month smoke test on 20 stocks
python run_backtest.py --start 2024-01-01 --end 2024-06-01 --dry-run

# 3-year full-universe backtest
python run_backtest.py --start 2023-01-01 --end 2026-01-01 --top-n 15

# Weekly rebalance with higher fees
python run_backtest.py --start 2024-01-01 --end 2025-01-01 --rebalance weekly --fee 0.002
```

| Option              | Default        | Description                              |
|---------------------|----------------|------------------------------------------|
| `--start`           | `2023-01-01`   | Backtest start date                      |
| `--end`             | `2026-01-01`   | Backtest end date                        |
| `--rebalance`       | `monthly`      | `monthly` / `biweekly` / `weekly`        |
| `--top-n`           | 15             | Portfolio size                           |
| `--fee`             | `0.001`        | Slippage/fee per trade (0.1%)            |
| `--capital`         | `100000`       | Initial capital in INR                   |
| `--dry-run`         | off            | 20 well-known stocks (fast test mode)    |
| `--limit N`         | all            | Limit universe to N symbols              |
| `--force-refresh`   | off            | Re-download all historical price data    |

### `validate_one_day.py` — Forward Validation

```bash
# Auto-detect last 2 trading days
python validate_one_day.py

# Specific dates
python validate_one_day.py --scoring-date 2026-08-14 --check-date 2026-08-17

# Full universe, top 15
python validate_one_day.py --full --top-n 15
```

### `scheduler.py`

Runs `run_pipeline(dry_run=False, force_refresh=True)` once per day at the
configured time (`DAILY_RUN_TIME = "08:30"` IST). Leave it running in the
background; press `Ctrl+C` to stop.

---

## 7. Configuration Reference (`config/settings.py`)

| Setting | Value | Purpose |
|---|---|---|
| `BASE_DIR` | project root | Base directory for all paths |
| `DB_PATH` | `data/stock_scores.db` | SQLite database path |
| `CACHE_DIR` | `data/cache` | Legacy cache root (migrating to SQLite) |
| `OUTPUT_DIR` / `RUNS_DIR` / `LOGS_DIR` | `output/reports[+runs]` / `output/logs` | Result & log locations |
| `MIN_MARKET_CAP_CR` | 500 | Universe filter — defined, not yet enforced |
| `MIN_AVG_DAILY_VOLUME` | 100,000 | Universe filter — defined, not yet enforced |
| `MIN_PRICE` | 10.0 | Penny stock filter |
| `LOOKBACK_DAYS` | 365 | Price history window for live scoring |
| `FACTOR_WEIGHTS` | momentum .26, quality .21, value .16, technical .16, liquidity .16, events .05 | Composite weights |
| `MOMENTUM_PERIOD_WEIGHTS` | 1M .30 / 3M .30 / 6M .20 / 12M .20 | Momentum horizon weights |
| `RSI_IDEAL_LOW/HIGH` | 40 / 70 | RSI ideal band |
| `EARNINGS_PENALTY_DAYS` | 3 | Imminent-earnings penalty window |
| `TOP_N_SHORTLIST` | 25 | Shortlist size for Stage 2 |
| `MIN_DATA_COMPLETENITY` | 0.5 | Below this, flag as low_confidence |
| `BACKTEST_LOOKBACK_DAYS` | 1650 (~4.5y) | Historical data buffer for backtests |
| `FUNDAMENTAL_WORKERS` | 4 | Parallel threads for Yahoo .info fetches |
| `PRICE_BATCH_CHUNK` | 100 | Tickers per yf.download call |
| `DAILY_RUN_TIME` | `"08:30"` | Scheduler run time (IST) |
| `NSE_HEADERS` / `NSE_BASE_URL` | — | Browser headers + base URL for NSE APIs |

---

## 8. What This Engine Does Differently

### Edge over purely-fundamental screeners

- **Momentum + Technical factors** — validated evidence that the strategy
  beats a benchmark, not just "trust our fundamentals."
- **Point-in-time backtester** — full Sharpe/Sortino/alpha/beta metrics with
  no lookahead bias. Every rebalance point scores only with data available
  at that date.
- **Sector-aware scoring** — doesn't penalize banks for having high D/E or
  miss the fact that EV/EBITDA is meaningless for financials.
- **Transparent flags** — no black boxes. Every stock shows exactly what data
  was available, what was imputed, and what adjustments were made.
- **Data completeness tracking** — a stock with 4 of 6 factors missing gets
  flagged as low_confidence rather than silently ranking on thin data.

### Philosophy

This engine combines fundamental analysis (quality, value) with technical
analysis (momentum, technicals) and validates the combined approach through
rigorous backtesting. The backtester uses price-only factors to avoid
lookahead bias from current snapshots of fundamentals — a deliberate trade-off
that understates live alpha but produces trustworthy performance numbers.

---

## Stage 2 (Coming Soon)

The Agent Research Pipeline will run deep LLM analysis on the shortlisted stocks
and produce structured BUY/PASS decisions with bull/bear cases.
