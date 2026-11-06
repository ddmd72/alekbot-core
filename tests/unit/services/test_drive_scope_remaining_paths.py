"""The edges of the scope walk the other scope tests leave: roots the drive lost or cannot report, a removal guard closed
by a scope saved mid-tick, a skeleton path whose trim waits, a deleting folder of an older revision, the derivation of
paths past a shallow chain read. The real service on the in-memory repositories and the stateful fake drive; outcomes are
stored records and the sync state. Synthetic items only."""
from dataclasses import replace

import pytest

from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_index import Reason, RecordState, Scope
from src.domain.drive_index_planner import schedule
from src.domain.drive_mirror import MirrorRoot
from src.adapters.in_memory_drive_mirror_repo import InMemoryDriveMirrorRepository
from src.services import drive_indexing_service as svc_mod

from .test_drive_scope_diff_tick import FEEDS, SCOPE, build, state_of
from .test_multi_drive_account_wide_indexing import CONN, USER


def marked(rec, w, *, unstored):
    return schedule(replace(rec, state=RecordState.DELETING, has_card=False, card_hash=None, cascade_unstored=unstored),
                    w.clock(), first_seen=False)


def reporting_deleted(w, item_id):
    """The drive reports `item_id` as deleted (trashed) when it is read, instead of not finding it."""
    original = w.drive.get_change

    async def read(handle, wanted):
        change = await original(handle, wanted)
        return replace(change, deleted=True) if wanted == item_id else change

    w.drive.get_change = read


@pytest.fixture(params=FEEDS, ids=["account", "area"])
async def world(request):
    w = await build(request.param)
    await w.scope(folders=[w.rd, w.hamburg, w.coimbra, w.acme])
    await w.settle()
    return w


# ----------------------------------------------------------------------------------------------- the walk's roots


async def test_a_new_root_the_drive_no_longer_has_is_skipped_and_the_rest_is_walked(world):
    w = world
    w.plant(w.springfield, "Archive", True)
    await w.drive.delete(w.handle, w.springfield)
    await w.scope(folders=[w.rd, w.hamburg, w.coimbra, w.acme, w.springfield])
    await w.settle()
    assert (await state_of(w)).applied_roots == tuple(sorted([w.rd, w.hamburg, w.coimbra, w.acme, w.springfield]))
    assert "Springfield" not in w.names() and "Tom's plan.docx" in w.names()


async def test_a_new_root_the_drive_reports_as_deleted_is_not_indexed(world):
    w = world
    reporting_deleted(w, w.springfield)
    await w.scope(folders=[w.rd, w.hamburg, w.coimbra, w.acme, w.springfield])
    await w.settle()
    assert "Springfield" not in w.names() and "birth-certificate.pdf" not in w.names()


async def test_a_new_root_that_is_the_bots_own_mirror_folder_is_never_walked():
    w = await build(FeedScope.ACCOUNT)
    mirror = InMemoryDriveMirrorRepository()
    w.svc._mirror_repo = mirror
    conn = await w.connections.get(USER, CONN)
    bin_id = w.plant(w.root, "_Transcripts", True)
    await mirror.save_root(MirrorRoot(user_id=USER, drive_identity=conn.drive_identity, root_item_id=bin_id,
                                      root_name="_Transcripts"))
    await w.scope(folders=[w.rd])
    await w.settle()
    await w.scope(folders=[w.rd, bin_id])
    await w.settle()
    assert (await state_of(w)).applied_roots == tuple(sorted([w.rd, bin_id]))


async def test_removing_a_root_the_index_holds_no_record_of_changes_nothing_and_ends(world):
    w = world
    await w.repo.delete_records(w.handle.scope, [w.coimbra])
    await w.scope(folders=[w.rd, w.hamburg, w.acme])
    await w.settle()
    state = await state_of(w)
    assert state.applied_roots == tuple(sorted([w.rd, w.hamburg, w.acme])) and state.removing_roots == ()


async def test_a_chain_read_that_ends_before_the_root_is_still_applied(world, monkeypatch):
    """A parent chain longer than the depth bound is read as far as the bound, never forever."""
    w = world
    monkeypatch.setattr(svc_mod, "MAX_CHAIN_DEPTH", 1)
    deep = w.plant(w.acme, "Deep", True)
    w.plant(deep, "deep note.txt")
    await w.scope(folders=[w.rd, w.hamburg, w.coimbra, w.acme, deep])
    await w.settle()
    assert (await state_of(w)).applied_roots == tuple(sorted([w.rd, w.hamburg, w.coimbra, w.acme, deep]))


