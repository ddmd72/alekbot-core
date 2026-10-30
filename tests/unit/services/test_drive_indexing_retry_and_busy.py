"""Throttle backoff and a tick that finds the lease busy (USER_DRIVE_INDEXING_RFC §7.1, §7.7, §8.2)."""
from datetime import timedelta

from src.domain.drive_index_params import NUDGE_RETRY_DELAY_S
from src.domain.user_drive import DriveThrottled
from src.services import drive_indexing_service as svc_mod

from .test_drive_indexing_service import World
from .drive_index_fakes import U_SCOPE


def _continuations(w, before):
    return [t for t in w.queue.tasks[before:] if ":cont:" in (t["key"] or "")]


class TestRepeatedThrottleBacksOff:

    async def test_first_throttle_honours_retry_after_then_backs_off(self):
        w = World()
        w.drive.add("a.pdf")
        await w.settle()
        delays = []
        for _ in range(3):
            w.drive.raise_next = DriveThrottled(retry_after_s=5)
            before = len(w.queue.tasks)
            assert (await w.tick()).status == "throttled"
            delays.append(_continuations(w, before)[-1]["delay"])
        assert delays == [5, svc_mod._backoff_s(1), svc_mod._backoff_s(2)]
        assert (await w.repo.get_sync_state(U_SCOPE)).systemic_failures == 3

    async def test_a_long_retry_after_wins_over_the_backoff(self):
        w = World()
        w.drive.add("a.pdf")
        await w.settle()
        w.drive.raise_next = DriveThrottled(retry_after_s=5)
        await w.tick()
        w.drive.raise_next = DriveThrottled(retry_after_s=900)
        before = len(w.queue.tasks)
        await w.tick()
        assert _continuations(w, before)[-1]["delay"] == 900

    async def test_a_clean_tick_resets_the_throttle_backoff(self):
        w = World()
        w.drive.add("a.pdf")
        await w.settle()
        w.drive.raise_next = DriveThrottled(retry_after_s=5)
        await w.tick()
        await w.tick()
        w.drive.raise_next = DriveThrottled(retry_after_s=5)
        before = len(w.queue.tasks)
        await w.tick()
        assert _continuations(w, before)[-1]["delay"] == 5


class TestBusyTickComesBack:

    async def test_a_busy_tick_requeues_itself_once_per_holder(self):
        w = World()
        w.drive.add("a.pdf")
        await w.settle()
        await w.repo.acquire_lease(U_SCOPE, "holder", w.clock.now, w.clock.now + timedelta(minutes=31), create=True)
        before = len(w.queue.tasks)
        assert (await w.tick()).status == "busy"
        assert (await w.tick()).status == "busy"          # a redelivery of the same task
        requeued = w.queue.tasks[before:]
        assert len(requeued) == 1 and requeued[0]["delay"] == NUDGE_RETRY_DELAY_S
        assert requeued[0]["payload"] == {"user_id": "u"}

    async def test_a_continuation_racing_its_parent_is_not_lost(self):
        """With more than one dispatch slot a continuation runs at once. It is enqueued only after its tick released
        the lease (§7.1), so it takes the lease and works; the busy path is not needed and the chain finishes."""
        w = World(page_size=10)
        for i in range(60):
            w.drive.add(f"doc{i:02d}.pdf")
        real_changes = w.drive.get_changes

        async def slow_changes(user_id, cursor):
            w.clock.advance(200)                 # a slow page: the start budget ends mid-round
            return await real_changes(user_id, cursor)

        w.drive.get_changes = slow_changes
        concurrent = []
        real_enqueue = w.queue.enqueue_worker_task

        async def enqueue(task_type, payload, delay_seconds=0, deadline_seconds=None, dedup_key=None):
            name = await real_enqueue(task_type, payload, delay_seconds, deadline_seconds, dedup_key)
            if dedup_key and ":cont:" in dedup_key and delay_seconds == 0 and not concurrent:
                concurrent.append(await w.svc.tick(U_SCOPE))   # a second dispatch slot runs it at once
            return name

        w.queue.enqueue_worker_task = enqueue
        await w.svc.tick(U_SCOPE)
        assert concurrent[0].status == "done"
        assert not [t for t in w.queue.tasks if "busy-" in (t["key"] or "")]
        w.drive.get_changes = real_changes
        w.queue.enqueue_worker_task = real_enqueue
        w.clock.advance(NUDGE_RETRY_DELAY_S)
        await w.settle()
        assert len(w.carded()) == 60
