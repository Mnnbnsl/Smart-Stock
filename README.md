# Stock Scoring Engine

A two-stage NSE stock selection system. **Stage 1** (this repo) is the quantitative scoring engine.

## Architecture

```
ALL NSE STOCKS (Nifty 500)
        │
        ▼
[1] Universe Builder     → data/universe.py
        ▼
[2] Data Fetchers        → data/fetchers/
    ├── price_fetcher.py       (OHLCV via yfinance, Parquet cache)
    ├── fundamental_fetcher.py (P/E, ROE, D/E via yfinance, JSON cache)
    └── fno_fetcher.py         (PCR, OI via NSE API, JSON cache)
        ▼
[3] Factor Computation   → scoring/factors/
    ├── momentum.py      (1M/3M/6M/12M relative to Nifty)
    ├── liquidity.py     (20-day ADV and turnover)
    ├── quality.py       (ROE, ROA, D/E, operating margin)
    ├── value.py         (P/E, P/B, EV/EBITDA)
    ├── technical.py     (RSI, SMA trend, MACD, 52w position)
    ├── fno_sentiment.py (Put-Call Ratio, OI trend)
    └── events.py        (Earnings calendar penalty)
        ▼
[4] Scoring Engine       → scoring/engine.py
    Weighted composite → Top 25 shortlist
        ▼
[5] Output               → output/
    ├── final_ranker.py  (saves JSON, CSV, HTML dashboard)
    └── reports/         (timestamped files + latest.json)

[6] Backtester (validation) → backtest/
    ├── data_loader.py   (multi-year price history + cache)
    ├── pit_engine.py    (point-in-time scoring, no lookahead)
    ├── runner.py        (rolling rebalance simulation)
    ├── metrics.py       (CAGR, Sharpe, MaxDD, Alpha vs Nifty)
    └── run_backtest.py  (CLI entry point)
```

## Factor Weights

| Factor    | Weight |
|-----------|--------|
| Momentum  | 25%    |
| Quality   | 20%    |
| Value     | 15%    |
| Technical | 15%    |
| Liquidity | 15%    |
| F&O       | 5%     |
| Events    | 5%     |

## How Stocks Are Ranked

Two layers form the system: the **live scoring engine** (`main.py`) ranks stocks
*as of today*, and the **backtester** replays that same scoring logic historically
to verify it actually beats the market. This section explains the live engine.

**1. Universe** (`data/universe.py`) — fetches the current Nifty 500 constituent
list from NSE (with a bundled fallback), then filters for tradability:
min ₹500 Cr market cap, min 100k average daily volume, min ₹10 price.

**2. Data collection** (`scoring/engine.py:_fetch_all`) — downloads OHLCV prices,
fundamental metrics, and F&O data (all cached; only fresh data is re-fetched).

**3. Factor computation** — each of the 7 factors produces a normalized **0–100
score per stock** (higher = better):

| Factor | Signals | Sub-composition |
|---|---|---|
| Momentum | 1M/3M/6M/12M return **minus Nifty's** return | 30/30/20/20 weighted |
| Liquidity | 20-day avg volume + avg daily turnover (₹) | 50/50 |
| Quality | ROE, ROA, D/E, margins | 35/20/30/15 |
| Value | P/E, P/B, EV/EBITDA (cheaper = better) | 45/30/25 |
| Technical | RSI (ideal band 40–70), SMA50, MACD, 52-week position | 25/30/25/20 |
| F&O | Put-Call Ratio, OI trend | — |
| Events | Earnings-date proximity penalty | — |

**4. Normalization** (`scoring/normalizer.py`) — raw factor values are winsorized
(clamped at the 2nd/98th percentile) then converted to **percentile ranks within
the universe**, so every factor is on the same 0–100 scale. Value is inverted so a
cheap stock scores higher.

**5. Composite & rank** (`scoring/engine.py:_composite`) — the weighted sum from
`FACTOR_WEIGHTS` above produces the `quant_score`. Missing data defaults to a
neutral 50 so a single bad data point doesn't zero a stock. Stocks are sorted by
`quant_score` descending; the **top 25** (`TOP_N_SHORTLIST`) are flagged
`shortlisted` and passed to Stage 2 agent research.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Test with 20 stocks (fast dry run)
python main.py --dry-run

# 3. Full Nifty 500 run
python main.py

# 4. Start daily scheduler (runs at 08:30 IST)
python scheduler.py
```

## CLI Options

```
python main.py --dry-run         # 20 test stocks
python main.py --refresh         # bypass cache
python main.py --limit 100       # limit to 100 symbols
python main.py --top-n 30        # shortlist 30 stocks
```

## Output Files

After each run, `output/reports/` contains:
- `latest.json`          — Machine-readable scored results
- `scores_YYYYMMDD_HHMMSS.json`
- `scores_YYYYMMDD_HHMMSS.csv`
- `dashboard.html`       — Visual report (open in browser)

## Point-in-Time Backtester

Verifies whether the scoring engine generates excess returns (alpha) over the
Nifty 50 benchmark via rolling historical simulation with **no lookahead bias**.

```bash
# Quick 6-month smoke test on 20 well-known stocks
python run_backtest.py --start 2024-01-01 --end 2024-06-01 --dry-run

