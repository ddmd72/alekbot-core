"""Search v2 P5 review fixes (review rv_sv2_p5; USER_DRIVE_INDEXING_RFC §25.4.5-§25.4.9): the initial build's dollars
kept out of the month's caps (M1), the inline first-seen run profiles only (M3), no ready job keeps an endless
zero-delay tick chain (M4), no enrichment on text an edit is about to replace (MINOR 1), the stalled-rebuild signal
(MINOR 2), spend recorded across a cut and a fatal sibling (MINOR 5), the other agent's queue during a backoff
(MINOR 6), a keys bump past a month-held record (MINOR 9) and the v1-index warning (MINOR 10)."""
import asyncio
import logging
import time
from dataclasses import replace
from datetime import timedelta

import pytest

from src.domain import drive_enrich_plan as ep
from src.domain.drive_enrich_plan import PROFILER_AGENT
from src.domain.drive_enrichment import JobState, UsageClass
from src.domain.drive_enrichment_calls import ModelReplyStatus
from src.domain.drive_index import (
    KEYS_VERSION, MIGRATION_GATE_KEY, PROFILE_VERSION, Priority, RecordState,
)
from src.domain.drive_index_params import (
    MODEL_BACKOFF_CAP_S, PASSAGE_USD_PER_MONTH, PROFILE_USD_PER_MONTH,
)
from src.domain.drive_index_planner import month_key, next_month_start
from src.domain.drive_index import DriveIndexStoreUnavailable
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import admitted, prose  # noqa: F401  (fixture)
from .drive_enrich_world import EnrichWorld
from .test_drive_indexing_enrichment_life import paragraphs
from .drive_usage_helpers import close_initial_build, initial_build, spend

pytestmark = pytest.mark.usefixtures("admitted")
S = ModelReplyStatus


def doc_text(word: str = "Acme", n: int = 30) -> str:
    return f"{word} Rentals. " + prose(n)


@pytest.fixture
def w():
    return EnrichWorld()


def _put(w, item, **fields):
    """A record changed in place and re-planned as a write would (outside a tick: nothing parked)."""
    rec = replace(w.rec(item), **fields)
    w.repo.records[("u", item)] = ep.planned(rec, ep.default_context(w.clock.now))


# -- M1: the initial build's dollars never count against the month's caps ---------------------------------------

async def test_dollars_the_initial_build_paid_never_count_against_the_month_cap(w):
    w.profiler.usd = 1.25
    for i in range(4):
        w.add_doc(f"d{i}.md", doc_text(f"Firm{i}"))
    await w.settle()
    usage = await w.repo.get_model_usage("u", month_key(w.clock.now))
    assert usage.profile_usd == pytest.approx(5.0) and usage.profile_initial_usd == pytest.approx(5.0)
    assert usage.cap_usd(UsageClass.PROFILE.value) == pytest.approx(0.0)
    await close_initial_build(w.repo, w.clock.now)
    late = w.add_doc("late.md", doc_text("Late"))
    await w.settle()
    assert w.rec(late).profile is not None   # the month's own profile cap ($4) was never spent
    usage = await w.repo.get_model_usage("u", month_key(w.clock.now))
    assert usage.cap_usd(UsageClass.PROFILE.value) == pytest.approx(1.25)
    assert PROFILE_USD_PER_MONTH > 1.25


def test_the_context_counts_only_the_month_caps_own_dollars():
    from datetime import datetime, timezone

    from src.domain.drive_enrichment import InitialBuild, ModelUsageMonth
    from src.domain.drive_usage import added_usage
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    usage = added_usage(ModelUsageMonth(), "2026-10-13", "profile", 5.0, 0.0, 4, 4.5)
    assert (usage.profile_usd, usage.profile_initial_usd, usage.daily["2026-10-13"]) == (5.0, 4.5, (5.0, 0.0))
    ctx = ep.enrich_context(now, usage, InitialBuild(allowance_usd=40.0, spent_usd=40.0, closed_at=now))
    assert ep.available_usd(ctx, "profile") == pytest.approx(PROFILE_USD_PER_MONTH - 0.5)


