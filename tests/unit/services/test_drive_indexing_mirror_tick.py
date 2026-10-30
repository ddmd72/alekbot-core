"""The transcript mirror's step in the drive indexer's tick (USER_DRIVE_INDEXING_RFC §27.9.2, §27.9.5, §27.9.12; plan
Task 21C; Δ209, Δ217, OQ-T10): the step runs last after a complete round, only for a user whose transcription is on, with
its reserved tail while it has work it can do now and none for held work, in its own budgets and window, never asking
for a continuation, with its counters on the tick line; an opt-out deletes nothing on the drive. The real
DriveIndexingService, DriveTranscriptStage and DriveTranscriptMirror on the in-memory repositories, the mirror writing to
the same fake drive the indexer reads. Synthetic items only."""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, List

import pytest

from src.domain.drive_index import RecordState, Scope
from src.domain.drive_index_params import (
    ENRICH_FILE_BUDGET_S, FILE_START_MARGIN_S, MIRROR_ARCHIVE_GRACE, MIRROR_OPS_PER_TICK, MIRROR_READS_PER_TICK,
    MIRROR_RESERVE_S, MIRROR_STAMPS_PER_TICK, TICK_DEADLINE_S, TICK_START_BUDGET, TRANSCRIBE_EXTRACT_WALL_S,
    TRANSCRIBE_SHARE, TRANSCRIBE_UPLOAD_S,
)
from src.domain.drive_mirror import KIND_FILE, MIRROR_ACTIVE, MIRROR_ARCHIVED
from src.domain.drive_mirror_plan import mirror_plan
from src.domain.language import LanguageCode
from src.services import drive_indexing_service as svc_mod
from src.services.drive_indexing_service import _Budget, _Mirror, _mirror_seed_version
from src.services.drive_transcript_mirror import seed_version

from .drive_content_world import admitted, prose  # noqa: F401  (fixture)
from .drive_index_fakes import ROOT
from .drive_mirror_tick_step_world import StepWorld
from .drive_mirror_tick_world import DRIVE_IDENTITY, mirror_key, source_identity
from .drive_index_fakes import U_SCOPE

pytestmark = pytest.mark.usefixtures("admitted")

TOTAL = TICK_START_BUDGET.total_seconds()


@pytest.fixture
def world(tmp_path):
    def make(**kw: Any) -> StepWorld:
        return StepWorld(tmp_path, **kw)
    return make


async def _mirrored(w: StepWorld, name: str = "Interview.mp4") -> str:
    """A recording transcribed and its mirror file written (the root and its read-me first)."""
    audio = w.add_recording(name)
    await w.to_current(audio)
    await w.settle()
    assert f"_Transcripts/{name}.md" in w.mirror_files(), w.mirror_files()
    return audio


async def _clones(w: StepWorld, base: str, count: int, *, day: str = "2026-12-01") -> List[str]:
    """`count` current recordings sharing `base`'s stored transcript (a copy of a recording, §27.8 b), newer than it,
    written as `_commit` writes them (so `mirror_plan` makes them due). They live in the index only."""
    rec = w.rec(base)
    ids = []
    for i in range(count):
        clone = replace(rec, item_id=f"p{i:03d}", identity=source_identity(f"p{i:03d}"), name=f"new{i:03d}.m4a",
                        effective_day=day, mirrored_digest="",
                        mirror_due=False, mirror_retry_at=None, mirror_held_digest="", has_card=False, card_hash=None)
        await w.repo.upsert_records([mirror_plan(clone)])
        ids.append(clone.item_id)
    return ids


# -- placement: last, after a complete round (§27.9.5) ------------------------------------------------------------

