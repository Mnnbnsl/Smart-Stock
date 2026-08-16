# Stock Scoring Engine

A two-stage NSE stock selection system. **Stage 1** (this repo) is a quantitative
scoring engine that ranks the Nifty 500 universe every day, plus a point-in-time
backtester that validates the strategy against the Nifty 50 benchmark.

- **Live scoring** (`main.py`) — fetches market data, scores every stock 0–100
  across 7 factors, and shortlists the top N.
- **Backtesting** (`run_backtest.py`) — replays the same scoring logic
  historically with **no lookahead bias** to measure whether the strategy
  actually beats the market.

---

## System Architecture

```
ALL NSE STOCKS (Nifty 500 constituents)
        │
        ▼
[1] Universe Builder          → data/universe.py
    NSE index CSV / API / fallback → nse_universe.csv (7-day cache)
    Delisted-symbol cache → data/cache/delisted.json
        │
        ▼
[2] Data Fetchers             → data/fetchers/
    ├── price_fetcher.py         OHLCV via yfinance          → Parquet cache (6h TTL)
    ├── fundamental_fetcher.py   P/E, ROE, D/E, margins etc.  → JSON cache  (24h TTL)
    └── fno_fetcher.py           PCR, OI, max pain via NSE    → JSON cache  (2h TTL)
        │
        ▼
[3] Factor Computation        → scoring/factors/
    ├── momentum.py       (1M/3M/6M/12M return vs Nifty)      → momentum_score
    ├── liquidity.py      (20-day ADV & turnover)             → liquidity_score
    ├── quality.py        (ROE, ROA, D/E, margins)            → quality_score
    ├── value.py          (P/E, P/B, EV/EBITDA, cheaper=better)→ value_score
    ├── technical.py      (RSI, SMA50, MACD, 52w position)    → technical_score
    ├── fno_sentiment.py  (Put-Call Ratio, OI trend)          → fno_score
    └── events.py         (Earnings-date proximity)           → events_score
        │
        ▼
[4] Scoring Engine            → scoring/engine.py
    Weighted composite (FACTOR_WEIGHTS) → quant_score 0–100 → Top-25 shortlist
        │
        ▼
[5] Output                    → output/
    ├── final_ranker.py         JSON + CSV (latest.* + runs/ archive)
    └── reports/  ·  logs/
        │
        ▼
[6] Backtester (validation)   → backtest/
    ├── data_loader.py     multi-year price history + cache (168h TTL)
    ├── pit_engine.py      point-in-time scoring (no lookahead)
    ├── runner.py          rolling rebalance simulation
    ├── metrics.py         CAGR, Sharpe, Sortino, MaxDD, Alpha, Beta
    └── run_backtest.py    CLI entry point
```

### Directory Map

```
main.py               Live scoring CLI entry point
run_backtest.py       Backtesting CLI entry point
scheduler.py          Daily scheduler (runs the scoring pipeline at 08:30 IST)
config/settings.py    Central configuration (weights, TTLs, paths, NSE headers)
data/
  universe.py         Nifty 500 universe loader + delisted-symbol cache
  nse_universe.csv    Cached universe list (refreshed weekly)
  fetchers/           price / fundamental / F&O data fetchers
  cache/              Parquet + JSON caches (git-ignored, regenerated on demand)
scoring/
  engine.py           Orchestrates fetch → factor → composite → rank
  normalizer.py       Winsorize + percentile/z-score normalization
  factors/            One module per factor
output/
  final_ranker.py     Saves JSON + CSV results
  reports/            latest.* files + timestamped runs/ archive
  logs/               engine.log, backtest.log
backtest/
  data_loader.py      Extended-history price loading with coverage check
  pit_engine.py       Point-in-time factor scoring
  runner.py           Rolling rebalance simulation loop
  metrics.py          Performance & risk metrics
```

---

## 1. Data Pipeline — What Is Fetched and How

All fetching is cache-first: only stale or missing data hits the network. Every
fetcher returns the cached copy when its TTL has not expired.

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
`nse_universe.csv` with `source` and `source_version` marker columns.

**Delisted handling** — symbols that fail to return price data (delisted,
renamed, or temporarily throttled) are recorded in `data/cache/delisted.json`
(`mark_delisted`). Future runs drop these symbols at load time (`_drop_delisted`)
so the engine stops wasting time on throttled retries.

