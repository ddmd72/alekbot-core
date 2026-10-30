"""The transcript stage in the drive indexer's tick (USER_DRIVE_INDEXING_RFC §27.3, §27.10-§27.13; plan Task 14A; G16,
G32, T-R3, T-R10, T-R20): the host the tick lends the stage, the kill switch and the per-user flag, placement and
shares, the continuation, `_commit`'s transcript plan and job drops, the tick line and the Cabinet's cap. The real
DriveIndexingService and DriveTranscriptStage on the in-memory repository, with synthetic recordings only."""
from __future__ import annotations

import itertools
import logging
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.batch_transcription import BatchStatus
from src.domain.drive_card import card_hash
from src.domain.drive_enrichment import JobState, UsageClass
from src.domain.drive_index import (
    CURRENT_VERSIONS, EXTRACTION_VERSION, DriveIndexRecord, DriveSyncState, Reason,
    RecordState, Scope,
)
from src.domain.drive_index_params import (
    BATCH_POLL_FIRST_S, ENRICH_FILE_BUDGET_S, ENRICH_SHARE, FILE_START_MARGIN_S, TICK_DEADLINE_S, TICK_START_BUDGET,
    TRANSCRIBE_EXTRACT_WALL_S, TRANSCRIBE_SHARE, TRANSCRIBE_UPLOAD_S, TRANSCRIBE_USD_PER_MONTH,
)
from src.domain.drive_index_planner import month_key
from src.domain.drive_search import NOT_READ_YET_COUNTER, DriveSearchRequest
from src.domain.drive_transcript import Segment, WindowOutcome, WindowResult, WindowTranscript
from src.domain.drive_transcript_plan import (
    TRANSCRIBER_AGENT, TranscriptContext, charge_attempt, effective_monthly_cap, leaves_job, transcript_plan,
)
from src.domain.exceptions import LLMClientError, LLMUnavailableError
from src.services import drive_indexing_service as svc_mod
from src.services.drive_indexing_service import DriveIndexingService, _Budget
from src.services.drive_search_service import DriveSearchService
from src.services.drive_transcript_stage import DriveTranscriptStage

from .drive_content_world import admitted, prose  # noqa: F401  (fixture)
from .drive_enrich_world import EnrichWorld
from .drive_index_fakes import T0
from .drive_transcript_fakes import FakeBatch, FakeDrive, FakeMedia, FakeRunner, Recording
from .drive_index_fakes import LegacyConnections

pytestmark = pytest.mark.usefixtures("admitted")

SPACE = "test-space/8"
NEW_QUERIES = ("transcript_queue", "transcript_wakeups", "get_open_batches", "jobs_by_content_key",
               "orphan_transcripts", "get_transcript", "transcript_records", "transcript_key_in_use")
SPOKEN = "We agreed to paint the flamingo fence in the garden before the spring market opens."


class SpyRepo(InMemoryDriveIndexRepository):
    """Records every call of the transcript queries (and the job read) the tick must not make while transcription
    is off."""

    def __init__(self) -> None:
        super().__init__()
        self.transcript_calls: List[str] = []

    async def transcript_queue(self, *a: Any, **kw: Any):
        self.transcript_calls.append("transcript_queue")
        return await super().transcript_queue(*a, **kw)

    async def transcript_wakeups(self, *a: Any, **kw: Any):
        self.transcript_calls.append("transcript_wakeups")
        return await super().transcript_wakeups(*a, **kw)

    async def get_open_batches(self, *a: Any, **kw: Any):
        self.transcript_calls.append("get_open_batches")
        return await super().get_open_batches(*a, **kw)

    async def jobs_by_content_key(self, *a: Any, **kw: Any):
        self.transcript_calls.append("jobs_by_content_key")
        return await super().jobs_by_content_key(*a, **kw)

    async def orphan_transcripts(self, *a: Any, **kw: Any):
        self.transcript_calls.append("orphan_transcripts")
        return await super().orphan_transcripts(*a, **kw)

    async def get_transcript(self, *a: Any, **kw: Any):
        self.transcript_calls.append("get_transcript")
        return await super().get_transcript(*a, **kw)

    async def transcript_records(self, *a: Any, **kw: Any):
        self.transcript_calls.append("transcript_records")
        return await super().transcript_records(*a, **kw)

    async def transcript_key_in_use(self, *a: Any, **kw: Any):
        self.transcript_calls.append("transcript_key_in_use")
        return await super().transcript_key_in_use(*a, **kw)

    async def get_transcript_job(self, *a: Any, **kw: Any):
        self.transcript_calls.append("get_transcript_job")
        return await super().get_transcript_job(*a, **kw)


