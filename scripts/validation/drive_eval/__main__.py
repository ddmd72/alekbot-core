"""
CLI for the drive search eval. Real embeddings (GEMINI_API_KEY from .env), the real place table and, from Slice 2+4,
the real content ports: the child-process parser, the document reader and the image triage agents with the prompts
in Firestore (content_wiring.py; ANTHROPIC_API_KEY or OPENAI_API_KEY and GCP credentials). The reader and triage calls
are billed to nobody (no quota service) and cost cents per run.

    venv/bin/python -m scripts.validation.drive_eval [--record] [--report PATH] [--gold FILE ...]

Exit codes: 0 pass or recorded, 1 recall fell below a recorded bar, 2 no verdict (malformed gold, did
not settle, no bars, bars without an `overall` recall, bars from another K/tree/gold/files/reader provider, a
search raised, or
--record with a refused case).
Run it when chat is quiet: about 4,000 embedding calls share the Gemini quota with the bot.
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
    parser.add_argument("--gold", type=Path, action="append",
                        help="a gold file; repeatable (default: gold_g1.json and gold_g2.json)")
    parser.add_argument("--bars", type=Path, default=_FIXTURES / "bars.json")
    parser.add_argument("--report", type=Path, default=_REPO / "scripts" / "memory" / "drive_eval_report.json")
    parser.add_argument("--record", action="store_true", help="write the bars instead of comparing against them")
    args = parser.parse_args(argv)
    if not args.gold:
        args.gold = [_FIXTURES / "gold_g1.json", _FIXTURES / "gold_g2.json"]
    return args


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

    from . import content_wiring

    try:
        extractors, triage = content_wiring.build_real_content()
        report = asyncio.run(run_eval(
            args.tree, args.gold, args.bars, args.report,
            embeddings=GeminiEmbeddingAdapter(api_key=key),
            embedding_space=GeminiEmbeddingAdapter.EMBEDDING_SPACE,
            place_of=PlaceIndex(load_place_rows()).lookup, record=args.record,
            extractors=extractors, triage=triage, reader_provider=content_wiring.reader_provider()))
    except content_wiring.ContentSetupError as exc:
        print(f"drive eval: no verdict — {exc}", file=sys.stderr)
        return EXIT_SETUP
    except EvalError as exc:
        print(f"drive eval: no verdict — {exc}", file=sys.stderr)
        return EXIT_SETUP
    for scope, row in report["recall"].items():
        print(f"{scope:<16} {int(row['hits']):>3}/{int(row['cases']):<3} recall@{report['k']} = {row['recall']:.3f}")
    for case in report["cases"]:
        if not case["hit"]:
            hard = f" (known hard: {case['known_hard']})" if case.get("known_hard") else ""
            text = (f" substring={case['substring_hit']} path={case['path_hit']}" if "substring_hit" in case else "")
            print(f"  miss {case['id']} [{case['case']}] {case['query']!r} status={case['status']}{hard} "
                  f"missing={case.get('missing', [])}{text}")
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
