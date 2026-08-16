"""
Quantitative Scoring Engine.

Orchestrates all 6 factors and computes a single composite score per stock.
Outputs a ranked DataFrame and shortlists the Top N stocks for Stage 2.

Usage:
    from scoring.engine import ScoringEngine
    engine = ScoringEngine()
    results = engine.run(tickers)
"""

import logging
import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table
from rich.progress import track

from config.settings import FACTOR_WEIGHTS, TOP_N_SHORTLIST
from data.fetchers.price_fetcher import fetch_price_batch, get_benchmark_data
from data.fetchers.fundamental_fetcher import fetch_fundamentals_batch
from scoring.factors.momentum import compute_momentum_scores
from scoring.factors.liquidity import compute_liquidity_scores
from scoring.factors.quality import compute_quality_scores
from scoring.factors.value import compute_value_scores
from scoring.factors.technical import compute_technical_scores
from scoring.factors.events import compute_events_scores

logger = logging.getLogger(__name__)
console = Console()

# Columns that hold per-factor scores (used for final composite)
FACTOR_SCORE_COLS = {
    "momentum":   "momentum_score",
    "liquidity":  "liquidity_score",
    "quality":    "quality_score",
    "value":      "value_score",
    "technical":  "technical_score",
    "events":     "events_score",
}


