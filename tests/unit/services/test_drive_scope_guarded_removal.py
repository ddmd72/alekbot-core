"""Every removal a scope change causes is guarded (DRIVE_SCOPE_WALK_RFC §5.6, §5.9, §0.1 of the part cards): it waits unless
the tick's ancestry is loaded, no folder changed in the tick and the connection still has the tick's scope; it judges the
folder again against the tick's scope and never touches a record the scope keeps. The real service on the in-memory
repositories and the stateful fake drive. Synthetic items only."""
import logging
from dataclasses import replace

import pytest

from src.adapters.in_memory_drive_mirror_repo import InMemoryDriveMirrorRepository
from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_index import EpochPhase, Priority, Reason, RecordState, Scope
from src.domain.drive_index_planner import schedule
from src.domain.drive_mirror import MirrorRoot
from src.domain.user_drive import DriveItemNotFoundError
from src.services import drive_indexing_service as svc_mod

from .test_drive_scope_diff_tick import FEEDS, SCOPE, build, spy_listings, state_of
from .test_multi_drive_account_wide_indexing import CONN, USER, World


@pytest.fixture(params=FEEDS, ids=["account", "area"])
async def world(request):
    w = await build(request.param)
    await w.scope(folders=[w.rd, w.hamburg, w.coimbra, w.acme])
    await w.settle()
    return w


def dying(rec, *, unstored, now):
    return schedule(replace(rec, state=RecordState.DELETING, has_card=False, card_hash=None, cascade_unstored=unstored),
                    now, first_seen=False)


async def test_a_kept_root_inside_a_removed_one_survives_with_its_subtree(world):
    w = world
    await w.scope(folders=[w.hamburg, w.coimbra, w.acme])               # R&D goes, Hamburg inside it stays
    await w.settle()
    names = w.names()
    assert "Tom's plan.docx" not in names
    assert {"Hamburg", "Hamburg notes.txt"} <= set(names) and names["R&D"].reason is Reason.SCOPE_ANCESTOR


async def test_a_mirror_folder_inside_a_removed_folder_survives(world):
    w = world
    mirror = InMemoryDriveMirrorRepository()
    w.svc._mirror_repo = mirror
    conn = await w.connections.get(USER, CONN)
    bin_id = w.plant(w.coimbra, "_Transcripts", True)
    await mirror.save_root(MirrorRoot(user_id=USER, drive_identity=conn.drive_identity, root_item_id=bin_id,
                                      root_name="_Transcripts"))
    await w.settle()
    assert "_Transcripts" in w.names()
    await w.scope(folders=[w.rd, w.hamburg, w.acme])
    await w.settle()
    names = w.names()
    assert "Coimbra notes.txt" not in names and "_Transcripts" in names


async def test_nothing_is_removed_in_a_tick_whose_feed_moved_a_folder_it_goes_the_next_tick(world):
    w = world
    await w.scope(folders=[w.rd, w.hamburg, w.acme])
    w.plant(w.springfield, "Archive 2024", True)                         # a folder change in this tick's feed
    await w.svc.tick(SCOPE)
    assert {"Coimbra notes.txt", "Itinerary.txt"} <= set(w.names())
    await w.settle()
    assert {"Coimbra notes.txt", "Itinerary.txt", "Coimbra 2024"} & set(w.names()) == set()


async def test_nothing_is_removed_while_a_path_link_cannot_be_read_and_it_goes_when_it_can(world):
    w = world
    await w.repo.delete_records(w.handle.scope, [w.projects])            # the skeleton above Acme is not stored
    original = w.drive.get_change

    async def failing(handle, item_id):
        if item_id == w.projects:
            raise ValueError("transient")
        return await original(handle, item_id)

    w.drive.get_change = failing
    await w.scope(folders=[w.rd, w.hamburg, w.acme])                     # Coimbra goes
    for _ in range(3):
        await w.svc.tick(SCOPE)
    assert {"Coimbra notes.txt", "Itinerary.txt"} <= set(w.names())
    w.drive.get_change = original
    await w.settle()
    assert "Coimbra notes.txt" not in w.names()


