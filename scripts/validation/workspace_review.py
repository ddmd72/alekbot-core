"""
Agent workspace review (docs/10_rfcs/AGENT_WORKSPACE_RFC.md §16). Owner-run and read-only, four weeks after deploy
and before day 28 (prompt_content keeps 30 days).

Reads 28 days of `[Workspace]` lines straight from Cloud Logging (`gcloud logging read --freshness=28d`; not
`make fetch-logs`, which keeps only the last K entries) and Smart's `prompt_content` rows of the traces that called the
workspace or fired a reminder with a workspace file (`bq query`), then writes the §16 measures. The report names
workspace paths, so it is written only under scripts/memory/ (gitignored).

Usage (project and credentials from the environment / ADC):
    python3 -m scripts.validation.workspace_review [--out scripts/memory/workspace_review.md]
Env:
    GOOGLE_CLOUD_PROJECT     GCP project (required)
    BIGQUERY_PROMPT_DATASET  dataset (required)
    BIGQUERY_PROMPT_TABLE    table (default: prompt_content)
Needs the gcloud and bq CLIs (gcloud SDK).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

from scripts.validation.workspace_review_logic import LogLine, build_traces, parse_line, render_report, review

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_OUT = _REPO / "scripts" / "memory" / "workspace_review.md"
_LOG_FILTER = 'resource.type="cloud_run_revision" AND textPayload:"[Workspace]"'


def _project() -> str:
    return os.environ["GOOGLE_CLOUD_PROJECT"]


def _table_fqn() -> str:
    table = os.environ.get("BIGQUERY_PROMPT_TABLE", "prompt_content")
    return f"{_project()}.{os.environ['BIGQUERY_PROMPT_DATASET']}.{table}"


def _run(cmd: List[str]) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"{cmd[0]} failed:\n{out.stderr.strip()}")
    return out.stdout


def fetch_log_lines() -> List[LogLine]:
    entries = json.loads(_run([
        "gcloud", "logging", "read", _LOG_FILTER, "--freshness=28d", "--format=json", "--limit=200000",
        f"--project={_project()}",
    ]) or "[]")
    lines = [parse_line(str(e.get("timestamp", "")), str(e.get("textPayload", ""))) for e in entries]
    return sorted((line for line in lines if line is not None), key=lambda line: line.timestamp)


def build_query(fqn: str) -> str:
    window = "agent_type LIKE '%smart%' AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 28 DAY)"
    return f"""
WITH hits AS (
  SELECT DISTINCT trace_id FROM `{fqn}`
  WHERE {window}
    AND (tool_calls LIKE '%"workspace"%' OR request_text LIKE '%Workspace file: workspace:%')
)
SELECT trace_id, FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', timestamp) AS timestamp, turn, request_text, tool_calls
FROM `{fqn}`
WHERE {window} AND trace_id IN (SELECT trace_id FROM hits)
ORDER BY timestamp, turn
"""


def fetch_rows() -> List[Dict[str, Any]]:
    return json.loads(_run([
        "bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=200000",
        f"--project_id={_project()}", build_query(_table_fqn()),
    ]) or "[]")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Agent workspace review (AGENT_WORKSPACE_RFC §16)")
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    args = parser.parse_args()
    since = (datetime.now(timezone.utc) - timedelta(days=28)).strftime("%Y-%m-%d")
    report = render_report(review(fetch_log_lines(), build_traces(fetch_rows())), since)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(report)
    print(f"Written to {args.out}")


if __name__ == "__main__":
    main()
