#!/usr/bin/env python3
"""
BigQuery usage/cost report — daily + per-model, with the cost decomposed into
uncached-input / output / cache so model comparisons aren't distorted by cheap
cache-read volume (cache reads are 90% off, so total_tokens alone is misleading).

Reads the canonical pricing from src.domain.billing so the cost math here stays
1:1 with the production calculate_cost(). Account / project / dataset come from the
environment (secrets rule — no infra IDs hardcoded in tracked files).

Usage:
    python scripts/billing/usage_report.py [--days 14] [--account <id>]

Env:
    GOOGLE_CLOUD_PROJECT     GCP project (required)
    BIGQUERY_PROMPT_DATASET  dataset (default: alek_observability_dev)
    BIGQUERY_PROMPT_TABLE    table   (default: prompt_content)
    DEV_ACCOUNT_ID           account to report on (default account)
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.domain.billing import _PRICING_PER_MILLION_TOKENS  # noqa: E402


def build_pricing_cte(pricing: dict) -> str:
    """Render the pricing dict as a BigQuery CTE of (model, inp, outp, cr, cw) rows.

    inp/outp = input/output $ per million; cr/cw = cache-read/cache-write multipliers
    applied to the input price — exactly the factors calculate_cost() uses.
    """
    rows = []
    for model, p in pricing.items():
        safe = model.replace('"', '')  # model ids are our own constants; defensive only
        rows.append(
            f'    STRUCT("{safe}" AS model, {p["input"]} AS inp, {p["output"]} AS outp, '
            f'{p.get("cache_read", 0)} AS cr, {p.get("cache_write", 0)} AS cw)'
        )
    return "WITH pricing AS (\n  SELECT * FROM UNNEST([\n" + ",\n".join(rows) + "\n  ])\n)"


# Cost expression shared by both reports — mirrors calculate_cost() term-for-term.
_COST_EXPR = (
    "d.prompt_tokens/1e6*p.inp"
    " + d.completion_tokens/1e6*p.outp"
    " + d.cache_read_tokens/1e6*p.inp*p.cr"
    " + d.cache_creation_tokens/1e6*p.inp*p.cw"
)


def _table_fqn() -> str:
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    dataset = os.environ.get("BIGQUERY_PROMPT_DATASET", "alek_observability_dev")
    table = os.environ.get("BIGQUERY_PROMPT_TABLE", "prompt_content")
    return f"{project}.{dataset}.{table}"


def _data_cte(fqn: str) -> str:
    return (
        f"data AS (\n"
        f"  SELECT DATE(timestamp) AS day, model, prompt_tokens, completion_tokens,\n"
        f"         cache_read_tokens, cache_creation_tokens, total_tokens\n"
        f"  FROM `{fqn}`\n"
        f"  WHERE account_id = @account\n"
        f"    AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)\n"
        f")"
    )


def daily_query(pricing: dict, fqn: str) -> str:
    return f"""{build_pricing_cte(pricing)},
{_data_cte(fqn)}
SELECT d.day,
  COUNT(*) AS calls,
  SUM(d.total_tokens) AS tokens,
  ROUND(SUM(d.prompt_tokens/1e6*p.inp), 4) AS in_cost,
  ROUND(SUM(d.completion_tokens/1e6*p.outp), 4) AS out_cost,
  ROUND(SUM(d.cache_read_tokens/1e6*p.inp*p.cr + d.cache_creation_tokens/1e6*p.inp*p.cw), 4) AS cache_cost,
  ROUND(SUM({_COST_EXPR}), 4) AS cost
