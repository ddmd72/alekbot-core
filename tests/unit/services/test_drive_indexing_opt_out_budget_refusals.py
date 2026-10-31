"""The tick: an opt-out mid-tick, item calls within the time budget, and card refusals (§7.1, §8.2)."""
from dataclasses import replace

import pytest

from src.domain import drive_index_planner as planner
from src.domain.drive_index import Priority, RecordState
from src.domain.drive_index_params import TICK_START_BUDGET
from src.domain.exceptions import (
    LLMClientError,
    LLMRateLimitError,
    LLMServerError,
    LLMUnavailableError,
)

from .drive_index_fakes import FakeEmbeddings
from .test_drive_indexing_service import World
from .drive_index_fakes import U_SCOPE

_BUDGET_S = TICK_START_BUDGET.total_seconds()
_SLOW_S = 200.0   # one slow provider call; three of them overrun the tick's start budget


class TestOptOutMidTickContinues:
    """An opt-out met mid-tick enqueues a continuation, so a lost toggle task does not leave the purge to a
    fan-out that skips the user for 30 min on the stale `continuation_enqueued_at`."""

    @staticmethod
    def _flip_on_first_write(w):
        orig = w.repo.upsert_records

        async def flip(records, card_vectors=None):
            await orig(records, card_vectors)
            w.users.enabled["u"] = False
            w.clock.advance(10)   # past the gate re-check interval

        w.repo.upsert_records = flip
        return orig

    @staticmethod
    async def _world_with_a_stale_marker():
        """A settled index, new files to observe, and the marker a previous tick's continuation left."""
        w = World(page_size=5)
        for i in range(10):
            w.drive.add(f"f{i}.pdf")
        await w.settle()
        for i in range(10, 40):
            w.drive.add(f"f{i}.pdf")
        st = await w.repo.get_sync_state(U_SCOPE)
        await w.repo.save_sync_state(replace(st, continuation_enqueued_at=w.clock.now))
        w.clock.advance(5)
        return w

    async def test_opt_out_mid_tick_enqueues_a_continuation(self):
        w = await self._world_with_a_stale_marker()
        orig = self._flip_on_first_write(w)
        before = len(w.queue.tasks)
        assert (await w.tick()).status == "disabled"
        w.repo.upsert_records = orig
        continued = [t for t in w.queue.tasks[before:] if ":cont:" in (t["key"] or "")]
        assert continued, w.queue.tasks[before:]

    async def test_the_continuation_purges_without_the_toggle_task(self):
        w = await self._world_with_a_stale_marker()
        orig = self._flip_on_first_write(w)
        before = len(w.queue.tasks)
        assert (await w.tick()).status == "disabled"
        w.repo.upsert_records = orig
        # Only the continuations the ticks enqueue run: the toggle task was lost, the fan-out is not involved.
        for _ in range(20):
            continued = [t for t in w.queue.tasks[before:] if ":cont:" in (t["key"] or "")]
            if not continued:
                break
            before = len(w.queue.tasks)
            w.clock.advance(max(1, max(t["delay"] for t in continued)))
            await w.tick()
        assert w.repo.records == {} and w.repo.states == {}


def _slow(w, method: str, starts: list, tick_start: dict):
    """The drive method takes `_SLOW_S`; each call's start is recorded relative to its tick's start."""
    orig = getattr(w.drive, method)

    async def slow(*args, **kwargs):
        starts.append(w.clock.mono - tick_start["t"])
        w.clock.advance(_SLOW_S)
        return await orig(*args, **kwargs)

    setattr(w.drive, method, slow)


async def _timed_ticks(w, tick_start: dict, n: int):
    for _ in range(n):
        tick_start["t"] = w.clock.mono
        await w.tick()
        w.clock.advance(5)


