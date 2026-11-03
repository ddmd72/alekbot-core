"""Pending change keys in the indexer (GOOGLE_DRIVE_RFC GE-36). The feed lists an item whose key costs a provider read
as pending; the tick resolves it only for an item it plans as a live record, in one batched port call per page, after
the scope verdicts: an out-of-scope item is never read (even when its folder leaves the scope in the same page), an
unchanged item keeps its stored key with no read, a new or edited one is read before planning and its record carries the
final key, an item whose verdict the budget withheld is read too, an unfiltered drive reads every live pending item, a
feed with nothing pending makes no resolution call, and a failed resolution leaves the page unapplied.
Served by the shared fake drive with deferred keys; "Tom's plan" in "R&D" is the invented universe's, the rest neutral."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_index import RecordState
from src.domain.drive_index_scope import IndexScope
from src.domain.user_drive import DriveThrottled
from src.services.drive_indexing_service import _Budget, _ScopeView, _TICK_SCOPE

from .test_multi_drive_account_wide_indexing import CONN, USER, World

T1 = datetime(2026, 1, 20, tzinfo=timezone.utc)
MD = "text/markdown"
SCOPE = ConnectionScope(user_id=USER, connection_id=CONN)


def _native(w: World, parent: str, name: str) -> str:
    return w.drive.plant(w.handle, parent, name, b"plan text", mime=MD, deferred=True)


@pytest.fixture
async def acct():
    w = World(FeedScope.ACCOUNT)
    await w.start()
    w.rd = w.plant(w.root, "R&D", True)
    w.springfield = w.plant(w.root, "folder-a", True)
    w.plan = _native(w, w.rd, "Tom's plan")
    w.outside = _native(w, w.springfield, "f-1")
    await w.scope(folders=[w.rd])
    return w


async def test_an_in_scope_new_native_is_resolved_and_stored_with_the_final_key(acct):
    await acct.settle()
    rec = acct.names()["Tom's plan"]
    assert rec.content_version == "r:rev-1"
    assert acct.plan in acct.drive.resolved


async def test_an_out_of_scope_native_is_never_resolved(acct):
    await acct.settle()
    assert "f-1" not in acct.names()
    assert acct.outside not in acct.drive.resolved


async def test_an_unchanged_native_keeps_its_stored_key_without_a_read(acct):
    await acct.settle()
    acct.drive.resolved.clear()
    acct.drive.touch(acct.handle, acct.plan)                 # re-emitted by the feed, same modified time
    await acct.settle()
    assert acct.drive.resolved == []
    assert acct.names()["Tom's plan"].content_version == "r:rev-1"


async def test_an_edited_native_is_resolved_again_before_planning(acct):
    await acct.settle()
    acct.drive.resolved.clear()
    acct.drive.touch(acct.handle, acct.plan, modified=T1, edited=True)
    await acct.settle()
    assert acct.drive.resolved == [acct.plan]
    rec = acct.names()["Tom's plan"]
    assert rec.content_version == "r:rev-2" and rec.modified_at == T1


async def test_a_native_whose_folder_leaves_the_scope_in_the_same_page_is_never_resolved(acct):
    inner = acct.plant(acct.rd, "folder-b", True)
    doc = _native(acct, inner, "f-2")
    await acct.settle()
    assert "f-2" in acct.names()
    acct.drive.resolved.clear()
    await acct.drive.move(acct.handle, inner, acct.springfield)                  # the folder first in the page ...
    acct.drive.touch(acct.handle, doc, modified=T1, edited=True)                 # ... then its edited native
    await acct.settle()
    assert doc not in acct.drive.resolved
    assert "f-2" not in acct.names()


async def test_a_native_whose_verdict_the_budget_withheld_is_still_resolved(acct):
    await acct.settle()
    folder = acct.plant(acct.springfield, "folder-c", True)
    doc = _native(acct, folder, "f-3")
    state = await acct.repo.get_sync_state(SCOPE)
    page = await acct.drive.get_changes(acct.handle, state.cursor)
    [change] = [c for c in page.changes if c.item_id == doc]
    assert change.content_version_pending
    acct.drive.resolved.clear()
    conn = await acct.connections.get(USER, CONN)
    token = _TICK_SCOPE.set(_ScopeView(IndexScope.of(conn, await acct.drive.capabilities(acct.handle))))
    try:
        budget = _Budget(started=0.0, monotonic=acct.clock.monotonic)
        budget.item_calls = 0                                                     # no chain read: no verdict
        await acct.svc._apply(acct.handle, acct.svc._ctx(acct.handle, state, acct.clock()), [change], budget, {})
    finally:
        _TICK_SCOPE.reset(token)
    assert acct.drive.resolved == [doc]
    rec = (await acct.repo.get_records(SCOPE, [doc]))[doc]
    assert rec is not None and rec.state is RecordState.UNRESOLVED and rec.content_version == "r:rev-1"


async def test_a_failed_resolution_leaves_the_page_unapplied(acct):
    await acct.settle()
    before = await acct.repo.get_sync_state(SCOPE)
    _native(acct, acct.rd, "f-4")

    async def throttled(handle, changes):
        raise DriveThrottled(60.0)
    acct.drive.resolve_content_versions = throttled
    await acct.svc.tick(SCOPE)
    after = await acct.repo.get_sync_state(SCOPE)
    assert "f-4" not in acct.names()
    assert after.cursor == before.cursor


async def test_an_unfiltered_drive_resolves_every_live_native():
    w = World(FeedScope.AREA)
    await w.start()
    folder = w.plant(w.root, "R&D", True)
    plan = _native(w, folder, "Tom's plan")
    await w.settle()
    assert w.names()["Tom's plan"].content_version == "r:rev-1"
    assert w.drive.resolved == [plan]
    w.drive.resolved.clear()
    await w.drive.delete(w.handle, plan)
    await w.settle()
    assert "Tom's plan" not in w.names() and w.drive.resolved == []


async def test_a_feed_with_nothing_pending_makes_no_resolution_call():
    w = World(FeedScope.AREA)
    await w.start()
    folder = w.plant(w.root, "R&D", True)
    w.plant(folder, "Tom's plan.docx")
    await w.settle()
    assert set(w.names()) == {"R&D", "Tom's plan.docx"}
    assert "resolve_content_versions" not in {m for m, _ in w.drive.calls}


async def test_the_planner_never_sees_a_pending_change_from_a_stored_rule_miss(acct):
    """A stored record with another modified time is no shortcut: the item is read, never planned pending."""
    await acct.settle()
    rec = acct.names()["Tom's plan"]
    await acct.repo.upsert_records([replace(rec, modified_at=T1)])
    acct.drive.resolved.clear()
    acct.drive.touch(acct.handle, acct.plan)
    await acct.settle()
    assert acct.drive.resolved == [acct.plan]
    assert acct.names()["Tom's plan"].content_version == "r:rev-1"
