"""
Search v2 eval (USER_DRIVE_INDEXING_RFC §25.7): index the synthetic fixture with the real tick, the real parser and the
recorded enrichment (no model call), run every case through the real DriveSearchService, and judge the families
against the pre-registered bars.

    venv/bin/python -m scripts.validation.drive_eval.search_v2_eval [--embeddings lexical|gemini]
                                                                    [--baseline-v1 PATH] [--report PATH]

The drive is tree.json plus the v2 overlay (tree_v2.json, files_v2/). The enrichment comes from enrichment.json
(`enrichment_replay.py`): the profiles, passage keywords, photo descriptions and reader transcriptions one live run
recorded (`dump_enrichment.py --fixture`). A snapshot that misses an input (the fixture or an input rule changed)
gives no verdict. Vectors: `lexical` (default, offline, deterministic; `lexical_embeddings.py` says what that
measures) or `gemini` (the real model, GEMINI_API_KEY; owner-run).

Cases: gold_v2.json (families with `expect_tier`, v2_metrics.py) and the v1 gold (gold_g1.json, gold_g2.json), each
scored by its family's rule. Bars: bars_v2.json, numbers fixed before any run (§25.7: "bars are recorded only if
they meet these numbers"); never written by this script. `--baseline-v1` adds the tag's run on the same fixture
(`v1_baseline.py`) as a second column and is the "every v1-passing case" guard.

Exit codes: 0 every bar met, 1 a bar missed, 2 no verdict (a stale snapshot, a malformed gold, an index that did not
settle, a search that raised).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.composition.drive_reader_runner import READER_VERSION as _RUNNER_READER_VERSION
from src.domain.drive_extraction import PARSER, READER
from src.domain.drive_index_planner import PlaceOf
from src.domain.drive_search import DriveSearchResult, render_results
from scripts.validation.drive_eval.eval_connection import ref_item_id
from src.ports.content_extractor_port import ContentExtractorPort
from src.ports.embedding_service import EmbeddingService
from src.services.drive_search_service import DriveSearchService

from . import fixture_texts_v2, v2_metrics
from .enrichment_replay import replay_ports
from .enrichment_snapshot import EnrichmentSnapshot
from .fixture_drive import FixtureDrive
from .metrics import normalize_snippet
from .runner import EVAL_USER, EvalClock, EvalError, _EnabledUsers, index_fixture, load_gold, request_for, validate_gold

_REPO = Path(__file__).resolve().parents[3]
FIXTURES = _REPO / "tests" / "fixtures" / "drive_eval"
READER_VERSION = _RUNNER_READER_VERSION   # the replay carries the runner's own stamp (a bump there is followed here)
EXIT_OK, EXIT_BARS, EXIT_SETUP = 0, 1, 2


def v2_drive(tree: Path, overlay: Optional[Path]) -> FixtureDrive:
    """tree.json plus the overlay's items and generated entries; file names resolve against the tree's folder."""
    manifest: Dict[str, Any] = json.loads(tree.read_text(encoding="utf-8"))
    if overlay is not None:
        extra = json.loads(overlay.read_text(encoding="utf-8"))
        for key in ("items", "generate"):
            manifest[key] = list(manifest.get(key, [])) + list(extra.get(key, []))
    return FixtureDrive(manifest, base_dir=tree.parent)


class TierCapture(DriveSearchService):
    """The real search, keeping what the rendered result only summarises: every member of each tier (the shown cards
    and the grouped rest), the Related lines with their similar-name flag, and each file's meaning score. An eval
    harness over two private steps of the service (`_tiers`, `_related`, `_meaning`); their contract is §25.5.3-§25.5.4."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.captured: Dict[str, Any] = {}

    def reset(self) -> None:
        self.captured = {"tiers": {}, "related": [], "meaning": {}}

    def _meaning(self, pool: Any, scope: Any, stats: Any) -> Dict[str, float]:
        meaning = super()._meaning(pool, scope, stats)
        self.captured["meaning"] = dict(meaning)
        self.captured["pools"] = (stats.pool_card, stats.pool_chunk, stats.pool_hint)
        return meaning

    def _tiers(self, *args: Any, **kwargs: Any) -> Any:
        by_tier, blocks, expired = super()._tiers(*args, **kwargs)
        self.captured["tiers"] = {tier.value: list(items) for tier, items in by_tier.items()}
        return by_tier, blocks, expired

    def _related(self, *args: Any, **kwargs: Any) -> Any:
        out, small = super()._related(*args, **kwargs)
        self.captured["related"] = list(out)
        return out, small


def _path(drive: FixtureDrive, item_id: str) -> str:
    return drive.path_of(item_id) or item_id


