"""Failures around pending change keys on a filtered scope (GOOGLE_DRIVE_RFC GE-36). One item failing in the verdict
pass is skipped once (counted once, never read, never stored) while the rest of the page is applied; a systemic error in
the verdict pass ends the tick with the page unapplied; a resolution that hands back a key still pending fails that one
item loudly at the planner and the rest of the page is applied. Technical ids; "Tom's plan" in "R&D" is the invented
universe's."""
from typing import Dict, List

import pytest

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_index import DriveChange
from src.domain.drive_index_scope import IndexScope
from src.domain.user_drive import DriveThrottled
from src.services.drive_indexing_service import _Budget, _ScopeView, _TICK_SCOPE

from .test_multi_drive_account_wide_indexing import CONN, USER, World

SCOPE = ConnectionScope(user_id=USER, connection_id=CONN)


def _native(w: World, parent: str, name: str) -> str:
    return w.drive.plant(w.handle, parent, name, b"plan text", mime="text/markdown", deferred=True)


@pytest.fixture
async def acct():
    w = World(FeedScope.ACCOUNT)
    await w.start()
    w.rd = w.plant(w.root, "R&D", True)
    await w.scope(folders=[w.rd])
    await w.settle()
    w.plan = _native(w, w.rd, "Tom's plan")
    w.bad = _native(w, w.rd, "f-1")
    return w


def _fail_verdict_for(w: World, item_id: str, exc: Exception) -> None:
    real = w.svc._scope_verdict

    async def verdict(handle, view, ctx, change, *args):
        if change.item_id == item_id:
            raise exc
        return await real(handle, view, ctx, change, *args)
    w.svc._scope_verdict = verdict


async def _apply_page(w: World, summary: Dict[str, int]) -> List[DriveChange]:
    state = await w.repo.get_sync_state(SCOPE)
    page = await w.drive.get_changes(w.handle, state.cursor)
    conn = await w.connections.get(USER, CONN)
    token = _TICK_SCOPE.set(_ScopeView(IndexScope.of(conn, await w.drive.capabilities(w.handle))))
    try:
        await w.svc._apply(w.handle, w.svc._ctx(w.handle, state, w.clock()), list(page.changes),
                           _Budget(started=0.0, monotonic=w.clock.monotonic), summary)
    finally:
        _TICK_SCOPE.reset(token)
    return list(page.changes)


async def test_an_item_failing_in_the_verdict_pass_is_skipped_once_and_the_page_is_applied(acct):
    _fail_verdict_for(acct, acct.bad, RuntimeError("chain read broke"))
    summary: Dict[str, int] = {}
    changes = await _apply_page(acct, summary)
    assert {acct.plan, acct.bad} <= {c.item_id for c in changes}
    assert summary["skipped"] == 1 and summary["observed"] == len(changes)
    assert acct.drive.resolved == [acct.plan]
    records = await acct.repo.get_records(SCOPE, [acct.plan, acct.bad])
    assert records.get(acct.bad) is None
    assert records[acct.plan] is not None and records[acct.plan].content_version == "r:rev-1"


async def test_a_systemic_error_in_the_verdict_pass_leaves_the_page_unapplied(acct):
    before = await acct.repo.get_sync_state(SCOPE)
    _fail_verdict_for(acct, acct.bad, DriveThrottled(60.0))
    await acct.svc.tick(SCOPE)
    after = await acct.repo.get_sync_state(SCOPE)
    assert after.cursor == before.cursor
    assert acct.drive.resolved == []
    assert "Tom's plan" not in acct.names() and "f-1" not in acct.names()


async def test_a_key_left_pending_by_the_port_fails_that_item_at_the_planner_only(acct):
    real = acct.drive.resolve_content_versions

    async def half_resolved(handle, changes):
        out = await real(handle, changes)
        return [c if c.item_id != acct.bad else next(p for p in changes if p.item_id == acct.bad) for c in out]
    acct.drive.resolve_content_versions = half_resolved
    summary: Dict[str, int] = {}
    await _apply_page(acct, summary)
    assert summary["skipped"] == 1
    records = await acct.repo.get_records(SCOPE, [acct.plan, acct.bad])
    assert records.get(acct.bad) is None
    assert records[acct.plan] is not None and records[acct.plan].content_version == "r:rev-1"
