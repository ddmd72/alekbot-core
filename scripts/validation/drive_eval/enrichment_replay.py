"""
Replay ports: the indexer's model ports answered from the recorded snapshot (`enrichment_snapshot.py`, RFC §25.7
F-22), so the Search v2 eval indexes the fixture with no model call. They implement the real ports and answer as the
runners would: verification and grounding still run in the indexer, exactly as in production.

- `ReplayProfiler` (DocumentProfilerPort), `ReplayPassages` (PassageHintPort), `ReplayTriage` (ImageTriagePort) and
  `ReplayReader` (ContentExtractorPort, extractor `reader`).
- **Generated bulk files** (a `generate` entry with a `content_template`: the 600 invoices) are never recorded: their
  profile is the hand-written fixture profile of their template (`fixture_texts.BULK_PROFILES`, `{n}` rendered), and
  their passages have no keywords. Recording 600 near-identical invoices would cost more than the whole fixture and
  teach nothing a template does not (the cost cap of `dump_enrichment.py`).
- **A miss** (an input the snapshot does not hold: the fixture, the chunker, an input rule or a prompt changed since
  the recording) is counted in `misses` and answered as a refusal, so the index still settles; the eval then gives no
  verdict until the snapshot is re-recorded (a stale snapshot would measure something else).

Several public classes in one module, by decision: the four replays are one mechanism read together.
"""
from __future__ import annotations

from collections import Counter
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from src.domain.drive_enrichment import DriveProfile
from src.domain.drive_enrichment_calls import (
    HintReply, HintRequest, ModelReplyStatus, ProfileReply, ProfileRequest, TriageReply, TriageResultV2,
)
from src.domain.drive_extraction import READER, ExtractionOutcome, ExtractionRequest, ExtractionStatus, TriageImage
from src.domain.drive_index import TriageVerdict
from src.ports.content_extractor_port import ContentExtractorPort
from src.ports.document_profiler_port import DocumentProfilerPort
from src.ports.image_triage_port import ImageTriagePort
from src.ports.passage_hint_port import PassageHintPort

from .enrichment_snapshot import (
    EnrichmentSnapshot, blob_key, hint_key, items_from_json, outcome_from_json, photo_from_json, photo_key,
    profile_from_json, profile_key,
)

REPLAY_MODEL = "replay"
# (template name, member number) of a generated bulk file, None for any other item
BulkOf = Callable[[str], Optional[Tuple[str, int]]]
# the hand-written profile of a bulk member, None when its template has none
BulkProfile = Callable[[str, int], Optional[DriveProfile]]


def _none_bulk(item_id: str) -> Optional[Tuple[str, int]]:
    return None


class ReplayProfiler(DocumentProfilerPort):
    def __init__(self, snapshot: EnrichmentSnapshot, *, bulk_of: BulkOf = _none_bulk,
                 bulk_profile: Optional[BulkProfile] = None, misses: Optional[Counter] = None) -> None:
        self._snapshot = snapshot
        self._bulk_of = bulk_of
        self._bulk_profile = bulk_profile
        self.misses: Counter = misses if misses is not None else Counter()
        self.calls = 0

    async def profile(self, request: ProfileRequest) -> ProfileReply:
        self.calls += 1
        member = self._bulk_of(request.item_id)
        if member is not None and self._bulk_profile is not None:
            profile = self._bulk_profile(*member)
            if profile is not None:
                return ProfileReply(status=ModelReplyStatus.OK, profile=profile, model=REPLAY_MODEL, usd=0.0)
        entry = self._snapshot.profiles.get(profile_key(request))
        if entry is None:
            self.misses["profile"] += 1
            return ProfileReply(status=ModelReplyStatus.REFUSED, profile=None, model=REPLAY_MODEL, usd=0.0)
        status = ModelReplyStatus(entry.get("status", "ok"))
        profile = profile_from_json(entry["profile"]) if entry.get("profile") is not None else None
        return ProfileReply(status=status, profile=profile if status is ModelReplyStatus.OK else None,
                            model=REPLAY_MODEL, usd=0.0)