class Users:
    """The user's two flags: drive indexing (the opt-in) and transcription (Δ125)."""

    def __init__(self, transcribe: bool) -> None:
        self.indexing = True
        self.transcribe = transcribe

    async def get_user(self, user_id: str) -> Any:
        return SimpleNamespace(user_id=user_id, account_id="a", config=SimpleNamespace(
            drive_indexing_enabled=self.indexing, drive_transcribe_enabled=self.transcribe))


class StageSpy:
    """Counts the stage's entry points (the stage itself is the real one)."""

    def __init__(self, stage: DriveTranscriptStage) -> None:
        self.calls: List[str] = []
        for name in ("extract_step", "cheap_steps", "drop_jobs"):
            real = getattr(stage, name)

            async def spy(*a: Any, _real: Any = real, _name: str = name, **kw: Any) -> Any:
                self.calls.append(_name)
                return await _real(*a, **kw)
            setattr(stage, name, spy)


class TranscriptWorld(EnrichWorld):
    def __init__(self, tmp_path: Any, *, switch: bool = True, flag: bool = True, enrich: bool = False,
                 seconds_per_window: float = 30.0, cap: float = TRANSCRIBE_USD_PER_MONTH,
                 tick_cap: float = TRANSCRIBE_USD_PER_MONTH) -> None:
        super().__init__()
        self.repo = SpyRepo()
        self.users = Users(flag)
        self.urls = FakeDrive()
        self.media = FakeMedia(self.clock, seconds_per_window=seconds_per_window)
        self.batch = FakeBatch(self.clock)
        self.batch.polling = True
        self.runner = FakeRunner()
        self.stage = DriveTranscriptStage(self.repo, self.urls, self.media, self.batch, self.runner, clock=self.clock,
                                          monotonic=self.clock.monotonic, monthly_cap_usd=cap,
                                          temp_root=str(tmp_path))
        self.stage_spy = StageSpy(self.stage)
        self.svc = DriveIndexingService(
            self.drive, self.repo, self.emb, self.queue, self.users, lambda lat, lon: "Rome (Roma), Italy", SPACE,
            extractors={"parser": self.parser, "reader": self.reader}, triage=self.triage,
            profiler=self.profiler if enrich else None, passages=self.passages if enrich else None,
            transcripts=self.stage, transcribe_enabled=switch, transcribe_cap_usd=tick_cap, connections=LegacyConnections(self.users, self.drive), clock=self.clock,
            monotonic=self.clock.monotonic)
        self.states: Dict[str, List[str]] = {}
        real_upsert = self.repo.upsert_records

        async def upsert(records, vectors=None):
            for r in records:
                seen = self.states.setdefault(r.item_id, [])
                if not seen or seen[-1] != r.transcript_state:
                    seen.append(r.transcript_state)
            return await real_upsert(records, vectors)
        self.repo.upsert_records = upsert

    def add_recording(self, name: str = "memo.m4a", duration: float = 600.0, **kw: Any) -> str:
        item = self.drive.add_file(name, b"\x00" * 64, mime_type="audio/mp4", **kw)
        self.media.recordings[item] = Recording(duration=duration)
        return item

    def batches(self):
        return sorted(self.repo.batches.values(), key=lambda b: b.name)

    def job(self, item_id: str):
        return self.repo.transcript_jobs.get(("u", item_id))

    def reserved(self) -> float:
        usage = self.repo.usage.get(("u", month_key(self.clock())))
        return usage.transcribe_reserved_usd if usage is not None else 0.0

    def answer(self, batch, text: str = SPOKEN) -> None:
        """Every window of the batch answered well: a stamp at least every 50 s and one at 95 % of the window."""
        out = {}
        for key in batch.request_keys:
            item_id, index = key.rsplit(":", 1)
            spec = next(w.spec for w in self.job(item_id).windows if w.spec.index == int(index))
            end = spec.duration_s * 0.95
            starts = [float(s) for s in range(0, int(end), 50)] + [end]
            segments = tuple(Segment(s, None, f"{text} Part {i}.") for i, s in enumerate(starts))
            transcript = WindowTranscript(language_codes=("en",), speakers=0, no_speech=False, segments=segments)
            out[key] = WindowResult(key=key, outcome=WindowOutcome.OK, transcript=transcript, usd=0.01, billed=True)
        self.runner.results[batch.name] = out
        self.batch.statuses[batch.name] = BatchStatus.SUCCEEDED

    def continued(self, since: int) -> bool:
        return any(":cont:" in (t["key"] or "") for t in self.queue.tasks[since:])

    async def tick_counting(self):
        before = len(self.queue.tasks)
        result = await self.tick()
        return result, self.continued(before)

    async def to_waiting(self, item_id: str) -> None:
        await self.settle()
        assert self.rec(item_id).transcript_state == "waiting", self.states.get(item_id)

    async def to_current(self, item_id: str) -> None:
        await self.to_waiting(item_id)
        self.answer(self.batches()[-1])
        self.clock.advance(BATCH_POLL_FIRST_S + 1)
        await self.settle()