class TestItemCallsRespectTheTimeBudget:
    """No item call (probe, parent lookup, re-listing page) starts after the tick's start budget."""

    async def test_inline_and_work_off_probes_stop_at_the_budget_and_resume(self):
        w = World(page_size=50)
        dense = [w.drive.add(f"dense{j}", folder=True, child_count=600) for j in range(10)]
        starts: list = []
        tick_start = {"t": 0.0}
        _slow(w, "list_children_changes", starts, tick_start)
        tick_start["t"] = w.clock.mono
        await w.tick()
        assert starts and all(s < _BUDGET_S for s in starts), starts
        await _timed_ticks(w, tick_start, 12)
        assert all(s < _BUDGET_S for s in starts), starts
        assert not any(w.rec(d).probe_pending for d in dense)

    async def test_parent_lookups_stop_at_the_budget(self):
        w = World(page_size=50)
        orphans = [w.drive.add(f"o{j}.pdf", parent=f"ghost{j}") for j in range(10)]
        starts: list = []
        tick_start = {"t": 0.0}
        _slow(w, "get_change", starts, tick_start)
        await _timed_ticks(w, tick_start, 1)
        assert starts and all(s < _BUDGET_S for s in starts), starts
        await _timed_ticks(w, tick_start, 12)
        assert all(s < _BUDGET_S for s in starts), starts
        assert all(w.rec(o) is None for o in orphans)

    async def test_relisting_pages_stop_at_the_budget(self):
        w = World(page_size=50)
        folders = [w.drive.add(f"dir{j}", folder=True) for j in range(10)]
        for j, f in enumerate(folders):
            w.drive.add(f"k{j}.pdf", parent=f)
        await w.settle()
        due = [planner.schedule(replace(w.rec(f), reobserve_pending=True), w.clock.now, first_seen=True)
               for f in folders]
        await w.repo.upsert_records(due)
        starts: list = []
        tick_start = {"t": 0.0}
        _slow(w, "list_children_changes", starts, tick_start)
        await _timed_ticks(w, tick_start, 1)
        assert starts and all(s < _BUDGET_S for s in starts), starts
        await _timed_ticks(w, tick_start, 12)
        assert all(s < _BUDGET_S for s in starts), starts
        assert not any(w.rec(f).reobserve_pending for f in folders)


class _RefusingEmbeddings(FakeEmbeddings):
    """Refuses one card text with `exc`; embeds every other."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    async def get_embedding(self, text, task_type="RETRIEVAL_DOCUMENT"):
        if text.startswith("refused"):
            raise self.exc
        return await super().get_embedding(text, task_type)


class TestOneRefusedCardDoesNotStallTheUser:
    """A card text the embedding service refuses backs off alone; the rest of the batch is stored (§8.2)."""

    @pytest.mark.parametrize("exc", [LLMClientError("400 INVALID_ARGUMENT", http_status=400)])
    async def test_refused_card_backs_off_alone(self, exc):
        w = World()
        w.svc._embeddings = _RefusingEmbeddings(exc)
        good = [w.drive.add(f"doc{i}.pdf") for i in range(4)]
        bad = w.drive.add("refused.pdf")
        result = await w.settle()
        assert result.status == "done"
        assert set(good) <= w.carded() and bad not in w.carded()
        rec = w.rec(bad)
        assert rec.attempts == 1 and rec.priority == int(Priority.RETRY) and rec.due_at > w.clock.now
        assert rec.state is not RecordState.DELETING
        assert (await w.repo.get_sync_state(U_SCOPE)).last_error is None

    async def test_rate_limit_stays_systemic(self):
        w = World()
        w.svc._embeddings = _RefusingEmbeddings(LLMRateLimitError("429", http_status=429))
        good = [w.drive.add(f"doc{i}.pdf") for i in range(4)]
        bad = w.drive.add("refused.pdf")
        result = await w.tick()
        assert result.status == "throttled"
        assert set(good) <= w.carded() and bad not in w.carded()   # the batch's other vectors are kept (§8.2)
        assert w.rec(bad).attempts == 0


class TestSystemicEmbeddingErrorsMoveNoRecord:
    """Only a definite bad-input refusal (LLMClientError 400) backs a card off alone. Every other
    embedding error is systemic (RFC §8.2, Systemic row): the tick ends with `last_error` set, and the failing record
    is not moved: no card, `attempts` untouched, not backed off. The batch's other vectors are kept."""

    @pytest.mark.parametrize("exc", [
        LLMUnavailableError("503", http_status=503),
        LLMServerError("500", http_status=500),
        LLMServerError("502", http_status=502),
        LLMClientError("403 PERMISSION_DENIED", http_status=403),
        RuntimeError("unmapped SDK or network error"),
    ], ids=["unavailable-503", "server-500", "server-502", "client-403", "unknown-runtime"])
    async def test_systemic_error_moves_no_record(self, exc):
        w = World()
        w.svc._embeddings = _RefusingEmbeddings(exc)
        good = [w.drive.add(f"doc{i}.pdf") for i in range(4)]
        bad = w.drive.add("refused.pdf")
        result = await w.tick()
        assert result.status in ("throttled", "error")
        assert (await w.repo.get_sync_state(U_SCOPE)).last_error is not None
        assert w.rec(bad).attempts == 0
        assert bad not in w.carded() and set(good) <= w.carded()
        assert w.rec(bad).priority != int(Priority.RETRY) and w.rec(bad).due_at <= w.clock.now
        assert w.rec(bad).state is not RecordState.FAILED and not w.rec(bad).card_refused
