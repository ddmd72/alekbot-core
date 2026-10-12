"""
CLI for the drive search eval. Real embeddings (GEMINI_API_KEY from .env) and the real place table.

    venv/bin/python -m scripts.validation.drive_eval [--record] [--report PATH]

Exit codes: 0 pass or recorded, 1 recall fell below a recorded bar, 2 no verdict (malformed gold, did
not settle, no bars, bars without an `overall` recall, bars from another K/tree/gold, a search raised, or
--record with a refused case).
Run it when chat is quiet: about 2,500 embedding calls share the Gemini quota with the bot.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

from .runner import EXIT_SETUP, EvalError, exit_code, run_eval

_REPO = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO / "tests" / "fixtures" / "drive_eval"


def _args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_eval")
    parser.add_argument("--tree", type=Path, default=_FIXTURES / "tree.json")
    parser.add_argument("--gold", type=Path, default=_FIXTURES / "gold_g1.json")
    parser.add_argument("--bars", type=Path, default=_FIXTURES / "bars.json")
    parser.add_argument("--report", type=Path, default=_REPO / "scripts" / "memory" / "drive_eval_report.json")
    parser.add_argument("--record", action="store_true", help="write the bars instead of comparing against them")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _args(argv)
    load_dotenv(_REPO / ".env")
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        print("GEMINI_API_KEY is not set (.env)", file=sys.stderr)
        return EXIT_SETUP

    from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
    from src.adapters.geo_place_table import load_place_rows
    from src.domain.place_lookup import PlaceIndex

    try:
        report = asyncio.run(run_eval(
            args.tree, args.gold, args.bars, args.report,
            embeddings=GeminiEmbeddingAdapter(api_key=key),
            embedding_space=GeminiEmbeddingAdapter.EMBEDDING_SPACE,
            place_of=PlaceIndex(load_place_rows()).lookup, record=args.record))
    except EvalError as exc:
        print(f"drive eval: no verdict — {exc}", file=sys.stderr)
        return EXIT_SETUP
    for scope, row in report["recall"].items():
        print(f"{scope:<16} {int(row['hits']):>3}/{int(row['cases']):<3} recall@{report['k']} = {row['recall']:.3f}")
    for case in report["cases"]:
        if not case["hit"]:
            hard = f" (known hard: {case['known_hard']})" if case.get("known_hard") else ""
            print(f"  miss {case['id']} [{case['case']}] {case['query']!r} status={case['status']}{hard} "
                  f"missing={case.get('missing', [])}")
    if report.get("bars_embedding_space") not in (None, report["embedding_space"]):
        print(f"note: bars were recorded in {report['bars_embedding_space']}, this run embeds in "
              f"{report['embedding_space']}")
    for reg in report.get("regressions", []):
        print(f"REGRESSION {reg['scope']}: {reg['now']:.3f} < bar {reg['bar']:.3f}")
    if report.get("lost_cases"):
        print(f"lost since the bars were recorded: {', '.join(report['lost_cases'])}")
    print(f"verdict: {report['verdict']} (report: {args.report})")
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