@pytest.fixture
def world(tmp_path):
    def make(**kw: Any) -> TranscriptWorld:
        return TranscriptWorld(tmp_path, **kw)
    return make


# -- the gates (§27.12, §27.13, Δ125) --------------------------------------------------------------------------

@pytest.mark.parametrize("switch,flag", [(False, True), (True, False)], ids=["kill_switch_off", "user_flag_off"])
async def test_kill_switch_off_no_stage_runs(world, switch, flag):
    w = world(switch=switch, flag=flag)
    audio = w.add_recording()
    w.add_doc("notes.md", prose(5))
    await w.settle()
    assert w.stage_spy.calls == []
    assert w.media.probes == [] and w.media.runs == [] and w.urls.calls == []       # no ffmpeg, no download URL
    assert w.batch.uploads == [] and w.batch.polls == [] and w.runner.submits == []  # no upload, no poll, no model
    assert w.repo.transcript_calls == []
    rec = w.rec(audio)
    assert rec.state is RecordState.PENDING and rec.transcript_state == "ready"   # planned, never acted on
    assert w.job(audio) is None and w.reserved() == 0.0


async def test_user_flag_off_no_stage_runs(world):
    w = world(switch=True, flag=False)
    w.add_recording()
    await w.settle()
    assert w.stage_spy.calls == [] and w.media.probes == [] and w.repo.transcript_calls == []


async def test_both_on_runs_stage(world):
    w = world()
    audio = w.add_recording()
    await w.tick()
    assert "cheap_steps" in w.stage_spy.calls and "extract_step" in w.stage_spy.calls
    assert len(w.media.probes) == 1 and len(w.runner.submits) == 1
    assert w.rec(audio).transcript_state == "waiting" and w.reserved() > 0


async def test_the_flag_is_read_once_per_tick(world):
    """A flag flipped mid-tick takes effect on the next tick: the gate's read is kept for the whole tick."""
    w = world(flag=False)
    w.add_recording()

    def flip(request):
        w.users.transcribe = True   # the opt-in re-check reads the flag again mid-tick
    w.parser.before = flip
    w.add_doc("a.md", prose(5))
    await w.tick()
    assert w.stage_spy.calls == []
    await w.tick()
    assert "extract_step" in w.stage_spy.calls


async def test_no_new_composite_query_while_off(world):
    """With either gate off, a whole sequence — onboarding, settled counters, an upgrade pass over media records,
    edits, a delete and the opt-out purge — calls none of the transcript queries (nor reads a job)."""
    for switch, flag in ((False, True), (True, False)):
        w = world(switch=switch, flag=flag)
        audio = w.add_recording()
        doc = w.add_doc("notes.md", prose(5))
        await w.settle()
        # an upgrade pass over a media record still at policy 2 (the §27.3 re-classification)
        old = w.rec(audio)
        w.repo.records[("u", audio)] = replace(old, state=RecordState.METADATA_ONLY, reason=Reason.KIND_NOT_ADMITTED,
                                               policy_version=2, transcript_state="none")
        state = w.repo.states["u"]
        w.repo.states["u"] = replace(state, applied_versions={**state.applied_versions, "policy": 2})
        w.clock.advance(timedelta(days=1).total_seconds())
        await w.settle()
        assert w.rec(audio).state is RecordState.PENDING
        assert w.repo.states["u"].applied_versions["policy"] == CURRENT_VERSIONS["policy"]
        w.drive.edit(doc, prose(6).encode(), modified_at=w.clock.now - timedelta(hours=1))
        w.drive.edit(audio, b"\x01" * 64, modified_at=w.clock.now - timedelta(hours=1))
        await w.settle()
        w.drive.delete(audio)
        await w.settle()
        w.users.indexing = False
        await w.settle()   # the opt-out purge
        assert w.repo.states.get("u") is None
        assert w.repo.transcript_calls == [], (switch, flag)
        assert w.stage_spy.calls == []


