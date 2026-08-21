"""
Backtest Simulator & Runner.

Simulates rolling rebalance periods over historical timelines:
  - Generates rebalance dates (Monthly, Bi-weekly, Weekly)
  - Point-in-time portfolio selection (no lookahead bias)
  - Equal-weighted Top-N portfolio with turnover-based fees
  - Tracks daily equity curve vs Nifty 50 benchmark
  - Computes complete performance report (JSON + CSV)
"""

import os
import json
import logging
from datetime import datetime

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from config.settings import OUTPUT_DIR, RUNS_DIR
from data.db import init_schema, insert_backtest
from data.universe import load_universe
from backtest.data_loader import load_backtest_benchmark, load_backtest_price_data
from backtest.pit_engine import score_point_in_time
from backtest.metrics import calculate_full_performance

logger = logging.getLogger(__name__)
console = Console()


def generate_rebalance_dates(
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    freq: str = "monthly",
) -> list[pd.Timestamp]:
    """Generate list of rebalance dates between start_date and end_date."""
    if freq == "monthly":
        dr = pd.date_range(start=start_date, end=end_date, freq="MS")
    elif freq == "biweekly":
        dr = pd.date_range(start=start_date, end=end_date, freq="2W-MON")
    elif freq == "weekly":
        dr = pd.date_range(start=start_date, end=end_date, freq="W-MON")
    else:
        raise ValueError(f"Unknown frequency: {freq}")

    return [d for d in dr]


