"""The root-setup hold ladder never overflows, and a connection with no write folder steps no ladder (2026-11-05).

Live: a read-only connection's mirror step counted "root setup made no progress" on every tick; at the 39th in a row
`ROOT_SETUP_HOLD_BASE * 2 ** (failures - 1)` overflowed timedelta before the cap applied, and every tick of that
connection failed with OverflowError. D7 (MULTI_DRIVE_RFC): no write folder, no mirror — nothing was tried."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from src.services import drive_indexing_service as svc_mod

from .drive_content_world import admitted, prose  # noqa: F401  (fixture)
from .drive_mirror_tick_step_world import StepWorld
from .drive_mirror_tick_world import DRIVE_IDENTITY
from .test_drive_indexing_mirror_tick_fix1 import _mirrored

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def world(tmp_path):
    def make(**kw: Any) -> StepWorld:
        return StepWorld(tmp_path, **kw)
    return make


@pytest.mark.parametrize("failures, expected", [
    (1, svc_mod.ROOT_SETUP_HOLD_BASE),
    (2, svc_mod.ROOT_SETUP_HOLD_BASE * 2),
    (3, svc_mod.ROOT_SETUP_HOLD_BASE * 4),
    (6, svc_mod.ROOT_SETUP_HOLD_BASE * 32),
    (7, svc_mod.ROOT_SETUP_HOLD_CAP),
    (39, svc_mod.ROOT_SETUP_HOLD_CAP),
    (1000, svc_mod.ROOT_SETUP_HOLD_CAP),
])
def test_the_ladder_doubles_to_the_cap_for_any_count(failures, expected):
    assert svc_mod._hold_after(failures) == expected


async def test_a_thousand_root_setup_failures_in_a_row_hold_at_the_cap_and_the_tick_goes_on(world):
    w = world()
    w.writes.fail["create_folder"] = [(ValueError("403 accessDenied"), None) for _ in range(10)]
    w.add_doc("notes.md", prose(5))
    w.svc._root_setup_holds["u"] = (1000, w.clock())
    await w.tick()
    assert w.runs, "the mirror step ran"
    failures, until = w.svc._root_setup_holds["u"]
    assert failures == 1001
    assert until - w.clock() == svc_mod.ROOT_SETUP_HOLD_CAP


def test_the_step_unavailable_ladder_holds_at_the_cap_too(world):
    w = world()
    w.svc._mirror_step_holds["u"] = (1000, w.clock())
    hold = w.svc._note_step_unavailable(SimpleNamespace(connection_id="u"))
    assert hold == svc_mod.ROOT_SETUP_HOLD_CAP
    assert w.svc._mirror_step_holds["u"][0] == 1001


async def test_a_connection_without_a_write_folder_steps_no_ladder(world, caplog):
    w = world()
    get = w.svc._connections.get

    async def read_only(user_id: str, connection_id: str):
        conn = await get(user_id, connection_id)
        return None if conn is None else replace(conn, write_folder_id=None)

    w.svc._connections.get = read_only
    w.add_doc("notes.md", prose(5))
    w.svc._root_setup_holds["u"] = (3, w.clock())       # a ladder left from before the folder was unset
    for _ in range(3):
        await w.tick()
        w.clock.advance(1)
    assert w.outs and all(out.get("mirror_no_write_folder") == 1 for out in w.outs)
    assert "u" not in w.svc._root_setup_holds
    assert not any("made no progress" in rec.getMessage() for rec in caplog.records)
    assert w.root_doc() is None


async def test_a_root_loss_mark_on_a_connection_that_lost_its_write_folder_clears_the_hold(world):
    """The one behaviour change past the no-progress case: a root in `gone_ids` used to keep its hold; with no write
    folder nothing is tried (`_mirror_ready` is False before any hold is read), so the hold is cleared and the stored
    mark is left as it is."""
    w = world()
    await _mirrored(w)
    root = w.root_doc()
    gone_at = w.clock() - timedelta(days=1)
    w.mirror.root_docs[("u", DRIVE_IDENTITY)] = replace(root, root_gone_at=gone_at)
    get = w.svc._connections.get

    async def read_only(user_id: str, connection_id: str):
        conn = await get(user_id, connection_id)
        return None if conn is None else replace(conn, write_folder_id=None)

    w.svc._connections.get = read_only
    w.svc._root_setup_holds["u"] = (4, w.clock() + timedelta(hours=1))
    await w.tick()
    assert w.outs[-1].get("mirror_no_write_folder") == 1
    assert "u" not in w.svc._root_setup_holds
    assert w.root_doc().root_gone_at == gone_at