def row_of(result: DriveSearchResult, captured: Mapping[str, Any], drive: FixtureDrive) -> Dict[str, Any]:
    """A v2 row (v2_metrics): every file's tier, the similar names, D29 facts and the rendered size."""
    row: Dict[str, Any] = {"status": result.status, "chars": len(render_results(result))}
    tiered = result.tiered
    if tiered is None:   # a filter-only listing: cards with no tier
        row["listing"] = True
        row["tiers"] = {}
        row["cards"] = [_card_paths(card, drive) for card in result.cards]   # in order: v1's top-K rule
        for paths in row["cards"]:
            for path in paths:
                row["tiers"][path] = v2_metrics.LISTING
        return row
    tiers: Dict[str, str] = {}
    for tier, items in (captured.get("tiers") or {}).items():
        for item in items:
            tiers[_path(drive, item)] = tier
    similar = []
    for item, is_similar in captured.get("related") or []:
        tiers.setdefault(_path(drive, item), "related")
        if is_similar:
            similar.append(_path(drive, item))
    row["inaccessible"] = sorted({p for card in result.cards if getattr(card, "inaccessible", False)
                                  for p in _card_paths(card, drive)})   # §26.2: cards whose index line says "cannot open"
    row["accuracy"] = {p: card.card_accuracy for card in result.cards for p in _card_paths(card, drive)}
    row.update(tiers=tiers, similar=similar, concepts=list(tiered.concepts), ignored=list(tiered.ignored),
               common=list(tiered.common), absent=list(tiered.absent), absent_in_scope=list(tiered.absent_in_scope),
               without=tiered.without, without_at_least=tiered.without_at_least,
               shown=[_card_paths(c, drive)[0] for c in result.cards],
               coverage=_coverage(tiered), pools=captured.get("pools"))
    return row


def _coverage(tiered: Any) -> Dict[str, Any]:
    cov = tiered.coverage
    return {k: v for k, v in cov.__dict__.items() if v}


def _card_paths(card: Any, drive: FixtureDrive) -> List[str]:
    item_id = ref_item_id(getattr(card, "ref", "") or "")
    own = (drive.path_of(item_id) if item_id else None) or str(getattr(card, "path", "")).strip("/")
    return [own] + [str(p).strip("/") for p in getattr(card, "also_at", ()) or ()]


def _shown_text(result: DriveSearchResult, drive: FixtureDrive) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for card in result.cards:
        text = f"{card.summary}\n{card.passage}"
        for path in _card_paths(card, drive):
            out.setdefault(path, text)
    return out


