"""The indexer applies a scope change as a diff (DRIVE_SCOPE_WALK_RFC step 1, §5.3-§5.5, §5.11; W3-W5, W11, W13): adding a
folder lists its subtree only, removing one marks it for the guarded removal, a scope that covers everything restarts,
and a walk shares the tick with the content classes. The real service on the in-memory repositories and the stateful
fake drive; outcomes are stored records, the sync state, the drive's calls and the mirror notices. Synthetic items only."""
import logging
from dataclasses import replace
from typing import Any, Dict, List, Tuple

import pytest

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_index import EpochPhase, Reason, RecordState
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP
from src.domain.user_drive import DriveItemNotFoundError
from src.services import drive_indexing_service as svc_mod

from .drive_content_fakes import FakeExtractor
from .test_multi_drive_account_wide_indexing import CONN, USER, World

SCOPE = ConnectionScope(user_id=USER, connection_id=CONN)
FEEDS = [FeedScope.ACCOUNT, FeedScope.AREA]


async def build(feed: FeedScope) -> World:
    """R&D (with Hamburg inside), Coimbra 2024, Springfield, Projects/Acme; for an account feed also a parentless item."""
    w = World(feed)
    await w.start()
    w.rd = w.plant(w.root, "R&D", True)
    w.plan = w.plant(w.rd, "Tom's plan.docx")
    w.hamburg = w.plant(w.rd, "Hamburg", True)
    w.hamburg_notes = w.plant(w.hamburg, "Hamburg notes.txt")
    w.coimbra = w.plant(w.root, "Coimbra 2024", True)
    w.coimbra_notes = w.plant(w.coimbra, "Coimbra notes.txt")
    w.itinerary = w.plant(w.coimbra, "Itinerary.txt")
    w.springfield = w.plant(w.root, "Springfield", True)
    w.birth = w.plant(w.springfield, "birth-certificate.pdf")
    w.projects = w.plant(w.root, "Projects", True)
    w.acme = w.plant(w.projects, "Acme", True)
    w.acme_work = w.plant(w.acme, "Acme work.docx")
    w.other = w.plant(w.projects, "other.docx")
    if feed is FeedScope.ACCOUNT:
        w.q3 = w.drive.plant_parentless(w.handle, "Q3 budget.xlsx", b"x")
    return w


async def state_of(w: World):
    return await w.repo.get_sync_state(SCOPE)


def spy_listings(w: World) -> List[str]:
    """The folder ids the drive is asked to list (a walk's pages)."""
    seen: List[str] = []
    original = w.drive.list_children_changes

    async def spy(handle, folder_id, limit, cursor=None):
        seen.append(folder_id)
        return await original(handle, folder_id, limit, cursor)

    w.drive.list_children_changes = spy
    return seen


def calls(w: World, method: str) -> int:
    return sum(1 for m, _ in w.drive.calls if m == method)


@pytest.fixture(params=FEEDS, ids=["account", "area"])
async def scoped(request):
    """The tree indexed under the scope {R&D}."""
    w = await build(request.param)
    await w.scope(folders=[w.rd])
    await w.settle()
    return w


async def test_adding_a_folder_lists_only_its_subtree(scoped, caplog):
    w = scoped
    caplog.set_level(logging.INFO)
    before = await state_of(w)
    kept = {n: r for n, r in w.names().items()}
    embedded, read = len(w.svc._embeddings.calls), len(w.svc._extractors["parser"].requests)
    listed = spy_listings(w)
    await w.scope(folders=[w.rd, w.coimbra])
    await w.settle()
    after = await state_of(w)
    assert {"Coimbra 2024", "Coimbra notes.txt", "Itinerary.txt"} <= set(w.names())
    assert set(listed) == {w.coimbra}                                    # the new subtree, nothing else
    assert (after.epoch, after.epoch_phase) == (before.epoch, EpochPhase.IDLE)   # no enumeration
    assert after.applied_roots == tuple(sorted((w.rd, w.coimbra)))
    for name, rec in kept.items():                                       # untouched records: same stamp, same card
        now = w.names()[name]
        assert (now.seen_epoch, now.has_card, now.card_hash) == (rec.seen_epoch, rec.has_card, rec.card_hash)
    new_reads = len(w.svc._extractors["parser"].requests) - read
    assert new_reads == 2 and len(w.svc._embeddings.calls) - embedded >= 2   # only the new files are read and embedded
    assert "applied as a diff: walk=1 remove=0" in caplog.text
    assert "changed; re-enumerating" not in caplog.text


