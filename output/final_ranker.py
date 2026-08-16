"""
Final Ranker.

For Stage 1 (scoring engine only), this module:
  1. Takes the full scored DataFrame from the Scoring Engine
  2. Saves JSON and CSV reports
  3. Renders the HTML dashboard

When the Agent Research Pipeline (Stage 2) is added, this module will
combine quant_score + agent_score into a final composite ranking.
"""

import os
import json
import logging
from datetime import datetime

import pandas as pd
from jinja2 import Environment, FileSystemLoader

from config.settings import OUTPUT_DIR, TEMPLATE_DIR

logger = logging.getLogger(__name__)


def save_results(
    df: pd.DataFrame,
    run_ts: str | None = None,
) -> dict[str, str]:
    """
    Save scored results to JSON, CSV, and HTML.

    Returns dict of file paths that were written.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = run_ts or datetime.now().strftime("%Y%m%d_%H%M%S")

    paths: dict[str, str] = {}

    # ── JSON ────────────────────────────────────────────────────────
    json_path = os.path.join(OUTPUT_DIR, f"scores_{ts}.json")
    _save_json(df, json_path, ts)
    paths["json"] = json_path

    # ── latest.json (always overwrite for easy consumption) ─────────
    latest_json = os.path.join(OUTPUT_DIR, "latest.json")
    _save_json(df, latest_json, ts)
    paths["latest_json"] = latest_json

    # ── CSV ─────────────────────────────────────────────────────────
    csv_path = os.path.join(OUTPUT_DIR, f"scores_{ts}.csv")
    df.to_csv(csv_path)
    paths["csv"] = csv_path

    # ── HTML Dashboard ───────────────────────────────────────────────
    html_path = os.path.join(OUTPUT_DIR, "dashboard.html")
    _render_dashboard(df, html_path, ts)
    paths["html"] = html_path

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


def _render_dashboard(df: pd.DataFrame, html_path: str, ts: str) -> None:
    """Render the Jinja2 HTML dashboard."""
    template_file = "dashboard.html"
    if not os.path.exists(os.path.join(TEMPLATE_DIR, template_file)):
        logger.warning(f"Dashboard template not found at {TEMPLATE_DIR}")
        return

    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    tmpl = env.get_template(template_file)

    shortlist_df = df[df.get("shortlisted", pd.Series(False, index=df.index)) == True]
    all_df = df.head(50)  # show top 50 in full table

    def row_to_dict(row):
        return {
            "ticker":          row.name,
            "symbol":          row.get("symbol", row.name),
            "rank":            int(row.get("rank", 999)),
            "quant_score":     round(float(row.get("quant_score", 0)), 1),
            "shortlisted":     bool(row.get("shortlisted", False)),
            "momentum_score":  round(float(row.get("momentum_score", 50)), 1),
            "liquidity_score": round(float(row.get("liquidity_score", 50)), 1),
            "quality_score":   round(float(row.get("quality_score", 50)), 1),
            "value_score":     round(float(row.get("value_score", 50)), 1),
            "technical_score": round(float(row.get("technical_score", 50)), 1),
            "fno_score":       round(float(row.get("fno_score", 50)), 1),
            "events_score":    round(float(row.get("events_score", 50)), 1),
        }

    shortlist_records = [row_to_dict(row) for _, row in shortlist_df.iterrows()]
    all_records       = [row_to_dict(row) for _, row in all_df.iterrows()]

    html = tmpl.render(
        run_at=ts,
        total_scored=len(df),
        shortlist=shortlist_records,
        all_stocks=all_records,
    )

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(f"Dashboard rendered -> {html_path}")