async def run_rows(search: TierCapture, drive: FixtureDrive,
                   gold: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for case in gold:
        search.reset()
        try:
            result = await search.search(EVAL_USER, request_for(dict(case)))
        except Exception as exc:   # a crash is a finding, not a reason to lose the other cases
            rows[case["id"]] = {"error": f"{type(exc).__name__}: {exc}", "tiers": {}}
            continue
        row = row_of(result, search.captured, drive)
        wanted = case.get("expected_substring")
        if wanted:
            texts = _shown_text(result, drive)
            pairs = wanted.items() if isinstance(wanted, dict) else [(case["expected_paths"][0], wanted)]
            row["substring_hit"] = all(normalize_snippet(s) in normalize_snippet(texts.get(p, "")) for p, s in pairs)
        row["query"] = case.get("query", "")
        rows[case["id"]] = row
    return rows


def evidence_of(snapshot: EnrichmentSnapshot) -> Dict[str, List[str]]:
    """Each recorded passage item's text -> its evidence quotes (AnchoredLexicalEmbeddings)."""
    out: Dict[str, List[str]] = {}
    for entry in snapshot.hints.values():
        for item in entry.get("items") or ():
            out.setdefault(str(item.get("text", "")), []).append(str(item.get("evidence", "")))
    return out


def build_embeddings(kind: str, snapshot: Optional[EnrichmentSnapshot] = None) -> Tuple[EmbeddingService, str]:
    """`lexical` (offline; with a snapshot, its keyword sets anchored to their passages) or `gemini`."""
    if kind == "gemini":
        from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise EvalError("GEMINI_API_KEY is not set (.env)")
        return GeminiEmbeddingAdapter(api_key=key), GeminiEmbeddingAdapter.EMBEDDING_SPACE
    from .lexical_embeddings import EMBEDDING_SPACE, AnchoredLexicalEmbeddings, LexicalEmbeddings
    if snapshot is not None:
        return AnchoredLexicalEmbeddings(evidence_of(snapshot)), EMBEDDING_SPACE   # type: ignore[return-value]
    return LexicalEmbeddings(), EMBEDDING_SPACE   # type: ignore[return-value]


async def index_v2(drive: FixtureDrive, snapshot: EnrichmentSnapshot, embeddings: EmbeddingService, space: str,
                   place_of: PlaceOf, parser: ContentExtractorPort, clock: EvalClock
                   ) -> Tuple[InMemoryDriveIndexRepository, Dict[str, Any]]:
    """The real tick over the drive with the snapshot's replays; returns the repository and the run's facts
    (ticks, record counts, replay misses)."""
    ports, misses = replay_ports(snapshot, reader_version=READER_VERSION, bulk_of=drive.generated_member,
                                 bulk_profile=fixture_texts_v2.bulk_profile)
    repo = InMemoryDriveIndexRepository()
    started = time.monotonic()
    out = await index_fixture(drive, repo, embeddings, place_of, space, clock, throttle_pause_s=0,
                              extractors={PARSER: parser, READER: ports["reader"]},  # type: ignore[dict-item]
                              triage=ports["triage"], profiler=ports["profiler"],  # type: ignore[arg-type]
                              passages=ports["passages"])  # type: ignore[arg-type]
    facts = {"ticks": out.ticks, "records": out.records, "seconds": round(time.monotonic() - started, 1),
             "summary": {k: v for k, v in out.summary.items() if v}, "misses": dict(misses)}
    return repo, facts


async def evaluate(*, tree: Path, overlay: Path, gold_v2_path: Path, gold_v1_paths: Sequence[Path], bars_path: Path,
                   snapshot_path: Path, embeddings: EmbeddingService, space: str, place_of: PlaceOf,
                   parser: ContentExtractorPort, baseline_v1: Optional[Mapping[str, Any]] = None,
                   v1_bars: Optional[Mapping[str, Any]] = None,
                   allow_misses: bool = False, embeddings_kind: str = "gemini") -> Dict[str, Any]:
    drive = v2_drive(tree, overlay)
    gold_v2 = json.loads(gold_v2_path.read_text(encoding="utf-8"))
    v2_metrics.validate_gold_v2(gold_v2, drive.paths())
    gold_v1 = load_gold(gold_v1_paths) if gold_v1_paths else []
    validate_gold(gold_v1, drive.paths())
    bars = json.loads(bars_path.read_text(encoding="utf-8"))
    snapshot = EnrichmentSnapshot.load(snapshot_path)
    clock = EvalClock()
    repo, facts = await index_v2(drive, snapshot, embeddings, space, place_of, parser, clock)
    report: Dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "embedding_space": space, "snapshot": {"sha256": snapshot.sha256(), **snapshot.meta},
                              "index": facts}
    if facts["misses"] and not allow_misses:
        report["verdict"] = "stale_snapshot"
        return report
    search = TierCapture(repo, embeddings, _EnabledUsers(), drive, clock=clock, embedding_space=space)
    rows = await run_rows(search, drive, gold_v2)
    rows_v1_gold = await run_rows(search, drive, gold_v1)
    errors = sorted(cid for cid, r in {**rows, **rows_v1_gold}.items() if r.get("error"))
    scored, skipped = v2_metrics.split_by_embeddings(gold_v2, embeddings_kind)
    table = v2_metrics.family_table(scored, rows)
    v1_passed = _v1_passed(baseline_v1, v1_bars)
    g1 = v2_metrics.g1_table(gold_v1, rows_v1_gold, v1_passed)
    report.update(families=table, g1=g1, skipped=[c["id"] for c in skipped], rows=rows, rows_v1_gold=rows_v1_gold, errors=errors,
                  bars=bars, misses_allowed=bool(facts["misses"]))
    if baseline_v1 is not None:
        report["v1"] = v2_metrics.family_table(scored, baseline_v1.get("rows", {}), v1=True)
    failures = v2_metrics.judge(table, g1, bars)
    report["bar_failures"] = failures
    report["verdict"] = "search_errors" if errors else ("pass" if not failures else "bars_missed")
    return report


def _v1_passed(baseline: Optional[Mapping[str, Any]], v1_bars: Optional[Mapping[str, Any]]) -> Dict[str, bool]:
    """Which G1/G2 cases v1 passed: the baseline run on this fixture when given (same vectors and enrichment as
    this run), else the recorded v1 bars."""
    if baseline is not None and baseline.get("v1_gold_hits"):
        return {k: bool(v) for k, v in baseline["v1_gold_hits"].items()}
    return {k: bool(v) for k, v in (v1_bars or {}).get("cases", {}).items()}