class BacktestRunner:
    """
    Rolling Point-in-Time Quantitative Backtester.
    """

    def __init__(
        self,
        start_date: str = "2024-01-01",
        end_date: str = "2026-01-01",
        freq: str = "monthly",
        top_n: int = 15,
        fee_pct: float = 0.001,  # 0.1% per trade slippage/fee
        initial_capital: float = 100_000.0,  # 1 Lakh INR
        symbols: list[str] | None = None,  # explicit NSE symbol universe (e.g. dry-run)
    ):
        self.start_date = pd.to_datetime(start_date)
        self.end_date = pd.to_datetime(end_date)
        self.freq = freq
        self.top_n = top_n
        self.fee_pct = fee_pct
        self.initial_capital = initial_capital
        self.symbols = symbols

    # ─────────────────────────────────────────────────────────────────
    # PUBLIC
    # ─────────────────────────────────────────────────────────────────

    def run(
        self,
        tickers: list[str] | None = None,
        force_refresh: bool = False,
    ) -> dict:
        """Execute backtest simulation."""
        init_schema()
        console.rule("[bold cyan]BACKTEST SIMULATION")
        console.print(f"Period: [yellow]{self.start_date.strftime('%Y-%m-%d')}[/] to [yellow]{self.end_date.strftime('%Y-%m-%d')}[/]")
        console.print(f"Frequency: [cyan]{self.freq}[/] | Portfolio Size: [cyan]Top {self.top_n}[/] | Fee: [cyan]{self.fee_pct * 100:.1f}%[/]\n")

        # Load universe tickers
        if tickers is None:
            if self.symbols:
                tickers = [f"{s}.NS" for s in self.symbols]
                console.print(f"[yellow]Using {len(tickers)} explicit symbols[/]\n")
            else:
                universe_df = load_universe(force_refresh=force_refresh)
                tickers = universe_df["yf_ticker"].tolist()

        # ── Step 1: Load historical price data (multi-year, cached) ──
        console.print(f"[bold yellow]Step 1/3[/] Loading historical price data ({len(tickers)} tickers)...")
        price_data = load_backtest_price_data(tickers, force_refresh=force_refresh)

        benchmark_raw = load_backtest_benchmark(force_refresh=force_refresh)
        if benchmark_raw is None or benchmark_raw.empty:
            console.print("[bold red]Failed to fetch benchmark data.[/]")
            return {}

        bench_close = benchmark_raw["Close"].copy()
        bench_close = bench_close[(bench_close.index >= self.start_date) & (bench_close.index <= self.end_date)]
        if bench_close.empty:
            console.print("[bold red]No benchmark data within backtest period.[/]")
            return {}

        rebalance_dates = generate_rebalance_dates(self.start_date, self.end_date, self.freq)
        if not rebalance_dates:
            console.print("[bold red]No rebalance dates found in range.[/]")
            return {}

        console.print(f"[bold yellow]Step 2/3[/] Running rolling PIT simulation across {len(rebalance_dates)} rebalance points...")

        # Track daily equity
        daily_dates = bench_close.index
        strategy_equity = pd.Series(index=daily_dates, dtype=float)
        strategy_equity.iloc[0] = self.initial_capital

        factors_used: dict = {}
        rebalance_logs = []
        period_returns = []

        current_portfolio: list[str] = []
        current_capital = self.initial_capital

        for i in range(len(rebalance_dates)):
            reb_date = rebalance_dates[i]
            next_reb_date = rebalance_dates[i + 1] if i + 1 < len(rebalance_dates) else self.end_date

            # Match nearest trading day
            trading_days_at_reb = daily_dates[daily_dates <= reb_date]
            if trading_days_at_reb.empty:
                continue
            actual_reb_day = trading_days_at_reb[-1]

            # Point-in-time scoring as of actual_reb_day
            shortlist_df = score_point_in_time(
                price_data=price_data,
                cutoff_date=actual_reb_day,
                benchmark_df=benchmark_raw,
                top_n=self.top_n,
            )
            if not shortlist_df.empty:
                factors_used = shortlist_df.attrs.get("factors_used", {})

            new_portfolio = shortlist_df.index.tolist() if not shortlist_df.empty else current_portfolio

            # Apply transaction fee on turnover
            turnover_pct = self._calc_turnover(current_portfolio, new_portfolio)
            fee_cost = current_capital * turnover_pct * self.fee_pct
            current_capital -= fee_cost

            current_portfolio = new_portfolio

            # Track returns from the trading day AFTER entry (buy at entry close)
            holding_mask = (daily_dates > actual_reb_day) & (daily_dates <= next_reb_date)
            holding_days = daily_dates[holding_mask]

            if len(holding_days) > 0:
                port_daily_rets = pd.Series(0.0, index=holding_days)
                valid_tickers = 0

                for t in current_portfolio:
                    if t in price_data and price_data[t] is not None and not price_data[t].empty:
                        p_df = price_data[t]["Close"]
                        window = p_df.reindex([actual_reb_day] + list(holding_days)).ffill().bfill()
                        r_all = window.pct_change().fillna(0.0)
                        r_sub = r_all.iloc[1:]  # exclude entry-day return (entered at close)
                        r_sub.index = holding_days
                        port_daily_rets += r_sub
                        valid_tickers += 1

                if valid_tickers > 0:
                    port_daily_rets /= valid_tickers

                # Compounding daily equity
                start_cap = current_capital
                for d in holding_days:
                    ret_d = port_daily_rets.loc[d]
                    current_capital *= (1.0 + ret_d)
                    strategy_equity.loc[d] = current_capital

                period_ret = ((current_capital / start_cap) - 1.0) * 100
                period_returns.append(period_ret)

            rebalance_logs.append({
                "date": actual_reb_day.strftime("%Y-%m-%d"),
                "portfolio": [t.replace(".NS", "") for t in current_portfolio],
                "portfolio_size": len(current_portfolio),
                "capital": round(current_capital, 2),
            })

        strategy_equity = strategy_equity.ffill().bfill()

        # Benchmark equity curve normalized to initial capital
        benchmark_equity = (bench_close / bench_close.iloc[0]) * self.initial_capital

        console.print("[bold yellow]Step 3/3[/] Calculating performance metrics...")
        metrics = calculate_full_performance(
            strategy_equity=strategy_equity,
            benchmark_equity=benchmark_equity,
            rebalance_returns=pd.Series(period_returns),
        )
        metrics["factors_used"] = factors_used

        self._print_backtest_summary(metrics)

        # Save backtest outputs
        output_paths = self._save_backtest_reports(
            metrics=metrics,
            strategy_equity=strategy_equity,
            benchmark_equity=benchmark_equity,
            rebalance_logs=rebalance_logs,
        )

        # Persist to SQLite
        try:
            backtest_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            params = {
                "start_date": self.start_date.strftime("%Y-%m-%d"),
                "end_date": self.end_date.strftime("%Y-%m-%d"),
                "freq": self.freq,
                "top_n": self.top_n,
                "fee_pct": self.fee_pct,
                "initial_capital": self.initial_capital,
            }
            equity_df = pd.DataFrame({
                "strategy_equity": strategy_equity,
                "benchmark_equity": benchmark_equity,
            })
            insert_backtest(backtest_id, params, metrics, equity_df)
        except Exception as e:
            logger.warning(f"Could not persist backtest to DB: {e}")

        return {
            "metrics": metrics,
            "paths": output_paths,
        }

    # ─────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────

    def _calc_turnover(self, old_port: list[str], new_port: list[str]) -> float:
        """Calculate turnover percentage between old and new portfolio."""
        if not old_port:
            return 1.0
        old_set, new_set = set(old_port), set(new_port)
        changed = len(old_set.symmetric_difference(new_set))
        return changed / (2.0 * max(len(old_port), 1))

    def _print_backtest_summary(self, metrics: dict) -> None:
        """Print rich summary table of backtest results."""
        table = Table(
            title="BACKTEST PERFORMANCE SUMMARY",
            show_header=True,
            header_style="bold magenta",
        )
        table.add_column("Metric", style="bold white", width=25)
        table.add_column("Quant Strategy", style="bold green", width=18)
        table.add_column("Nifty 50 Benchmark", style="bold cyan", width=20)

        table.add_row("Total Return", f"{metrics['total_return']:.2f}%", f"{metrics['benchmark_total_return']:.2f}%")
        table.add_row("CAGR (Annual Return)", f"{metrics['cagr']:.2f}%", f"{metrics['benchmark_cagr']:.2f}%")
        table.add_row("Annualized Volatility", f"{metrics['volatility']:.2f}%", f"{metrics['benchmark_volatility']:.2f}%")
        table.add_row("Sharpe Ratio (Rf=6%)", f"{metrics['sharpe_ratio']:.2f}", "-")
        table.add_row("Sortino Ratio", f"{metrics['sortino_ratio']:.2f}", "-")
        table.add_row("Max Drawdown", f"-{metrics['max_drawdown']:.2f}%", f"-{metrics['benchmark_max_drawdown']:.2f}%")
        table.add_row("Alpha vs Nifty 50", f"{metrics['alpha']:.2f}%", "0.00%")
        table.add_row("Beta vs Nifty 50", f"{metrics['beta']:.2f}", "1.00")
        table.add_row("Win Rate (Rebalance Periods)", f"{metrics['win_rate']:.2f}%", "-")

        console.print(table)
        console.print(f"\n[dim]Price-only PIT factors used: {metrics.get('factors_used', {})}[/]\n")

    def _save_backtest_reports(
        self,
        metrics: dict,
        strategy_equity: pd.Series,
        benchmark_equity: pd.Series,
        rebalance_logs: list[dict],
    ) -> dict[str, str]:
        """Save JSON & CSV backtest reports."""
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = os.path.join(RUNS_DIR, ts)
        os.makedirs(run_dir, exist_ok=True)

        report_data = {
            "run_at": ts,
            "period": {
                "start": self.start_date.strftime("%Y-%m-%d"),
                "end": self.end_date.strftime("%Y-%m-%d"),
            },
            "parameters": {
                "freq": self.freq,
                "top_n": self.top_n,
                "fee_pct": self.fee_pct,
                "initial_capital": self.initial_capital,
            },
            "metrics": metrics,
            "factors_used": metrics.get("factors_used", {}),
            "rebalance_logs": rebalance_logs,
            "equity_curve": {
                "dates": [d.strftime("%Y-%m-%d") for d in strategy_equity.index],
                "strategy": strategy_equity.round(2).tolist(),
                "benchmark": benchmark_equity.round(2).tolist(),
            },
        }

        paths: dict[str, str] = {}

        # ── Timestamped archive ──────────────────────────────────────
        json_path = os.path.join(run_dir, f"backtest_{ts}.json")
        with open(json_path, "w") as f:
            json.dump(report_data, f, indent=2, default=str)
        paths["json"] = json_path

        equity_csv = os.path.join(run_dir, f"backtest_{ts}.csv")
        self._save_equity_csv(strategy_equity, benchmark_equity, equity_csv)
        paths["csv"] = equity_csv

        rebalance_csv = os.path.join(run_dir, f"backtest_{ts}_rebalance.csv")
        self._save_rebalance_csv(rebalance_logs, rebalance_csv)
        paths["rebalance_csv"] = rebalance_csv

        # ── latest.* (always overwrite for easy consumption) ─────────
        latest_json = os.path.join(OUTPUT_DIR, "latest_backtest.json")
        with open(latest_json, "w") as f:
            json.dump(report_data, f, indent=2, default=str)
        paths["latest_json"] = latest_json

        latest_csv = os.path.join(OUTPUT_DIR, "latest_backtest.csv")
        self._save_equity_csv(strategy_equity, benchmark_equity, latest_csv)
        paths["latest_csv"] = latest_csv

        latest_rebalance_csv = os.path.join(OUTPUT_DIR, "latest_backtest_rebalance.csv")
        self._save_rebalance_csv(rebalance_logs, latest_rebalance_csv)
        paths["latest_rebalance_csv"] = latest_rebalance_csv

        console.print(f"\n[bold green]Backtest reports saved -> {json_path}[/]")
        return paths

    @staticmethod
    def _save_equity_csv(
        strategy_equity: pd.Series,
        benchmark_equity: pd.Series,
        path: str,
    ) -> None:
        """Save daily equity curve as CSV."""
        equity_df = pd.DataFrame({
            "date": [d.strftime("%Y-%m-%d") for d in strategy_equity.index],
            "strategy_equity": strategy_equity.round(2).values,
            "benchmark_equity": benchmark_equity.round(2).values,
        })
        equity_df.to_csv(path, index=False)

    @staticmethod
    def _save_rebalance_csv(rebalance_logs: list[dict], path: str) -> None:
        """Save rebalance/trade log as CSV."""
        rows = [
            {
                "date": log["date"],
                "portfolio_size": log["portfolio_size"],
                "capital": log["capital"],
                "portfolio": "; ".join(log["portfolio"]),
            }
            for log in rebalance_logs
        ]
        pd.DataFrame(rows).to_csv(path, index=False)