# -- placement and shares (§27.3, G16) -------------------------------------------------------------------------

def test_deadline_sum_assertion():
    """§27.3: the extraction step's worst case joins the §7.1 deadline sum, as enrichment's does: a step started at
    the end of its window — the window starting after a whole start budget and an enrichment file — still ends before
    the deadline, and `extraction_stop` never lets a step start past the last start that would."""
    worst = TRANSCRIBE_EXTRACT_WALL_S + TRANSCRIBE_UPLOAD_S + FILE_START_MARGIN_S
    start = TICK_START_BUDGET.total_seconds()
    assert start + ENRICH_FILE_BUDGET_S + FILE_START_MARGIN_S + TRANSCRIBE_SHARE * start + worst < TICK_DEADLINE_S
    clock = SimpleNamespace(t=0.0)
    budget = _Budget(started=0.0, monotonic=lambda: clock.t)
    for at in (0.0, start, start + ENRICH_FILE_BUDGET_S, TICK_DEADLINE_S - worst - 10, TICK_DEADLINE_S - worst):
        clock.t = at
        stop = budget.extraction_stop()
        if stop is None:
            assert at >= TICK_DEADLINE_S - worst
            continue
        assert stop > at and stop - at <= TRANSCRIBE_SHARE * start + 1e-9
        assert stop + worst <= TICK_DEADLINE_S
    clock.t = 0.0
    assert budget.extraction_stop() == pytest.approx(TRANSCRIBE_SHARE * start)   # the share, from where it begins


def test_shares_rule_unchanged_without_transcript_work():
    total = TICK_START_BUDGET.total_seconds()
    clock = SimpleNamespace(t=0.0)
    budget = _Budget(started=0.0, monotonic=lambda: clock.t)
    assert budget.transcribe_share == 0.0 and budget.enrichment_limit() == total
    clock.t = (1 - ENRICH_SHARE) * total - 0.01
    assert budget.content_allowed()
    budget.transcribe_share = TRANSCRIBE_SHARE
    assert not budget.content_allowed()                                   # content leaves the extraction share
    assert budget.enrichment_limit() == pytest.approx((1 - TRANSCRIBE_SHARE) * total)
    clock.t = (1 - ENRICH_SHARE - TRANSCRIBE_SHARE) * total - 0.01
    assert budget.content_allowed()


def _slow_docs(w, n: int = 6, seconds: float = 100.0) -> List[str]:
    w.parser.before = lambda request: w.clock.advance(seconds)
    return [w.add_doc(f"d{i}.md", f"Firm{i} Rentals. " + prose(30)) for i in range(n)]


async def test_shares_unchanged_when_no_transcript_work(world):
    """The enrichment tick's share test, with transcription on and no recording: the same three files (G16 does not
    fire without extraction work)."""
    w = world(enrich=True)
    docs = _slow_docs(w)
    await w.tick()
    assert sum(1 for d in docs if w.rec(d).state is RecordState.INDEXED) == 3   # 0, 100, 200 s; the share ends at 288 s


async def test_share_left_for_extraction_after_enrichment(world):
    """With a recording ready, content leaves TRANSCRIBE_SHARE (ends at 168 s): two files, then the extraction step
    in the same tick."""
    w = world(enrich=True)
    docs = _slow_docs(w)
    audio = w.add_recording()
    await w.tick()
    assert sum(1 for d in docs if w.rec(d).state is RecordState.INDEXED) == 2
    assert len(w.media.probes) == 1 and w.rec(audio).transcript_state == "waiting"


async def test_extraction_runs_when_due_loop_stopped_at_a_class(world):
    """The due loop stops at a content class (its share spent, the rest waits): the extraction step still runs."""
    w = world()
    docs = _slow_docs(w, n=4, seconds=200.0)
    audio = w.add_recording()
    result, continued = await w.tick_counting()
    assert sum(1 for d in docs if w.rec(d).state is RecordState.INDEXED) == 1
    assert continued and result.status == "done"
    assert len(w.media.probes) == 1 and w.rec(audio).transcript_state == "waiting"


# -- the continuation (§7.1, Δ104) -----------------------------------------------------------------------------