class ReplayPassages(PassageHintPort):
    def __init__(self, snapshot: EnrichmentSnapshot, *, bulk_of: BulkOf = _none_bulk,
                 misses: Optional[Counter] = None) -> None:
        self._snapshot = snapshot
        self._bulk_of = bulk_of
        self.misses: Counter = misses if misses is not None else Counter()
        self.calls = 0

    async def hints(self, request: HintRequest) -> HintReply:
        self.calls += 1
        items: Dict[int, Sequence] = {}
        outcomes: Dict[int, ModelReplyStatus] = {}
        bulk = self._bulk_of(request.item_id) is not None
        for index, text in request.chunks:
            if bulk:
                items[index], outcomes[index] = (), ModelReplyStatus.OK
                continue
            entry = self._snapshot.hints.get(hint_key(text))
            if entry is None:
                self.misses["passages"] += 1
                outcomes[index] = ModelReplyStatus.REFUSED   # a refused chunk gets no hints and never holds the file
                continue
            status = ModelReplyStatus(entry.get("status", "ok"))
            outcomes[index] = status
            if status is ModelReplyStatus.OK:
                items[index] = items_from_json(entry.get("items", ()))
        return HintReply(status=ModelReplyStatus.OK, items_by_index=items, model=REPLAY_MODEL, usd=0.0,
                         outcome_by_index=outcomes)


class ReplayTriage(ImageTriagePort):
    def __init__(self, snapshot: EnrichmentSnapshot, *, misses: Optional[Counter] = None) -> None:
        self._snapshot = snapshot
        self.misses: Counter = misses if misses is not None else Counter()
        self.calls = 0

    async def triage(self, user_id: str, account_id: str, images: Sequence[TriageImage]) -> TriageReply:
        self.calls += 1
        results: List[TriageResultV2] = []
        refused: List[str] = []
        for image in images:
            entry = self._snapshot.photos.get(photo_key(image.thumbnail, image.camera_prior))
            if entry is None:
                self.misses["triage"] += 1
                refused.append(image.item_id)
                continue
            if entry.get("refused"):
                refused.append(image.item_id)   # refused (or stopped at the cap) alone when recorded
                continue
            verdict = TriageVerdict(entry["verdict"])
            photo = photo_from_json(entry.get("photo")) if verdict is TriageVerdict.PHOTO else None
            if verdict is TriageVerdict.PHOTO and photo is None:
                refused.append(image.item_id)   # recorded as a photo without a description: what the runner refused
                continue
            results.append(TriageResultV2(item_id=image.item_id, verdict=verdict, photo=photo))
        return TriageReply(status=ModelReplyStatus.OK, results=tuple(results), model=REPLAY_MODEL, usd=0.0,
                           model_calls=0, refused_item_ids=tuple(refused))


class ReplayReader(ContentExtractorPort):
    """The document reader's recorded transcription of a file, by its bytes. `version` is the real reader's, so a
    replayed index carries the same `extracted_by` as a live one."""

    def __init__(self, snapshot: EnrichmentSnapshot, *, version: int, misses: Optional[Counter] = None) -> None:
        self._snapshot = snapshot
        self._version = version
        self.misses: Counter = misses if misses is not None else Counter()
        self.calls = 0

    @property
    def extractor_id(self) -> str:
        return READER

    @property
    def version(self) -> int:
        return self._version

    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome:
        self.calls += 1
        with open(request.path, "rb") as fh:
            data = fh.read()
        entry = self._snapshot.reader.get(blob_key(data))
        if entry is None:
            self.misses["reader"] += 1
            return ExtractionOutcome(status=ExtractionStatus.ERROR, detail="not in the enrichment snapshot")
        return outcome_from_json(entry["outcome"])


def replay_ports(snapshot: EnrichmentSnapshot, *, reader_version: int, bulk_of: BulkOf = _none_bulk,
                 bulk_profile: Optional[BulkProfile] = None) -> Tuple[Dict[str, object], Counter]:
    """`{profiler, passages, triage, reader}` sharing one misses counter (returned beside them)."""
    misses: Counter = Counter()
    return {"profiler": ReplayProfiler(snapshot, bulk_of=bulk_of, bulk_profile=bulk_profile, misses=misses),
            "passages": ReplayPassages(snapshot, bulk_of=bulk_of, misses=misses),
            "triage": ReplayTriage(snapshot, misses=misses),
            "reader": ReplayReader(snapshot, version=reader_version, misses=misses)}, misses

