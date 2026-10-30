"""Task 21C fix round 1 (review Minor 1 and 3; USER_DRIVE_INDEXING_RFC §27.9.2, §27.9.5, Δ209, Δ217): a confirmed
root loss whose ledger reset is under way is work the step can do now (it carries the root-loss mark, but the step acts
on it); root setup that keeps failing does not take the reserved tail every tick (a back-off in process memory), though
the step still tries it; and with the ledger wired and transcription off, a tick costs exactly one `roots()` read and no
other mirror query. Synthetic items only."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any, Dict, List

import pytest

from src.domain.drive_index_params import MIRROR_RESERVE_S
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import admitted, prose  # noqa: F401  (fixture)
from .drive_mirror_tick_step_world import StepWorld
from .drive_mirror_tick_world import DRIVE_IDENTITY

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def world(tmp_path):
    def make(**kw: Any) -> StepWorld:
        return StepWorld(tmp_path, **kw)
    return make


async def _mirrored(w: StepWorld, name: str = "Interview.mp4") -> str:
    audio = w.add_recording(name)
    await w.to_current(audio)
    await w.settle()
    assert f"_Transcripts/{name}.md" in w.mirror_files(), w.mirror_files()
    return audio


# -- Minor 1: a ledger reset under way is work --------------------------------------------------------------------

async def test_reserve_while_a_ledger_reset_is_under_way(world):
    """A confirmed loss (the old id already in `previous_root_ids`) keeps `root_gone_at` on its document until the new
    root exists: the tick reads it into `gone_ids`, yet the step resets the ledger now, so it takes the reserve."""
    w = world()
    await _mirrored(w)
    root = w.root_doc()
    w.mirror.root_docs[("u", DRIVE_IDENTITY)] = replace(root, previous_root_ids=(root.root_item_id,),
                                               root_gone_at=w.clock() - timedelta(days=2))
    await w.tick()
    assert w.budgets[-1].mirror_reserve == MIRROR_RESERVE_S
    out = w.outs[-1]
    assert out.get("mirror_reset", 0) + out.get("mirror_reset_dropped", 0) >= 1 or out.get("mirror_root_created")
    assert "u" not in w.svc._root_setup_holds


# -- Minor 1: failing root setup is held off the reserve ----------------------------------------------------------

async def test_failing_root_creation_backs_off_the_reserve_but_keeps_trying(world):
    w = world()
    w.writes.fail["create_folder"] = [(ValueError("403 accessDenied"), None) for _ in range(100)]
    w.add_doc("notes.md", prose(5))
    await w.tick()
    assert w.budgets[-1].mirror_reserve == MIRROR_RESERVE_S and w.root_doc() is None   # the first try has the tail
    runs = len(w.runs)
    w.clock.advance(1)
    await w.tick()
    assert w.budgets[-1].mirror_reserve == 0.0           # held: no tail for it
    assert len(w.runs) == runs + 1                         # but the step still runs and tries again
    assert w.svc._root_setup_holds["u"][0] == 2
    w.clock.advance(svc_mod.ROOT_SETUP_HOLD_BASE.total_seconds() * 2 + 1)   # past the second (doubled) hold
    await w.tick()
    assert w.budgets[-1].mirror_reserve == MIRROR_RESERVE_S
    assert w.svc._root_setup_holds["u"][0] == 3
    w.clock.advance(svc_mod.ROOT_SETUP_HOLD_BASE.total_seconds() * 2 + 1)   # the third hold is 40 min: still held
    await w.tick()
    assert w.budgets[-1].mirror_reserve == 0.0
    w.writes.fail["create_folder"] = []
    await w.tick()                                         # tried in what the start budget left: the root is made
    assert w.root_doc() is not None and "u" not in w.svc._root_setup_holds


def test_root_setup_hold_is_bounded():
    assert svc_mod.ROOT_SETUP_HOLD_BASE == timedelta(minutes=10)
    assert svc_mod.ROOT_SETUP_HOLD_CAP == timedelta(hours=6)


async def test_the_hold_reaches_its_cap(world):
    w = world()
    w.writes.fail["create_folder"] = [(ValueError("403 accessDenied"), None) for _ in range(200)]
    for _ in range(8):
        await w.tick()
        w.clock.advance(svc_mod.ROOT_SETUP_HOLD_CAP.total_seconds() + 1)
    failures, until = w.svc._root_setup_holds["u"]
    assert failures >= 8
    assert until - w.clock() <= svc_mod.ROOT_SETUP_HOLD_CAP


# -- Minor 3: the production wiring's cost with transcription off ---------------------------------------------------

def _count(w: StepWorld) -> Dict[str, int]:
    counts: Dict[str, int] = {"mirror_queue": 0, "mirror_wakeups": 0, "ready_marks": 0}
    for owner, name in ((w.repo, "mirror_queue"), (w.repo, "mirror_wakeups"), (w.mirror, "ready_marks")):
        real = getattr(owner, name)

        async def spy(*a: Any, _real: Any = real, _name: str = name, **kw: Any) -> Any:
            counts[_name] += 1
            return await _real(*a, **kw)
        setattr(owner, name, spy)
    return counts


@pytest.mark.parametrize("kw", [{"flag": False}, {"switch": False, "built": False}], ids=["flag_off", "switch_off"])
async def test_transcription_off_costs_one_roots_read_per_tick(world, kw):
    w = world(**kw)
    counts = _count(w)
    w.add_root()
    w.add_recording("Interview.mp4")
    doc = w.add_doc("notes.md", prose(5))
    await w.settle()
    ticks: List[Any] = []
    for _ in range(3):
        w.mirror.calls.clear()
        w.drive.edit(doc, prose(6 + len(ticks)).encode(), modified_at=w.clock.now - timedelta(hours=1))
        w.clock.advance(timedelta(days=1).total_seconds())
        ticks.append(await w.tick())
        assert w.mirror.calls.count("roots") == 1, w.mirror.calls
        assert "pinned" not in w.mirror.calls and "pinned_keys" not in w.mirror.calls
    assert counts == {"mirror_queue": 0, "mirror_wakeups": 0, "ready_marks": 0}
    assert all(t.status == "done" for t in ticks) and w.runs == [] and w.writes.writes() == []
