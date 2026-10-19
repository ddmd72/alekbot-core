"""
Write the SYNTHETIC enrichment of the §26 / D39 / D40 fixture additions into the recorded snapshot
(tests/fixtures/drive_eval/enrichment.json; USER_DRIVE_INDEXING_RFC §26.8 Eval). OFFLINE: no model, no embedding
service, no Firestore.

    venv/bin/python -m scripts.validation.drive_eval.synthetic_s26 [--snapshot PATH]

The answers are the hand-written `fixture_texts_s26.ENRICHMENT` (what the reader, profiler, triage and passages models
might return for the new files). They go through the same recording ports a live re-record uses
(`enrichment_record.recording_ports`), so every key is computed by the real indexer from the real fixture bytes, never
typed; each new entry is marked `"synthetic": true`. Existing (recorded) entries are untouched. Re-running first drops
the old synthetic entries, so a changed fixture or text re-derives them; a run that still misses an input of a
synthetic file (a non-synthetic miss is not this script's) fails with exit 2.

The owner's live `dump_enrichment.py --fixture` re-record replaces the whole snapshot, synthetic entries included, with
real model answers; the new gold cases are written to survive that (they assert what the index must do with a right
answer, not these exact words).

Several public classes in one module, by decision: the four stand-ins for the model ports are one mechanism.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from src.domain.drive_enrichment_calls import (
    HintReply, HintRequest, ModelReplyStatus, ProfileReply, ProfileRequest, TriageReply, TriageResultV2,
)
from src.domain.drive_extraction import ExtractionOutcome, ExtractionRequest, ExtractionStatus, TriageImage
from src.domain.drive_index import TriageVerdict
from src.ports.content_extractor_port import ContentExtractorPort
from src.ports.document_profiler_port import DocumentProfilerPort
from src.ports.image_triage_port import ImageTriagePort
from src.ports.passage_hint_port import PassageHintPort

from .fixture_texts_s26 import ENRICHMENT
from .enrichment_snapshot import (
    EnrichmentSnapshot, outcome_from_json, photo_from_json, profile_from_json,
)

MODEL = "synthetic"
_SECTIONS = ("profiles", "hints", "photos", "reader")


class SyntheticProfiler(DocumentProfilerPort):
    def __init__(self, path_of: Any) -> None:
        self._path_of = path_of

    async def profile(self, request: ProfileRequest) -> ProfileReply:
        entry = ENRICHMENT.get(self._path_of(request.item_id) or "", {}).get("profile")
        if entry is None:
            return ProfileReply(status=ModelReplyStatus.REFUSED, profile=None, model=MODEL, usd=0.0)
        return ProfileReply(status=ModelReplyStatus.OK, profile=profile_from_json(entry), model=MODEL, usd=0.0)


class SyntheticPassages(PassageHintPort):
    """No keywords and no questions: the new files are found by their names, values and profile, not by a passage."""

    async def hints(self, request: HintRequest) -> HintReply:
        return HintReply(status=ModelReplyStatus.OK, items_by_index={i: () for i, _ in request.chunks}, model=MODEL,
                         usd=0.0, outcome_by_index={i: ModelReplyStatus.OK for i, _ in request.chunks})


class SyntheticTriage(ImageTriagePort):
    def __init__(self, path_of: Any) -> None:
        self._path_of = path_of

    async def triage(self, user_id: str, account_id: str, images: Sequence[TriageImage]) -> TriageReply:
        results, refused = [], []
        for image in images:
            entry = ENRICHMENT.get(self._path_of(image.item_id) or "", {}).get("photo")
            if entry is None:
                refused.append(image.item_id)
                continue
            verdict = TriageVerdict(entry["verdict"])
            results.append(TriageResultV2(item_id=image.item_id, verdict=verdict,
                                          photo=photo_from_json(entry.get("photo")), capture=entry.get("capture", "")))
        return TriageReply(status=ModelReplyStatus.OK, results=tuple(results), model=MODEL, usd=0.0, model_calls=0,
                           refused_item_ids=tuple(refused))


class SyntheticReader(ContentExtractorPort):
    def __init__(self, path_of: Any, version: int) -> None:
        self._path_of = path_of
        self._version = version

    @property
    def extractor_id(self) -> str:
        from src.domain.drive_extraction import READER
        return READER

    @property
    def version(self) -> int:
        return self._version

    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome:
        entry = ENRICHMENT.get(self._path_of(request.item_id) or "", {}).get("reader")
        if entry is None:
            return ExtractionOutcome(status=ExtractionStatus.ERROR, detail="no synthetic transcription")
        return outcome_from_json(entry)


def drop_synthetic(snapshot: EnrichmentSnapshot) -> int:
    dropped = 0
    for name in _SECTIONS:
        table: Dict[str, Dict[str, Any]] = getattr(snapshot, name)
        for key in [k for k, v in table.items() if v.get("synthetic")]:
            del table[key]
            dropped += 1
    return dropped


def mark_new(snapshot: EnrichmentSnapshot, before: Dict[str, set]) -> int:
    marked = 0
    for name in _SECTIONS:
        for key, entry in getattr(snapshot, name).items():
            if key not in before[name]:
                entry["synthetic"] = True
                marked += 1
    return marked


async def write_synthetic(snapshot_path: Path) -> int:
    from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
    from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
    from src.composition.drive_indexing_setup import build_place_lookup
    from src.domain.drive_extraction import PARSER, READER

    from . import fixture_texts_v2
    from .enrichment_record import CostMeter, recording_ports
    from .lexical_embeddings import EMBEDDING_SPACE, LexicalEmbeddings
    from .parse_cache import ParseCache
    from .runner import EvalClock, index_fixture
    from .search_v2_eval import FIXTURES, READER_VERSION, _REPO, v2_drive

    snapshot = EnrichmentSnapshot.load(snapshot_path)
    print(f"dropped {drop_synthetic(snapshot)} earlier synthetic entries")
    before = {name: set(getattr(snapshot, name)) for name in _SECTIONS}
    drive = v2_drive(FIXTURES / "tree.json", FIXTURES / "tree_v2.json")
    real = {"profiler": SyntheticProfiler(drive.path_of), "passages": SyntheticPassages(),
            "triage": SyntheticTriage(drive.path_of), "reader": SyntheticReader(drive.path_of, READER_VERSION)}
    ports = recording_ports(real, snapshot, CostMeter(cap_usd=0.0), path_of=drive.path_of,
                            bulk_of=drive.generated_member, bulk_profile=fixture_texts_v2.bulk_profile)
    parser = ParseCache(SubprocessContentExtractor(), _REPO / "scripts" / "memory" / "drive_eval_parse_cache")
    await index_fixture(drive, InMemoryDriveIndexRepository(), LexicalEmbeddings(), build_place_lookup(),
                        EMBEDDING_SPACE, EvalClock(), throttle_pause_s=0,
                        extractors={PARSER: parser, READER: ports["reader"]},  # type: ignore[dict-item]
                        triage=ports["triage"], profiler=ports["profiler"],  # type: ignore[arg-type]
                        passages=ports["passages"])  # type: ignore[arg-type]
    marked = mark_new(snapshot, before)
    snapshot.meta["synthetic_entries"] = sum(
        1 for name in _SECTIONS for v in getattr(snapshot, name).values() if v.get("synthetic"))
    snapshot.save(snapshot_path)
    print(f"wrote {marked} synthetic entries → {snapshot_path}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    from .search_v2_eval import FIXTURES

    parser = argparse.ArgumentParser(prog="python -m scripts.validation.drive_eval.synthetic_s26")
    parser.add_argument("--snapshot", type=Path, default=FIXTURES / "enrichment.json")
    return asyncio.run(write_synthetic(parser.parse_args(argv).snapshot))


if __name__ == "__main__":
    sys.exit(main())