async def test_no_continuation_while_only_waiting(world):
    w = world()
    audio = w.add_recording()
    await w.to_waiting(audio)
    assert w.batches() and w.batches()[0].next_poll_at is not None   # open
    _, continued = await w.tick_counting()   # the batch is open, nothing ready: the fan-out polls it
    assert not continued


async def test_no_continuation_for_a_future_wake_up(world):
    """A recording the month's cap cannot pay is parked until next month: nothing to continue for."""
    w = world(cap=0.0)
    audio = w.add_recording()
    _, continued = await w.tick_counting()
    rec = w.rec(audio)
    assert rec.transcript_state == "parked" and rec.transcript_wake_at > w.clock()
    assert not continued
    _, continued = await w.tick_counting()
    assert not continued


async def test_continuation_while_extraction_ready(world):
    """Two recordings fit one extraction window (100 s each in a 120 s window); the third continues the chain."""
    w = world(seconds_per_window=100.0)
    items = [w.add_recording(f"memo{i}.m4a") for i in range(3)]
    _, continued = await w.tick_counting()
    states = sorted(w.rec(i).transcript_state for i in items)
    assert states.count("ready") == 1 and continued
    await w.settle()
    assert all(w.rec(i).transcript_state == "waiting" for i in items)


async def test_no_continuation_while_the_backoff_holds(world):
    w = world()
    w.add_recording()
    w.runner.raise_next = LLMUnavailableError("down")
    _, continued = await w.tick_counting()
    state = w.repo.states["u"]
    assert TRANSCRIBER_AGENT in state.model_backoff_by_agent   # the stage's outage, persisted with the state
    assert not continued


async def test_a_step_that_cannot_move_does_not_chain(world):
    """An edited recording whose old batch still runs (T-R20) is ready but left alone: the chain stops (at most one
    tick re-plans its wake-up), it never loops with zero delay."""
    w = world()
    audio = w.add_recording()
    await w.to_waiting(audio)
    w.drive.edit(audio, b"\x02" * 64, modified_at=w.clock.now - timedelta(hours=1))
    w.clock.advance(timedelta(hours=25).total_seconds())   # past the edit cooldown (T-R9)
    await w.settle(max_ticks=5)
    rec = w.rec(audio)
    assert rec.transcript_state == "ready"
    assert w.job(audio) is not None and w.batches()[0].next_poll_at is not None   # left for collection (T-R20)
    _, continued = await w.tick_counting()
    assert not continued


# -- the host (§27.8 a, G32) -----------------------------------------------------------------------------------

async def test_finalized_transcript_is_searchable(world):
    """ready -> working -> waiting -> current, then found by what was said: the transcript is the record's readable
    text (chunks), its card carries the transcript line and is embedded again, and the profile and passage jobs plan
    it as any text."""
    w = world()
    audio = w.add_recording()
    await w.to_current(audio)
    rec = w.rec(audio)
    seq = w.states[audio]
    assert [s for s in seq if s in ("ready", "working", "waiting", "current")][-4:] == [
        "ready", "working", "waiting", "current"]
    assert rec.transcript_state == "current" and rec.state is RecordState.INDEXED
    assert rec.extraction_version == EXTRACTION_VERSION and rec.extracted_by.startswith("transcribe")
    chunks = w.readable(audio)
    assert chunks and any("flamingo" in c.text for c in chunks)
    assert "transcript:" in w.card_texts()[audio]
    assert rec.card_hash == card_hash(rec)                                   # the card was embedded again
    assert rec.profile_state == JobState.READY.value                         # the enrichment jobs take it as text
    assert w.job(audio) is None and w.reserved() == pytest.approx(0.0)       # settled
    search = DriveSearchService(w.repo, w.emb, w.users, w.drive, clock=w.clock,
                                nonce=lambda c=itertools.count(1): f"nonce{next(c):04d}", embedding_space=SPACE)
    found = await search.search("u", DriveSearchRequest(query="flamingo"))
    assert [c.path for c in found.cards] == ["memo.m4a"]


async def test_index_transcript_without_a_slot_leaves_the_record_untouched(world, monkeypatch):
    w = world()
    audio = w.add_recording()
    await w.to_waiting(audio)
    w.answer(w.batches()[-1])
    w.clock.advance(BATCH_POLL_FIRST_S + 1)
    monkeypatch.setattr(svc_mod, "TICK_FILE_BUDGET", 0)
    before = w.rec(audio)
    await w.tick()
    after = w.rec(audio)
    assert after.transcript_state == "waiting" and after.index_generation == before.index_generation
    assert w.readable(audio) == [] and w.job(audio) is not None
    monkeypatch.setattr(svc_mod, "TICK_FILE_BUDGET", 10)
    await w.settle()
    assert w.rec(audio).transcript_state == "current"


