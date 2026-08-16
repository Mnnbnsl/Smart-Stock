"""
Performance & Risk Metrics Calculator for Quantitative Backtesting.

Calculates key investment metrics:
  - CAGR (Compound Annual Growth Rate)
  - Annualized Volatility
  - Sharpe Ratio (using Indian risk-free rate ~6%)
  - Sortino Ratio (downside risk-adjusted)
  - Max Drawdown & Max Drawdown Duration
  - Win Rate & Profit Factor
  - Alpha & Beta vs Benchmark (Nifty 50)
  - Monthly returns heatmap matrix
"""

import numpy as np
import pandas as pd

# Default Indian Risk-Free Rate (6.0% annual)
DEFAULT_RISK_FREE_RATE = 0.06


def compute_cagr(equity_curve: pd.Series) -> float:
    """Compute Compound Annual Growth Rate (%) from a daily equity curve."""
    if equity_curve.empty or len(equity_curve) < 2:
        return 0.0
    start_val = equity_curve.iloc[0]
    end_val = equity_curve.iloc[-1]
    if start_val <= 0:
        return 0.0

    days = (equity_curve.index[-1] - equity_curve.index[0]).days
    if days <= 0:
        return 0.0

    years = days / 365.25
    cagr = ((end_val / start_val) ** (1.0 / years)) - 1.0
    return round(float(cagr * 100), 2)


def compute_volatility(daily_returns: pd.Series) -> float:
    """Compute annualized volatility (%) from daily returns."""
    if daily_returns.empty or len(daily_returns) < 2:
        return 0.0
    std = daily_returns.std()
    ann_vol = std * np.sqrt(252)
    return round(float(ann_vol * 100), 2)