async def test_a_parent_the_drive_reports_as_deleted_ends_the_chain(world):
    w = world
    reporting_deleted(w, w.projects)
    await w.repo.delete_records(w.handle.scope, [w.projects])
    await w.scope(folders=[w.rd, w.hamburg, w.coimbra])                    # Acme goes, its path link is read
    await w.settle()
    assert "Acme work.docx" not in w.names()


# --------------------------------------------------------------------------------------------- the removal guard


async def test_a_scope_saved_while_a_removal_is_marked_keeps_the_removed_folder_and_applies_next_tick(world):
    w = world
    await w.scope(folders=[w.rd, w.hamburg, w.acme])                      # Coimbra goes
    saved = []
    original = w.drive.get_changes

    async def saving(handle, cursor):
        if not saved:
            saved.append(True)
            await w.scope(folders=[w.rd, w.hamburg, w.coimbra, w.acme])   # the owner changes their mind mid-tick
        return await original(handle, cursor)

    w.drive.get_changes = saving
    w.clock.advance(60)
    await w.svc.tick(SCOPE)
    assert {"Coimbra notes.txt", "Itinerary.txt"} <= set(w.names())
    await w.settle()
    assert {"Coimbra 2024", "Coimbra notes.txt", "Itinerary.txt"} <= set(w.names())
    assert w.names()["Coimbra 2024"].state is not RecordState.DELETING


# ------------------------------------------------------------------------------- skeletons and their trim


async def _narrowed_to_hamburg(feed):
    w = await build(feed)
    await w.scope(folders=[w.rd, w.hamburg])
    await w.settle()
    await w.scope(folders=[w.hamburg])                                    # R&D becomes a path folder with a trim pending
    return w


@pytest.mark.parametrize("feed", FEEDS, ids=["account", "area"])
async def test_a_trim_waits_while_a_folder_changes_in_the_tick_and_runs_the_next(feed):
    w = await _narrowed_to_hamburg(feed)
    w.plant(w.springfield, "Archive 2024", True)
    await w.svc.tick(SCOPE)
    assert "Tom's plan.docx" in w.names()
    await w.settle()
    names = w.names()
    assert "Tom's plan.docx" not in names and {"Hamburg", "Hamburg notes.txt"} <= set(names)
    assert names["R&D"].reason is Reason.SCOPE_ANCESTOR and not names["R&D"].trim_pending


async def test_a_path_folder_a_root_covers_again_is_a_normal_folder_with_its_subtree_listed(world):
    w = world
    rd = (await w.repo.get_records(w.handle.scope, [w.rd]))[w.rd]
    # the record of an older revision: a path folder with a trim pending although its own root is still saved
    await w.repo.upsert_records([schedule(replace(rd, reason=Reason.SCOPE_ANCESTOR, trim_pending=True), w.clock(),
                                          first_seen=False)])
    await w.settle()
    rec = w.names()["R&D"]
    assert rec.reason is not Reason.SCOPE_ANCESTOR and not rec.trim_pending
    assert {"Tom's plan.docx", "Hamburg", "Hamburg notes.txt"} <= set(w.names())


# --------------------------------------------------------------- a deleting folder of an older revision


async def _mirror_inside_coimbra(w):
    mirror = InMemoryDriveMirrorRepository()
    w.svc._mirror_repo = mirror
    conn = await w.connections.get(USER, CONN)
    bin_id = w.plant(w.coimbra, "_Transcripts", True)
    await mirror.save_root(MirrorRoot(user_id=USER, drive_identity=conn.drive_identity, root_item_id=bin_id,
                                      root_name="_Transcripts"))
    await w.settle()
    coimbra = (await w.repo.get_records(w.handle.scope, [w.coimbra]))[w.coimbra]
    await w.repo.upsert_records([marked(coimbra, w, unstored=False)])    # a removal of an older revision: no mark
    await w.scope(folders=[w.rd, w.hamburg, w.acme])                      # Coimbra goes, the mirror folder stays
    return bin_id


async def test_a_deleting_folder_without_the_mark_that_holds_a_kept_child_is_marked_unstored_after_a_live_read(world):
    w = world
    await _mirror_inside_coimbra(w)
    await w.settle()
    names = w.names()
    assert "_Transcripts" in names and "Coimbra notes.txt" not in names


async def test_a_deleting_folder_whose_read_fails_waits_and_then_goes(world):
    w = world
    original = w.drive.get_change
    await _mirror_inside_coimbra(w)

    async def failing(handle, item_id):
        if item_id == w.coimbra:
            raise ValueError("transient")
        return await original(handle, item_id)

    w.drive.get_change = failing
    for _ in range(3):
        await w.svc.tick(SCOPE)
    assert "Coimbra notes.txt" in w.names()
    w.drive.get_change = original
    await w.settle()
    assert "Coimbra notes.txt" not in w.names() and "_Transcripts" in w.names()