async def test_mirror_runs_last_on_a_complete_round_only(world):
    w = world()
    await _mirrored(w)
    order: List[str] = []
    for name in ("_transcript_extract", "_upgrade_step", "_release_card_cap", "_release_chunk_cap", "_has_more"):
        real = getattr(w.svc, name)

        async def spy(*a: Any, _real: Any = real, _name: str = name, **kw: Any) -> Any:
            order.append(_name)
            return await _real(*a, **kw)
        setattr(w.svc, name, spy)
    real_run = w.step.run

    async def run(*a: Any, **kw: Any) -> Any:
        order.append("mirror")
        return await real_run(*a, **kw)
    w.step.run = run
    await w.tick()
    assert order == ["_transcript_extract", "_upgrade_step", "_release_card_cap", "_release_chunk_cap", "mirror",
                     "_has_more"]
    # a round the start budget cuts (four pages at 200 s each): no work-off, so no mirror step
    for i in range(4):
        w.add_doc(f"late{i}.md", prose(4))
    w.drive.page_size = 1
    real_changes = w.drive.get_changes

    async def slow(user_id: str, cursor: Any) -> Any:
        w.clock.advance(200)
        return await real_changes(user_id, cursor)
    w.drive.get_changes = slow
    order.clear()
    await w.tick()
    assert not w.repo.states["u"].round_complete and "mirror" not in order
    w.drive.get_changes = real_changes
    await w.tick()
    assert w.repo.states["u"].round_complete and "mirror" in order


async def test_mirror_progresses_under_a_saturated_due_loop(world):
    """Two content files of 250 s fill the start budget (the second ends at 500 s): the due loop stops at a class, and
    the mirror step still runs in its reserved tail and moves the renamed recording's file in the same tick."""
    w = world()
    audio = await _mirrored(w)
    w.parser.before = lambda request: w.clock.advance(250)
    docs = [w.add_doc(f"d{i}.md", f"Firm{i} Rentals. " + prose(30)) for i in range(3)]
    w.drive.rename(audio, "Renamed.mp4")
    before = len(w.runs)
    await w.tick()
    assert sum(1 for d in docs if w.rec(d).state is RecordState.INDEXED) == 2
    assert w.budgets[-1].mirror_reserve == MIRROR_RESERVE_S
    assert len(w.runs) == before + 1 and w.runs[-1]["time_left"] > 0
    assert "_Transcripts/Renamed.mp4.md" in w.mirror_files()
    assert "_Transcripts/Interview.mp4.md" not in w.mirror_files()
    assert w.outs[-1].get("mirror_moved") == 1


async def test_without_work_the_step_gets_only_what_the_start_budget_left(world):
    """The same saturated tick with no mirror work: no reserve, and past the start budget the step has no window."""
    w = world()
    await _mirrored(w)
    w.parser.before = lambda request: w.clock.advance(250)
    for i in range(3):
        w.add_doc(f"d{i}.md", f"Firm{i} Rentals. " + prose(30))
    before = len(w.runs)
    await w.tick()
    assert w.budgets[-1].mirror_reserve == 0.0
    assert len(w.runs) == before   # 500 s into the tick: no window left, the step waits for the next tick


# -- the reserve (Δ209, Δ217) ------------------------------------------------------------------------------------

async def test_reserve_only_while_mirror_work_exists(world):
    w = world()
    audio = await _mirrored(w)
    await w.tick()
    idle = w.budgets[-1]
    assert idle.mirror_reserve == 0.0
    assert idle.enrichment_limit() == TOTAL   # the shares are unchanged without mirror work
    w.drive.rename(audio, "Renamed.mp4")   # a due record
    await w.tick()
    assert w.budgets[-1].mirror_reserve == MIRROR_RESERVE_S
    await w.tick()   # the move is done: no work now
    assert w.budgets[-1].mirror_reserve == 0.0


async def test_reserve_for_the_first_root_and_an_unfinished_seed(world):
    w = world()
    await w.tick()
    assert w.budgets[-1].mirror_reserve == MIRROR_RESERVE_S   # no root document yet: root setup is due
    assert w.root_doc() is not None and w.root_doc().seed_version == seed_version()
    await w.tick()
    assert w.budgets[-1].mirror_reserve == 0.0
    w.mirror.root_docs[("u", DRIVE_IDENTITY)] = replace(w.root_doc(), seed_version="0.0")   # a version bump: the seed again
    await w.tick()
    assert w.budgets[-1].mirror_reserve == MIRROR_RESERVE_S


