"""The read fence and crashed runs in the tick (USER_DRIVE_INDEXING_RFC §7.4, §8.1 step 2, §8.2, §17.1 (e)).
Properties over seeded random histories (RFC Δ8) with crashes injected into chunk writes."""
import random
from datetime import timedelta

import pytest

from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import RecordState
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP, TICK_DEADLINE_S

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")
SEEDS = range(60)


class Crash(BaseException):
    """The instance dies: nothing in the tick catches it, so no outcome is written (§8.2 crashed attempt)."""


class CrashingWrites:
    """Arms a crash in the repository's chunk writes: after `after` chunks of the next write call are stored."""

    def __init__(self, world: ContentWorld) -> None:
        self.world = world
        self.armed = None
        self.crashed = set()   # (item_id, generation) of every run that died mid-write
        original = world.repo.write_chunks

        async def write_chunks(chunks, vectors):
            if self.armed is not None and chunks:
                after, self.armed = self.armed, None
                await original(chunks[:after], vectors[:after])
                self.crashed.add((chunks[0].item_id, chunks[0].generation))
                raise Crash()
            await original(chunks, vectors)

        world.repo.write_chunks = write_chunks


async def crash_tick(w):
    try:
        return await w.tick()
    except Crash:
        return None


async def test_a_crash_mid_write_leaves_the_previous_generation_readable():
    w = ContentWorld()
    crash = CrashingWrites(w)
    doc = w.add_doc("a.md", prose(200))
    await w.settle()
    before = [c.text for c in w.readable(doc)]
    w.drive.edit(doc, prose(220, "rider").encode(), modified_at=w.clock.now - timedelta(hours=1))
    crash.armed = 2
    await crash_tick(w)
    rec = w.rec(doc)
    assert (rec.indexed_generation, rec.index_generation) == (1, 2) and rec.attempt_started_at is not None
    assert [c.text for c in w.readable(doc)] == before and 2 in w.generations(doc)   # the partial run is invisible


async def test_a_crashed_attempt_is_counted_and_its_chunks_never_served():
    w = ContentWorld()
    crash = CrashingWrites(w)
    doc = w.add_doc("a.md", prose(200))
    crash.armed = 3
    await crash_tick(w)
    assert w.rec(doc).indexed_generation is None and w.readable(doc) == []
    w.clock.advance(TICK_DEADLINE_S - 1)
    await w.tick()
    assert w.rec(doc).attempts == 0 and w.drive.downloads.count(doc) == 1   # not crashed yet: still held
    w.clock.advance(1)
    await w.tick()
    rec = w.rec(doc)
    assert (rec.attempts, rec.attempt_started_at, rec.state) == (1, None, RecordState.PENDING)
    assert w.generations(doc) == []   # the crashed run's partial chunks are collected with its outcome
    w.clock.advance(RETRY_CAP.total_seconds())
    await w.settle()
    rec = w.rec(doc)
    assert (rec.state, rec.indexed_generation, rec.attempts) == (RecordState.INDEXED, 2, 0)
    assert w.generations(doc) == [2]


async def test_repeated_crashes_end_failed():
    w = ContentWorld()
    crash = CrashingWrites(w)
    doc = w.add_doc("a.md", prose(200))
    for _ in range(4 * MAX_ATTEMPTS):
        crash.armed = 1
        await crash_tick(w)
        w.clock.advance(TICK_DEADLINE_S + RETRY_CAP.total_seconds())
    crash.armed = None
    await w.settle()
    assert w.rec(doc).state is RecordState.FAILED and w.readable(doc) == []
    assert len(crash.crashed) == MAX_ATTEMPTS and w.generations(doc) == []


async def test_a_result_whose_content_moved_is_dropped():
    w = ContentWorld()
    doc = w.add_doc("a.md", prose(50))
    await w.settle()

    def concurrent_edit(request):   # a writer outside the lease would do this; the fence must hold anyway
        key = ("u", doc)
        rec = w.repo.records[key]
        w.repo.records[key] = rec.__class__(**{**rec.__dict__, "content_version": "elsewhere"})
    w.drive.edit(doc, prose(60).encode(), modified_at=w.clock.now - timedelta(hours=1))
    w.parser.before = concurrent_edit
    await w.tick()
    rec = w.rec(doc)
    assert rec.indexed_generation == 1 and w.generations(doc) == [1] and rec.attempt_started_at is None
    assert w.summary_total("dropped") == 1


def check_fence(w, crash):
    for (user, item_id), rec in w.repo.records.items():
        if rec.state is RecordState.METADATA_ONLY or rec.state is RecordState.DELETING:
            assert rec.indexed_generation is None, (item_id, rec.state)
        if rec.indexed_generation is not None:
            assert (item_id, rec.indexed_generation) not in crash.crashed
            assert len(w.readable(item_id)) == rec.chunk_count   # a readable generation is always whole


@pytest.mark.parametrize("seed", SEEDS)
async def test_fence_properties(seed):
    rng = random.Random(seed)
    w = ContentWorld()
    crash = CrashingWrites(w)
    docs = []
    for step in range(rng.randint(15, 40)):
        op = rng.random()
        live = [d for d in docs if d in w.drive.items]
        if op < 0.25 or not live:
            docs.append(w.add_doc(f"doc{step}.md", prose(rng.randint(1, 250), rng.choice(["clause", "rider"]))))
        elif op < 0.4:
            w.drive.edit(rng.choice(live), prose(rng.randint(1, 250)).encode(),
                         modified_at=w.clock.now - timedelta(minutes=rng.choice([0, 30, 120])))
        elif op < 0.5:
            w.drive.rename(rng.choice(live), rng.choice([".hidden.md", "renamed.md", "x.md"]))
        elif op < 0.58:
            w.drive.delete(rng.choice(live))
        elif op < 0.7:
            name = w.drive.items[rng.choice(live)].name
            w.parser.outcomes[name] = ExtractionOutcome(
                status=rng.choice([ExtractionStatus.NO_TEXT, ExtractionStatus.ERROR, ExtractionStatus.TIMEOUT]))
        elif op < 0.78:
            w.parser.outcomes.clear()
        elif op < 0.88:
            crash.armed = rng.randint(0, 3)
        if rng.random() < 0.6:
            await crash_tick(w)
            check_fence(w, crash)
        w.clock.advance(rng.choice([0, 60, 900, 3600, TICK_DEADLINE_S]))
    crash.armed = None
    w.parser.outcomes.clear()
    for _ in range(MAX_ATTEMPTS + 2):   # convergence: every backoff and crash window elapses
        w.clock.advance(TICK_DEADLINE_S + RETRY_CAP.total_seconds())
        await w.settle()
        check_fence(w, crash)
    w.drive.resync_next = True   # an enumeration's sweep is the backstop for every crash window (§7.5)
    await w.settle()
    check_fence(w, crash)
    for (user, item_id, generation, _), _value in w.repo.chunks.items():
        rec = w.rec(item_id)
        assert rec is not None and rec.indexed_generation == generation, (item_id, generation)
