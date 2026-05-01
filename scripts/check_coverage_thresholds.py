#!/usr/bin/env python3
"""
Per-file coverage gate.

Reads ``coverage.json`` produced by ``pytest --cov=src --cov-report=json``
and asserts each file in ``THRESHOLDS`` meets its required coverage. Files
outside ``THRESHOLDS`` are NOT checked individually — they fall under the
global ``--cov-fail-under`` enforced by pytest itself.

Mandate source: docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md § 8.4.

Why a script and not pytest-cov flags:
  - pytest-cov supports a single global ``--cov-fail-under`` only.
  - Per-file thresholds matter here because the RFC names specific files
    that were the source of the 2026-04-30 incident; their coverage is
    load-bearing while other files have legitimate pre-existing gaps.
  - This script is the single source of truth for those per-file
    thresholds. Editing the table here is the only way to weaken a
    guarantee — the RFC line and CI gate stay in sync.

Exit codes:
  0 — all thresholds met
  1 — one or more violations OR coverage.json missing
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# Per-file thresholds
# ---------------------------------------------------------------------------
#
# Format: relative path → required coverage percent (0–100).
# Use 100.0 for "no uncovered lines allowed" (mandate § 8.4 lists three).
# Use 95.0 for files where the mandate says "≥95% on adapter".
# Use a slightly lower value for files with documented pre-existing gaps
# that are out of scope for the RFC; document the gap inline.
#
# DO NOT lower a threshold without:
#   1. A line in the RFC explaining why coverage of that path was
#      deliberately reduced.
#   2. The user's explicit per-file approval (CLAUDE.md test rule).

THRESHOLDS: Dict[str, float] = {
    # Strict 100% — the three files at the centre of the 2026-04-30 incident.
    "src/services/user_notification_service.py": 100.0,
    "src/services/reminders_service.py":          100.0,
    "src/agents/core/smart_response_agent.py":    100.0,

    # ≥95% per mandate; current actual ≈ 98%.
    "src/adapters/firestore_agent_note_adapter.py": 95.0,
    "src/infrastructure/task_execution_resolver.py": 100.0,

    # New domain / infrastructure value objects added by the RFC. All
    # achieved 100% on first commit and have no infrastructure surface
    # that justifies a lower bar.
    "src/domain/notification_kind.py":      100.0,
    "src/domain/notify_result.py":          100.0,
    "src/domain/agent_note.py":             100.0,
    "src/infrastructure/notification_sla.py": 100.0,
    "src/ports/agent_note_port.py":         100.0,

    # Floor reflecting current state (post-RFC actual ≈ 91%). Pre-existing
    # uncovered branches: dispatch lines unexercised by direct method-call
    # tests, deep_research_polling retry path, daily_email_review failure
    # path. Lifting this floor requires its own RFC.
    "src/handlers/worker_handler.py": 90.0,
}


COVERAGE_JSON = os.environ.get("COVERAGE_JSON_PATH", "coverage.json")


def _normalise_path(p: str) -> str:
    """coverage.py emits absolute paths; we compare against repo-relative."""
    cwd = os.getcwd().rstrip(os.sep) + os.sep
    if p.startswith(cwd):
        return p[len(cwd):]
    return p


def load_coverage() -> Dict[str, dict]:
    """Return ``{relative_path: file_summary}`` from coverage.json.

    Exits with non-zero if the file is missing — the caller forgot to
    run pytest with ``--cov=src --cov-report=json`` first.
    """
    if not os.path.isfile(COVERAGE_JSON):
        print(
            f"❌ {COVERAGE_JSON} not found. Run pytest with "
            f"`--cov=src --cov-report=json` before this script.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(COVERAGE_JSON) as f:
        report = json.load(f)

    files = report.get("files", {})
    out: Dict[str, dict] = {}
    for raw_path, file_data in files.items():
        out[_normalise_path(raw_path)] = file_data
    return out


def check_thresholds(
    coverage: Dict[str, dict],
    thresholds: Dict[str, float],
) -> Tuple[List[Tuple[str, float, float]], List[str]]:
    """Return (violations, missing).

    violations: [(path, required, actual)]
    missing:    [path] — file in THRESHOLDS but not in coverage.json,
                e.g. file deleted or not exercised by any test.
    """
    violations: List[Tuple[str, float, float]] = []
    missing: List[str] = []

    for path, required in thresholds.items():
        file_data = coverage.get(path)
        if file_data is None:
            missing.append(path)
            continue
        # coverage.py reports percent_covered as float 0–100.
        actual = float(file_data.get("summary", {}).get("percent_covered", 0.0))
        # Allow tiny float-rounding tolerance below the threshold.
        if actual + 1e-6 < required:
            violations.append((path, required, actual))
    return violations, missing


def main() -> int:
    coverage = load_coverage()
    violations, missing = check_thresholds(coverage, THRESHOLDS)

    if not violations and not missing:
        print(
            f"✅ Coverage gate: all {len(THRESHOLDS)} thresholds met."
        )
        return 0

    if violations:
        print("❌ Coverage threshold violations:", file=sys.stderr)
        for path, required, actual in violations:
            print(
                f"   {path}: {actual:.2f}% < required {required:.1f}%",
                file=sys.stderr,
            )

    if missing:
        print(
            "❌ Files listed in THRESHOLDS but absent from coverage.json:",
            file=sys.stderr,
        )
        for path in missing:
            print(f"   {path}", file=sys.stderr)
        print(
            "\nIf the file was renamed/deleted, update THRESHOLDS in "
            "scripts/check_coverage_thresholds.py.",
            file=sys.stderr,
        )

    return 1


if __name__ == "__main__":
    sys.exit(main())
