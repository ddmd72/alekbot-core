"""The sync-state counters are refreshed by every tick, settled or not (USER_DRIVE_INDEXING_RFC §12.1)."""
from dataclasses import replace

import pytest

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")


def _unsettled_world(monkeypatch) -> ContentWorld:
    w = ContentWorld()

    async def more(*args, **kwargs):
        return True   # work is left after every tick, as during a first build

    monkeypatch.setattr(w.svc, "_has_more", more)
    for i in range(4):
        w.add_doc(f"doc{i}.md", prose(5))
    return w


async def test_an_unsettled_tick_writes_the_full_counters(monkeypatch):
    w = _unsettled_world(monkeypatch)
    before = len(w.queue.tasks)
    await w.tick()
    assert any(":cont:" in (t["key"] or "") for t in w.queue.tasks[before:])   # more work is left
    state = await w.repo.get_sync_state("u")
    assert state.counters.get("indexed", 0) >= 1      # per-state counts, not only `unread`
    assert state.counters.get("chunks", 0) >= 1


async def test_counters_grow_between_unsettled_ticks(monkeypatch):
    w = _unsettled_world(monkeypatch)
    await w.tick()
    first = (await w.repo.get_sync_state("u")).counters.get("indexed", 0)
    w.add_doc("late.md", prose(5))
    w.clock.advance(3600)
    await w.tick()
    second = (await w.repo.get_sync_state("u")).counters.get("indexed", 0)
    assert second > first


async def test_a_lost_lease_before_the_counter_write_writes_nothing(monkeypatch):
    w = _unsettled_world(monkeypatch)
    real = w.repo.count_by_state

    async def stolen(user_id):
        st = w.repo.states["u"]
        w.repo.states["u"] = replace(st, lease_owner="B", lease_until=w.clock.now.replace(year=2100))
        return await real(user_id)

    w.repo.count_by_state = stolen
    result = await w.tick()
    assert result.status == "lease_lost"
    state = await w.repo.get_sync_state("u")
    assert state.lease_owner == "B" and not state.counters.get("indexed")