class ScoringEngine:
    """
    Two-pass quantitative scoring engine.

    Pass 1 — Data retrieval: downloads price and fundamental data.
    Pass 2 — Factor computation: scores each factor, weights, composites.

    The engine is stateless between runs; each call to `.run()` is independent.
    """

    def __init__(self, top_n: int = TOP_N_SHORTLIST):
        self.top_n = top_n

    # ─────────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────────

    def run(
        self,
        tickers: list[str],
        nse_symbols: list[str] | None = None,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """
        Run the full scoring pipeline.

        Parameters
        ----------
        tickers : list[str]
            Yahoo Finance tickers (with .NS suffix), e.g. ['TCS.NS', 'INFY.NS'].
        nse_symbols : list[str] | None
            Corresponding NSE symbols (without suffix). If None, derived from tickers.
        force_refresh : bool
            Bypass all caches if True.

        Returns
        -------
        pd.DataFrame
            Full ranked DataFrame. Top N rows are the shortlist.
            Key columns: [symbol, quant_score, rank, shortlisted, <factor>_score, ...]
        """
        if nse_symbols is None:
            nse_symbols = [t.replace(".NS", "") for t in tickers]

        ticker_to_symbol = dict(zip(tickers, nse_symbols))
        self.ticker_to_symbol = ticker_to_symbol

        console.rule("[bold cyan]STAGE 1 -- QUANTITATIVE SCORING ENGINE")

        # ── Step 1: Fetch data ──────────────────────────────────────
        console.print("[bold yellow]Step 1/3[/] Fetching market data...")
        price_data, benchmark_df, fundamentals = self._fetch_all(
            tickers, nse_symbols, force_refresh
        )

        # ── Step 2: Compute factor scores ──────────────────────────
        console.print("[bold yellow]Step 2/3[/] Computing factor scores...")
        factor_frames = self._compute_factors(
            price_data, benchmark_df, fundamentals, tickers
        )

        # ── Step 3: Composite score ─────────────────────────────────
        console.print("[bold yellow]Step 3/3[/] Compositing and ranking...")
        results = self._composite(factor_frames, ticker_to_symbol)

        return results

    def get_shortlist(self, results: pd.DataFrame) -> pd.DataFrame:
        """Return only the Top N shortlisted stocks."""
        return results[results["shortlisted"] == True].copy()

    # ─────────────────────────────────────────────────────────────────
    # PRIVATE
    # ─────────────────────────────────────────────────────────────────

    def _fetch_all(
        self,
        tickers: list[str],
        nse_symbols: list[str],
        force_refresh: bool,
    ) -> tuple:
        """Fetch price and fundamental data."""
        # Price data (batch download)
        price_data = fetch_price_batch(tickers, force_refresh=force_refresh)
        benchmark_df = get_benchmark_data(force_refresh=force_refresh)

        # Only run the slow downstream fetches for symbols with valid price data.
        # Delisted/renamed symbols cost 10-40s each on throttled Yahoo retries.
        valid_tickers = [
            t for t in tickers
            if t in price_data and price_data[t] is not None and not price_data[t].empty
        ]
        dropped = set(tickers) - set(valid_tickers)
        if dropped:
            logger.warning(
                f"Skipping {len(dropped)} symbols without price data: "
                f"{sorted(dropped)}. Removing from fundamentals/events."
            )
            try:
                from data.universe import mark_delisted
                mark_delisted([t.replace(".NS", "") for t in dropped])
            except Exception as e:
                logger.debug(f"Could not persist delisted symbols: {e}")
            logger.info(f"Proceeding with {len(valid_tickers)}/{len(tickers)} valid tickers.")

        # Fundamental data
        fundamentals = fetch_fundamentals_batch(
            valid_tickers, force_refresh=force_refresh
        )

        return price_data, benchmark_df, fundamentals

    def _compute_factors(
        self,
        price_data: dict[str, pd.DataFrame],
        benchmark_df: pd.DataFrame | None,
        fundamentals: pd.DataFrame,
        tickers: list[str],
    ) -> dict[str, pd.DataFrame]:
        """Compute all 6 factor score DataFrames."""
        frames: dict[str, pd.DataFrame] = {}

        # Momentum
        mom = compute_momentum_scores(price_data, benchmark_df)
        if not mom.empty:
            frames["momentum"] = mom[["momentum_score"]]

        # Liquidity
        liq = compute_liquidity_scores(price_data)
        if not liq.empty:
            frames["liquidity"] = liq[["liquidity_score"]]

        # Quality
        if not fundamentals.empty:
            qual = compute_quality_scores(fundamentals)
            if not qual.empty:
                frames["quality"] = qual[["quality_score"]]

        # Value
        if not fundamentals.empty:
            val = compute_value_scores(fundamentals)
            if not val.empty:
                frames["value"] = val[["value_score"]]

        # Technical
        tech = compute_technical_scores(price_data)
        if not tech.empty:
            frames["technical"] = tech[["technical_score"]]

        # Events (uses tickers directly — only those with valid price data)
        events = compute_events_scores(sorted(price_data.keys()))
        if not events.empty:
            frames["events"] = events[["events_score"]]

        return frames

    def _composite(
        self,
        factor_frames: dict[str, pd.DataFrame],
        ticker_to_symbol: dict[str, str],
    ) -> pd.DataFrame:
        """Merge all factor frames and compute weighted composite score."""
        if not factor_frames:
            logger.error("No factor frames available — cannot compute scores.")
            return pd.DataFrame()

        # Merge all factor DataFrames on index (ticker)
        merged = pd.concat(factor_frames.values(), axis=1, join="outer")

        # Fill missing factor scores with neutral 50 (not penalize unavailable data)
        for col in FACTOR_SCORE_COLS.values():
            if col not in merged.columns:
                merged[col] = 50.0
            else:
                merged[col] = merged[col].fillna(50.0)

        # Weighted composite
        quant_score = pd.Series(0.0, index=merged.index)
        for factor, col in FACTOR_SCORE_COLS.items():
            weight = FACTOR_WEIGHTS.get(factor, 0)
            quant_score += weight * merged[col]

        merged["quant_score"] = quant_score.clip(0, 100).round(2)

        # Add symbol column
        merged["symbol"] = merged.index.map(
            lambda t: ticker_to_symbol.get(t, t.replace(".NS", ""))
        )

        # Rank (1 = best)
        merged["rank"] = merged["quant_score"].rank(ascending=False, method="min").astype(int)
        merged = merged.sort_values("quant_score", ascending=False)

        # Flag shortlist
        merged["shortlisted"] = merged["rank"] <= self.top_n

        # Reorder columns nicely
        priority_cols = [
            "symbol", "quant_score", "rank", "shortlisted",
            "momentum_score", "liquidity_score", "quality_score",
            "value_score", "technical_score", "events_score",
        ]
        existing = [c for c in priority_cols if c in merged.columns]
        rest = [c for c in merged.columns if c not in existing]
        merged = merged[existing + rest]

        self._print_summary(merged)
        return merged

    def _print_summary(self, df: pd.DataFrame) -> None:
        """Print a rich table of the top results."""
        table = Table(
            title="Quantitative Scoring -- Top Results",
            show_header=True,
            header_style="bold magenta",
        )
        table.add_column("Rank", style="bold cyan", width=5)
        table.add_column("Symbol", style="bold white", width=12)
        table.add_column("Score", style="bold green", width=8)
        table.add_column("Momentum", width=10)
        table.add_column("Quality", width=10)
        table.add_column("Value", width=10)
        table.add_column("Technical", width=10)
        table.add_column("Shortlisted", width=12)

        top = df.head(min(30, len(df)))
        for _, row in top.iterrows():
            shortlisted = "YES" if row.get("shortlisted") else "-"
            table.add_row(
                str(int(row.get("rank", 0))),
                str(row.get("symbol", "")),
                f"{row.get('quant_score', 0):.1f}",
                f"{row.get('momentum_score', 0):.1f}",
                f"{row.get('quality_score', 0):.1f}",
                f"{row.get('value_score', 0):.1f}",
                f"{row.get('technical_score', 0):.1f}",
                shortlisted,
            )

        console.print(table)
        console.print(
            f"\n[bold green]OK Shortlisted {df['shortlisted'].sum()} stocks "
            f"from {len(df)} scored.[/]\n"
        )
