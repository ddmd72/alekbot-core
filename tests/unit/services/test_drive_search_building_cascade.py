"""`building` is also true while an unresolved record is being resolved (its parent looked up, probed) at CASCADE
priority: its card may still appear. A record waiting on a 6-hour lookup retry (RETRY) or parked with no priority
does not count, or `building` would stay true for good on a parent lookup that never succeeds
(USER_DRIVE_INDEXING_RFC §10.2, §7.8)."""
import pytest

from src.domain.drive_index import Priority, RecordState, Scope

from .drive_index_fakes import T0
from .test_drive_search_service import World


async def _settled_with(**state):
    w = World()
    w.drive.add("lease.pdf")
    stray = w.drive.add("stray.pdf")
    await w.settle()
    w.set_state(stray, state=RecordState.UNRESOLVED, scope=Scope.UNRESOLVED, has_card=False, **state)
    return w


async def test_an_unresolved_record_at_cascade_priority_is_building():
    w = await _settled_with(due_at=T0, priority=int(Priority.CASCADE))
    assert (await w.search("lease")).envelope.building
    assert (await w.search_svc.indexing_status("u")).building


@pytest.mark.parametrize("scheduling", [dict(due_at=None, priority=None),
                                        dict(due_at=T0, priority=int(Priority.RETRY))], ids=["parked", "retry"])
async def test_a_parked_or_retrying_lookup_is_not_building(scheduling):
    w = await _settled_with(**scheduling)
    assert not (await w.search("lease")).envelope.building
    assert not (await w.search_svc.indexing_status("u")).building
