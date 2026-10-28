"""
The passage-recall eval (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28, plan S1.3): does `DriveSearchService.excerpts`
return the passages the G2 cases need?

Each G2 case asks its query as the passages question (its `folder` filter as the scope) against the fixture index and
hits when every expected path is among the returned files and its expected substring is in that file's returned text
(adjacent passages merged and markdown escapes undone, as the model reads them). The window G2 eval (`runner.py`) is
untouched; this module has its own bars (`bars_excerpts.json`, written by hand): recall at least the window's G2
recall, and no case that is `true` in `bars.json` misses.

The index is the G2 fixture's, built by the production tick with the real parser, reader and triage, and cached
(`index_cache.load_or_build`, only `tree.json`). A cache hit costs the question embeddings only (one per G2 case); a
miss also runs the reader models. `--probe` says which it will be and calls nothing.

    venv/bin/python -m scripts.validation.drive_eval.excerpts_eval [--probe] [--rebuild-index] [--report PATH]

Exit codes: 0 pass, 1 recall below the bars, 2 no verdict (setup, malformed gold, bars that cannot judge).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_excerpt_request import excerpt_request
from src.domain.drive_excerpt_result import ExcerptResult
from src.domain.drive_excerpts import display_text, merge_runs
from src.ports.embedding_service import EmbeddingService
from src.services.drive_search_service import DriveSearchService

from .fixture_drive import FixtureDrive
from .index_cache import cache_key, load_or_build
from .metrics import case_hit, expected_substrings, substring_hit
from .runner import (
    EVAL_USER, EXIT_OK, EXIT_REGRESSION, EXIT_SETUP, EvalClock, EvalError, _EnabledUsers, gold_sha256, index_fixture,
    load_gold, sha256_of, validate_gold,
)

_REPO = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO / "tests" / "fixtures" / "drive_eval"
TEXT_REPORT_CHARS = 400


def bars_mismatch(bars: Mapping[str, Any], *, tree_sha256: str, gold_sha: str, case_ids: Sequence[str],
                  window_bars: Mapping[str, Any]) -> List[str]:
    """Why these bars cannot judge this run (empty: they can): another tree or gold file, a must-hit case that is not
    in the gold, or a window `bars.json` that is not the one the must-hit list was written from."""
    reasons: List[str] = []
    if bars.get("tree_sha256") != tree_sha256:
        reasons.append("tree.json changed since the excerpt bars were written")
    if bars.get("gold_sha256") != gold_sha:
        reasons.append("the gold file changed since the excerpt bars were written")
    if not isinstance(bars.get("recall_min"), (int, float)):
        reasons.append("the excerpt bars have no recall_min")
    must = list(bars.get("must_hit") or ())
    unknown = sorted(set(must) - set(case_ids))
    if unknown:
        reasons.append(f"must_hit names cases that are not in the gold: {unknown}")
    if bars.get("window_bars_recorded_at") != window_bars.get("recorded_at"):
        reasons.append("bars.json was re-recorded since the excerpt bars were written")
    window_true = sorted(cid for cid, hit in (window_bars.get("cases") or {}).items() if hit is True and cid in case_ids)
    if window_true != sorted(must):
        reasons.append("must_hit differs from the G2 cases that are true in bars.json")
    return reasons


def returned_texts(result: ExcerptResult, drive: FixtureDrive) -> Dict[str, str]:
    """{path: the text the model would read for it}: a file's passages merged into runs, escapes undone; an identical
    copy (`also_at`) reads the same text."""
    out: Dict[str, str] = {}
    for file in result.files:
        runs = merge_runs(file.chunks)
        text = "\n".join(display_text(run.text, file.kind) for run in runs)
        own = drive.path_of(file.item_id) or file.path
        for path in (own, file.path, *file.also_at):
            out.setdefault(str(path).strip("/"), text)
    return out


async def run_case(search: DriveSearchService, drive: FixtureDrive, case: Mapping[str, Any]) -> Dict[str, Any]:
    filters = case.get("filters") or {}
    row: Dict[str, Any] = {"id": case["id"], "case": case["case"], "query": case.get("query", ""),
                           "expected": list(case["expected_paths"])}
    try:
        request = excerpt_request(case.get("query"), filters.get("folder") or "")
        result = await search.excerpts(EVAL_USER, request)
    except Exception as exc:   # a crash is a finding, not a reason to lose the other cases
        row.update(status=f"exception: {type(exc).__name__}: {exc}", hit=False, error=True)
        return row
    texts = returned_texts(result, drive)
    wanted = expected_substrings(case["expected_paths"], case.get("expected_substring"))
    path_hit = result.status == "ok" and case_hit(case["expected_paths"], set(texts), len(case["expected_paths"]),
                                                  case.get("min_hits"))
    found = substring_hit(wanted, texts)
    row.update(status=result.status, path_hit=path_hit, substring_hit=found, hit=path_hit and found,
               returned=sorted(texts), passages=sum(len(f.chunks) for f in result.files),
               texts={p: texts.get(p, "")[:TEXT_REPORT_CHARS] for p in wanted})
    return row


def judge(rows: Sequence[Mapping[str, Any]], bars: Mapping[str, Any]) -> Dict[str, Any]:
    hits = sum(1 for r in rows if r["hit"])
    recall = hits / len(rows) if rows else 0.0
    lost = sorted(cid for cid in bars.get("must_hit", ()) if not next((r["hit"] for r in rows if r["id"] == cid), False))
    below = recall < float(bars["recall_min"]) - 1e-9
    errors = sum(1 for r in rows if r.get("error"))
    verdict = "search_errors" if errors else ("regression" if below or lost else "pass")
    return {"cases": len(rows), "hits": hits, "recall": round(recall, 6), "recall_min": bars["recall_min"],
            "lost_cases": lost, "search_errors": errors, "verdict": verdict}


def exit_code(report: Mapping[str, Any]) -> int:
    return {"pass": EXIT_OK, "regression": EXIT_REGRESSION}.get(report["verdict"], EXIT_SETUP)


Content = Tuple[Dict[str, Any], Any]


async def evaluate(*, tree: Path, gold_paths: Sequence[Path], bars_path: Path, window_bars_path: Path, snapshot: Path,
                   embeddings: EmbeddingService, embedding_space: str, place_of: Callable[[float, float], Any],
                   build_content: Callable[[], Content], cache_dir: Optional[Path] = None, rebuild: bool = False
                   ) -> Dict[str, Any]:
    """Judge the bars first (a mismatch refuses before anything is built), then load or build the index, ask every
    gold case and score. `build_content` returns (extractors, triage) and is called only on a cache miss."""
    drive = FixtureDrive.from_file(tree)
    gold = load_gold(list(gold_paths))
    validate_gold(gold, drive.paths())
    bars = json.loads(bars_path.read_text(encoding="utf-8"))
    window_bars = json.loads(window_bars_path.read_text(encoding="utf-8"))
    mismatch = bars_mismatch(bars, tree_sha256=sha256_of(tree), gold_sha=gold_sha256(list(gold_paths)),
                             case_ids=[c["id"] for c in gold], window_bars=window_bars)
    if mismatch:
        raise EvalError("; ".join(mismatch) + " — review the change, then rewrite bars_excerpts.json by hand")
    if bars.get("embedding_space") != embedding_space:
        raise EvalError(f"the bars are for {bars.get('embedding_space')}, this run embeds in {embedding_space}")
    clock = EvalClock()

    async def build() -> Tuple[InMemoryDriveIndexRepository, Dict[str, Any]]:
        extractors, triage = build_content()
        repo = InMemoryDriveIndexRepository()
        out = await index_fixture(drive, repo, embeddings, place_of, embedding_space, clock,
                                  extractors=extractors, triage=triage)   # type: ignore[arg-type]
        return repo, {"ticks": out.ticks, "records": out.records, "misses": {}}

    repo, facts = await load_or_build(build, trees=(tree,), snapshot=snapshot, drive=drive,
                                      embedding_space=embedding_space, clock=clock, rebuild=rebuild,
                                      cache_dir=cache_dir)
    if not (facts.get("records") or {}).get("carded"):
        raise EvalError("the index has no card; nothing to measure")
    search = DriveSearchService(repo, embeddings, _EnabledUsers(), drive, clock=clock, embedding_space=embedding_space)
    rows = [await run_case(search, drive, case) for case in gold]
    report: Dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "embedding_space": embedding_space, "index_cache": facts.get("cache"),
                              **judge(rows, bars), "rows": rows}
    return report


def probe(tree: Path, snapshot: Path, embedding_space: str, cache_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Offline: the cache key of the G2 index and whether a paid rebuild is due. Calls no model."""
    from .index_cache import _CACHE_DIR
    drive = FixtureDrive.from_file(tree)
    key = cache_key((tree,), snapshot, drive, embedding_space)
    path = (cache_dir or _CACHE_DIR) / f"index_{key}.pickle"
    return {"key": key, "path": str(path), "cached": path.exists()}