# ------------------------------------------------------------------------------ the paths to the roots, derived again


async def _deep(feed):
    """R&D, and Projects/Acme/Sub/Leaf: the scope {R&D, Leaf} keeps three path folders above Leaf."""
    w = await build(feed)
    w.sub = w.plant(w.acme, "Sub", True)
    w.leaf = w.plant(w.sub, "Leaf", True)
    w.plant(w.leaf, "leaf note.txt")
    await w.scope(folders=[w.rd, w.leaf])
    await w.settle()
    return w


@pytest.mark.parametrize("feed", FEEDS, ids=["account", "area"])
async def test_the_path_folders_above_a_removed_root_are_all_removed_with_it(feed):
    w = await _deep(feed)
    assert {"Projects", "Acme", "Sub", "Leaf", "leaf note.txt"} <= set(w.names())
    assert w.names()["Sub"].reason is Reason.SCOPE_ANCESTOR
    await w.scope(folders=[w.rd])
    await w.settle()
    names = set(w.names())
    assert not ({"Projects", "Acme", "Sub", "Leaf", "leaf note.txt"} & names) and {"R&D", "Tom's plan.docx"} <= names
    state = await state_of(w)
    assert not state.ancestry_dirty and state.removing_roots == ()


@pytest.mark.parametrize("feed", FEEDS, ids=["account", "area"])
async def test_a_path_folder_that_a_new_root_now_covers_becomes_a_normal_folder(feed):
    w = await _deep(feed)
    await w.scope(folders=[w.rd, w.acme])                                 # Acme was a path folder; Sub sits under it
    await w.settle()
    names = w.names()
    assert names["Sub"].reason is not Reason.SCOPE_ANCESTOR and names["Acme"].reason is not Reason.SCOPE_ANCESTOR
    assert {"Leaf", "leaf note.txt", "Acme work.docx"} <= set(names)
    assert names["Projects"].reason is Reason.SCOPE_ANCESTOR


@pytest.mark.parametrize("feed", FEEDS, ids=["account", "area"])
async def test_a_path_that_cannot_be_read_is_derived_on_a_later_tick(feed):
    w = await _deep(feed)
    await w.repo.delete_records(w.handle.scope, [w.acme])                  # no record tells where Acme sits
    original = w.drive.get_change

    async def failing(handle, item_id):
        if item_id == w.acme:
            raise ValueError("transient")
        return await original(handle, item_id)

    w.drive.get_change = failing
    await w.scope(folders=[w.rd])
    for _ in range(3):
        await w.svc.tick(SCOPE)
    assert "Sub" in w.names() and (await state_of(w)).ancestry_dirty
    w.drive.get_change = original
    await w.settle()
    assert not ({"Sub", "Leaf", "Projects"} & set(w.names())) and not (await state_of(w)).ancestry_dirty


async def test_a_chain_longer_than_the_depth_bound_still_ends_the_derivation(monkeypatch):
    w = await _deep(FeedScope.ACCOUNT)
    monkeypatch.setattr(svc_mod, "MAX_CHAIN_DEPTH", 2)
    await w.scope(folders=[w.rd])
    await w.settle()
    assert not (await state_of(w)).ancestry_dirty and "Leaf" not in w.names()


async def test_a_flag_left_by_a_scope_that_is_no_longer_folders_is_cleared():
    w = await build(FeedScope.ACCOUNT)
    await w.scope(whole=True)
    await w.settle()
    await w.repo.save_sync_state(replace(await state_of(w), ancestry_dirty=True))
    await w.svc.tick(SCOPE)
    assert not (await state_of(w)).ancestry_dirty


async def test_a_top_level_removal_entry_of_a_scope_that_is_no_longer_folders_is_dropped():
    w = await build(FeedScope.ACCOUNT)
    await w.scope(whole=True)
    await w.settle()
    state = await state_of(w)
    await w.repo.save_sync_state(replace(state, removing_roots=(state.root_item_id,)))
    await w.svc.tick(SCOPE)
    assert (await state_of(w)).removing_roots == ()
    assert {"R&D", "Springfield"} <= set(w.names())