> **Note:** `config/settings.py` defines tradability filters (`MIN_MARKET_CAP_CR`,
> `MIN_AVG_DAILY_VOLUME`, `MIN_PRICE`), but these are **not yet applied** by
> `load_universe` — the universe is currently the full Nifty 500 constituent list.

### 1.2 Price Fetcher (`data/fetchers/price_fetcher.py`)

- **Source:** Yahoo Finance (`yfinance`) — OHLCV daily bars with
  `auto_adjust=True` (splits/dividends adjusted).
- **Window:** `LOOKBACK_DAYS = 365` trading days.
- **Batching:** `fetch_price_batch` downloads in chunks of `PRICE_BATCH_CHUNK = 100`
  tickers per `yf.download` call (a 500-stock universe = 5 chunks). Any ticker
  that fails in a batch is retried individually so transient throttling is not
  mistaken for a delisted symbol.
- **Cache:** one Parquet file per ticker under `data/cache/price/`
  (`PRICE_CACHE_TTL_HOURS = 6`).
- **Benchmark:** `get_benchmark_data()` fetches the Nifty 50 index (`^NSEI`) using
  the same fetcher.

### 1.3 Fundamental Fetcher (`data/fetchers/fundamental_fetcher.py`)

- **Source:** `yfinance` `Ticker.info` (a single snapshot dict per stock).
- **Extracted fields:** ~29 `WANTED_FIELDS`, grouped as:
  - *Valuation* — trailing/forward P/E, P/B, EV/EBITDA, EV/Revenue
  - *Profitability* — ROE, ROA, profit/gross/operating margins
  - *Growth* — earnings/revenue/quarterly growth
  - *Financial health* — D/E, current & quick ratios, cash/share
  - *Size* — market cap, enterprise value, shares outstanding
  - *Dividends / ownership / other* — yield, payout, insider & institutional
    holdings, beta, EPS, book value, sector, industry, name
- **Concurrency:** `FUNDAMENTAL_WORKERS = 4` threads via `ThreadPoolExecutor`.
- **Cache:** one JSON file per ticker under `data/cache/fundamentals/`
  (`FUNDAMENTAL_CACHE_TTL_HOURS = 24`).

### 1.4 F&O Sentiment Fetcher (`data/fetchers/fno_fetcher.py`)

- **Source:** NSE public option-chain API
  (`/api/option-chain-equities?symbol=SYMBOL`), which requires session cookies
  established by visiting the NSE homepage first.
- **Computed per stock:**
  - `pcr` — Put-Call Ratio = total put OI / total call OI across the whole chain
  - `total_call_oi`, `total_put_oi` — summed open interest
  - `max_pain` — strike price with the highest combined OI
  - `oi_trend` — `bullish` (PCR > 1.2) / `bearish` (PCR < 0.7) / `neutral`
- **Concurrency:** 4 threads, each with its **own session** (`requests.Session`
  is not thread-safe). NSE rate-limits aggressive scraping, so the worker count
  is deliberately modest.
- **Cache:** one JSON file per symbol under `data/cache/fno/`
  (`FNO_CACHE_TTL_HOURS = 2`).

### 1.5 Events Fetcher (`scoring/factors/events.py`)

- **Source:** `yfinance` `Ticker.calendar` → the next earnings date.
- **Concurrency:** 8 threads.
- **Cache:** none — earnings calendars are fetched fresh every run.

### 1.6 Cache & Throughput Reference

| Data        | Format  | Location                     | TTL  | Concurrency |
|-------------|---------|------------------------------|------|-------------|
| Universe    | CSV     | `data/nse_universe.csv`      | 7 d  | —           |
| Delisted    | JSON    | `data/cache/delisted.json`   | ∞    | —           |
| Prices      | Parquet | `data/cache/price/`          | 6 h  | 100/chunk   |
| Fundamentals| JSON    | `data/cache/fundamentals/`   | 24 h | 4 threads   |
| F&O         | JSON    | `data/cache/fno/`            | 2 h  | 4 threads   |
| Events      | —       | (none)                       | —    | 8 threads   |

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

### 2.2 The Seven Factors

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