# -- M3: the inline first-seen run profiles only ---------------------------------------------------------------

async def test_the_inline_first_seen_run_profiles_only_and_passages_run_in_the_phase(w):
    log = []
    w.profiler.before = lambda request: log.append("profile")
    hints = w.passages.hints

    async def logged(request):
        log.append("passages")
        return await hints(request)

    w.passages.hints = logged
    docs = [w.add_doc(f"d{i}.md", doc_text(f"Firm{i}")) for i in range(3)]
    await w.tick()
    assert all(w.rec(d).state is RecordState.INDEXED for d in docs)
    assert "passages" in log
    assert log[:log.index("passages")] == ["profile"] * 3   # every inline run was a profile, nothing else
    await w.settle()
    assert all(w.rec(d).passages_state == JobState.CURRENT.value for d in docs)


# -- M4: no ready job keeps a zero-delay tick chain ------------------------------------------------------------

async def test_a_ready_profile_with_no_stored_text_is_held_and_the_tick_settles(w):
    doc = w.add_doc("a.md", doc_text())
    await w.settle()
    for key in [k for k in w.repo.chunks if k[1] == doc]:
        del w.repo.chunks[key]
    _put(w, doc, profile_version=PROFILE_VERSION - 1)   # a version bump: the profile is due again
    assert w.rec(doc).profile_state == JobState.READY.value
    await w.settle()
    rec = w.rec(doc)
    assert (rec.profile_state, rec.profile_hold) == (JobState.HELD.value, "failed")
    assert w.summary_total("profile_unusable") == 1


async def test_a_missing_chunk_is_skipped_and_the_passages_finish(w):
    w.passages.script = [S.UNAVAILABLE]
    doc = w.add_doc("lease.md", paragraphs(20))
    await w.settle()   # indexed and profiled; passages behind their agent's backoff
    rec = w.rec(doc)
    assert rec.chunk_count >= 3 and rec.passages_state == JobState.READY.value
    del w.repo.chunks[("u", doc, rec.indexed_generation, 1)]
    w.clock.advance(MODEL_BACKOFF_CAP_S)
    await w.settle()
    rec = w.rec(doc)
    assert rec.passages_state == JobState.CURRENT.value
    assert 1 not in {h.index for h in w.hints(doc)} and w.summary_total("passage_missing_chunks") == 1


async def test_passages_the_dollars_left_pay_one_call_for_run_it_and_park(w):
    await close_initial_build(w.repo, w.clock.now)
    await spend(w.repo, UsageClass.PASSAGE, PASSAGE_USD_PER_MONTH - 0.0009, w.clock.now)   # one 2-chunk call
    doc = w.add_doc("lease.md", paragraphs(20))
    await w.settle()
    rec = w.rec(doc)
    assert rec.chunk_count >= 5
    assert [len(r.chunks) for r in w.passages.requests] == [2]
    assert rec.passages_state == JobState.PARKED.value and len(w.hints(doc)) == 2


async def test_a_run_that_writes_nothing_is_not_waited_for_again_in_the_tick(w, monkeypatch):
    w.profiler.script = [S.UNAVAILABLE]
    doc = w.add_doc("a.md", doc_text())
    await w.settle()   # the profile waits behind the profiler's backoff
    assert w.rec(doc).profile_state == JobState.READY.value
    w.clock.advance(MODEL_BACKOFF_CAP_S)

    async def silent(user_id, rec, *args, **kwargs):   # an exit no hold covers: nothing written
        return rec

    monkeypatch.setattr(w.svc, "_profile_job", silent)
    result = await w.settle()
    assert result.status == "done" and w.rec(doc).profile_state == JobState.READY.value
    assert w.summary_total("enrich_idle") == 1


# -- MINOR 1: no enrichment from text an edit is about to replace ----------------------------------------------