def format_table(report: Mapping[str, Any]) -> List[str]:
    lines = []
    v2 = report.get("families", {})
    v1 = report.get("v1")
    bars = report.get("bars", {})
    head = f"{'family':<12} {'v2 pass':>10} {'v2 files':>10} {'bar':>6}" + (f" {'v1 pass':>10}" if v1 else "")
    lines.append(head)
    for fam in v2_metrics.FAMILIES:
        line = v2.get("families", {}).get(fam)
        if not line:
            continue
        bar = bars.get("pass_rate", {}).get(fam, bars.get("file_recall", {}).get(fam, ""))
        files = f"{line.get('files_found', '')}/{line.get('files', '')}" if "files" in line else ""
        text = f"{fam:<12} {line['passed']:>4}/{line['cases']:<5} {files:>10} {bar!s:>6}"
        if v1:
            v1_line = v1.get("families", {}).get(fam, {})
            text += f" {v1_line.get('passed', 0):>4}/{v1_line.get('cases', 0):<5}"
        lines.append(text)
    for key in ("exact_junk_per_case", "automatic_junk_per_case", "related_per_negative", "chars_median",
                "chars_max", "book_in_exact"):
        text = f"{key:<26} v2={v2.get(key)!r}"
        if v1 and key in v1:
            text += f"  v1={v1.get(key)!r}"
        lines.append(text)
    for cid, line in (v2.get("known_hard") or {}).items():
        lines.append(f"known hard {cid}: {'passes now' if line['passed'] else 'misses'} — {line['reason']}")
    g1 = report.get("g1", {})
    lines.append(f"v1 gold under v2: {g1.get('passed')}/{g1.get('cases')} pass; lost v1-passing: {g1.get('lost')}")
    for cid, reason in (g1.get("accepted_losses") or {}).items():
        lines.append(f"accepted loss {cid}: {reason}")
    for cid in report.get("skipped") or ():
        lines.append(f"skipped (needs semantic embeddings) {cid}")
    return lines


def _args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_eval.search_v2_eval")
    parser.add_argument("--tree", type=Path, default=FIXTURES / "tree.json")
    parser.add_argument("--overlay", type=Path, default=FIXTURES / "tree_v2.json")
    parser.add_argument("--gold-v2", type=Path, default=FIXTURES / "gold_v2.json")
    parser.add_argument("--gold", type=Path, action="append", help="v1 gold; default gold_g1.json and gold_g2.json")
    parser.add_argument("--bars", type=Path, default=FIXTURES / "bars_v2.json")
    parser.add_argument("--v1-bars", type=Path, default=FIXTURES / "bars.json")
    parser.add_argument("--snapshot", type=Path, default=FIXTURES / "enrichment.json")
    parser.add_argument("--baseline-v1", type=Path, default=FIXTURES / "baseline_v1.json")
    parser.add_argument("--embeddings", choices=["lexical", "gemini"], default="lexical")
    parser.add_argument("--parse-cache", type=Path, default=_REPO / "scripts" / "memory" / "drive_eval_parse_cache")
    parser.add_argument("--report", type=Path, default=_REPO / "scripts" / "memory" / "drive_eval_v2_report.json")
    parser.add_argument("--allow-snapshot-misses", action="store_true")
    args = parser.parse_args(argv)
    if not args.gold:
        args.gold = [FIXTURES / "gold_g1.json", FIXTURES / "gold_g2.json"]
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    from dotenv import load_dotenv

    from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
    from src.composition.drive_indexing_setup import build_place_lookup

    from .parse_cache import ParseCache

    args = _args(argv)
    load_dotenv(_REPO / ".env")
    try:
        embeddings, space = build_embeddings(args.embeddings, EnrichmentSnapshot.load(args.snapshot))
        baseline = (json.loads(args.baseline_v1.read_text(encoding="utf-8"))
                    if args.baseline_v1 and args.baseline_v1.exists() else None)
        v1_bars = json.loads(args.v1_bars.read_text(encoding="utf-8")) if args.v1_bars.exists() else None
        report = asyncio.run(evaluate(
            tree=args.tree, overlay=args.overlay, gold_v2_path=args.gold_v2, gold_v1_paths=args.gold,
            bars_path=args.bars, snapshot_path=args.snapshot, embeddings=embeddings, space=space,
            place_of=build_place_lookup(), parser=ParseCache(SubprocessContentExtractor(), args.parse_cache),
            baseline_v1=baseline, v1_bars=v1_bars, allow_misses=args.allow_snapshot_misses,
            embeddings_kind=args.embeddings))
    except (EvalError, v2_metrics.GoldError, FileNotFoundError, ValueError) as exc:
        print(f"drive search v2 eval: no verdict — {exc}", file=sys.stderr)
        return EXIT_SETUP
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    if report["verdict"] == "stale_snapshot":
        print(f"no verdict: the enrichment snapshot misses {report['index']['misses']} — re-record it "
              f"(dump_enrichment.py --fixture)")
        return EXIT_SETUP
    for line in format_table(report):
        print(line)
    for failure in report.get("bar_failures", []):
        print(f"BAR MISSED {failure}")
    print(f"verdict: {report['verdict']} (report: {args.report})")
    return {"pass": EXIT_OK, "bars_missed": EXIT_BARS}.get(report["verdict"], EXIT_SETUP)


if __name__ == "__main__":
    sys.exit(main())