`relative_return = stock_return − benchmark_return`; requires ≥ 22 rows of price
history.

**Liquidity** (`liquidity.py`) — *"can I get in and out cheaply?"*
Equal 50/50 blend of two 20-day averages, each percentile-scored:
- 20-day **average daily volume** (shares)
- 20-day **average daily turnover** = ADV × avg price, in ₹ crores

**Quality** (`quality.py`) — *"is this a financially healthy business?"*

| Signal  | Input        | Direction | Weight |
|---------|--------------|-----------|--------|
| ROE     | `returnOnEquity` | higher is better | 35% |
| ROA     | `returnOnAssets` (proxy for ROCE) | higher is better | 20% |
| D/E     | `debtToEquity` | lower is better (inverted) | 30% |
| Margin  | `operatingMargins` | higher is better | 15% |

**Value** (`value.py`) — *"is this stock relatively cheap?"*
All three ratios are inverted (cheaper = higher score) and sanitized before
scoring: P/E kept in (0, 500), P/B > 0, EV/EBITDA in (0, 200).

| Signal     | Input              | Direction          | Weight |
|------------|--------------------|--------------------|--------|
| P/E        | `trailingPE`       | lower is better    | 45%    |
| P/B        | `priceToBook`      | lower is better    | 30%    |
| EV/EBITDA  | `enterpriseToEbitda` | lower is better  | 25%    |

**Technical** (`technical.py`) — *"is the price chart setup constructive?"*
Uses the `ta` library (RSI, MACD); falls back to neutral if unavailable.

| Signal        | Calculation                          | Score logic                        | Weight |
|---------------|--------------------------------------|------------------------------------|--------|
| RSI (14)      | `ta.momentum.RSIIndicator`           | ideal band 40–70 → 100; <30 → 20; >80 → 30; linear ramps between | 25% |
| SMA trend     | % price above its 50-day SMA         | percentile (higher = better)       | 30% |
| MACD          | `macd_line − signal_line` (last)     | percentile (positive = bullish)    | 25% |
| 52-week position | current close / 52-week high       | percentile (near high = strength)  | 20% |

**F&O sentiment** (`fno_sentiment.py`) — *"where does derivatives positioning point?"*
60% PCR + 40% OI-trend.

- **PCR → score** (heuristic): ≥ 2.0 → 95; ≥ 1.5 → 80; ≥ 1.2 → 70; ≥ 0.8 → 50;
  ≥ 0.5 → 30; else → 15. High PCR = heavy put writing, treated as contrarian
  bullish; low PCR = call dominance = bearish.
- **OI trend → score:** bullish 75 / neutral 50 / bearish 25.

**Events** (`events.py`) — *"is any earnings surprise imminent?"*
Penalizes stocks approaching an earnings date (uncertainty):

| Days to earnings      | Score |
|-----------------------|-------|
| Unknown (no data)     | 70    |
| ≤ 0 (just passed)     | 50    |
| ≤ 3 (`EARNINGS_PENALTY_DAYS`) | 10 (strong penalty) |
| 4–7                   | 30    |
| 8–30                  | 50    |
| > 30                  | 70    |

---

## 3. Final Ranking (`scoring/engine.py`)

The engine runs in three steps:

1. **Fetch** — prices + benchmark, then fundamentals and F&O **only for tickers
   with valid price data** (skips delisted symbols, which are recorded and
   skipped in future runs).
2. **Compute factors** — each factor module returns a DataFrame indexed by ticker
   with its `*_score` column.
3. **Composite** (`_composite`):
   - All factor frames are merged on ticker (`outer` join).
   - Any missing factor column/value is filled with the **neutral 50** — a
     missing data point never zeros a stock.
   - `quant_score = Σ weight × factor_score` using `FACTOR_WEIGHTS`
     (defaults below), clipped to [0, 100] and rounded.
   - `rank` is assigned by descending `quant_score` (`method="min"`, ties share
     the same rank).
   - The top `TOP_N_SHORTLIST = 25` ranks are flagged `shortlisted`.

### Factor Weights (config/settings.py)

| Factor    | Weight |
|-----------|--------|
| Momentum  | 25%    |
| Quality   | 20%    |
| Value     | 15%    |
| Technical | 15%    |
| Liquidity | 15%    |
| F&O       | 5%     |
| Events    | 5%     |