FROM data d LEFT JOIN pricing p USING(model)
GROUP BY day ORDER BY day
"""


def model_query(pricing: dict, fqn: str) -> str:
    return f"""{build_pricing_cte(pricing)},
{_data_cte(fqn)}
SELECT d.model,
  COUNT(*) AS calls,
  SUM(d.prompt_tokens) AS in_tok,
  SUM(d.completion_tokens) AS out_tok,
  SUM(d.cache_read_tokens) AS cache_tok,
  ROUND(SUM(d.prompt_tokens/1e6*p.inp), 4) AS in_cost,
  ROUND(SUM(d.completion_tokens/1e6*p.outp), 4) AS out_cost,
  ROUND(SUM(d.cache_read_tokens/1e6*p.inp*p.cr + d.cache_creation_tokens/1e6*p.inp*p.cw), 4) AS cache_cost,
  ROUND(SUM({_COST_EXPR}), 4) AS cost,
  COUNTIF(p.model IS NULL) AS unpriced_rows
FROM data d LEFT JOIN pricing p USING(model)
GROUP BY d.model ORDER BY cost DESC
"""


def _run(query: str, account: str, days: int, project: str) -> list:
    """Run a parameterized query via the `bq` CLI (gcloud SDK) and return rows as dicts.

    Shelling out to `bq` keeps this a zero-setup local tool — it needs only the
    gcloud SDK, not the google-cloud-bigquery Python lib. Params are bound by bq
    (no string interpolation of user input).
    """
    cmd = [
        "bq", "query", "--use_legacy_sql=false", "--format=json",
        f"--project_id={project}",
        f"--parameter=account:STRING:{account}",
        f"--parameter=days:INT64:{int(days)}",
        query,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"bq query failed:\n{out.stderr.strip()}")
    return json.loads(out.stdout or "[]")


def _i(v) -> int:
    return int(v or 0)


def _f(v) -> float:
    return float(v or 0.0)


def main() -> None:
    ap = argparse.ArgumentParser(description="BigQuery usage/cost report")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--account", default=os.environ.get("DEV_ACCOUNT_ID"))
    args = ap.parse_args()

    if not args.account:
        sys.exit("No account: pass --account or set DEV_ACCOUNT_ID in the environment.")

    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    fqn = _table_fqn()

    print(f"Usage report — account {args.account[:16]}…  last {args.days} days\n")

    daily = _run(daily_query(_PRICING_PER_MILLION_TOKENS, fqn), args.account, args.days, project)
    print(f"{'day':<12}{'calls':>6}{'tokens':>12}{'in$':>9}{'out$':>9}{'cache$':>9}{'cost$':>9}")
    tot = [0, 0, 0.0, 0.0, 0.0, 0.0]
    for r in daily:
        print(f"{str(r['day']):<12}{_i(r['calls']):>6}{_i(r['tokens']):>12,}"
              f"{_f(r['in_cost']):>9.3f}{_f(r['out_cost']):>9.3f}{_f(r['cache_cost']):>9.3f}{_f(r['cost']):>9.3f}")
        tot = [tot[0] + _i(r['calls']), tot[1] + _i(r['tokens']),
               tot[2] + _f(r['in_cost']), tot[3] + _f(r['out_cost']),
               tot[4] + _f(r['cache_cost']), tot[5] + _f(r['cost'])]
    print("-" * 66)
    print(f"{'TOTAL':<12}{tot[0]:>6}{tot[1]:>12,}{tot[2]:>9.3f}{tot[3]:>9.3f}{tot[4]:>9.3f}{tot[5]:>9.3f}\n")

    models = _run(model_query(_PRICING_PER_MILLION_TOKENS, fqn), args.account, args.days, project)
    print(f"{'model':<28}{'calls':>6}{'in_tok':>11}{'out_tok':>10}{'cache_tok':>11}"
          f"{'in$':>8}{'out$':>8}{'cache$':>8}{'cost$':>8}")
    for r in models:
        flag = "  ⚠ no price" if _i(r['unpriced_rows']) else ""
        print(f"{r['model']:<28}{_i(r['calls']):>6}{_i(r['in_tok']):>11,}{_i(r['out_tok']):>10,}"
              f"{_i(r['cache_tok']):>11,}{_f(r['in_cost']):>8.3f}{_f(r['out_cost']):>8.3f}"
              f"{_f(r['cache_cost']):>8.3f}{_f(r['cost']):>8.3f}{flag}")


if __name__ == "__main__":
    main()