async def test_removing_a_folder_makes_no_provider_call_except_the_feed(scoped):
    w = scoped
    await w.scope(folders=[w.rd, w.coimbra])
    await w.settle()
    w.drive.calls.clear()
    await w.scope(folders=[w.rd])
    await w.settle()
    assert {m for m, _ in w.drive.calls} <= {"get_changes", "capabilities"}
    assert {"Coimbra 2024", "Coimbra notes.txt"} & set(w.names()) == set()


async def test_a_root_inside_another_is_not_walked_and_removing_the_outer_keeps_the_inner(scoped, caplog):
    w = scoped
    caplog.set_level(logging.INFO)
    listed = spy_listings(w)
    await w.scope(folders=[w.rd, w.hamburg])                             # 3a: Hamburg is inside R&D already
    await w.settle()
    assert listed == [] and "applied as a diff: walk=0 remove=0" in caplog.text
    await w.scope(folders=[w.hamburg])                                   # 3b: the outer root goes, the inner stays
    await w.settle()
    names = w.names()
    assert "Tom's plan.docx" not in names
    assert {"Hamburg", "Hamburg notes.txt"} <= set(names)
    outer = names["R&D"]
    assert outer.reason is Reason.SCOPE_ANCESTOR and not outer.has_card and not outer.trim_pending
    assert names["Hamburg"].reason is not Reason.SCOPE_ANCESTOR
    assert (await w.repo.get_records(w.handle.scope, [w.rd]))[w.rd] is not None   # one record per item id


async def test_whole_drive_narrowed_to_a_folder_removes_the_rest_without_listing(request):
    """5a (account feed) and 5c (the area default): everything outside the new root is removed, its path becomes
    skeletons, and no folder is listed."""
    for feed in FEEDS:
        w = await build(feed)
        await w.scope(whole=True) if feed is FeedScope.ACCOUNT else None
        await w.settle()
        assert {"Q3 budget.xlsx", "Springfield"} <= set(w.names()) or feed is FeedScope.AREA
        listed = spy_listings(w)
        await w.scope(folders=[w.acme])
        await w.settle()
        names = w.names()
        assert set(names) == {"Projects", "Acme", "Acme work.docx"}, feed
        assert names["Projects"].reason is Reason.SCOPE_ANCESTOR and not names["Projects"].has_card
        assert listed == []
        state = await state_of(w)
        assert state.removing_roots == () and state.applied_roots == (w.acme,)


async def test_widening_to_the_whole_drive_restarts_once_and_reads_nothing_again(scoped, caplog):
    w = scoped
    if w.drive.feed_scope is FeedScope.AREA:
        pytest.skip("on an area feed the whole drive and the default scope are the same enumeration")
    caplog.set_level(logging.INFO)
    before = await state_of(w)
    read = len(w.svc._extractors["parser"].requests)
    await w.scope(whole=True)
    await w.settle()
    after = await state_of(w)
    assert after.epoch == before.epoch + 1
    assert caplog.text.count("changed; re-enumerating") == 1
    assert {"Q3 budget.xlsx", "Springfield", "Coimbra notes.txt"} <= set(w.names())
    # the three files of R&D were stored before: not read again; only what the wider scope brought is read
    again = {r.item_id for r in w.svc._extractors["parser"].requests[read:]}
    assert w.hamburg_notes not in again and w.plan not in again
    assert {w.coimbra_notes, w.itinerary} <= again