async def test_reserve_for_a_ripe_wake_up_and_a_ready_mark(world):
    w = world()
    audio = await _mirrored(w)
    w.writes.fail["move"] = [(ValueError("423 locked"), None)]
    w.drive.rename(audio, "Renamed.mp4")
    await w.tick()   # the move fails: the record is held, off the queue
    rec = w.rec(audio)
    assert not rec.mirror_due and rec.mirror_retry_at is not None
    w.clock.advance((rec.mirror_retry_at - w.clock()).total_seconds() + 1)
    await w.tick()   # the hold is ripe: a wake-up is work the step can do now
    assert w.budgets[-1].mirror_reserve == MIRROR_RESERVE_S
    assert "_Transcripts/Renamed.mp4.md" in w.mirror_files()
    w.drive.delete(audio)
    await w.tick()   # the mark is written; it waits for the grace
    assert w.budgets[-1].mirror_reserve == 0.0
    w.clock.advance(MIRROR_ARCHIVE_GRACE.total_seconds() + 1)
    await w.tick()
    assert w.budgets[-1].mirror_reserve == MIRROR_RESERVE_S   # the mark is ready
    assert any(p.startswith("_Transcripts/Archive/") for p in w.mirror_files())


@pytest.mark.parametrize("held", ["parked_record", "held_mark", "full_drive", "root_loss"])
async def test_no_reserve_when_the_only_work_is_held(world, held):
    w = world()
    audio = await _mirrored(w)
    if held == "parked_record":
        w.writes.fail["move"] = [(ValueError("423 locked"), None)]
        w.drive.rename(audio, "Renamed.mp4")
        await w.tick()
        assert w.rec(audio).mirror_retry_at is not None and not w.rec(audio).mirror_due
    elif held == "held_mark":
        w.drive.delete(audio)
        await w.tick()
        doc = w.mirror.docs[mirror_key(audio, KIND_FILE)]
        assert doc.gone_pending and doc.mark_ready_at > w.clock()
    elif held == "full_drive":
        w.mirror.root_docs[("u", DRIVE_IDENTITY)] = replace(w.root_doc(), quota_hold_until=w.clock() + timedelta(hours=6))
        w.drive.rename(audio, "Renamed.mp4")   # due, but the step holds for the full drive
    else:
        w.drive.delete(w.root_doc().root_item_id)   # the owner deletes `_Transcripts`: the tick marks the root lost
        w.drive.rename(audio, "Renamed.mp4")        # due, but the step stops at its root-loss check (Δ217)
    writes = len(w.writes.writes())
    await w.tick()
    assert w.budgets[-1].mirror_reserve == 0.0
    assert w.budgets[-1].enrichment_limit() == TOTAL and w.budgets[-1].extraction_stop() is not None
    if held in ("full_drive", "root_loss"):
        assert len(w.writes.writes()) == writes   # the step wrote nothing on the drive
    if held == "root_loss":
        assert w.root_doc().root_gone_at is not None


async def test_older_due_records_progress_behind_a_page_of_parked_ones(world):
    """55 newer recordings whose creates fail are held off the queue (Δ217), so an older due recording is written in
    the next tick while their holds have not ripened, not starved behind a page of them."""
    w = world()
    audio = await _mirrored(w)
    newer = await _clones(w, audio, 55)
    w.writes.fail["upload"] = [(ValueError("400 invalidRequest"), lambda n: n.startswith("new"))
                               for _ in range(len(newer))]
    w.drive.rename(audio, "Older.mp4")
    for _ in range(3):
        await w.tick()
        w.clock.advance(1)
    held = [w.rec(i) for i in newer]
    assert all(not r.mirror_due and r.mirror_retry_at is not None and r.mirror_retry_at > w.clock() for r in held)
    assert "_Transcripts/Older.mp4.md" in w.mirror_files()


# -- the deadline sum (§7.1, Δ209) -----------------------------------------------------------------------------

