#!/usr/bin/env python3
"""
Stage 1 on memory-review replies (Facts S4 plan T12b, §8 bench 6)
=================================================================
WARNING: this makes PAID model calls (one Stage 1 `consolidate` per case; see the estimate printed first). It writes
nothing live.

Runs the real Stage 1 on the dry-run kit over conversation windows built with the production history shapes
(`review_frame`, `question_text`, `reply_part`, `not_delivered_frame`): a digit reply and a reply in words add nothing for
the quoted fact; a plain later "2" with no marker is read by the ordinary rules (a bare digit states no fact); a correction
adds one fact about the true value; the frame alone, unrelated chat, a not-delivered pair and the old echo control add
nothing. G4: every case matches its rule (judged on the Stage 1 writes of the run; Stage 2 rows, if a write started one,
are in the report).

    python scripts/consolidation/test_review_answers_dryrun.py --s3-kit
    python scripts/consolidation/test_review_answers_dryrun.py --s3-kit --case correction_translated_quote

`--case NAME` (repeatable) runs only the named cases; the estimate printed first scales with their number.

Output: counts and verdicts on the terminal; the full report (the windows and what was written) in
`scripts/memory/s3_dry_run_review_answers_<stamp>.json`.

Pre-conditions: the dotenv file with DEV_USER_ID, DEV_ACCOUNT_ID, FIRESTORE_DATABASE and the provider keys; the S4 prompt
snapshot in `prompts_snapshot/`.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.consolidation.review_bench_cases import AnswerCase, answer_cases, judge, window_messages  # noqa: E402
from scripts.consolidation.review_bench_support import cost_banner  # noqa: E402
from scripts.consolidation.s3_bench_support import (  # noqa: E402
    KitRunFailed,
    add_s3_arguments,
    biographical_facts,
    build_agent,
    execute,
    kit_message,
    kit_session,
    load_env,
)
from scripts.consolidation.s3_dry_run_kit import DryRunKit  # noqa: E402
from src.domain.fact_tool_result import WRITTEN_STATUSES  # noqa: E402
from src.domain.pass_actor import PassActor  # noqa: E402

BENCH = "review_answers"
ANSWER_TASK = "consolidate"     # the task the gate reads
ANSWER_PASS = PassActor.STAGE1.value   # the pass label the gate reads


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Stage 1 on memory-review replies, on the S3 kit (plan bench 6)")
    ap.add_argument("--user-id", default=os.getenv("DEV_USER_ID"))
    ap.add_argument("--account-id", default=os.getenv("DEV_ACCOUNT_ID"))
    ap.add_argument("--case", action="append", default=None, metavar="NAME", choices=[c.name for c in answer_cases()],
                    help="run only this case (repeatable; default: all)")
    add_s3_arguments(ap)
    return ap


def selected_cases(names: Optional[List[str]]) -> List[AnswerCase]:
    """The cases to run: those named (in the bench's order), else all."""
    return [c for c in answer_cases() if not names or c.name in names]


def estimate_text(cases: int) -> str:
    """The banner's estimate: about $0.075 a case on Sonnet, $0.15 on Opus."""
    return f"about ${cases * 0.075:.2f} on Sonnet, ${cases * 0.15:.2f} on Opus"


def build_message(
    kit: DryRunKit, case: AnswerCase, bio_facts: List[Dict[str, Any]], user_id: str, account_id: str, now: datetime,
):
    """The Stage 1 message of one case: a `consolidate` over the case's window."""
    return kit_message(
        kit, sender="review_answers_bench", user_id=user_id, account_id=account_id, task=ANSWER_TASK,
        payload={"messages": window_messages(case, now), "biographical_context": bio_facts},
        session_id="review_answers_bench",
    )


async def stage1_writes(kit: DryRunKit, first_row: int) -> List[Dict[str, str]]:
    """The facts the Stage 1 pass wrote since `first_row`, as `{"action", "text"}` (the stored text of each)."""
    out: List[Dict[str, str]] = []
    for row in kit.rows[first_row:]:
        if row["pass"] != ANSWER_PASS or row.get("status") not in WRITTEN_STATUSES:
            continue
        fact = await kit.repo.get_fact_by_id(row["fact_id"]) if row.get("fact_id") else None
        out.append({"action": row["action"], "text": fact.text if fact is not None else ""})
    return out


async def run_case(
    kit: DryRunKit, handles: Any, case: AnswerCase, bio_facts: List[Dict[str, Any]], args: Any,
) -> Dict[str, Any]:
    kit.new_run()
    first_row = len(kit.rows)
    message = build_message(kit, case, bio_facts, args.user_id, args.account_id, datetime.now(timezone.utc))
    try:
        await execute(handles.agent, message, args.user_id, args.account_id, f"Stage 1 of {case.name}")
    except KitRunFailed:     # the error text may hold fact text: the terminal gets fixed words, the log has the rest
        return {"case": case.name, "rule": case.rule, "ok": False, "why": "the run failed", "writes": []}
    writes = await stage1_writes(kit, first_row)
    ok, why = judge(case, writes)
    return {
        "case": case.name, "rule": case.rule, "ok": ok, "why": why, "writes": writes,
        "window": [{"role": r, "text": t} for r, t in case.window],
        "other_passes": sorted({r["pass"] for r in kit.rows[first_row:] if r["pass"] != ANSWER_PASS}),
    }


def answers_summary(report: Dict[str, Any], extra: Dict[str, Any]) -> List[str]:
    cases = extra.get("cases", [])
    failed = [c for c in cases if not c["ok"]]
    lines = [f"G4 {'FAIL' if failed else 'ok'}: {len(cases) - len(failed)} of {len(cases)} case(s) match their rule"]
    lines += [f"  FAIL {c['case']}: {c['why']}" for c in failed]
    if report.get("spend", {}).get("aborted"):
        lines.append("RUN STOPPED by the spend meter (cap or unpriced model): the cases above are incomplete")
    return lines


async def run_s3_kit(handles: Any, args: Any) -> None:
    cases = selected_cases(getattr(args, "case", None))
    for line in cost_banner("STAGE 1 ON MEMORY-REVIEW REPLIES, ON THE S3 KIT", estimate_text(len(cases))):
        print(line)
    bio_facts = await biographical_facts(handles, args.account_id)
    async with kit_session(
        handles, args, args.user_id, args.account_id, BENCH, summary=answers_summary,
    ) as (kit, extra):
        extra.update({"model": handles.agent.model_name, "cases": []})
        for case in cases:
            result = await run_case(kit, handles, case, bio_facts, args)
            extra["cases"].append(result)
            print(f"  {case.name:20} {'ok' if result['ok'] else 'FAIL'}: {result['why']}")


async def main_s3_kit(args: Any) -> None:
    await run_s3_kit(await build_agent(args.user_id), args)


def main(argv: Optional[List[str]] = None) -> None:
    load_env()
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.s3_kit:
        ap.error("this bench runs on the dry-run kit only: add --s3-kit")
    if not args.user_id or not args.account_id:
        ap.error("set DEV_USER_ID / DEV_ACCOUNT_ID in the dotenv file")
    asyncio.run(main_s3_kit(args))


if __name__ == "__main__":
    main()