async def test_a_profile_is_not_made_from_the_old_text_while_an_edit_waits(w):
    w.profiler.script = [S.UNAVAILABLE]
    doc = w.add_doc("lease.md", doc_text("Acme"))
    await w.settle()   # indexed; its profile behind the profiler's backoff
    w.drive.edit(doc, doc_text("Brio").encode(), modified_at=w.clock.now)
    w.clock.advance(120)   # the backoff is over, the edit's quiet period is not
    await w.settle()
    assert w.rec(doc).profile_state == JobState.DEFERRED.value and len(w.profiler.requests) == 1
    w.clock.advance(3600)
    await w.settle()
    rec = w.rec(doc)
    assert len(w.profiler.requests) == 2 and "Brio" in w.profiler.requests[-1].input.head
    assert rec.profile.what_it_is == "A document about Brio" and rec.profile_state == JobState.CURRENT.value


# -- MINOR 2: the stalled-rebuild signal ----------------------------------------------------------------------

async def test_a_quiet_day_is_not_a_stall(w):
    w.add_doc("a.md", doc_text())
    await w.settle()
    w.clock.advance(25 * 3600)
    await w.tick()   # the fan-out's tick: nothing ready, nothing to stall
    assert w.repo.states["u"].described_grew_at == w.clock.now


async def test_passage_only_progress_is_not_a_stall(w):
    w.passages.script = [S.UNAVAILABLE]
    doc = w.add_doc("lease.md", paragraphs(20))
    await w.settle()
    described_at = w.repo.states["u"].described_grew_at
    w.clock.advance(25 * 3600)
    await w.tick()   # the described count stays; the passages move
    assert w.rec(doc).passages_state == JobState.CURRENT.value
    assert w.repo.states["u"].described_grew_at == w.clock.now != described_at


# -- MINOR 5: what was spent is recorded; a fatal error stops the siblings ------------------------------------

@pytest.mark.slow
async def test_a_paid_reply_is_recorded_when_the_file_budget_cuts_the_run(w, monkeypatch):
    monkeypatch.setattr(svc_mod, "ENRICH_FILE_BUDGET_S", 0.05)
    w.profiler.usd = 0.5
    slow = w.repo.spend_initial_build

    async def slow_spend(*args, **kwargs):
        await asyncio.sleep(0.2)
        return await slow(*args, **kwargs)

    monkeypatch.setattr(w.repo, "spend_initial_build", slow_spend)
    w.add_doc("a.md", doc_text())
    await w.tick()
    await asyncio.sleep(0.5)   # the shielded writes finish after the cut
    usage = await w.repo.get_model_usage("u", month_key(w.clock.now))
    assert usage.profile_usd == pytest.approx(0.5) and usage.profile_initial_usd == pytest.approx(0.5)
    assert (await initial_build(w.repo)).spent_usd >= 0.5


@pytest.mark.timing  # wall-clock assertion: off the main gate, `make test-timing`
async def test_a_fatal_error_of_one_file_cancels_its_siblings(w, monkeypatch):
    w.profiler.script = [S.UNAVAILABLE, S.UNAVAILABLE]
    fast, slow = w.add_doc("a.md", doc_text("Acme")), w.add_doc("b.md", doc_text("Brio"))
    await w.settle()   # both indexed, both profiles behind the profiler's backoff
    assert w.rec(fast).profile_state == w.rec(slow).profile_state == JobState.READY.value
    w.clock.advance(MODEL_BACKOFF_CAP_S)
    answer = w.profiler.profile

    async def profile(request):
        if request.item_id == fast:
            raise DriveIndexStoreUnavailable("the store went away")
        await asyncio.sleep(2.0)
        return await answer(request)

    monkeypatch.setattr(w.profiler, "profile", profile)
    started = time.perf_counter()
    result = await w.tick()
    assert result.status == "error" and time.perf_counter() - started < 1.0


# -- MINOR 6: one agent's backoff does not hide the other agent's work ---------------------------------------

