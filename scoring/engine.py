"""
Quantitative Scoring Engine.

Orchestrates all 6 factors and computes a single composite score per stock.
Outputs a ranked DataFrame and shortlists the Top N stocks for Stage 2.

Features:
  - Sector-aware Quality and Value factors (drops D/E for banks, etc.)
  - Per-stock data completeness tracking
  - Transparent flag system (no black boxes)
  - SQLite persistence of every run
"""

import logging
import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from config.settings import (
    FACTOR_WEIGHTS, TOP_N_SHORTLIST, MIN_DATA_COMPLETENITY,
    EARNINGS_PENALTY_DAYS,
)
from data.fetchers.price_fetcher import fetch_price_batch, get_benchmark_data
from data.fetchers.fundamental_fetcher import fetch_fundamentals_batch
from scoring.factors.momentum import compute_momentum_scores
from scoring.factors.liquidity import compute_liquidity_scores
from scoring.factors.quality import compute_quality_scores
from scoring.factors.value import compute_value_scores
from scoring.factors.technical import compute_technical_scores
from scoring.factors.events import compute_events_scores
from scoring.flags import collect_flags

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

# Total number of factors used for completeness calculation
TOTAL_FACTORS = len(FACTOR_SCORE_COLS)


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

        Returns DataFrame with columns:
        [symbol, quant_score, rank, shortlisted, <factor>_score, ...,
         data_completeness, flags]
        """
        if nse_symbols is None:
            nse_symbols = [t.replace(".NS", "") for t in tickers]

        ticker_to_symbol = dict(zip(tickers, nse_symbols))
        self.ticker_to_symbol = ticker_to_symbol

        console.rule("[bold cyan]STAGE 1 -- QUANTITATIVE SCORING ENGINE")

        # ── Step 1: Fetch data ──────────────────────────────────────
        console.print("[bold yellow]Step 1/4[/] Fetching market data...")
        price_data, benchmark_df, fundamentals = self._fetch_all(
            tickers, nse_symbols, force_refresh
        )

        # ── Step 2: Compute factor scores ──────────────────────────
        console.print("[bold yellow]Step 2/4[/] Computing factor scores...")
        factor_frames, factor_meta = self._compute_factors(
            price_data, benchmark_df, fundamentals, tickers
        )

        # ── Step 3: Composite score + flags ────────────────────────
        console.print("[bold yellow]Step 3/4[/] Compositing and ranking...")
        results = self._composite(
            factor_frames, factor_meta, ticker_to_symbol, fundamentals
        )

        # ── Step 4: Collect flags ──────────────────────────────────
        console.print("[bold yellow]Step 4/4[/] Collecting transparency flags...")
        results = self._attach_flags(results, fundamentals, factor_meta)

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
        price_data = fetch_price_batch(tickers, force_refresh=force_refresh)
        benchmark_df = get_benchmark_data(force_refresh=force_refresh)

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
    ) -> tuple[dict[str, pd.DataFrame], dict[str, dict]]:
        """
        Compute all 6 factor score DataFrames.

        Returns
        -------
        factor_frames : dict[str, pd.DataFrame]
            { factor_name -> DataFrame with score column }
        factor_meta : dict[str, dict]
            { ticker -> { factor_name: {"imputed": bool, ...} } }
            Tracks which factors were actually computed vs imputed.
        """
        frames: dict[str, pd.DataFrame] = {}
        factor_meta: dict[str, dict] = {}

        # Initialize meta for all valid tickers
        valid_tickers = [
            t for t in tickers
            if t in price_data and price_data[t] is not None and not price_data[t].empty
        ]
        for t in valid_tickers:
            factor_meta[t] = {}

        # Momentum
        mom = compute_momentum_scores(price_data, benchmark_df)
        if not mom.empty:
            frames["momentum"] = mom[["momentum_score"]]
            for t in mom.index:
                if t in factor_meta:
                    factor_meta[t]["momentum"] = {
                        "imputed": pd.isna(mom.loc[t, "momentum_score"]),
                        "value": float(mom.loc[t, "momentum_score"]) if pd.notna(mom.loc[t, "momentum_score"]) else None,
                    }

        # Liquidity
        liq = compute_liquidity_scores(price_data)
        if not liq.empty:
            frames["liquidity"] = liq[["liquidity_score"]]
            for t in liq.index:
                if t in factor_meta:
                    factor_meta[t]["liquidity"] = {
                        "imputed": pd.isna(liq.loc[t, "liquidity_score"]),
                        "value": float(liq.loc[t, "liquidity_score"]) if pd.notna(liq.loc[t, "liquidity_score"]) else None,
                    }

        # Quality (sector-aware)
        qual = pd.DataFrame()
        if not fundamentals.empty:
            qual = compute_quality_scores(fundamentals)
            if not qual.empty:
                frames["quality"] = qual[["quality_score"]]
                for t in qual.index:
                    if t in factor_meta:
                        factor_meta[t]["quality"] = {
                            "imputed": pd.isna(qual.loc[t, "quality_score"]),
                            "value": float(qual.loc[t, "quality_score"]) if pd.notna(qual.loc[t, "quality_score"]) else None,
                            "sector_bucket": qual.loc[t, "sector_bucket"] if "sector_bucket" in qual.columns else "other",
                        }

        # Value (sector-aware)
        val = pd.DataFrame()
        if not fundamentals.empty:
            val = compute_value_scores(fundamentals)
            if not val.empty:
                frames["value"] = val[["value_score"]]
                for t in val.index:
                    if t in factor_meta:
                        factor_meta[t]["value"] = {
                            "imputed": pd.isna(val.loc[t, "value_score"]),
                            "value": float(val.loc[t, "value_score"]) if pd.isna(val.loc[t, "value_score"]) is False else None,
                            "sector_bucket": val.loc[t, "sector_bucket"] if "sector_bucket" in val.columns else "other",
                        }

        # Technical
        tech = compute_technical_scores(price_data)
        if not tech.empty:
            frames["technical"] = tech[["technical_score"]]
            for t in tech.index:
                if t in factor_meta:
                    factor_meta[t]["technical"] = {
                        "imputed": pd.isna(tech.loc[t, "technical_score"]),
                        "value": float(tech.loc[t, "technical_score"]) if pd.notna(tech.loc[t, "technical_score"]) else None,
                    }

        # Events
        events = compute_events_scores(sorted(price_data.keys()))
        if not events.empty:
            frames["events"] = events[["events_score"]]
            for t in events.index:
                if t in factor_meta:
                    factor_meta[t]["events"] = {
                        "imputed": False,
                        "value": float(events.loc[t, "events_score"]) if pd.notna(events.loc[t, "events_score"]) else None,
                        "earnings_days": int(events.loc[t, "days_to_earnings"]) if "days_to_earnings" in events.columns and pd.notna(events.loc[t, "days_to_earnings"]) else None,
                    }

        return frames, factor_meta

    def _compute_completeness(self, ticker: str, factor_meta: dict) -> float:
        """
        Compute data completeness for a ticker: fraction of factors with
        real (non-imputed) scores.
        """
        if ticker not in factor_meta:
            return 0.0
        meta = factor_meta[ticker]
        computed = 0
        for factor_name in FACTOR_SCORE_COLS:
            info = meta.get(factor_name)
            if info and not info.get("imputed", True):
                computed += 1
        return computed / TOTAL_FACTORS

    def _composite(
        self,
        factor_frames: dict[str, pd.DataFrame],
        factor_meta: dict[str, dict],
        ticker_to_symbol: dict[str, str],
        fundamentals: pd.DataFrame,
    ) -> pd.DataFrame:
        """Merge all factor frames and compute weighted composite score."""
        if not factor_frames:
            logger.error("No factor frames available — cannot compute scores.")
            return pd.DataFrame()

        merged = pd.concat(factor_frames.values(), axis=1, join="outer")

        # Track which factors were imputed per ticker
        imputed_flags: dict[str, dict[str, bool]] = {}
        for ticker in merged.index:
            imputed_flags[ticker] = {}
            for col in FACTOR_SCORE_COLS.values():
                if col not in merged.columns or pd.isna(merged.loc[ticker, col]):
                    imputed_flags[ticker][col] = True
                else:
                    imputed_flags[ticker][col] = False

        # Fill missing factor scores with neutral 50
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

        # Data completeness
        completeness = pd.Series(0.0, index=merged.index)
        for ticker in merged.index:
            completeness[ticker] = self._compute_completeness(ticker, factor_meta)
        merged["data_completeness"] = completeness.round(3)

        # Imputed tracker (needed for flags later)
        merged.attrs["imputed_flags"] = imputed_flags

        # Add symbol column
        merged["symbol"] = merged.index.map(
            lambda t: ticker_to_symbol.get(t, t.replace(".NS", ""))
        )

        # Rank
        merged["rank"] = merged["quant_score"].rank(ascending=False, method="min").astype(int)
        merged = merged.sort_values("quant_score", ascending=False)

        # Flag shortlist
        merged["shortlisted"] = merged["rank"] <= self.top_n

        # Reorder columns
        priority_cols = [
            "symbol", "quant_score", "rank", "shortlisted", "data_completeness",
            "momentum_score", "liquidity_score", "quality_score",
            "value_score", "technical_score", "events_score",
        ]
        existing = [c for c in priority_cols if c in merged.columns]
        rest = [c for c in merged.columns if c not in existing]
        merged = merged[existing + rest]

        self._print_summary(merged)
        return merged

    def _attach_flags(
        self,
        results: pd.DataFrame,
        fundamentals: pd.DataFrame,
        factor_meta: dict[str, dict],
    ) -> pd.DataFrame:
        """Attach transparency flags to each stock in the results."""
        imputed_flags = results.attrs.get("imputed_flags", {})
        flags_list = []

        for ticker in results.index:
            # Get fundamentals row for this ticker
            fund_row = None
            if not fundamentals.empty and ticker in fundamentals.index:
                fund_row = fundamentals.loc[ticker].to_dict()

            # Get factor meta
            meta = factor_meta.get(ticker, {})

            # Check if quality/value were imputed
            quality_imputed = imputed_flags.get(ticker, {}).get("quality_score", True)
            value_imputed = imputed_flags.get(ticker, {}).get("value_score", True)

            # Get earnings days
            events_meta = meta.get("events", {})
            earnings_days = events_meta.get("earnings_days")

            # Get raw P/E (before scoring)
            pe_raw = None
            if fund_row:
                pe_raw = fund_row.get("trailingPE")

            # Compute completeness
            completeness = results.loc[ticker, "data_completeness"] if "data_completeness" in results.columns else 1.0

            flags = collect_flags(
                fundamentals_row=fund_row,
                quality_score=results.loc[ticker, "quality_score"] if "quality_score" in results.columns else 50.0,
                value_score=results.loc[ticker, "value_score"] if "value_score" in results.columns else 50.0,
                quality_was_imputed=quality_imputed,
                value_was_imputed=value_imputed,
                earnings_days=earnings_days,
                pe_raw=pe_raw,
                data_completeness=completeness,
                min_completeness=MIN_DATA_COMPLETENITY,
            )
            flags_list.append(flags)

        results["flags"] = flags_list
        return results

    def _print_summary(self, df: pd.DataFrame) -> None:
        """Print a rich table of the top results with flags."""
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
        table.add_column("Complete", width=8)
        table.add_column("Shortlisted", width=12)

        top = df.head(min(30, len(df)))
        for _, row in top.iterrows():
            shortlisted = "YES" if row.get("shortlisted") else "-"
            completeness = f"{row.get('data_completeness', 0):.0%}"
            table.add_row(
                str(int(row.get("rank", 0))),
                str(row.get("symbol", "")),
                f"{row.get('quant_score', 0):.1f}",
                f"{row.get('momentum_score', 0):.1f}",
                f"{row.get('quality_score', 0):.1f}",
                f"{row.get('value_score', 0):.1f}",
                f"{row.get('technical_score', 0):.1f}",
                completeness,
                shortlisted,
            )

        console.print(table)
        console.print(
            f"\n[bold green]OK Shortlisted {df['shortlisted'].sum()} stocks "
            f"from {len(df)} scored.[/]\n"
        )