async def test_a_folder_added_again_while_its_removal_runs_is_revived_and_listed(world, monkeypatch):
    w = world
    for i in range(6):
        w.plant(w.coimbra, f"Day {i}.txt")
    await w.settle()
    monkeypatch.setattr(svc_mod, "CASCADE_OPS_PER_TICK", 2)
    await w.scope(folders=[w.rd, w.hamburg, w.acme])
    await w.svc.tick(SCOPE)
    left = {n: r for n, r in w.names().items() if n.startswith(("Day ", "Coimbra", "Itinerary"))}
    assert left and w.names()["Coimbra 2024"].state is RecordState.DELETING   # the removal is under way
    await w.scope(folders=[w.rd, w.hamburg, w.acme, w.coimbra])
    await w.settle()
    names = w.names()
    assert names["Coimbra 2024"].state is not RecordState.DELETING
    assert {f"Day {i}.txt" for i in range(6)} | {"Coimbra notes.txt", "Itinerary.txt"} <= set(names)
    for name, rec in left.items():                                       # what was still stored kept its content
        if name in names and not rec.is_folder:
            assert names[name].has_card == rec.has_card and names[name].card_hash == rec.card_hash


async def test_a_unstored_cascade_whose_folder_is_in_scope_again_is_revived_not_removed(world):
    w = world
    folder = w.names()["Coimbra 2024"]
    await w.repo.upsert_records([dying(folder, unstored=True, now=w.clock())])   # a removal mark; the stamp is pending
    await w.svc.tick(SCOPE)
    await w.settle()
    names = w.names()
    assert names["Coimbra 2024"].state is not RecordState.DELETING
    assert {"Coimbra notes.txt", "Itinerary.txt"} <= set(names)


async def test_a_walk_under_writes_ends_with_the_records_of_a_fresh_walk_of_the_final_tree(monkeypatch):
    monkeypatch.setattr(svc_mod, "WALK_PAGE", 2)
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.rd])
    await w.settle()
    inner = w.plant(w.coimbra, "Day trips", True)
    for i in range(5):
        w.plant(inner, f"Trip {i}.txt")
    await w.scope(folders=[w.rd, w.coimbra])
    await w.svc.tick(SCOPE)                                              # the first pages
    await w.drive.move(w.handle, w.plan, w.coimbra)                      # move in
    await w.svc.tick(SCOPE)
    await w.drive.move(w.handle, w.coimbra_notes, w.springfield)         # move out
    w.plant(inner, "Trip 9.txt")                                         # create
    await w.svc.tick(SCOPE)
    await w.drive.delete(w.handle, w.itinerary)                          # delete
    await w.drive.move(w.handle, inner, new_name="Day trips 2")          # rename
    await w.settle()

    fresh = World(FeedScope.ACCOUNT)
    await fresh.start()
    ids = {w.root: fresh.root}
    tree = w.drive.tree(w.handle)
    todo = sorted((n for n in tree.nodes.values() if n.item_id != w.root), key=lambda n: n.order)
    for _ in range(1000):
        if not todo:
            break
        node = todo.pop(0)
        if not node.parent_id:
            ids[node.item_id] = fresh.drive.plant_parentless(fresh.handle, node.name, b"x", folder=node.is_folder)
        elif node.parent_id in ids:
            ids[node.item_id] = fresh.plant(ids[node.parent_id], node.name, node.is_folder)
        else:
            todo.append(node)
    assert not todo
    await fresh.scope(folders=[ids[w.rd], ids[w.coimbra]])
    await fresh.settle()

    def shape(world):
        by_id = {r.item_id: r for (c, _), r in world.repo.records.items() if c == CONN}
        return sorted((r.name, by_id[r.parent_id].name if r.parent_id in by_id else "-", str(r.reason), str(r.state))
                      for r in by_id.values())

    assert shape(w) == shape(fresh)