async def test_a_profiler_backoff_does_not_hide_passage_work_further_down(w, monkeypatch):
    docs = [w.add_doc(f"d{i}.md", doc_text(f"Firm{i}")) for i in range(3)]
    await w.settle()
    first, second, last = docs
    _put(w, first, profile_version=PROFILE_VERSION - 1)    # profile-only work, first in the queue
    _put(w, second, profile_version=PROFILE_VERSION - 1)
    for key in [k for k in w.repo.hints if k[1] == last]:
        del w.repo.hints[key]
    _put(w, last, passages_next_index=0, passages_done_at=None)   # passage work, past a page of two
    state = w.repo.states["u"]
    w.repo.states["u"] = replace(state, model_backoff_by_agent={PROFILER_AGENT: w.clock.now + timedelta(hours=1)})
    monkeypatch.setattr(svc_mod, "ENRICH_QUEUE_PAGE", 2)
    profiles, passages = len(w.profiler.requests), len(w.passages.requests)
    await w.tick()
    assert len(w.profiler.requests) == profiles
    assert {r.item_id for r in w.passages.requests[passages:]} == {last}
    assert w.rec(last).passages_state == JobState.CURRENT.value


# -- MINOR 9: a keys bump does not wait a month behind a month-held record -----------------------------------

def test_pending_keys_are_due_now_whatever_the_record_waits_for():
    from datetime import datetime, timezone

    from src.domain.drive_index import DriveIndexRecord, Scope
    now = datetime(2026, 10, 13, 9, tzinfo=timezone.utc)
    held = DriveIndexRecord(user_id="u", connection_id="u", item_id="d", parent_id="root", name="scan.pdf", kind="pdf",
                            scope=Scope.NORMAL, state=RecordState.PENDING, indexed_generation=1, chunk_count=2,
                            keys_version=KEYS_VERSION - 1, due_at=next_month_start(now),
                            defer_until=next_month_start(now), priority=int(Priority.MAINTENANCE))
    rec = ep.keys_planned(held, now)
    assert rec.keys_pending and (rec.due_at, rec.priority) == (now, int(Priority.MAINTENANCE))
    assert rec.defer_until == next_month_start(now)    # the content work still waits for its month


async def test_a_keys_bump_rekeys_a_month_held_record_now(w):
    doc = w.add_doc("lease.md", doc_text())
    await w.settle()
    rec = w.rec(doc)
    month = next_month_start(w.clock.now)
    w.repo.records[("u", doc)] = replace(rec, keys_version=KEYS_VERSION - 1, state=RecordState.PENDING,
                                         content_version=rec.content_version + "-edited", defer_until=month,
                                         due_at=month, priority=int(Priority.MAINTENANCE))
    state = w.repo.states["u"]
    # the `keys` version is behind (no longer the gate key since Δ65, which is `accuracy`: it stays stamped)
    w.repo.states["u"] = replace(state, applied_versions={**state.applied_versions, "keys": KEYS_VERSION - 1})
    await w.settle()
    rec = w.rec(doc)
    assert rec.keys_version == KEYS_VERSION and not rec.keys_pending
    assert rec.defer_until == month and rec.due_at == month   # still waiting for its month's re-read
    assert w.repo.states["u"].applied_versions["keys"] == KEYS_VERSION
    assert MIGRATION_GATE_KEY in w.repo.states["u"].applied_versions


# -- MINOR 10: a v1 index warns once, not every tick --------------------------------------------------------

async def test_a_v1_index_warns_once_then_logs_at_info(w, caplog):
    w.add_doc("a.md", doc_text())
    await w.settle()
    state = w.repo.states["u"]
    w.repo.states["u"] = replace(state, applied_versions={k: v for k, v in state.applied_versions.items()
                                                          if k != MIGRATION_GATE_KEY})
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            assert (await w.tick()).status == "awaiting_migration"
    lines = [r for r in caplog.records if "awaiting its rebuild" in r.getMessage()]   # §26.7 wording
    assert [r.levelno for r in lines] == [logging.WARNING, logging.INFO, logging.INFO]