def test_deadline_sum_includes_the_reserve():
    """The mirror's reserve and one Graph call's margin join the §7.1 sum after the extraction step's worst case; its
    window never lets a call start past the last start that ends before the deadline, and without mirror work every
    other stop is what it was."""
    extract_worst = TRANSCRIBE_EXTRACT_WALL_S + TRANSCRIBE_UPLOAD_S + FILE_START_MARGIN_S
    assert (TOTAL + ENRICH_FILE_BUDGET_S + FILE_START_MARGIN_S + TRANSCRIBE_SHARE * TOTAL + extract_worst
            + MIRROR_RESERVE_S + FILE_START_MARGIN_S) < TICK_DEADLINE_S
    clock = SimpleNamespace(t=0.0)
    plain = _Budget(started=0.0, monotonic=lambda: clock.t)
    held = _Budget(started=0.0, monotonic=lambda: clock.t, mirror_reserve=MIRROR_RESERVE_S)
    for at in (0.0, TOTAL - MIRROR_RESERVE_S, TOTAL, TOTAL + 100, TICK_DEADLINE_S - FILE_START_MARGIN_S - 10,
               TICK_DEADLINE_S - FILE_START_MARGIN_S, TICK_DEADLINE_S):
        clock.t = at
        for budget in (plain, held):
            stop = budget.mirror_stop()
            if stop is not None:
                assert stop > at and stop + FILE_START_MARGIN_S <= TICK_DEADLINE_S
        stop = held.mirror_stop()
        if at + MIRROR_RESERVE_S + FILE_START_MARGIN_S <= TICK_DEADLINE_S:
            assert stop is not None and stop - at >= MIRROR_RESERVE_S - 1e-9   # at least the reserve
        plain_stop = plain.mirror_stop()
        assert (plain_stop is None) == (at >= TOTAL)   # no reserve: only what the start budget left
    clock.t = 0.0
    assert held.enrichment_limit() == TOTAL - MIRROR_RESERVE_S and plain.enrichment_limit() == TOTAL
    assert held.extraction_stop() == pytest.approx(TRANSCRIBE_SHARE * TOTAL - MIRROR_RESERVE_S)
    assert plain.extraction_stop() == pytest.approx(TRANSCRIBE_SHARE * TOTAL)
    clock.t = TOTAL - MIRROR_RESERVE_S
    assert not held.content_allowed()
    clock.t = TOTAL - MIRROR_RESERVE_S - 1
    held.enrich_s = plain.enrich_s = TOTAL   # the share rule passes: only the end binds
    assert held.content_allowed() and plain.content_allowed()
    clock.t = TOTAL - 1
    assert plain.content_allowed() and not held.content_allowed()


def test_the_seed_version_is_the_steps():
    assert _mirror_seed_version() == seed_version()


# -- the switches (§27.9.12) -------------------------------------------------------------------------------------

async def test_mirror_not_run_when_the_switch_is_off_but_the_root_stays_opaque(world):
    """A step handed to the service while the kill switch is off is never run: no write, no language read, no reserve;
    the bot's folder stays opaque (classification does not depend on the switches, Δ205)."""
    w = world(switch=False)
    root = w.add_root()
    md = w.drive.add_file("Interview.mp4.md", prose(3).encode(), parent=root)
    w.add_recording("Interview.mp4")
    w.add_doc("notes.md", prose(5))
    await w.settle()
    assert w.runs == [] and w.writes.writes() == [] and w.lang.calls == 0
    assert all(b.mirror_reserve == 0.0 for b in w.budgets)
    assert w.rec(root).scope is Scope.INSIDE_OPAQUE and w.rec(md) is None


async def test_switch_off_with_the_ledger_only(world):
    """Composition with the switch off: the ledger, no step. Classification and marks work; nothing is written."""
    w = world(switch=False, built=False)
    w.add_root()
    w.add_doc("notes.md", prose(5))
    await w.settle()
    assert w.writes.writes() == [] and w.lang.calls == 0
    assert w.rec(w.root_doc().root_item_id).scope is Scope.INSIDE_OPAQUE


async def test_mirror_not_run_for_a_user_without_the_flag(world):
    w = world(flag=False)
    w.add_recording("Interview.mp4")
    w.add_doc("notes.md", prose(5))
    await w.settle()
    assert w.runs == [] and w.writes.writes() == [] and w.root_doc() is None
    assert all(b.mirror_reserve == 0.0 for b in w.budgets)
    assert w.lang.calls == 0


# -- the continuation and the tick line ----------------------------------------------------------------------------

async def test_no_continuation_for_mirror_work(world):
    """More due files than one tick's Graph writes: the rest waits for the fan-out, never a continuation (Δ209)."""
    w = world()
    audio = await _mirrored(w)
    await _clones(w, audio, MIRROR_OPS_PER_TICK + 5)
    _, continued = await w.tick_counting()
    assert not continued
    assert w.outs[-1].get("mirror_ops") == MIRROR_OPS_PER_TICK
    assert await w.repo.mirror_queue(U_SCOPE, 1)   # work is left, and only the fan-out's next tick takes it
    _, continued = await w.tick_counting()
    assert not continued and not await w.repo.mirror_queue(U_SCOPE, 1)