def compute_sharpe_ratio(
    daily_returns: pd.Series,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> float:
    """Compute annualized Sharpe Ratio."""
    if daily_returns.empty or len(daily_returns) < 2:
        return 0.0
    std = daily_returns.std()
    if std == 0 or np.isnan(std):
        return 0.0

    mean_ret = daily_returns.mean() * 252
    rf = risk_free_rate
    sharpe = (mean_ret - rf) / (std * np.sqrt(252))
    return round(float(sharpe), 2)


def compute_sortino_ratio(
    daily_returns: pd.Series,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> float:
    """Compute annualized Sortino Ratio (using downside deviation)."""
    if daily_returns.empty or len(daily_returns) < 2:
        return 0.0

    downside_returns = daily_returns[daily_returns < 0]
    if downside_returns.empty:
        return 99.9  # No negative days

    downside_std = np.sqrt((downside_returns ** 2).mean()) * np.sqrt(252)
    if downside_std == 0 or np.isnan(downside_std):
        return 0.0

    mean_ret = daily_returns.mean() * 252
    rf = risk_free_rate
    sortino = (mean_ret - rf) / downside_std
    return round(float(sortino), 2)


def compute_max_drawdown(equity_curve: pd.Series) -> tuple[float, int]:
    """
    Compute Maximum Drawdown (%) and Max Drawdown Duration (in days).

    Returns
    -------
    tuple[float, int]
        (max_drawdown_pct, max_drawdown_days)
    """
    if equity_curve.empty or len(equity_curve) < 2:
        return 0.0, 0

    cummax = equity_curve.cummax()
    drawdown = (equity_curve - cummax) / cummax
    max_dd = abs(float(drawdown.min())) * 100

    # Drawdown duration calculation
    is_in_dd = drawdown < 0
    dd_durations = []
    curr = 0
    for flag in is_in_dd:
        if flag:
            curr += 1
        else:
            if curr > 0:
                dd_durations.append(curr)
            curr = 0
    if curr > 0:
        dd_durations.append(curr)

    max_days = max(dd_durations) if dd_durations else 0
    return round(max_dd, 2), max_days


def compute_alpha_beta(
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> tuple[float, float]:
    """
    Compute Annualized Alpha (%) and Beta vs Benchmark.

    Returns
    -------
    tuple[float, float]
        (alpha_pct, beta)
    """
    df = pd.concat([strategy_returns, benchmark_returns], axis=1).dropna()
    if len(df) < 10:
        return 0.0, 1.0

    strat_ret = df.iloc[:, 0]
    bench_ret = df.iloc[:, 1]

    cov_matrix = np.cov(strat_ret, bench_ret)
    bench_var = cov_matrix[1, 1]

    if bench_var == 0 or np.isnan(bench_var):
        return 0.0, 1.0

    beta = cov_matrix[0, 1] / bench_var

    # Annualized Alpha (Capital Asset Pricing Model)
    strat_ann = strat_ret.mean() * 252
    bench_ann = bench_ret.mean() * 252
    alpha = (strat_ann - risk_free_rate) - beta * (bench_ann - risk_free_rate)

    return round(float(alpha * 100), 2), round(float(beta), 2)


def compute_win_rate(period_returns: pd.Series) -> float:
    """Compute percentage of positive rebalance periods (%)."""
    if period_returns.empty:
        return 0.0
    wins = (period_returns > 0).sum()
    total = len(period_returns)
    return round(float((wins / total) * 100), 2)


def compute_monthly_matrix(equity_curve: pd.Series) -> pd.DataFrame:
    """
    Generate Monthly Percentage Returns Matrix (Years x Months).
    """
    if equity_curve.empty:
        return pd.DataFrame()

    # Resample to monthly last value
    monthly = equity_curve.resample("ME").last()
    monthly_ret = monthly.pct_change() * 100

    # Fix first month return relative to initial equity
    if len(monthly) > 0:
        monthly_ret.iloc[0] = ((monthly.iloc[0] / equity_curve.iloc[0]) - 1) * 100

    df_monthly = pd.DataFrame({
        "Year": monthly_ret.index.year,
        "Month": monthly_ret.index.month,
        "Return": monthly_ret.values,
    })

    month_names = {
        1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
        7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"
    }
    df_monthly["Month"] = df_monthly["Month"].map(month_names)

    pivot = df_monthly.pivot(index="Year", columns="Month", values="Return")
    # Order months correctly
    ordered_cols = [m for m in month_names.values() if m in pivot.columns]
    pivot = pivot[ordered_cols].round(2)

    # Add Annual total return column
    annual = equity_curve.resample("YE").last().pct_change() * 100
    if len(annual) > 0:
        annual.iloc[0] = ((equity_curve.resample("YE").last().iloc[0] / equity_curve.iloc[0]) - 1) * 100

    annual_dict = {y.year: round(val, 2) for y, val in annual.items()}
    pivot["Year_Total"] = pivot.index.map(annual_dict)

    return pivot.fillna(0.0)


def calculate_full_performance(
    strategy_equity: pd.Series,
    benchmark_equity: pd.Series,
    rebalance_returns: pd.Series | None = None,
) -> dict:
    """
    Calculate full performance dictionary combining all metrics.
    """
    strat_daily = strategy_equity.pct_change().dropna()
    bench_daily = benchmark_equity.pct_change().dropna()

    cagr = compute_cagr(strategy_equity)
    bench_cagr = compute_cagr(benchmark_equity)
    total_ret = round(float(((strategy_equity.iloc[-1] / strategy_equity.iloc[0]) - 1) * 100), 2)
    bench_total_ret = round(float(((benchmark_equity.iloc[-1] / benchmark_equity.iloc[0]) - 1) * 100), 2)

    vol = compute_volatility(strat_daily)
    bench_vol = compute_volatility(bench_daily)

    sharpe = compute_sharpe_ratio(strat_daily)
    sortino = compute_sortino_ratio(strat_daily)
    max_dd, dd_days = compute_max_drawdown(strategy_equity)
    bench_max_dd, _ = compute_max_drawdown(benchmark_equity)

    alpha, beta = compute_alpha_beta(strat_daily, bench_daily)

    win_rate = 0.0
    if rebalance_returns is not None and not rebalance_returns.empty:
        win_rate = compute_win_rate(rebalance_returns)

    monthly_pivot = compute_monthly_matrix(strategy_equity)

    return {
        "total_return": total_ret,
        "benchmark_total_return": bench_total_ret,
        "cagr": cagr,
        "benchmark_cagr": bench_cagr,
        "volatility": vol,
        "benchmark_volatility": bench_vol,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "max_drawdown": max_dd,
        "max_drawdown_days": dd_days,
        "benchmark_max_drawdown": bench_max_dd,
        "alpha": alpha,
        "beta": beta,
        "win_rate": win_rate,
        "monthly_matrix": monthly_pivot.to_dict(orient="index"),
    }