# 3-year full-universe backtest
python run_backtest.py --start 2023-01-01 --end 2026-01-01 --top-n 15

# Weekly rebalance with higher fees
python run_backtest.py --start 2024-01-01 --end 2025-01-01 --rebalance weekly --fee 0.002
```

### CLI Options

```
--start, --end            Backtest period (YYYY-MM-DD)
--rebalance               monthly | biweekly | weekly (default: monthly)
--top-n                   Portfolio size (default: 15)
--fee                     Slippage/fee per trade (default: 0.001 = 0.1%)
--capital                 Initial capital in INR (default: 100000)
--dry-run                 20 well-known stocks (fast test mode)
--limit N                 Limit universe to N symbols
--force-refresh           Re-download all historical price data
```

### Outputs

`output/reports/backtest_report.html` (dark-mode interactive report):
- Equity curve vs Nifty 50, drawdown charts, monthly return heatmaps
- Performance cards: Total Return, CAGR, Sharpe, Sortino, Max Drawdown, Alpha, Beta, Win Rate
- Trade log table (portfolio constituents per rebalance)

Plus `latest_backtest.json` / `backtest_YYYYMMDD_HHMMSS.json` (machine-readable).

### How Backtesting Works

The backtest answers: *"If I had run the scoring engine every month over the past
3 years and held the top 15 stocks, would I have beaten the Nifty 50?"*

**No-lookahead rule** — the core design. At rebalance date `t`, the scorer only
sees `df[df.index <= t]` (`backtest/pit_engine.py`). Any data after `t` is
invisible, so there is no information leakage.

**Fidelity adaptation** (`backtest/pit_engine.py:_price_factor_weights`) — only
price-derived factors can be computed historically. Quality/Value/F&O/Events use
*current* snapshots and would leak future data, so they are excluded. The live
`FACTOR_WEIGHTS` are **renormalized** over the price-only subset →
momentum 45.5% / technical 27.3% / liquidity 27.3%, so the backtest tests the
same *relative* factor emphasis as the live engine.

**Simulation loop** (`backtest/runner.py:run`):

1. **Load data** — ~4.5 years of daily OHLCV per ticker
   (`backtest/data_loader.py`, cached as Parquet with a coverage check that
   re-downloads any ticker whose cached history is too short).
2. **Generate rebalance dates** — `monthly` (month-start), `biweekly`, or `weekly`.
3. **Per rebalance date**:
   - Snap to the nearest real trading day
   - Run `score_point_in_time` → select top-N (equal-weighted)
   - Charge turnover fees: `capital × % of portfolio changed × 0.1%`
   - **Buy at that day's close; returns accrue from the next trading day**
   - Compound daily until the next rebalance
4. **Benchmark** — Nifty 50 (^NSEI) buy-and-hold, normalized to the same
   starting capital.

**Metrics** (`backtest/metrics.py`) — CAGR, annualized volatility, Sharpe and
Sortino ratios (6% risk-free), max drawdown + duration, win rate across rebalance
periods, and **alpha/beta via regression against benchmark daily returns**, plus a
monthly-return matrix.

### Methodology & Limitations

- **Point-in-time**: at each rebalance date, only data up to that date is used.
- **Factors**: price-derived only — momentum, technical, liquidity — renormalized
  from the live `FACTOR_WEIGHTS` (momentum ≈ 45%, technical ≈ 27%, liquidity ≈ 27%).
  Quality/Value/F&O/Events are excluded because the current data sources (yfinance
  snapshot, NSE live PCR) are not available historically and would leak lookahead.
- **Fees**: turnover × `--fee` applied at each rebalance.
- **Entry timing**: positions enter at rebalance-day close; returns accrue from the
  next trading day.
- **Survivorship bias**: the universe reflects *current* Nifty 500 constituents, so
  delisted/removed stocks are absent — results may be slightly optimistic.

### Known Limitations & Potential Improvements

- **Price-only factors understate live alpha** — Quality/Value could add real
  signal. The largest fidelity upgrade available is computing point-in-time
  fundamentals from yfinance's quarterly statements (with reporting lag).
- **Equal-weight, no sector caps** — a momentum-driven portfolio can concentrate
  in one sector (e.g., banks). Sector-neutralization or caps would reduce risk.
- **Close-price entry is optimistic** vs real fills; next-day-open entry would be
  more conservative.
- **Corporate actions** are handled only via yfinance `auto_adjust`.

## Stage 2 (Coming Soon)

The Agent Research Pipeline will run deep LLM analysis on the shortlisted stocks
and produce structured BUY/PASS decisions with bull/bear cases.
