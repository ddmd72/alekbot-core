"""
The v1 baseline on the Search v2 fixture (USER_DRIVE_INDEXING_RFC §25.7: "the v1 baseline is the tag run on the same
fixture"). Runs INSIDE a worktree of the tag `drive-search-v1`, with that tag's indexer and search, over the same drive
(tree.json + tree_v2.json), the same reader transcriptions and photo descriptions (from enrichment.json; v1 gets a
photo's description cut to its ten-word caption) and the same offline lexical vectors, and writes
`baseline_v1.json`: each v2 gold case's top-K cards (v1 had no tiers: every card is a claimed match) and each v1 gold
case's hit by v1's own rule. search_v2_eval.py reads it as the second column and as the "every v1-passing case" guard.

It imports only what the tag has, plus lexical_embeddings.py (no `src` imports). How to run it (owner or agent;
nothing is written outside the two worktrees):

    git worktree add ../alekbot-core-v1-baseline drive-search-v1
    cp scripts/validation/drive_eval/{v1_baseline.py,lexical_embeddings.py} \\
       ../alekbot-core-v1-baseline/scripts/validation/drive_eval/
    rm -rf ../alekbot-core-v1-baseline/tests/fixtures/drive_eval/{files,files_v2}
    cp -r tests/fixtures/drive_eval/{tree.json,tree_v2.json,files,files_v2,gold_v2.json,gold_g1.json,gold_g2.json,enrichment.json} \\
       ../alekbot-core-v1-baseline/tests/fixtures/drive_eval/
    (cd ../alekbot-core-v1-baseline && venv/bin/python -m scripts.validation.drive_eval.v1_baseline \\
       --out "$OLDPWD/tests/fixtures/drive_eval/baseline_v1.json")
    git worktree remove --force ../alekbot-core-v1-baseline
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_REPO = Path(__file__).resolve().parents[3]
FIXTURES = _REPO / "tests" / "fixtures" / "drive_eval"
CAPTION_WORDS = 10


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _caption(photo: Optional[Dict[str, Any]]) -> str:
    words = str((photo or {}).get("description", "")).split()
    return " ".join(words[:CAPTION_WORDS])


def build_ports(snapshot: Dict[str, Any], parser: Any) -> Dict[str, Any]:
    """v1's reader and triage answered from the snapshot, and a parse cache in front of the tag's parser."""
    from src.domain.drive_extraction import (
        READER, ExtractionOutcome, ExtractionStatus, TriageBatchOutcome, TriageResult,
    )
    from src.domain.drive_index import DocumentBrief, TriageVerdict
    from src.ports.content_extractor_port import ContentExtractorPort
    from src.ports.image_triage_port import ImageTriagePort

    def outcome(data: Dict[str, Any]) -> Any:
        brief = None
        if data.get("brief"):
            b = data["brief"]
            brief = DocumentBrief(doc_type=b.get("doc_type", ""), description=b.get("description", ""),
                                  doc_date=datetime.fromisoformat(b["doc_date"]) if b.get("doc_date") else None,
                                  parties=tuple(b.get("parties", ())), amount=b.get("amount", ""))
        return ExtractionOutcome(status=ExtractionStatus(data["status"]), text=data.get("text", ""),
                                 pages_total=int(data.get("pages_total", 0)), pages_sent=int(data.get("pages_sent", 0)),
                                 truncated=bool(data.get("truncated")), brief=brief,
                                 extracted_by=data.get("extracted_by", ""))

    class Reader(ContentExtractorPort):
        misses = 0

        @property
        def extractor_id(self) -> str:
            return READER

        @property
        def version(self) -> int:
            return 1

        async def extract(self, request: Any) -> Any:
            entry = snapshot.get("reader", {}).get(_sha(Path(request.path).read_bytes()))
            if entry is None:
                Reader.misses += 1
                return ExtractionOutcome(status=ExtractionStatus.ERROR, detail="not in the snapshot")
            return outcome(entry["outcome"])

    class Triage(ImageTriagePort):
        misses = 0

        async def triage(self, user_id: str, account_id: str, images: Sequence[Any]) -> Any:
            results = []
            for image in images:
                entry = snapshot.get("photos", {}).get(f"{_sha(image.thumbnail)}:{int(image.camera_prior)}")
                if entry is None or entry.get("refused"):
                    Triage.misses += entry is None
                    results.append(TriageResult(item_id=image.item_id, verdict=TriageVerdict.PHOTO, caption=""))
                    continue
                verdict = TriageVerdict(entry["verdict"])
                results.append(TriageResult(item_id=image.item_id, verdict=verdict,
                                            caption=_caption(entry.get("photo")) if verdict is TriageVerdict.PHOTO
                                            else ""))
            return TriageBatchOutcome(results=tuple(results))

    class Parsed(ContentExtractorPort):
        def __init__(self) -> None:
            self._memory: Dict[str, Any] = {}

        @property
        def extractor_id(self) -> str:
            return parser.extractor_id

        @property
        def version(self) -> int:
            return parser.version

        async def extract(self, request: Any) -> Any:
            key = _sha(f"{request.kind}:{request.mime_type}:".encode() + Path(request.path).read_bytes())
            if key not in self._memory:
                self._memory[key] = await parser.extract(request)
            return self._memory[key]

    return {"reader": Reader(), "triage": Triage(), "parser": Parsed()}


async def run(tree: Path, overlay: Path, gold_v2: Path, gold_v1: Sequence[Path], snapshot_path: Path) -> Dict[str, Any]:
    from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
    from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
    from src.composition.drive_indexing_setup import build_place_lookup
    from src.domain.drive_extraction import PARSER, READER
    from src.domain.drive_search import render_results
    from src.services.drive_search_service import DriveSearchService

    from .fixture_drive import FixtureDrive
    from .lexical_embeddings import EMBEDDING_SPACE, LexicalEmbeddings
    from .runner import EVAL_USER, EvalClock, _card_paths, index_fixture, load_gold, request_for, run_cases

    manifest = json.loads(tree.read_text(encoding="utf-8"))
    extra = json.loads(overlay.read_text(encoding="utf-8"))
    for key in ("items", "generate"):
        manifest[key] = list(manifest.get(key, [])) + list(extra.get(key, []))
    drive = FixtureDrive(manifest, base_dir=tree.parent)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    ports = build_ports(snapshot, SubprocessContentExtractor())
    embeddings = LexicalEmbeddings()
    clock = EvalClock()
    repo = InMemoryDriveIndexRepository()
    indexed = await index_fixture(drive, repo, embeddings, build_place_lookup(), EMBEDDING_SPACE, clock,
                                  throttle_pause_s=0, extractors={PARSER: ports["parser"], READER: ports["reader"]},
                                  triage=ports["triage"])
    from .eval_connection import EvalDriveConnections, eval_connection
    search = DriveSearchService(repo, embeddings, drive, EvalDriveConnections(eval_connection(EVAL_USER)), clock=clock,
                                embedding_space=EMBEDDING_SPACE)
    rows: Dict[str, Any] = {}
    for case in json.loads(gold_v2.read_text(encoding="utf-8")):
        try:
            result = await search.search(EVAL_USER, request_for(case))
        except Exception as exc:   # noqa: BLE001 — a crash is a finding
            rows[case["id"]] = {"error": f"{type(exc).__name__}: {exc}", "tiers": {}}
            continue
        tiers: Dict[str, str] = {}
        for card in result.cards:
            for path in _card_paths(card, drive):
                tiers.setdefault(path, "v1")
        rows[case["id"]] = {"status": result.status, "tiers": tiers, "chars": len(render_results(result)),
                            "cards": len(result.cards)}
    v1_run = await run_cases(search, drive, load_gold(list(gold_v1)))
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=_REPO, capture_output=True, text=True,
                                check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = ""
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "commit": commit,
            "embedding_space": EMBEDDING_SPACE, "records": indexed.records, "ticks": indexed.ticks,
            "snapshot_misses": {"reader": ports["reader"].misses, "triage": ports["triage"].misses},
            "rows": rows, "v1_gold_hits": {row["id"]: bool(row["hit"]) for row in v1_run.cases}}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_eval.v1_baseline")
    parser.add_argument("--tree", type=Path, default=FIXTURES / "tree.json")
    parser.add_argument("--overlay", type=Path, default=FIXTURES / "tree_v2.json")
    parser.add_argument("--gold-v2", type=Path, default=FIXTURES / "gold_v2.json")
    parser.add_argument("--gold", type=Path, action="append")
    parser.add_argument("--snapshot", type=Path, default=FIXTURES / "enrichment.json")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    gold = args.gold or [FIXTURES / "gold_g1.json", FIXTURES / "gold_g2.json"]
    report = asyncio.run(run(args.tree, args.overlay, args.gold_v2, gold, args.snapshot))
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"v1 baseline: {len(report['rows'])} v2 cases, {sum(report['v1_gold_hits'].values())}/"
          f"{len(report['v1_gold_hits'])} v1 gold hits, misses {report['snapshot_misses']} → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