async def test_the_top_level_removal_waits_while_a_folder_changes_in_the_tick_and_runs_the_next():
    w = await build(FeedScope.ACCOUNT)
    await w.scope(whole=True)
    await w.settle()
    assert {"Springfield", "birth-certificate.pdf", "Q3 budget.xlsx"} <= set(w.names())
    await w.scope(folders=[w.rd])
    w.plant(w.springfield, "Archive 2024", True)
    await w.svc.tick(SCOPE)
    assert "birth-certificate.pdf" in w.names()
    await w.settle()
    names = set(w.names())
    assert not ({"Springfield", "birth-certificate.pdf", "Q3 budget.xlsx", "Coimbra 2024"} & names)
    assert {"R&D", "Tom's plan.docx"} <= names


async def test_a_folder_that_waits_is_not_retried_in_the_tick_while_another_removal_goes_on(world):
    w = world
    original = w.drive.get_change
    await _mirror_inside_coimbra(w)                                       # Coimbra: unmarked removal that must read
    await w.scope(folders=[w.rd, w.hamburg])                              # Acme goes as well
    reads = []

    async def failing(handle, item_id):
        reads.append(item_id)
        if item_id == w.coimbra:
            raise ValueError("transient")
        return await original(handle, item_id)

    w.drive.get_change = failing
    await w.svc.tick(SCOPE)
    assert reads.count(w.coimbra) == 1                                    # one read, then it waits for the next tick
    assert "Acme work.docx" not in w.names() and "Coimbra notes.txt" in w.names()
    w.drive.get_change = original
    await w.settle()
    assert "Coimbra notes.txt" not in w.names() and "_Transcripts" in w.names()


async def test_an_opaque_folder_added_again_while_its_removal_runs_stays_opaque_and_is_not_deleting(monkeypatch):
    w = await build(FeedScope.ACCOUNT)
    w.plant(w.coimbra, ".git", True)
    for i in range(4):
        w.plant(w.coimbra, f"Day {i}.txt")
    await w.scope(folders=[w.rd, w.coimbra])
    await w.settle()
    assert w.names()["Coimbra 2024"].scope is Scope.OPAQUE_ROOT
    monkeypatch.setattr(svc_mod, "CASCADE_OPS_PER_TICK", 1)
    await w.scope(folders=[w.rd])
    await w.svc.tick(SCOPE)
    assert w.names()["Coimbra 2024"].state is RecordState.DELETING          # the removal is under way
    await w.scope(folders=[w.rd, w.coimbra])
    await w.settle()
    rec = w.names()["Coimbra 2024"]
    assert rec.state is not RecordState.DELETING and rec.scope is Scope.OPAQUE_ROOT and not rec.reobserve_pending


async def test_a_deleting_folder_and_a_trim_whose_chain_cannot_be_read_wait_and_then_go(world):
    """Two records outside every root, whose parent's record is gone: the verdict needs a provider read that fails."""
    w = world
    stray = w.plant(w.springfield, "Archive", True)
    old_paths = w.plant(w.springfield, "Old paths", True)
    await w.settle()                                                      # outside every root: no record of either
    template = w.names()["Coimbra 2024"]
    dying = marked(replace(template, item_id=stray, parent_id=w.springfield, name="Archive"), w, unstored=True)
    path = schedule(replace(w.names()["Projects"], item_id=old_paths,
                            parent_id=w.springfield, name="Old paths", trim_pending=True), w.clock(), first_seen=False)
    await w.repo.upsert_records([dying, path])
    original = w.drive.get_change

    async def failing(handle, item_id):
        if item_id == w.springfield:
            raise ValueError("transient")
        return await original(handle, item_id)

    w.drive.get_change = failing
    for _ in range(3):
        await w.svc.tick(SCOPE)
    held = await w.repo.get_records(w.handle.scope, [stray, path.item_id])
    assert held[stray] is not None and held[path.item_id] is not None            # nothing was removed while it waits
    w.drive.get_change = original
    w.clock.advance(3600)
    await w.settle()
    gone = await w.repo.get_records(w.handle.scope, [stray, path.item_id])
    assert gone.get(stray) is None


async def test_a_folder_touched_by_the_feed_while_its_removal_runs_stays_in_removal(world, monkeypatch):
    w = world
    for i in range(4):
        w.plant(w.coimbra, f"Day {i}.txt")
    await w.settle()
    monkeypatch.setattr(svc_mod, "CASCADE_OPS_PER_TICK", 1)
    await w.scope(folders=[w.rd, w.hamburg, w.acme])
    await w.svc.tick(SCOPE)
    assert w.names()["Coimbra 2024"].state is RecordState.DELETING
    w.drive.touch(w.handle, w.coimbra, edited=True)
    await w.svc.tick(SCOPE)
    assert w.names()["Coimbra 2024"].state is RecordState.DELETING
    monkeypatch.undo()
    await w.settle()
    assert not {"Coimbra 2024", "Coimbra notes.txt", "Day 0.txt"} & set(w.names())