async def test_a_refused_transcript_embedding_is_finalized_again(world):
    """A per-file failure of the index run (a chunk the embedding model refuses) is counted on the record like any
    run; the stage gets None and finalizes from the job again in a later step, with no model call."""
    w = world()
    audio = w.add_recording()
    await w.to_waiting(audio)
    w.answer(w.batches()[-1])
    w.clock.advance(BATCH_POLL_FIRST_S + 1)
    real = w.emb.get_embedding
    refused = {"n": 0}

    async def once(text, task_type="RETRIEVAL_DOCUMENT"):
        if "flamingo" in text and not refused["n"]:
            refused["n"] += 1
            raise LLMClientError("400 INVALID_ARGUMENT", http_status=400)
        return await real(text, task_type)
    w.emb.get_embedding = once
    await w.tick()
    rec = w.rec(audio)
    assert rec.transcript_state == "waiting" and rec.attempts == 1 and w.job(audio) is not None
    assert w.readable(audio) == []
    w.clock.advance(timedelta(hours=1).total_seconds())
    await w.settle()
    rec = w.rec(audio)
    assert rec.transcript_state == "current" and rec.state is RecordState.INDEXED and w.readable(audio)
    assert len(w.runner.collects) == 1                                   # collected once, never bought again


async def test_a_systemic_embedding_error_ends_the_tick_and_keeps_the_job(world):
    w = world()
    audio = w.add_recording()
    await w.to_waiting(audio)
    w.answer(w.batches()[-1])
    w.clock.advance(BATCH_POLL_FIRST_S + 1)
    real = w.emb.get_embedding

    async def down(text, task_type="RETRIEVAL_DOCUMENT"):
        if "flamingo" in text:
            raise LLMUnavailableError("embeddings down")
        return await real(text, task_type)
    w.emb.get_embedding = down
    result = await w.tick()
    assert result.status == "throttled"
    rec = w.rec(audio)
    assert rec.transcript_state == "waiting" and rec.attempts == 0 and rec.attempt_started_at is None
    w.emb.get_embedding = real
    w.clock.advance(timedelta(hours=1).total_seconds())
    await w.settle()
    assert w.rec(audio).transcript_state == "current"


async def test_tick_line_counters(world, caplog):
    w = world()
    audio = w.add_recording()
    with caplog.at_level(logging.INFO):
        await w.to_current(audio)
    ticks = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[DriveIndex] tick ")]
    assert any("'windows_extracted': 1" in t and "'windows_submitted': 1" in t and "transcribe_reserved_usd=$+" in t
               for t in ticks)
    assert any("'windows_collected': 1" in t and "'transcripts': 1" in t and "transcribe_usd=$0.0100" in t
               and "transcribe_reserved_usd=$-" in t for t in ticks)


async def test_counters_count_recordings_only_while_transcribing(world):
    on = world()
    audio = on.add_recording()
    await on.settle()
    assert on.rec(audio).state is RecordState.PENDING
    assert on.repo.states["u"].counters.get(NOT_READ_YET_COUNTER) == 1
    assert on.repo.states["u"].counters.get("pending") == 1
    off = world(switch=False)
    off.add_recording()
    await off.settle()
    assert NOT_READ_YET_COUNTER not in off.repo.states["u"].counters
    assert "pending" not in off.repo.states["u"].counters


# -- jobs that leave the pipeline (Task 12 carry, T-R20) --------------------------------------------------------

async def test_a_deleted_recording_drops_its_job(world):
    w = world(seconds_per_window=100.0)
    audio = w.add_recording(duration=4000.0)   # three windows, two per step: still working after the tick
    await w.tick()
    assert w.rec(audio).transcript_state == "working" and w.job(audio) is not None and w.reserved() > 0
    w.drive.delete(audio)
    await w.tick()
    assert w.rec(audio) is None and w.job(audio) is None
    assert w.reserved() == pytest.approx(0.0) and w.batch.deleted   # released once, provider files deleted