async def test_a_walk_listing_new_subfolders_does_not_hold_a_removal_of_the_same_tick():
    w = await build(FeedScope.ACCOUNT)
    w.plant(w.coimbra, "Day trips", True)
    await w.scope(folders=[w.rd, w.springfield])
    await w.settle()
    await w.scope(folders=[w.rd, w.coimbra])                             # Springfield goes, Coimbra (with a sub-folder) walks
    await w.svc.tick(SCOPE)
    assert {"Springfield", "birth-certificate.pdf"} & set(w.names()) == set()
    assert "Day trips" in w.names()


async def test_a_walk_listing_a_stored_folder_holds_the_removals_of_the_tick(world):
    w = world
    from src.services.drive_indexing_service import _Budget, _ScopeView, _TICK_SCOPE
    from src.domain.drive_index_scope import IndexScope
    conn = await w.connections.get(USER, CONN)
    view = _ScopeView(IndexScope.of(conn, await w.drive.capabilities(w.handle)))
    token = _TICK_SCOPE.set(view)
    try:
        state = await state_of(w)
        ctx = w.svc._ctx(w.handle, state, w.clock())
        budget = _Budget(started=0.0, monotonic=w.clock.monotonic)
        new = w.plant(w.coimbra, "Day trips", True)
        await w.svc._apply(w.handle, ctx, [await w.drive.get_change(w.handle, new)], budget, {}, relisting=True)
        assert not view.folder_changed                                    # a folder new to the index
        await w.svc._apply(w.handle, ctx, [await w.drive.get_change(w.handle, w.hamburg)], budget, {}, relisting=True)
        assert view.folder_changed                                        # a stored folder met again
    finally:
        _TICK_SCOPE.reset(token)


async def test_a_renamed_root_changes_only_its_name():
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.acme])
    await w.settle()
    await w.drive.move(w.handle, w.acme, new_name="Acme 2")
    await w.settle()
    names = w.names()
    assert set(names) == {"Projects", "Acme 2", "Acme work.docx"} and names["Projects"].reason is Reason.SCOPE_ANCESTOR


async def test_a_root_moved_elsewhere_gets_new_paths_and_loses_the_old_ones():
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.acme])
    await w.settle()
    archive = w.plant(w.root, "Archive", True)
    await w.drive.move(w.handle, w.acme, archive)
    await w.settle()
    names = w.names()
    assert set(names) == {"Archive", "Acme", "Acme work.docx"} and names["Archive"].reason is Reason.SCOPE_ANCESTOR


async def test_a_root_moved_under_another_root_leaves_no_skeleton_between():
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.rd, w.acme])
    await w.settle()
    await w.drive.move(w.handle, w.acme, w.hamburg)
    await w.settle()
    names = w.names()
    assert not [n for n, r in names.items() if r.reason is Reason.SCOPE_ANCESTOR]
    assert {"Acme", "Acme work.docx", "Hamburg"} <= set(names) and "Projects" not in names


async def test_a_deleted_root_takes_its_subtree_and_its_paths_and_stays_in_the_applied_roots():
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.acme])
    await w.settle()
    await w.drive.delete(w.handle, w.acme)
    await w.settle()
    assert w.names() == {}
    assert (await state_of(w)).applied_roots == (w.acme,)


async def test_a_walked_folder_closes_on_a_git_child_and_opens_again_when_it_is_deleted():
    w = await build(FeedScope.ACCOUNT)
    marker = w.plant(w.coimbra, ".git", True)
    await w.scope(folders=[w.rd, w.coimbra])
    await w.settle()
    assert w.names()["Coimbra 2024"].scope is Scope.OPAQUE_ROOT
    await w.drive.delete(w.handle, marker)
    await w.settle()
    assert w.names()["Coimbra 2024"].scope is Scope.NORMAL and ".git" not in w.names()
    assert {"Coimbra notes.txt", "Itinerary.txt"} <= set(w.names())


