#!/usr/bin/env python3
"""
Memory-review dry run (Facts S4 plan T12b, §8 bench 7)
======================================================
WARNING: this makes PAID model calls (the review pass; see the estimate printed first). It writes nothing live.

Runs the REAL `FactReviewService.run` (daily) on the dry-run kit: the live stale facts (the 2 legacy plans), `--stage N`
live facts staled in the kit's overlay, and the seeded cases (a plan whose date has passed, a revive with no basis, two
stale duplicates, an undecidable fact, and three questions with the owner's reply in words: unrelated, "yes, still the
case", "stopped in October"). The review service, the answer service, the question store, the lease, the queue, the
platform and the notifier are the kit's stand-ins; the agent is the kit-installed `ConsolidationAgent`, called through
`execute` (never `process`). The owner's primary channel and language are read live, once per run.

    python scripts/consolidation/test_review_dryrun.py --s3-kit --stage 18 --runs 2
    python scripts/consolidation/test_review_dryrun.py --s3-kit --stage 4 --runs 1 --mirror ru

`--stage N` is a ceiling: one batch holds at most 20 facts, so the live facts staged are what is left after the live stale
facts and the seeded cases. `--runs R` replays the same input R times, each from the live state (the model's dispersion).
`--mirror CODE` turns mirror mode on with no fixed language and checks the language the review reports from the owner's own
recent messages against CODE (it is the owner's messages that decide, not the flag).

Output: counts and gate lines on the terminal; the full report (fact text, outcomes, question texts) in
`scripts/memory/s3_dry_run_review_<stamp>.json`.

Pre-conditions: the dotenv file with DEV_USER_ID, DEV_ACCOUNT_ID, FIRESTORE_DATABASE and the provider keys; the S4 prompt
snapshot in `prompts_snapshot/`.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.consolidation.review_bench_support import (  # noqa: E402
    cost_banner,
    estimate_usd,
    review_summary,
    run_counts,
    run_once,
)
from scripts.consolidation.s3_bench_support import add_s3_arguments, build_agent, kit_session, load_env  # noqa: E402
from src.domain.fact_review_rules import REVIEW_BATCH  # noqa: E402

BENCH = "review"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Memory review dry run on the S3 kit (plan bench 7)")
    ap.add_argument("--stage", type=int, default=18, help="live facts to stale in the kit (a ceiling: one batch is 20)")
    ap.add_argument("--runs", type=int, default=2, help="how many times to replay the same input")
    ap.add_argument("--mirror", default=None, metavar="CODE", help="mirror mode on; the language expected from his messages")
    ap.add_argument("--user-id", default=os.getenv("DEV_USER_ID"))
    ap.add_argument("--account-id", default=os.getenv("DEV_ACCOUNT_ID"))
    add_s3_arguments(ap)
    return ap


def header(args: Any) -> List[str]:
    estimate = estimate_usd(REVIEW_BATCH, args.runs)
    return cost_banner(
        f"MEMORY REVIEW ON THE S3 KIT: {args.runs} run(s), --stage {args.stage}, up to {REVIEW_BATCH} facts a batch",
        f"about ${estimate['sonnet']:.2f} on Sonnet, ${estimate['opus']:.2f} on Opus",
    )


async def run_s3_kit(handles: Any, live_state: Any, args: Any) -> None:
    for line in header(args):
        print(line)
    async with kit_session(
        handles, args, args.user_id, args.account_id, BENCH, with_review=True, summary=review_summary,
    ) as (kit, extra):
        extra.update({"model": handles.agent.model_name, "stage": args.stage, "mirror": args.mirror, "runs": []})
        for number in range(1, args.runs + 1):
            run = await run_once(
                kit, handles, live_state, user_id=args.user_id, account_id=args.account_id, requested=args.stage,
                mirror=args.mirror,
            )
            extra["runs"].append(run)
            print(f"\n  run {number}")
            for line in run_counts(run, kit):
                print(f"    {line}")


async def main_s3_kit(args: Any) -> None:
    from src.adapters.firestore_notification_state_adapter import FirestoreNotificationStateAdapter

    handles = await build_agent(args.user_id)
    await run_s3_kit(handles, FirestoreNotificationStateAdapter(handles.db, handles.config["ENVIRONMENT_CONFIG"]), args)


def main(argv: Optional[List[str]] = None) -> None:
    load_env()
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.s3_kit:
        ap.error("this bench runs on the dry-run kit only: add --s3-kit")
    if not args.user_id or not args.account_id:
        ap.error("set DEV_USER_ID / DEV_ACCOUNT_ID in the dotenv file")
    if args.runs < 1 or args.stage < 0:
        ap.error("--runs is at least 1 and --stage at least 0")
    asyncio.run(main_s3_kit(args))


if __name__ == "__main__":
    main()