async def test_an_edit_drops_the_old_job_at_once(world):
    """A new content version takes the record off its job: the job goes at the edit's write, its reservation
    released then, not when the stage reaches the record after the edit cooldown (T-R9)."""
    w = world(seconds_per_window=100.0)
    audio = w.add_recording(duration=4000.0)
    await w.tick()
    assert w.job(audio) is not None and w.reserved() > 0
    w.drive.edit(audio, b"\x03" * 64, modified_at=w.clock.now - timedelta(hours=1))
    await w.tick()
    rec = w.rec(audio)
    assert (rec.transcript_state, rec.transcript_hold) == ("held", "cooldown")
    assert w.job(audio) is None and w.reserved() == pytest.approx(0.0) and w.batch.deleted


async def test_a_job_with_a_window_in_a_batch_is_left_on_delete(world):
    w = world()
    audio = w.add_recording()
    await w.to_waiting(audio)
    reserved = w.reserved()
    w.drive.delete(audio)
    await w.tick()
    assert w.rec(audio) is None
    assert w.job(audio) is not None and w.reserved() == pytest.approx(reserved)   # collection settles it (G33)
    w.answer(w.batches()[-1])
    w.clock.advance(BATCH_POLL_FIRST_S + 1)
    await w.settle()
    assert w.job(audio) is None and w.reserved() == pytest.approx(0.0)


async def test_a_card_capped_recording_leaves_the_pipeline_and_its_job_is_dropped(world, monkeypatch):
    """T-R24 (I1): a recording the card cap evicts is not bought — its reading would never be found — so its job goes
    and the reservation is released exactly once; nothing more is extracted for it."""
    w = world(seconds_per_window=100.0)
    audio = w.add_recording(duration=4000.0)   # three windows, two per step: still working after the tick
    await w.tick()
    assert w.rec(audio).transcript_state == "working" and w.reserved() > 0
    releases: List[float] = []
    real = w.repo.reserve_model_usd

    async def spy(user_id, month, cls, delta):
        if delta < 0:
            releases.append(delta)
        return await real(user_id, month, cls, delta)
    w.repo.reserve_model_usd = spy
    # the card cap is full and a better-ranked file arrives: the recording's card is evicted (`capped`, §5.4)
    monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 1)
    w.repo.records[("u", audio)] = replace(w.rec(audio), card_rank=99)
    w.add_doc("lease.md", prose(5))
    runs = len(w.media.runs)
    await w.settle()
    rec = w.rec(audio)
    assert rec.card_capped and rec.transcript_state == "none"
    assert w.job(audio) is None and w.reserved() == pytest.approx(0.0) and len(releases) == 1
    assert len(w.media.runs) == runs and w.runner.submits == []


async def test_a_transcript_the_chunk_cap_refused_is_indexed_from_the_store_once_released(world, monkeypatch):
    """T-R24 (I2): the chunk cap refuses the transcript (`metadata_only / user_chunk_cap`, the transcript stored and
    `current`); once the cap releases the record it is `pending`, not current, and the stage indexes it from the store:
    no new extraction, no new submission, no new collection."""
    from src.domain import drive_index_run as run_mod
    w = world()
    audio = w.add_recording()
    await w.to_waiting(audio)
    w.answer(w.batches()[-1])
    w.clock.advance(BATCH_POLL_FIRST_S + 1)
    monkeypatch.setattr(run_mod, "MAX_CHUNKS_PER_USER", 0)   # no headroom while it is finalized
    await w.tick()
    assert w.summary_total("demoted") == 1 and w.readable(audio) == []
    rec = w.rec(audio)
    assert rec.reason is Reason.USER_CHUNK_CAP or rec.transcript_state == "ready"   # refused, or released already
    assert w.job(audio) is None                                             # finalized: the job is gone
    monkeypatch.setattr(run_mod, "MAX_CHUNKS_PER_USER", 10_000)
    w.clock.advance(timedelta(hours=1).total_seconds())
    await w.settle()
    rec = w.rec(audio)
    assert rec.transcript_state == "current" and rec.state is RecordState.INDEXED and w.readable(audio)
    assert len(w.runner.submits) == 1 and len(w.runner.collects) == 1 and len(w.media.runs) == 1


def _ctx(w) -> TranscriptContext:
    return TranscriptContext(now=w.clock(), enabled=True, month_usd=0.0, reserved_usd=0.0, cap_usd=20.0)


def _media(**kw: Any) -> DriveIndexRecord:
    base: Dict[str, Any] = dict(user_id="u", item_id="m", kind="audio", scope=Scope.NORMAL, state=RecordState.PENDING,
                                content_version="c1", name="m.m4a", size_bytes=10, modified_at=T0 - timedelta(days=3))
    base.update(kw)
    return DriveIndexRecord(**base)