**Final result columns:** `symbol`, `quant_score`, `rank`, `shortlisted`, plus
each `momentum/liquidity/quality/value/technical/fno/events_score`.

---

## 4. Output Layer (`output/final_ranker.py`)

Every scoring run writes **JSON + CSV only** (no HTML). Results land in
`output/reports/`:

```
output/reports/
├── latest.json                     # Machine-readable scored results (overwritten each run)
├── latest.csv                      # Same results, tabular form
├── latest_backtest.json            # Latest backtest summary + equity curve
├── latest_backtest.csv             # Equity curve (date, strategy, benchmark)
├── latest_backtest_rebalance.csv   # Trade/rebalance log
└── runs/YYYYMMDD_HHMMSS/           # Timestamped archive per run
    ├── scores_YYYYMMDD_HHMMSS.json
    ├── scores_YYYYMMDD_HHMMSS.csv
    ├── backtest_YYYYMMDD_HHMMSS.json
    ├── backtest_YYYYMMDD_HHMMSS.csv
    └── backtest_YYYYMMDD_HHMMSS_rebalance.csv
```

The JSON schema is `{ run_at, total_scored, shortlist_size, stocks: [ { ticker,
symbol, rank, quant_score, shortlisted, factor_scores: {...} } ] }`.
Console/file logs live in `output/logs/` (`engine.log`, `backtest.log`).

---

## 5. Point-in-Time Backtester

The backtester answers: *"If I had run the scoring engine every month over the
past 3 years and held the top 15 stocks, would I have beaten the Nifty 50?"*

### 5.1 Data Loading (`backtest/data_loader.py`)

- Requests **~4.5 years** of daily OHLCV per ticker
  (`BACKTEST_LOOKBACK_DAYS = 1650` — enough to compute the 12-month momentum at
  the earliest rebalance date).
- Reuses the same Parquet cache with an extended **168-hour (1-week) TTL** —
  historical prices are immutable.
- **Coverage check:** any cached ticker whose history does not reach back far
  enough is re-downloaded individually.
- **Thin-data filter:** tickers with fewer than `MIN_TRADING_DAYS = 60` rows are
  dropped.

### 5.2 Point-in-Time Scoring (`backtest/pit_engine.py`)

The **no-lookahead rule**: at rebalance date `t`, the scorer slices every price
series to `df[df.index <= t]` (minimum 30 rows) and only computes factors from
that slice — nothing after `t` is visible.

**Fidelity adaptation** — only price-derived factors can be computed
historically. Quality/Value/F&O/Events are excluded because the current
fundamentals snapshot and live NSE option chain would leak future information.
The live `FACTOR_WEIGHTS` are **renormalized over the price-only subset** so the
backtest keeps the same *relative* emphasis as the live engine:

| Price-only factor | Live weight | Renormalized |
|-------------------|------------|--------------|
| Momentum          | 25%        | 45.5%        |
| Technical         | 15%        | 27.3%        |
| Liquidity         | 15%        | 27.3%        |

The top-N are selected equal-weighted with the same rank logic as live.

### 5.3 Simulation Loop (`backtest/runner.py`)

1. **Generate rebalance dates** — `monthly` (month-start), `biweekly`
   (`2W-MON`), or `weekly` (`W-MON`).
2. **Per rebalance date:**
   - Snap to the nearest actual trading day in the benchmark series.
   - Run `score_point_in_time` → select the top-N portfolio.
   - Charge **turnover fees**: `capital × (% of portfolio changed) × fee`,
     where turnover = symmetric difference / (2 × portfolio size).
   - **Buy at that day's close; returns accrue from the next trading day**
     (equal-weighted daily returns across holdings).
   - Compound capital daily until the next rebalance.
3. **Benchmark** — Nifty 50 (`^NSEI`) buy-and-hold, normalized to the same
   starting capital.
4. A rebalance log records date, portfolio constituents, size, and capital.

### 5.4 Metrics (`backtest/metrics.py`)

All computed from the daily strategy equity curve vs the benchmark:

- **Total return** — strategy & benchmark, over the full period.
- **CAGR** — compound annual growth rate, using 365.25-day years.
- **Annualized volatility** — daily std × √252.
- **Sharpe ratio** — (annualized return − 6% risk-free) / annualized vol.
- **Sortino ratio** — same numerator / downside deviation only.
- **Max drawdown** — deepest peak-to-trough decline (%), plus its duration in days.
- **Alpha & Beta** — CAPM regression of strategy returns on benchmark returns.
- **Win rate** — % of rebalance periods with positive return.
- **Monthly matrix** — year × month return table with a `Year_Total` column.

### 5.5 Backtest Outputs

Same latest + archive pattern as scoring, all JSON/CSV:
- `latest_backtest.json` — full report (parameters, metrics, equity curve, logs).
- `latest_backtest.csv` — daily equity curve (`date, strategy_equity, benchmark_equity`).
- `latest_backtest_rebalance.csv` — trade log (`date, portfolio_size, capital, portfolio`).

### 5.6 Methodology & Limitations

- **No lookahead:** at each rebalance date, only data up to that date is used.
- **Price-only factors understate live alpha** — Quality/Value could add real
  signal; point-in-time fundamentals (with reporting lag) are the largest
  fidelity upgrade available.
- **Survivorship bias:** the universe reflects *current* Nifty 500 constituents,
  so delisted/removed stocks are absent — results may be slightly optimistic.
- **Equal-weight, no sector caps:** a momentum-driven portfolio can concentrate
  in one sector (e.g. banks).
- **Close-price entry is optimistic** vs real fills; next-day-open entry would be
  more conservative.
- **Corporate actions** are handled only via yfinance `auto_adjust`.
- **`MIN_*` universe filters are defined in config but not yet enforced.**

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

### `scheduler.py`

Runs `run_pipeline(dry_run=False, force_refresh=True)` once per day at the
configured time (`DAILY_RUN_TIME = "08:30"` IST). Leave it running in the
background; press `Ctrl+C` to stop.

---

## 7. Configuration Reference (`config/settings.py`)

| Setting | Value | Purpose |
|---|---|---|
| `CACHE_DIR` | `data/cache` | Market-data cache root |
| `OUTPUT_DIR` / `RUNS_DIR` / `LOGS_DIR` | `output/reports[+runs]` / `output/logs` | Result & log locations |
| `MIN_MARKET_CAP_CR` | 500 | Universe filter — defined, not yet enforced |
| `MIN_AVG_DAILY_VOLUME` | 100,000 | Universe filter — defined, not yet enforced |
| `MIN_PRICE` | 10.0 | Universe filter — defined, not yet enforced |
| `LOOKBACK_DAYS` | 365 | Price history window for live scoring |
| `FACTOR_WEIGHTS` | momentum .25, quality .20, value .15, technical .15, liquidity .15, fno .05, events .05 | Composite weights |
| `MOMENTUM_PERIOD_WEIGHTS` | 1M .30 / 3M .30 / 6M .20 / 12M .20 | Momentum horizon weights |
| `RSI_IDEAL_LOW/HIGH` | 40 / 70 | RSI ideal band |
| `EARNINGS_PENALTY_DAYS` | 3 | Imminent-earnings penalty window |
| `TOP_N_SHORTLIST` | 25 | Shortlist size for Stage 2 |
| `BACKTEST_LOOKBACK_DAYS` | 1650 (~4.5y) | Historical data buffer for backtests |
| `BACKTEST_PRICE_CACHE_TTL_HOURS` | 168 | 1-week cache for immutable price history |
| `PRICE_CACHE_TTL_HOURS` | 6 | Live price cache freshness |
| `FUNDAMENTAL_CACHE_TTL_HOURS` | 24 | Fundamentals freshness |
| `FNO_CACHE_TTL_HOURS` | 2 | F&O data freshness |
| `FUNDAMENTAL_WORKERS` | 4 | Parallel threads for `.info` fetches |
| `PRICE_BATCH_CHUNK` | 100 | Tickers per `yf.download` call |
| `DAILY_RUN_TIME` | `"08:30"` | Scheduler run time (IST) |
| `NSE_HEADERS` / `NSE_BASE_URL` | — | Browser headers + base URL for NSE APIs |

---

## Stage 2 (Coming Soon)

The Agent Research Pipeline will run deep LLM analysis on the shortlisted stocks
and produce structured BUY/PASS decisions with bull/bear cases.
