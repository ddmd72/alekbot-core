"""Error and edge paths of the scope filter and the transcript reserve (slice 2b, MULTI_DRIVE_RFC §11.3): a scope change
before anything was enumerated is stamped without a restart; the filter leaves a mirror root's children alone; the
parent-chain read serves the tick's own reads first, stops when the item budget is spent, answers "" for an item the
provider does not have and gives up (None) on a read error; a chain walk that cannot read a link yields no verdict; a
connection with no write folder takes no transcript reserve. Technical ids only."""
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_index_scope import IndexScope
from src.services.drive_indexing_service import DriveIndexingService, _Budget, _ScopeView

from .test_multi_drive_account_wide_indexing import CONN, USER, World


@pytest.fixture
async def acct():
    w = World(FeedScope.ACCOUNT)
    await w.start()
    w.rd = w.plant(w.root, "R&D", True)
    w.plan = w.plant(w.rd, "Tom's plan.docx")
    return w


async def _view(w: World) -> _ScopeView:
    conn = await w.connections.get(USER, CONN)
    return _ScopeView(IndexScope.of(conn, await w.drive.capabilities(w.handle)))


def _budget(w: World, item_calls: int = 5) -> _Budget:
    budget = _Budget(started=0.0, monotonic=w.clock.monotonic)
    budget.item_calls = item_calls
    return budget


async def test_a_scope_chosen_before_anything_was_enumerated_is_stamped_without_a_restart(acct):
    scope = ConnectionScope(user_id=USER, connection_id=CONN)
    await acct.settle()
    state = await acct.repo.get_sync_state(scope)
    await acct.repo.save_sync_state(replace(state, cursor=None, applied_scope=""))   # a state that enumerated nothing yet
    await acct.scope(folders=[acct.rd])
    await acct.settle()
    after = await acct.repo.get_sync_state(scope)
    assert after.applied_scope == (await acct.connections.get(USER, CONN)).scope_key != ""
    assert "Tom's plan.docx" in acct.names()


def test_the_filter_leaves_the_children_of_a_mirror_root_alone():
    ctx = SimpleNamespace(root_item_id="root", mirror_root_ids=frozenset({"mirror-1"}))
    inside = SimpleNamespace(item_id="item-1", parent_id="mirror-1")
    outside = SimpleNamespace(item_id="item-2", parent_id="folder-1")
    assert DriveIndexingService._scope_applies(inside, {}, ctx) is False
    assert DriveIndexingService._scope_applies(outside, {}, ctx) is True


async def test_the_chain_read_serves_what_this_tick_already_read_without_a_provider_call(acct):
    view = await _view(acct)
    view.parents["item-1"] = "folder-1"
    calls_before = len(acct.drive.calls)
    assert await acct.svc._chain_parent(acct.handle, view, "item-1", _budget(acct)) == "folder-1"
    assert len(acct.drive.calls) == calls_before


async def test_the_chain_read_stops_when_the_item_budget_is_spent(acct):
    view = await _view(acct)
    calls_before = len(acct.drive.calls)
    assert await acct.svc._chain_parent(acct.handle, view, acct.plan, _budget(acct, item_calls=0)) is None
    assert acct.plan not in view.parents
    assert len(acct.drive.calls) == calls_before


async def test_an_item_the_provider_does_not_have_has_no_reachable_parent(acct):
    view = await _view(acct)
    budget = _budget(acct)
    assert await acct.svc._chain_parent(acct.handle, view, "missing-1", budget) == ""
    assert view.parents["missing-1"] == "" and budget.item_calls == 4


async def test_a_failing_chain_read_gives_up_and_logs(acct, caplog):
    async def boom(handle, item_id):
        raise OSError("network down")
    acct.drive.get_change = boom
    view = await _view(acct)
    with caplog.at_level(logging.WARNING):
        assert await acct.svc._chain_parent(acct.handle, view, "item-1", _budget(acct)) is None
    assert any("scope chain read failed" in r.getMessage() and "network down" in r.getMessage()
               for r in caplog.records)
    assert "item-1" not in view.parents


async def test_the_ancestor_walk_stays_incomplete_when_a_link_cannot_be_read(acct):
    await acct.settle()
    await acct.scope(folders=[acct.plan])                 # a chosen folder whose parent is not stored: one read needed
    view = await _view(acct)
    ctx = acct.svc._ctx(acct.handle, await acct.repo.get_sync_state(ConnectionScope(user_id=USER, connection_id=CONN)),
                        acct.clock())
    await acct.svc._load_scope_ancestors(acct.handle, view, ctx, _budget(acct, item_calls=0))
    assert view.loaded is False and view.ancestors == set()


async def test_a_verdict_is_withheld_when_a_link_of_the_chain_cannot_be_read(acct):
    await acct.settle()
    await acct.scope(folders=[acct.rd])
    view = await _view(acct)
    state = await acct.repo.get_sync_state(ConnectionScope(user_id=USER, connection_id=CONN))
    ctx = acct.svc._ctx(acct.handle, state, acct.clock())
    change = await acct.drive.get_change(acct.handle, acct.plan)
    deeper = type(change)(item_id="item-9", name="Acme.docx", parent_id="unknown-folder")
    verdict = await acct.svc._scope_verdict(acct.handle, view, ctx, deeper, {}, {}, _budget(acct, item_calls=0))
    assert verdict is None
