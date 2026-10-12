"""Throttle backoff (USER_DRIVE_INDEXING_RFC §7.7, §8.2)."""
from src.domain.user_drive import DriveThrottled
from src.services import drive_indexing_service as svc_mod

from .test_drive_indexing_service import World


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
        assert (await w.repo.get_sync_state("u")).systemic_failures == 3

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
