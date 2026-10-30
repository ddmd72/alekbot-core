"""Fakes for the transcript stage (USER_DRIVE_INDEXING_RFC §27.3-§27.6): a drive that hands out a fresh download URL per
call, a media extractor over synthetic recordings, a batch port, a runner and the tick's host. All data is synthetic."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.batch_transcription import BatchRef, BatchStatus, FileRef
from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_handle import DriveHandle
from src.domain.drive_media_source import MediaSource
from src.domain.drive_index import EXTRACTION_VERSION, DriveIndexRecord, RecordState, Scope
from src.domain.drive_index_params import TRANSCRIBE_MAX_SECONDS
from src.domain.drive_index_planner import drops_readable, month_key
from src.domain.drive_transcript import (
    MediaProbe, MediaProbeOutcome, MediaStatus, MediaWindowsOutcome, WindowInput, WindowSpec,
)
from src.domain.drive_transcript_plan import TranscriptContext, transcript_plan

USER = "u-test"
# the connection the stage runs for (MULTI_DRIVE_RFC §8); its id is the user's, so a repository key reads as before
HANDLE = DriveHandle(user_id=USER, connection_id=USER, credential_id="cred-test", kind="fake")
SCOPE = HANDLE.scope
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=3)
URL_PREFIX = "https://graph.invalid/download/"


class Clock:
    def __init__(self) -> None:
        self.now = NOW
        self.mono = 0.0

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.mono += seconds


def record(item_id: str, *, kind: str = "audio", content_hash: Optional[str] = None, day: str = "2026-10-01",
           **kw: Any) -> DriveIndexRecord:
    base: Dict[str, Any] = dict(user_id=USER, connection_id=USER, item_id=item_id, identity=f"fake:d1:{item_id}",
                                kind=kind, scope=Scope.NORMAL, state=RecordState.PENDING, content_version="c1",
                                content_identity=ContentIdentity("fam", content_hash or f"hash-{item_id}", 1_000_000),
                                name=f"{item_id}.webm", mime_type="audio/webm", size_bytes=1_000_000,
                                modified_at=OLD, effective_day=day, parent_id="p")
    base.update(kw)
    return DriveIndexRecord(**base)


class FakeDrive:
    """`media_source` hands out a new header-free URL on every call (the URL lives ~60 min, Δ134); `headers` makes it a
    source that needs request headers (E24)."""

    def __init__(self) -> None:
        self.calls: List[str] = []
        self.raise_for: Dict[str, Exception] = {}
        self.headers: Dict[str, str] = {}

    async def media_source(self, handle: Any, item_id: str) -> MediaSource:
        if item_id in self.raise_for:
            raise self.raise_for[item_id]
        self.calls.append(item_id)
        return MediaSource(url=f"{URL_PREFIX}{item_id}/{len(self.calls)}?token=secret-{len(self.calls)}",
                           headers=dict(self.headers))


@dataclass
class Recording:
    """A synthetic recording: its real length, whether the container states it, and what goes wrong."""
    duration: float
    has_audio: bool = True
    known: bool = True
    container: str = "matroska,webm"
    header_duration: Optional[float] = None    # what the header says when it overstates (T-R16)
    probe_error: Optional[BaseException] = None
    probe_status: MediaStatus = MediaStatus.OK
    extract_error: Optional[BaseException] = None
    status: MediaStatus = MediaStatus.OK     # TIMEOUT/MEMORY with nothing produced
    cut_after: Optional[int] = None          # TIMEOUT after this many windows in one run


def _item_of(url: str) -> str:
    return url[len(URL_PREFIX):].split("/", 1)[0]


class FakeMedia:
    """Extracts windows of synthetic recordings. `seconds_per_window` advances the clock (the wall is real time)."""

    def __init__(self, clock: Clock, seconds_per_window: float = 30.0) -> None:
        self.clock = clock
        self.recordings: Dict[str, Recording] = {}
        self.seconds_per_window = seconds_per_window
        self.probes: List[str] = []
        self.runs: List[Dict[str, Any]] = []
        self.on_extract: Optional[Callable[[str], None]] = None

    async def probe(self, url: str) -> MediaProbeOutcome:
        assert url.startswith(URL_PREFIX)
        self.probes.append(url)
        rec = self.recordings[_item_of(url)]
        if rec.probe_error is not None:
            raise rec.probe_error
        if rec.probe_status is not MediaStatus.OK:
            return MediaProbeOutcome(status=rec.probe_status, detail="cut")
        duration = rec.header_duration if rec.header_duration is not None else rec.duration
        return MediaProbeOutcome(status=MediaStatus.OK, probe=MediaProbe(
            duration_s=duration if rec.known else 0.0, has_audio=rec.has_audio, container=rec.container,
            duration_known=rec.known))

    async def extract_windows(self, url: str, windows: Sequence[WindowSpec], out_dir: str, *, wall_s: float,
                              container: str, duration_known: bool = True) -> MediaWindowsOutcome:
        item = _item_of(url)
        self.runs.append({"item": item, "url": url, "indexes": [w.index for w in windows], "wall_s": wall_s,
                          "container": container, "duration_known": duration_known})
        if self.on_extract is not None:
            self.on_extract(item)
        rec = self.recordings[item]
        if rec.extract_error is not None:
            raise rec.extract_error
        if rec.status is not MediaStatus.OK:
            self.clock.advance(wall_s)
            return MediaWindowsOutcome(status=rec.status, detail="cut")
        produced: List[Tuple[WindowSpec, str]] = []
        eof = False
        for spec in windows:
            walled = (len(produced) + 1) * self.seconds_per_window > wall_s
            if walled or (rec.cut_after is not None and len(produced) >= rec.cut_after):
                self.clock.advance(max(0.0, wall_s - self.seconds_per_window * len(produced)))
                return MediaWindowsOutcome(status=MediaStatus.TIMEOUT, windows=tuple(produced), detail="wall")
            if spec.start_s >= rec.duration:
                eof = True
                break
            length = min(spec.duration_s, rec.duration - spec.start_s)
            path = os.path.join(out_dir, f"window_{spec.index}.opus")
            with open(path, "wb") as fh:
                fh.write(b"OggS-synthetic")
            produced.append((WindowSpec(spec.index, spec.start_s, length), path))
            self.clock.advance(self.seconds_per_window)
            if length < spec.duration_s:
                eof = True
                break
        bound = (not duration_known and not eof and rec.duration > TRANSCRIBE_MAX_SECONDS
                 and windows[-1].start_s + windows[-1].duration_s >= TRANSCRIBE_MAX_SECONDS)
        return MediaWindowsOutcome(status=MediaStatus.OK, windows=tuple(produced),
                                   duration_s=rec.duration if eof else 0.0, eof=eof, reached_bound=bound)


class FakeBatch:
    """The batch port's file side (the runner owns submit/collect in these tests). Polling answers only once a test turns
    it on (`polling = True`, Task 13): stage A must never poll."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.uploads: List[str] = []
        self.deleted: List[str] = []
        self.fail_upload: List[Optional[BaseException]] = []   # consumed one per upload; None = succeed
        self.polling = False
        self.statuses: Dict[str, BatchStatus] = {}             # by batch name; RUNNING when absent
        self.poll_errors: Dict[str, BaseException] = {}
        self.polls: List[str] = []

    async def upload_audio(self, path: str, mime: str) -> FileRef:
        assert os.path.exists(path) and mime == "audio/ogg"
        if self.fail_upload:
            failure = self.fail_upload.pop(0)
            if failure is not None:
                raise failure
        self.uploads.append(path)
        n = len(self.uploads)
        return FileRef(name=f"files/f{n}", uri=f"https://files.invalid/v1/files/f{n}",
                       expires_at=self.clock.now + timedelta(hours=48))

    async def submit(self, requests: Any, *, display_name: str) -> BatchRef:
        raise AssertionError("the stage submits through the runner")

    async def poll(self, batch: BatchRef) -> Any:
        if not self.polling:
            raise AssertionError("polling is collection's (Task 13)")
        assert batch.keys is not None
        self.polls.append(batch.name)
        if batch.name in self.poll_errors:
            raise self.poll_errors[batch.name]
        return self.statuses.get(batch.name, BatchStatus.RUNNING)

    async def collect(self, batch: BatchRef) -> Any:
        raise AssertionError("collection is Task 13's")

    async def delete_file(self, ref: FileRef) -> None:
        self.deleted.append(ref.name)