async def test_tick_line_counters(world, caplog):
    w = world()
    audio = w.add_recording("Secret Interview.mp4")
    with caplog.at_level(logging.DEBUG):
        await w.to_current(audio)
        await w.settle()
        w.drive.rename(audio, "Secret Renamed.mp4")
        await w.tick()
    ticks = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[DriveIndex] tick ")]
    assert any("mirror={" in t and "'mirror_created': 1" in t and "'mirror_ops': 1" in t for t in ticks)
    assert any("'mirror_moved': 1" in t for t in ticks)
    assert all("mirror_reads" not in t for t in ticks)   # only the tick line's counters; the step logs the rest
    lines = [r.getMessage() for r in caplog.records if "Mirror" in r.getMessage() or "mirror" in r.getMessage()]
    assert lines and not any("Secret" in line or "_Transcripts/" in line or "https://" in line for line in lines)
    assert w.results[-1].summary.get("mirror_ops") is None   # kept out of the summary (the index's progress)


async def test_host_budgets_and_facts(world):
    """The step's own budgets, the drive id, the user's zone (read with the gate) and UI language, the round and the
    epoch; the read-me and the files are written in that language."""
    w = world(lang=LanguageCode.UK)
    real = w.users.get_user

    async def get_user(user_id: str) -> Any:
        profile = await real(user_id)
        profile.config.timezone = "Europe/Madrid"
        return profile
    w.users.get_user = get_user
    audio = await _mirrored(w)
    run = w.runs[-1]
    assert (run["ops"], run["reads"], run["stamps"]) == (MIRROR_OPS_PER_TICK, MIRROR_READS_PER_TICK,
                                                         MIRROR_STAMPS_PER_TICK)
    assert run["drive_identity"] == DRIVE_IDENTITY and run["language"] == "uk" and run["tz"] == "Europe/Madrid"
    assert run["round_complete"] and run["epoch_idle"]
    readme = w.writes.content("_Transcripts/_README.md").decode()
    assert "Archive/" in readme and "Розшифровки" in readme
    assert "Цей файл веде бот" in w.writes.content("_Transcripts/Interview.mp4.md").decode()   # the managed line
    assert w.mirror.docs[mirror_key(audio, KIND_FILE)].render_language == "uk"


async def test_the_index_item_call_budget_is_untouched(world):
    """The mirror's Graph reads come from its own bucket (§27.9.5): the index's item calls are not spent."""
    w = world()
    await _mirrored(w)
    await w.tick()
    assert w.budgets[-1].item_calls == svc_mod.ITEM_CALLS_PER_TICK


async def test_epoch_idle_needs_the_initial_enumeration(world):
    w = world()
    await _mirrored(w)
    state = w.repo.states["u"]
    w.repo.states["u"] = replace(state, initial_enumeration_done=False)
    await w.tick()
    assert w.runs[-1]["epoch_idle"] is False


async def test_root_mark_changed_keeps_the_ticks_list(world):
    w = world()
    host = svc_mod._TickHost(w.svc, (await w.connection()).handle, _Budget(started=0.0, monotonic=lambda: 0.0), None, {},
                             svc_mod._Transcription(on=True))
    mirror = _Mirror(root_ids=frozenset({"r"}), current_ids=frozenset({"r"}), gone_ids=set(), drive_identity=DRIVE_IDENTITY)
    token = svc_mod._TICK_MIRROR.set(mirror)
    try:
        host.root_mark_changed("r", True)
        assert mirror.gone_ids == {"r"}
        host.root_mark_changed("r", False)
        assert mirror.gone_ids == set()
    finally:
        svc_mod._TICK_MIRROR.reset(token)


# -- the step's failures never stall the index -------------------------------------------------------------------

async def test_the_language_unreadable_waits_for_the_next_tick(world, caplog):
    w = world()
    w.lang.fail = RuntimeError("the user document is unreadable at https://example.invalid/secret")
    w.add_recording("Interview.mp4")
    with caplog.at_level(logging.WARNING):
        result = await w.tick()
    assert result.status == "done" and w.runs == [] and w.root_doc() is None
    assert not any("example.invalid" in r.getMessage() for r in caplog.records)
    w.lang.fail = None
    await w.tick()
    assert w.root_doc() is not None