async def test_a_change_while_an_enumeration_runs_restarts(scoped, caplog):
    w = scoped
    caplog.set_level(logging.INFO)
    state = await state_of(w)
    await w.repo.save_sync_state(replace(state, epoch_phase=EpochPhase.ENUMERATING))
    await w.scope(folders=[w.rd, w.coimbra])
    await w.svc.tick(SCOPE)
    after = await state_of(w)
    assert after.epoch == state.epoch + 1 and "changed; re-enumerating" in caplog.text


async def test_a_state_the_step_one_code_wrote_is_adopted_when_the_key_is_the_same(scoped, caplog):
    w = scoped
    caplog.set_level(logging.INFO)
    state = await state_of(w)
    await w.repo.save_sync_state(replace(state, applied_whole=False, applied_roots=()))   # no pair stamped
    await w.svc.tick(SCOPE)
    after = await state_of(w)
    assert after.epoch == state.epoch and after.applied_roots == (w.rd,)
    assert "adopted as applied" in caplog.text and "re-enumerating" not in caplog.text


async def test_a_state_under_another_key_restarts_exactly_once_and_stamps_the_pair(scoped, caplog):
    w = scoped
    caplog.set_level(logging.INFO)
    state = await state_of(w)
    await w.repo.save_sync_state(replace(state, applied_scope="an-older-key", applied_roots=()))
    await w.settle()
    after = await state_of(w)
    assert after.epoch == state.epoch + 1 and caplog.text.count("changed; re-enumerating") == 1
    assert after.applied_roots == (w.rd,) and after.applied_scope == (await w.connections.get(USER, CONN)).scope_key


async def test_the_rolled_back_shape_is_adopted_again(scoped, caplog):
    """A rolled-back revision saved the new key over the old roots: the key is the connection's, the roots are not."""
    w = scoped
    caplog.set_level(logging.INFO)
    state = await state_of(w)
    await w.repo.save_sync_state(replace(state, applied_roots=(w.coimbra,)))
    await w.svc.tick(SCOPE)
    after = await state_of(w)
    assert after.epoch == state.epoch and after.applied_roots == (w.rd,) and "re-enumerating" not in caplog.text


async def test_a_scope_saved_during_the_tick_stops_paid_work_and_is_applied_by_the_next_tick(scoped, caplog):
    w = scoped
    caplog.set_level(logging.INFO)
    w.plant(w.rd, "Plan B.txt")
    w.plant(w.rd, "Plan C.txt")
    w.plant(w.rd, "Plan D.txt")

    class Saving(FakeExtractor):
        async def extract(self, request):
            if not self.requests:
                w.clock.advance(60)                       # the opt-out re-read is rate limited
                await w.scope(folders=[w.rd, w.coimbra])  # the owner saves while a file is being read
            return await super().extract(request)

    saving = Saving("parser")
    w.svc._extractors = {**w.svc._extractors, "parser": saving}
    await w.svc.tick(SCOPE)
    assert len(saving.requests) == 1                      # no index run started after the save
    assert "changed mid-tick; no more paid work this tick" in caplog.text
    assert "applied as a diff: walk=1" not in caplog.text
    await w.svc.tick(SCOPE)
    assert "applied as a diff: walk=1 remove=0" in caplog.text


async def test_a_walk_out_of_its_share_lets_a_pending_file_elsewhere_get_its_card(scoped, monkeypatch):
    w = scoped
    monkeypatch.setattr(svc_mod, "WALK_SHARE", 0.0)
    fresh = w.plant(w.rd, "Tom's agenda.docx")
    await w.scope(folders=[w.rd, w.coimbra])
    await w.svc.tick(SCOPE)
    names = w.names()
    assert names["Tom's agenda.docx"].has_card                      # class 2 ran: the walk's page waited
    assert "Coimbra notes.txt" not in names and names["Coimbra 2024"].reobserve_pending
    assert fresh in {r.item_id for r in names.values()}
    assert (await state_of(w)).walking


