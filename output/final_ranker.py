"""
Final Ranker.

For Stage 1 (scoring engine only), this module:
  1. Takes the full scored DataFrame from the Scoring Engine
  2. Saves JSON and CSV reports
  3. Overwrites latest.json / latest.csv for easy consumption

When the Agent Research Pipeline (Stage 2) is added, this module will
combine quant_score + agent_score into a final composite ranking.
"""

import os
import json
import logging
from datetime import datetime

import pandas as pd

from config.settings import OUTPUT_DIR, RUNS_DIR

logger = logging.getLogger(__name__)


def save_results(
    df: pd.DataFrame,
    run_ts: str | None = None,
) -> dict[str, str]:
    """
    Save scored results to JSON and CSV.

    Timestamped files are archived under runs/<ts>/ while latest.json and
    latest.csv are always overwritten for easy consumption.

    Returns dict of file paths that were written.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = run_ts or datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(RUNS_DIR, ts)
    os.makedirs(run_dir, exist_ok=True)

    paths: dict[str, str] = {}

    # ── Timestamped archive ──────────────────────────────────────────
    json_path = os.path.join(run_dir, f"scores_{ts}.json")
    _save_json(df, json_path, ts)
    paths["json"] = json_path

    csv_path = os.path.join(run_dir, f"scores_{ts}.csv")
    df.to_csv(csv_path)
    paths["csv"] = csv_path

    # ── latest.* (always overwrite for easy consumption) ─────────────
    latest_json = os.path.join(OUTPUT_DIR, "latest.json")
    _save_json(df, latest_json, ts)
    paths["latest_json"] = latest_json

    latest_csv = os.path.join(OUTPUT_DIR, "latest.csv")
    df.to_csv(latest_csv)
    paths["latest_csv"] = latest_csv

    logger.info(f"Results saved: {paths}")
    return paths


def _save_json(df: pd.DataFrame, path: str, ts: str) -> None:
    """Save results as structured JSON."""
    shortlisted = df[df.get("shortlisted", pd.Series(False, index=df.index)) == True]

    records = []
    for ticker, row in df.iterrows():
        records.append({
            "ticker":           ticker,
            "symbol":           row.get("symbol", ticker),
            "rank":             int(row.get("rank", 999)),
            "quant_score":      round(float(row.get("quant_score", 0)), 2),
            "shortlisted":      bool(row.get("shortlisted", False)),
            "factor_scores": {
                "momentum":   round(float(row.get("momentum_score", 50)), 2),
                "liquidity":  round(float(row.get("liquidity_score", 50)), 2),
                "quality":    round(float(row.get("quality_score", 50)), 2),
                "value":      round(float(row.get("value_score", 50)), 2),
                "technical":  round(float(row.get("technical_score", 50)), 2),
                "fno":        round(float(row.get("fno_score", 50)), 2),
                "events":     round(float(row.get("events_score", 50)), 2),
            },
        })

    output = {
        "run_at":        ts,
        "total_scored":  len(df),
        "shortlist_size": int(shortlisted.shape[0]),
        "stocks":        records,
    }

    with open(path, "w") as f:
        json.dump(output, f, indent=2, default=str)