def test_leaves_job_only_when_the_record_leaves_or_changes_version():
    ctx = TranscriptContext(now=T0, enabled=True, month_usd=0.0, reserved_usd=0.0, cap_usd=20.0)
    working = _media(transcript_state="working", transcript_key="item:m:c1")
    assert not leaves_job(working, transcript_plan(working, ctx))                       # same version: kept
    edited = replace(working, content_version="c2")
    assert leaves_job(edited, transcript_plan(edited, ctx))                             # a new version resets it
    gone = replace(working, kind="pdf")
    assert leaves_job(gone, transcript_plan(gone, ctx))                                 # out of the pipeline
    held = charge_attempt(working, now=T0)                                              # a retry hold ...
    woken = transcript_plan(held, replace(ctx, now=T0 + timedelta(days=1)))
    assert woken.transcript_state == "ready" and not leaves_job(held, woken)            # ... ending keeps its windows
    for state in ("none", "current", "failed"):
        rec = _media(transcript_state=state)
        assert not leaves_job(rec, replace(rec, transcript_state="none"))


# -- money (G8, G9, T-R3) --------------------------------------------------------------------------------------

async def test_reservations_and_spend_go_through_the_usage_documents(world):
    w = world()
    audio = w.add_recording()
    await w.to_waiting(audio)
    month = month_key(w.clock())
    usage = w.repo.usage[("u", month)]
    assert usage.transcribe_reserved_usd > 0 and usage.transcribe_usd == 0.0
    w.answer(w.batches()[-1])
    w.clock.advance(BATCH_POLL_FIRST_S + 1)
    await w.settle()
    usage = w.repo.usage[("u", month)]
    assert usage.transcribe_usd == pytest.approx(0.01) and usage.transcribe_reserved_usd == pytest.approx(0.0)
    assert usage.transcribe_initial_usd == 0.0                                          # G8: the build never pays


@pytest.mark.parametrize("raw,cap", [("0", 0.0), ("5", 5.0), ("50", TRANSCRIBE_USD_PER_MONTH),
                                     ("", TRANSCRIBE_USD_PER_MONTH), ("oops", TRANSCRIBE_USD_PER_MONTH)])
async def test_cap_override_only_lowers(world, raw, cap):
    """T-R3: the composition passes `effective_monthly_cap(DRIVE_TRANSCRIBE_USD_CAP)` (REQ-ARCH-17: no service reads
    the environment): the knob lowers the cap the tick's readiness math uses, never raises it, and a typo is the
    default. With nothing left of it, a ready recording is no extraction work (no share, no continuation); what is
    reserved this month counts against it."""
    assert effective_monthly_cap(raw) == cap
    w = world(tick_cap=effective_monthly_cap(raw))
    ready = transcript_plan(_media(), _ctx(w))
    assert ready.transcript_state == "ready"
    await w.repo.upsert_records([ready])
    models = svc_mod._Models(DriveSyncState(user_id="u", connection_id="u"))
    assert await w.svc._extraction_ready("u", models) is (cap > 0)
    await w.repo.reserve_model_usd("u", month_key(w.clock()), UsageClass.TRANSCRIBE.value, 5.0)
    assert await w.svc._extraction_ready("u", models) is (cap > 5.0)


async def test_a_spent_cap_yields_no_share(world):
    """With the month's cap spent, a ready recording takes no share from content (G16 fires only for work the stage can
    do) and does not continue the chain."""
    w = world(enrich=True, tick_cap=0.0, cap=0.0)   # the composition gives both the same cap
    docs = _slow_docs(w)
    audio = w.add_recording()
    await w.tick()
    assert sum(1 for d in docs if w.rec(d).state is RecordState.INDEXED) == 3
    assert w.rec(audio).transcript_state == "parked" and w.media.runs == []   # probed, parked, nothing extracted


@pytest.mark.parametrize("cap", [5.0, TRANSCRIBE_USD_PER_MONTH])
async def test_the_cabinet_shows_the_effective_cap(world, cap):
    w = world()
    w.add_doc("a.md", prose(3))
    await w.settle()
    search = DriveSearchService(w.repo, w.emb, w.users, w.drive, clock=w.clock, embedding_space=SPACE,
                                transcribe_cap_usd=cap)
    status = await search.indexing_status("u")
    assert status.usd_caps[UsageClass.TRANSCRIBE.value] == cap
    assert status.usd_caps[UsageClass.READER.value] > 0   # the other classes unchanged