async def test_a_listing_that_keeps_failing_is_blocked_not_dropped_and_comes_back(scoped):
    w = scoped
    original = w.drive.list_children_changes
    failing = {"on": True}

    async def listing(handle, folder_id, limit, cursor=None):
        if folder_id == w.coimbra and failing["on"]:
            raise ValueError("refused")
        return await original(handle, folder_id, limit, cursor)

    w.drive.list_children_changes = listing
    await w.scope(folders=[w.rd, w.coimbra])
    for _ in range(MAX_ATTEMPTS + 2):
        await w.settle()
        w.clock.advance(RETRY_CAP.total_seconds())
        failing["on"] = failing["on"]
    folder = w.names()["Coimbra 2024"]
    state = await state_of(w)
    assert folder.reobserve_pending and folder.attempts == MAX_ATTEMPTS
    assert state.walk_blocked == 1 and not state.walking
    assert folder.due_at is not None and folder.due_at >= w.clock() - RETRY_CAP
    failing["on"] = False
    w.clock.advance(RETRY_CAP.total_seconds())
    await w.settle()
    state = await state_of(w)
    assert {"Coimbra notes.txt", "Itinerary.txt"} <= set(w.names())
    assert not w.names()["Coimbra 2024"].reobserve_pending and state.walk_blocked == 0


async def test_the_walk_ends_while_a_folder_nobody_can_list_stays_pending(scoped):
    w = scoped
    original = w.drive.list_children_changes

    async def listing(handle, folder_id, limit, cursor=None):
        if folder_id == w.coimbra:
            raise DriveItemNotFoundError(folder_id)
        return await original(handle, folder_id, limit, cursor)

    w.drive.list_children_changes = listing
    await w.scope(folders=[w.rd, w.coimbra])
    await w.settle()
    state = await state_of(w)
    assert w.names()["Coimbra 2024"].reobserve_pending and not state.walking


async def test_the_descendants_of_a_removed_folder_notice_unstored_never_gone(scoped):
    w = scoped
    await w.scope(folders=[w.rd, w.coimbra])
    await w.settle()
    w.notices.clear()
    await w.scope(folders=[w.rd])
    await w.settle()
    why = dict(w.notices)
    assert why[w.coimbra_notes] == svc_mod.UNSTORED and why[w.itinerary] == svc_mod.UNSTORED
    assert all(reason != svc_mod.GONE for _, reason in w.notices)


async def test_a_diff_short_of_an_item_call_marks_without_stamping_and_a_re_add_keeps_the_records(scoped, caplog):
    w = scoped
    caplog.set_level(logging.INFO)
    await w.scope(folders=[w.rd, w.coimbra])
    await w.settle()
    key_before = (await state_of(w)).applied_scope
    original = w.drive.get_change

    reads = {"n": 0}

    async def flaky(handle, item_id):
        if item_id == w.springfield:
            reads["n"] += 1
            if reads["n"] > 1:                    # the path read (first) works, the root's own read fails
                raise ValueError("transient")
        return await original(handle, item_id)

    w.drive.get_change = flaky
    await w.scope(folders=[w.rd, w.springfield])                         # add an unstored root, remove Coimbra
    await w.svc.tick(SCOPE)
    state = await state_of(w)
    assert state.applied_scope == key_before and "(stamp waits for the next tick)" in caplog.text
    assert "Coimbra 2024" not in w.names()   # the removal mark was written and, the guard open, worked off
    w.drive.get_change = original
    await w.scope(folders=[w.rd, w.coimbra, w.springfield])              # 8c: the folder is added again
    await w.settle()
    names = w.names()
    assert {"Coimbra notes.txt", "Itinerary.txt", "birth-certificate.pdf"} <= set(names)
    assert names["Coimbra 2024"].state is not RecordState.DELETING and not names["Coimbra 2024"].reobserve_pending
    assert (await state_of(w)).applied_scope == (await w.connections.get(USER, CONN)).scope_key
