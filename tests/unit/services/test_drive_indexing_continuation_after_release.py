"""A tick enqueues its continuation only after it released the lease (USER_DRIVE_INDEXING_RFC §7.1), so the
continuation can never find its own parent still holding it."""
from dataclasses import replace

import pytest

from src.domain.user_drive import DriveConnection, DriveThrottled

from .test_drive_indexing_service import World
from .drive_index_fakes import U_SCOPE


def _watch_lease_at_enqueue(w):
    """Records, for every continuation enqueued, who held the lease at that moment."""
    holders = []
    real = w.queue.enqueue_worker_task

    async def enqueue(task_type, payload, delay_seconds=0, deadline_seconds=None, dedup_key=None):
        if dedup_key and ":cont:" in dedup_key:
            st = w.repo.states.get("u")
            holders.append(st.lease_owner if st is not None else None)
        return await real(task_type, payload, delay_seconds, deadline_seconds, dedup_key)

    w.queue.enqueue_worker_task = enqueue
    return holders


async def _mid_round(w):
    for i in range(60):
        w.drive.add(f"doc{i:02d}.pdf")
    real = w.drive.get_changes

    async def slow(user_id, cursor):
        w.clock.advance(200)   # the start budget ends mid-round
        return await real(user_id, cursor)

    w.drive.get_changes = slow


async def _throttled(w):
    w.drive.add("a.pdf")
    await w.settle()
    w.drive.raise_next = DriveThrottled(retry_after_s=5)


async def _failed(w):
    w.drive.add("a.pdf")
    await w.settle()
    w.drive.raise_next = RuntimeError("repository unavailable")


async def _drive_switched(w):
    w.drive.add("a.pdf")
    await w.settle()
    w.drive.connection_state = DriveConnection(connected=True, drive_id="d2")


async def _purging_slowly(w):
    for i in range(5):
        w.drive.add(f"doc{i}.pdf")
    await w.settle()
    real = w.repo.delete_user_records

    async def slow_delete(user_id, limit):
        w.clock.advance(600)   # the purge outlives the start budget: it continues in the next tick
        return await real(user_id, 1)

    w.repo.delete_user_records = slow_delete
    st = await w.repo.get_sync_state(U_SCOPE)
    await w.repo.save_sync_state(replace(st, purging=True))


@pytest.mark.parametrize("arrange,status", [
    (_mid_round, "done"), (_throttled, "throttled"), (_failed, "error"), (_drive_switched, "purged"),
    (_purging_slowly, "purging"),
], ids=["more-work", "throttled", "error", "purge-then-onboard", "purge-continues"])
async def test_the_continuation_is_enqueued_after_the_lease_is_released(arrange, status):
    w = World(page_size=10)
    await arrange(w)
    holders = _watch_lease_at_enqueue(w)
    result = await w.tick()
    assert result.status == status
    assert holders and holders == [None] * len(holders)
