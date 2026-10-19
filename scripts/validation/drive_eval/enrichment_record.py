"""
Recording ports: the real model runners, wrapped so one live indexing run of the synthetic fixture writes the
enrichment snapshot (`enrichment_snapshot.py`, USER_DRIVE_INDEXING_RFC §25.7 F-22). Used only by
`dump_enrichment.py --fixture` (owner-run, paid).

Each wrapper forwards only what the snapshot does not hold yet, so every distinct input is paid once (the twelve
Rome photos share one thumbnail; the two copies of a lease share their chunks), stores what the model answered —
never a transient outage (`unavailable`, `failed`), which the indexer retries — and adds the reply's cost to a
`CostMeter` that raises `CostCapReached` once the run passed its cap. Bulk members (a template's generated files)
are answered by their template's hand-written profile and get no keywords, never a call (enrichment_replay.py).

Several public classes in one module, by decision: the four recorders and their meter are one mechanism.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional, Sequence

from src.domain.drive_enrichment import HintItem
from src.domain.drive_enrichment_calls import (
    HintReply, HintRequest, ModelReplyStatus, ProfileReply, ProfileRequest, TriageReply, TriageResultV2,
)
from src.domain.drive_extraction import ExtractionOutcome, ExtractionRequest, ExtractionStatus, TriageImage
from src.domain.drive_index import TriageVerdict
from src.ports.content_extractor_port import ContentExtractorPort
from src.ports.document_profiler_port import DocumentProfilerPort
from src.ports.image_triage_port import ImageTriagePort
from src.ports.passage_hint_port import PassageHintPort

from .enrichment_replay import BulkOf, BulkProfile, ReplayPassages, ReplayProfiler, ReplayTriage, _none_bulk
from .enrichment_snapshot import (
    EnrichmentSnapshot, blob_key, hint_key, items_to_json, outcome_from_json, outcome_to_json, photo_key,
    photo_to_json, profile_key, profile_to_json,
)

S = ModelReplyStatus
TRANSIENT = frozenset({S.UNAVAILABLE, S.FAILED})
TRANSIENT_EXTRACTION = frozenset({ExtractionStatus.TIMEOUT, ExtractionStatus.BUDGET_EXHAUSTED})
PathOf = Callable[[str], Optional[str]]


def _no_path(item_id: str) -> Optional[str]:
    return None


class CostCapReached(RuntimeError):
    """The recording spent more than its cap: stop, nothing is saved (a partial snapshot would give misses)."""


class CostMeter:
    def __init__(self, cap_usd: float) -> None:
        self.cap_usd = cap_usd
        self.spent = 0.0
        self.calls = 0
        self.models: Dict[str, int] = {}

    def check(self) -> None:
        if self.spent > self.cap_usd:
            raise CostCapReached(f"spent ${self.spent:.4f}, cap ${self.cap_usd:.2f}")

    def add(self, usd: float, model: str = "") -> None:
        """Count one paid call; raises once the total passed the cap (the overshoot is at most that call)."""
        self.spent += usd
        self.calls += 1
        if model:
            self.models[model] = self.models.get(model, 0) + 1
        self.check()


class RecordingProfiler(DocumentProfilerPort):
    def __init__(self, inner: DocumentProfilerPort, snapshot: EnrichmentSnapshot, meter: CostMeter, *,
                 path_of: PathOf = _no_path, bulk_of: BulkOf = _none_bulk,
                 bulk_profile: Optional[BulkProfile] = None) -> None:
        self._inner = inner
        self._snapshot = snapshot
        self._meter = meter
        self._path_of = path_of
        self._replay = ReplayProfiler(snapshot, bulk_of=bulk_of, bulk_profile=bulk_profile)
        self._bulk_of = bulk_of
        self._bulk_profile = bulk_profile

    async def profile(self, request: ProfileRequest) -> ProfileReply:
        member = self._bulk_of(request.item_id)
        if (member is not None and self._bulk_profile is not None) or profile_key(request) in self._snapshot.profiles:
            return await self._replay.profile(request)
        self._meter.check()
        reply = await self._inner.profile(request)
        self._meter.add(reply.usd, reply.model)
        if reply.status not in TRANSIENT:
            self._snapshot.profiles[profile_key(request)] = {
                "path": self._path_of(request.item_id), "status": reply.status.value,
                "profile": profile_to_json(reply.profile) if reply.profile is not None else None}
        return reply


class RecordingPassages(PassageHintPort):
    def __init__(self, inner: PassageHintPort, snapshot: EnrichmentSnapshot, meter: CostMeter, *,
                 path_of: PathOf = _no_path, bulk_of: BulkOf = _none_bulk) -> None:
        self._inner = inner
        self._snapshot = snapshot
        self._meter = meter
        self._path_of = path_of
        self._bulk_of = bulk_of
        self._replay = ReplayPassages(snapshot, bulk_of=bulk_of)

    async def hints(self, request: HintRequest) -> HintReply:
        if self._bulk_of(request.item_id) is not None:
            return await self._replay.hints(request)
        unknown = [(i, t) for i, t in request.chunks if hint_key(t) not in self._snapshot.hints]
        status, model, usd, fallback = S.OK, "replay", 0.0, 0.0
        if unknown:
            self._meter.check()
            reply = await self._inner.hints(HintRequest(
                user_id=request.user_id, account_id=request.account_id, item_id=request.item_id, chunks=unknown,
                title=request.title, what_it_is=request.what_it_is))
            self._meter.add(reply.usd, reply.model)
            status, model, usd, fallback = reply.status, reply.model, reply.usd, reply.fallback_usd
            texts = dict(unknown)
            for index, outcome in reply.outcome_by_index.items():
                if outcome in TRANSIENT or index not in texts:
                    continue
                items: Sequence[HintItem] = reply.items_by_index.get(index, ()) if outcome is S.OK else ()
                self._snapshot.hints[hint_key(texts[index])] = {
                    "path": self._path_of(request.item_id), "index": index, "status": outcome.value,
                    "items": items_to_json(items)}
        known = [(i, t) for i, t in request.chunks if hint_key(t) in self._snapshot.hints]
        replayed = await self._replay.hints(HintRequest(
            user_id=request.user_id, account_id=request.account_id, item_id=request.item_id, chunks=known,
            title=request.title, what_it_is=request.what_it_is))
        return HintReply(status=status, items_by_index=dict(replayed.items_by_index), model=model, usd=usd,
                         fallback_usd=fallback, outcome_by_index=dict(replayed.outcome_by_index))


class RecordingTriage(ImageTriagePort):
    def __init__(self, inner: ImageTriagePort, snapshot: EnrichmentSnapshot, meter: CostMeter, *,
                 path_of: PathOf = _no_path) -> None:
        self._inner = inner
        self._snapshot = snapshot
        self._meter = meter
        self._path_of = path_of
        self._replay = ReplayTriage(snapshot)

    async def triage(self, user_id: str, account_id: str, images: Sequence[TriageImage]) -> TriageReply:
        first: Dict[str, TriageImage] = {}
        for image in images:
            key = photo_key(image.thumbnail, image.camera_prior)
            if key not in self._snapshot.photos and key not in first:
                first[key] = image
        status, model, usd, fallback, calls = S.OK, "replay", 0.0, 0.0, 0
        if first:
            self._meter.check()
            reply = await self._inner.triage(user_id, account_id, list(first.values()))
            self._meter.add(reply.usd, reply.model)
            status, model, usd, fallback, calls = (reply.status, reply.model, reply.usd, reply.fallback_usd,
                                                   reply.model_calls)
            by_item = {r.item_id: r for r in reply.results}
            refused = set(reply.refused_item_ids) | set(reply.capped_item_ids)
            for key, image in first.items():
                paths = [self._path_of(image.item_id)]
                result: Optional[TriageResultV2] = by_item.get(image.item_id)
                if result is not None:
                    self._snapshot.photos[key] = {"paths": paths, "verdict": result.verdict.value,
                                                  "photo": photo_to_json(result.photo)}
                    if result.capture:   # §26.3: how the model said a text image was made
                        self._snapshot.photos[key]["capture"] = result.capture
                elif image.item_id in refused:
                    self._snapshot.photos[key] = {"paths": paths, "verdict": TriageVerdict.PHOTO.value,
                                                  "photo": None, "refused": True}
        settled = [i for i in images if photo_key(i.thumbnail, i.camera_prior) in self._snapshot.photos]
        replayed = await self._replay.triage(user_id, account_id, settled)
        return TriageReply(status=status, results=replayed.results, model=model, usd=usd, fallback_usd=fallback,
                           model_calls=calls, refused_item_ids=replayed.refused_item_ids)


class RecordingReader(ContentExtractorPort):
    def __init__(self, inner: ContentExtractorPort, snapshot: EnrichmentSnapshot, meter: CostMeter, *,
                 path_of: PathOf = _no_path) -> None:
        self._inner = inner
        self._snapshot = snapshot
        self._meter = meter
        self._path_of = path_of

    @property
    def extractor_id(self) -> str:
        return self._inner.extractor_id

    @property
    def version(self) -> int:
        return self._inner.version

    async def extract(self, request: ExtractionRequest) -> ExtractionOutcome:
        with open(request.path, "rb") as fh:
            key = blob_key(fh.read())
        entry = self._snapshot.reader.get(key)
        if entry is not None:
            return outcome_from_json(entry["outcome"])
        self._meter.check()
        outcome = await self._inner.extract(request)
        self._meter.add(outcome.model_cost)
        if outcome.status not in TRANSIENT_EXTRACTION:
            self._snapshot.reader[key] = {"path": self._path_of(request.item_id), "outcome": outcome_to_json(outcome)}
        return outcome


def recording_ports(real: Dict[str, object], snapshot: EnrichmentSnapshot, meter: CostMeter, *,
                    path_of: PathOf, bulk_of: BulkOf, bulk_profile: BulkProfile) -> Dict[str, object]:
    """`real` holds the runners: profiler, passages, triage, reader."""
    return {
        "profiler": RecordingProfiler(real["profiler"], snapshot, meter, path_of=path_of,  # type: ignore[arg-type]
                                      bulk_of=bulk_of, bulk_profile=bulk_profile),
        "passages": RecordingPassages(real["passages"], snapshot, meter, path_of=path_of,  # type: ignore[arg-type]
                                      bulk_of=bulk_of),
        "triage": RecordingTriage(real["triage"], snapshot, meter, path_of=path_of),  # type: ignore[arg-type]
        "reader": RecordingReader(real["reader"], snapshot, meter, path_of=path_of),  # type: ignore[arg-type]
    }