def _args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_eval.excerpts_eval",
                                     description="Passage recall of DriveSearchService.excerpts over the G2 cases. "
                                                 "A cache miss runs the reader models; --probe calls nothing.")
    parser.add_argument("--tree", type=Path, default=_FIXTURES / "tree.json")
    parser.add_argument("--gold", type=Path, default=_FIXTURES / "gold_g2.json")
    parser.add_argument("--bars", type=Path, default=_FIXTURES / "bars_excerpts.json")
    parser.add_argument("--window-bars", type=Path, default=_FIXTURES / "bars.json")
    parser.add_argument("--snapshot", type=Path, default=_FIXTURES / "enrichment.json")
    parser.add_argument("--report", type=Path, default=_REPO / "scripts" / "memory" / "drive_excerpts_report.json")
    parser.add_argument("--rebuild-index", action="store_true", help="ignore the cache and build (paid)")
    parser.add_argument("--probe", action="store_true", help="print the cache key and whether it is cached; no model call")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _args(argv)
    from dotenv import load_dotenv
    load_dotenv(_REPO / ".env")
    from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
    space = GeminiEmbeddingAdapter.EMBEDDING_SPACE
    if args.probe:
        info = probe(args.tree, args.snapshot, space)
        print(f"index cache key {info['key']}: {'HIT (only the question embeddings cost)' if info['cached'] else 'MISS (a rebuild runs the reader models)'}")
        print(info["path"])
        return EXIT_OK
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        print("GEMINI_API_KEY is not set (.env)", file=sys.stderr)
        return EXIT_SETUP
    from src.adapters.geo_place_table import load_place_rows
    from src.domain.place_lookup import PlaceIndex

    from . import content_wiring
    try:
        report = asyncio.run(evaluate(
            tree=args.tree, gold_paths=[args.gold], bars_path=args.bars, window_bars_path=args.window_bars,
            snapshot=args.snapshot, embeddings=GeminiEmbeddingAdapter(api_key=key), embedding_space=space,
            place_of=PlaceIndex(load_place_rows()).lookup, build_content=content_wiring.build_real_content,
            rebuild=args.rebuild_index))
    except (content_wiring.ContentSetupError, EvalError) as exc:
        print(f"excerpts eval: no verdict — {exc}", file=sys.stderr)
        return EXIT_SETUP
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"recall {report['hits']}/{report['cases']} = {report['recall']:.4f} (bar {report['recall_min']}), "
          f"index cache: {report['index_cache']}")
    for row in report["rows"]:
        if not row["hit"]:
            print(f"  miss {row['id']} [{row['case']}] {row['query']!r} status={row['status']} "
                  f"path={row.get('path_hit')} substring={row.get('substring_hit')}")
    if report["lost_cases"]:
        print(f"LOST (true in bars.json): {', '.join(report['lost_cases'])}")
    print(f"verdict: {report['verdict']} (report: {args.report})")
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