async def test_the_files_of_a_removed_folder_notice_unstored_so_the_mirror_detaches(world):
    w = world
    inner = w.plant(w.coimbra, "Day trips", True)
    deep = w.plant(inner, "Trip 1.txt")
    await w.settle()
    w.notices.clear()
    await w.scope(folders=[w.rd, w.hamburg, w.acme])
    await w.settle()
    why = dict(w.notices)
    assert why[deep] == svc_mod.UNSTORED and why[w.itinerary] == svc_mod.UNSTORED
    assert svc_mod.GONE not in why.values()


# -- review fixes -------------------------------------------------------------------------------------------------------

async def test_a_cascade_under_way_is_not_resumed_when_the_owner_saves_the_whole_drive(monkeypatch, caplog):
    """Hole 1: the restart reads no parent map, so a failing read cannot delay it, and nothing under the folder goes."""
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.rd, w.coimbra])
    await w.settle()
    caplog.set_level(logging.INFO)
    folder = w.names()["Coimbra 2024"]
    await w.repo.upsert_records([dying(folder, unstored=True, now=w.clock())])
    before = set(w.names())
    original = w.drive.get_change

    async def failing(handle, item_id):
        raise ValueError("transient")

    w.drive.get_change = failing
    state = await state_of(w)
    await w.scope(whole=True)
    await w.svc.tick(SCOPE)
    assert "changed; re-enumerating" in caplog.text
    assert before <= set(w.names())                                       # nothing under Coimbra was deleted
    assert (await state_of(w)).epoch == state.epoch + 1
    w.drive.get_change = original
    await w.settle()
    names = w.names()
    assert names["Coimbra 2024"].state is not RecordState.DELETING and {"Coimbra notes.txt", "Itinerary.txt"} <= set(names)


async def test_a_deleting_folder_without_the_mark_takes_the_guarded_path_after_one_read():
    """Hole 2: a cascade the earlier code started (no `cascade_unstored`) over a folder that now holds a scope root."""
    w = World(FeedScope.ACCOUNT)
    await w.start()
    rd = w.plant(w.root, "R&D", True)
    plan = w.plant(rd, "Tom's plan.txt")
    sub = w.plant(rd, "Springfield", True)
    birth = w.plant(sub, "birth-certificate.txt")
    await w.scope(folders=[rd])
    await w.settle()
    rec = (await w.repo.get_records(w.handle.scope, [rd]))[rd]
    await w.repo.upsert_records([dying(rec, unstored=False, now=w.clock())])
    original = w.drive.get_change
    reads = []

    async def counting(handle, item_id):
        reads.append(item_id)
        return await original(handle, item_id)

    w.drive.get_change = counting
    await w.scope(folders=[sub])
    await w.settle()
    names = w.names()
    assert {"Springfield", "birth-certificate.txt"} <= set(names)
    assert "Tom's plan.txt" not in names and names["R&D"].reason is Reason.SCOPE_ANCESTOR
    assert reads.count(rd) == 1


async def test_a_deleting_folder_the_drive_reports_deleted_still_cascades_as_gone():
    w = World(FeedScope.ACCOUNT)
    await w.start()
    rd = w.plant(w.root, "R&D", True)
    plan = w.plant(rd, "Tom's plan.txt")
    sub = w.plant(rd, "Springfield", True)
    w.plant(sub, "birth-certificate.txt")
    await w.scope(folders=[rd])
    await w.settle()
    rec = (await w.repo.get_records(w.handle.scope, [rd]))[rd]
    await w.repo.upsert_records([dying(rec, unstored=False, now=w.clock())])
    original = w.drive.get_change

    async def missing(handle, item_id):
        if item_id == rd:
            raise DriveItemNotFoundError(item_id)
        return await original(handle, item_id)

    w.drive.get_change = missing
    w.notices.clear()
    await w.scope(folders=[sub])
    await w.settle()
    assert (plan, svc_mod.GONE) in w.notices
    assert {"Springfield", "birth-certificate.txt"} <= set(w.names())     # the scope root is never part of the cascade
