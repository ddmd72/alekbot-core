#!/usr/bin/env python3
"""
Consolidation A/B — Sonnet 4.6 vs Sonnet 5
==========================================
Runs the proven consolidation dry-run harness (scripts/email/test_consolidation_dryrun.py)
twice over the SAME input batch — once on claude-sonnet-4-6, once on claude-sonnet-5 — by
toggling the CLAUDE_PERFORMANCE_MODEL env var (the same rollback flag used in production).
Nothing is written (the dry-run intercepts all fact writes); this is safe to run repeatedly.

The dry-run prints a per-model summary (create / update / merge / discard counts, facts
processed, elapsed). This wrapper captures each run's full output to a gitignored file under
scripts/memory/ (PII-safe per repo policy) and prints the two summaries side by side so you can
eyeball the quality delta before flipping traffic.

Usage:
    python scripts/consolidation/ab_sonnet5.py --limit 50
    python scripts/consolidation/ab_sonnet5.py --limit 50 --category healthcare
    # any extra flags are passed straight through to test_consolidation_dryrun.py

Pre-conditions (same as the underlying dry-run):
    .env with DEV_USER_ID, DEV_ACCOUNT_ID, FIRESTORE_DATABASE, ANTHROPIC/GEMINI keys.
    A facts file produced by test_email_classification_poc.py (--save).

NOTE: this makes real LLM calls on both models — it spends API budget. Run it deliberately.
"""

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_DRYRUN = _ROOT / "scripts" / "email" / "test_consolidation_dryrun.py"
_OUT_DIR = _ROOT / "scripts" / "memory"  # gitignored — safe for PII-bearing output

_MODELS = ("claude-sonnet-4-6", "claude-sonnet-5")


def _run(model: str, passthrough: list[str]) -> str:
    """Run the dry-run harness pinned to `model`; capture + persist its output."""
    import os

    env = dict(os.environ, CLAUDE_PERFORMANCE_MODEL=model)
    print(f"\n=== Running consolidation dry-run on {model} ===", flush=True)
    proc = subprocess.run(
        [sys.executable, str(_DRYRUN), *passthrough],
        env=env,
        capture_output=True,
        text=True,
    )
    out = proc.stdout + ("\n[STDERR]\n" + proc.stderr if proc.stderr.strip() else "")
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = _OUT_DIR / f"ab_consolidation_{model.replace('.', '_')}.txt"
    dest.write_text(out)
    print(f"  → full output saved to {dest.relative_to(_ROOT)} (rc={proc.returncode})")
    return out


def _summary_tail(output: str, n: int = 30) -> str:
    """Return the trailing summary block of a dry-run output."""
    lines = output.rstrip().splitlines()
    return "\n".join(lines[-n:])


def main() -> None:
    passthrough = sys.argv[1:]
    results = {model: _run(model, passthrough) for model in _MODELS}

    print("\n" + "=" * 78)
    print("A/B SUMMARY — compare create/update/merge/discard counts + facts processed")
    print("=" * 78)
    for model in _MODELS:
        print(f"\n----- {model} -----")
        print(_summary_tail(results[model]))
    print(
        "\nEyeball the two summaries: comparable fact coverage and dedup decisions (fewer "
        "spurious 'create' duplicates, sane 'merge'/'discard') means Sonnet 5 is a safe swap. "
        "If Sonnet 5 regresses, roll back with CLAUDE_PERFORMANCE_MODEL=claude-sonnet-4-6."
    )


if __name__ == "__main__":
    main()