class FakeRunner:
    def __init__(self) -> None:
        self.submits: List[List[WindowInput]] = []
        self.raise_next: Optional[BaseException] = None
        self.results: Dict[str, Mapping[str, Any]] = {}       # by batch name: what `collect` returns (Task 13)
        self.collects: List[Tuple[BatchRef, str]] = []
        self.bills: List[bool] = []                          # `bill` of each collect: True = the account billed
        self.collect_error: Optional[BaseException] = None

    async def submit(self, inputs: Sequence[WindowInput], *, display_name: str) -> BatchRef:
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        self.submits.append(list(inputs))
        return BatchRef(name=f"batches/b{len(self.submits)}", keys=tuple(i.key for i in inputs))

    async def collect(self, batch: BatchRef, *, account_id: str, bill: bool = True) -> Mapping[str, Any]:
        if batch.name not in self.results:
            raise AssertionError("collection is Task 13's: no results set for this batch")
        assert batch.keys, "collect needs the batch's request keys (a BatchRef rebuilt without them drops results)"
        if self.collect_error is not None:
            exc, self.collect_error = self.collect_error, None
            raise exc
        self.collects.append((batch, account_id))
        self.bills.append(bill)
        return self.results[batch.name]


class FakeHost:
    """The tick's side as Task 14 will build it: every commit re-plans the transcript job (`transcript_plan`) and
    collects a dropped readable generation; reservations go to the usage document of their month."""

    def __init__(self, repo: InMemoryDriveIndexRepository, clock: Clock) -> None:
        self.repo = repo
        self.clock = clock
        self.fences = 0
        self.commits: List[DriveIndexRecord] = []
        self.collected: List[str] = []
        self.indexed: List[Tuple[str, str, str]] = []    # (item_id, body, extracted_by)
        self.reserves: List[Tuple[float, str]] = []
        self.releases: List[Tuple[float, str]] = []
        self.spends: List[Tuple[str, float, int, str]] = []
        self.backed_off: set = set()
        self.outages: List[str] = []
        self.drop_index_result = False
        self.account_id = "acct-test"
        self.events: List[Tuple[str, float, str]] = []   # ("spend" | "reserve" | "release", usd, month), in order
        self.file_slots: Optional[int] = None            # the tick's TICK_FILE_BUDGET slots left (G32); None = no limit
        self.on_index: Optional[Callable[[DriveIndexRecord], Any]] = None   # runs inside `index_transcript`
        self.handle = HANDLE                              # the connection the tick is for (MULTI_DRIVE_RFC §8)

    def fence(self) -> None:
        self.fences += 1

    def plan_ctx(self) -> TranscriptContext:
        return TranscriptContext(now=self.clock(), enabled=True, month_usd=0.0, reserved_usd=0.0, cap_usd=20.0)

    async def commit(self, records: Sequence[DriveIndexRecord], before: Mapping[str, DriveIndexRecord]) -> None:
        planned = [transcript_plan(r, self.plan_ctx()) for r in records]
        for r in planned:
            if drops_readable(before.get(r.item_id), r):
                self.collected.append(r.item_id)
        self.commits.extend(planned)
        await self.repo.upsert_records(planned)

    async def index_transcript(self, rec: DriveIndexRecord, body: str,
                               extracted_by: str) -> Optional[DriveIndexRecord]:
        if self.file_slots is not None:
            if self.file_slots <= 0:
                return None
            self.file_slots -= 1
        self.indexed.append((rec.item_id, body, extracted_by))
        if self.on_index is not None:
            if await self.on_index(rec) is False:
                return None
        if self.drop_index_result:
            return None
        done = replace(rec, state=RecordState.INDEXED, reason=None, indexed_generation=rec.index_generation + 1,
                       index_generation=rec.index_generation + 1, indexed_version=rec.content_version,
                       extraction_version=EXTRACTION_VERSION, extracted_by=extracted_by, text_chars=len(body))
        await self.commit([done], {rec.item_id: rec})
        return (await self.repo.get_records(self.handle.scope, [rec.item_id]))[rec.item_id]

    async def spend(self, cls: str, usd: float, calls: int, month: str) -> None:
        self.spends.append((cls, usd, calls, month))
        self.events.append(("spend", usd, month))

    async def reserve(self, usd: float, month: str) -> None:
        self.reserves.append((usd, month))
        self.events.append(("reserve", usd, month))
        await self.repo.reserve_model_usd(USER, month, "transcribe", usd)

    async def release(self, usd: float, month: str) -> None:
        self.releases.append((usd, month))
        self.events.append(("release", usd, month))
        await self.repo.reserve_model_usd(USER, month, "transcribe", -usd)

    def agent_available(self, agent: str) -> bool:
        return agent not in self.backed_off

    async def agent_outage(self, agent: str) -> None:
        self.outages.append(agent)
        self.backed_off.add(agent)

    async def reserved(self) -> float:
        return (await self.repo.get_model_usage(USER, month_key(self.clock()))).transcribe_reserved_usd