async def test_a_failed_action_leaves_the_counters_refresh(world):
    """A mirror action that fails is charged inside the step; the tick ends `done` and refreshes its counters."""
    w = world()
    audio = await _mirrored(w)
    w.writes.fail["move"] = [(OSError("reset"), None)]
    w.drive.rename(audio, "Renamed.mp4")
    result = await w.tick()
    assert result.status == "done"
    assert w.repo.states["u"].counters.get(RecordState.INDEXED.value)
    assert w.rec(audio).mirror_retry_at is not None


async def test_an_opt_out_found_by_a_mirror_write_ends_the_tick_as_disabled(world):
    w = world()
    audio = await _mirrored(w)
    w.drive.rename(audio, "Renamed.mp4")
    real_move = w.writes.move

    async def move(*a: Any, **kw: Any) -> Any:
        out = await real_move(*a, **kw)
        w.users.indexing = False    # turned off while the step writes: the next record write finds it
        w.clock.advance(10)         # past the gate's re-check interval
        return out
    w.writes.move = move
    result = await w.tick()
    assert result.status == "disabled"
    writes = len(w.writes.writes())
    await w.settle()   # the purge
    assert w.repo.states.get("u") is None and len(w.writes.writes()) == writes


# -- OQ-T10 and the lifecycle -------------------------------------------------------------------------------------

async def test_opt_out_purge_deletes_nothing_on_the_drive(world):
    w = world()
    await _mirrored(w)
    files = w.mirror_files()
    roots, docs = dict(w.mirror.root_docs), dict(w.mirror.docs)
    writes = len(w.writes.writes())
    w.users.indexing = False
    await w.settle()
    assert w.repo.states.get("u") is None
    assert w.mirror_files() == files and len(w.writes.writes()) == writes   # nothing deleted on the drive
    assert w.mirror.root_docs == roots and w.mirror.docs == docs            # the root document and the ledger stay


async def test_fake_drive_lifecycle(world):
    """Record → current → its file appears; a rename → moved; a delete → archived after the grace; a restore → back;
    a purge and a re-onboarding → the same root and no record of any `.md` file."""
    w = world()
    audio = await _mirrored(w)
    root = w.root_doc()
    key = mirror_key(audio, KIND_FILE)
    body = w.writes.content("_Transcripts/Interview.mp4.md").decode()
    assert body.startswith("# Interview.mp4\n") and "Managed by the bot" in body
    w.drive.rename(audio, "Renamed.mp4")
    await w.settle()
    assert w.mirror_files() == ["_Transcripts/Renamed.mp4.md", "_Transcripts/_README.md"]
    w.drive.delete(audio)
    await w.settle()
    assert "_Transcripts/Renamed.mp4.md" in w.mirror_files()   # within the grace: not yet
    w.clock.advance(MIRROR_ARCHIVE_GRACE.total_seconds() + 1)
    await w.settle()
    day = w.mirror.docs[key].archive_day
    assert w.mirror.docs[key].state == MIRROR_ARCHIVED
    assert w.mirror_files() == [f"_Transcripts/Archive/{day}/Renamed.mp4.md", "_Transcripts/_README.md"]
    w.drive.add("Renamed.mp4", ROOT, item_id=audio, size_bytes=64, mime_type="audio/mp4")   # a recycle-bin restore
    await w.settle()
    assert w.rec(audio).transcript_state == "current"
    assert w.mirror.docs[key].state == MIRROR_ACTIVE
    assert "_Transcripts/Renamed.mp4.md" in w.mirror_files()
    w.users.indexing = False
    await w.settle()
    w.users.indexing = True
    await w.settle()
    assert list(w.mirror.root_docs.values())[0].root_item_id == root.root_item_id and len(w.mirror.root_docs) == 1
    assert sum(1 for p in w.writes.files() if p.endswith("_README.md")) == 1
    assert w.rec(root.root_item_id).scope is Scope.INSIDE_OPAQUE
    assert all(w.rec(i) is None for i, c in w.drive.items.items() if c.name.endswith(".md"))
