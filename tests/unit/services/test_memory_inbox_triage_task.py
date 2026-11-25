"""MemoryInboxTriageTask (MCP_MEMORY_SUBMISSIONS_RFC §10.2, §11): the rhythm around a triage pass, against a scripted
`TriagePass`, the real inbox store and lease adapter on the evaluating fake, and a recording queue and notifier."""
from datetime import datetime, timedelta

from src.domain.language import LanguageCode
from src.domain.memory_inbox_task_keys import triage_defer_key
from src.domain.ui_messages import UIMessage
from src.services.memory_inbox_triage_task import MemoryInboxTriageTask
from tests.support.memory_inbox_rhythm import (
    UTC,
    FakePass,
    Languages,
    Notifier,
    Rig,
    triage_outcome,
)

NOW = datetime(2026, 11, 25, 11, 30, tzinfo=UTC)
PAYLOAD = {"task_type": "memory_inbox_triage", "user_id": "u1", "account_id": "a1"}


class World(Rig):
    def __init__(self, zone: str = "Europe/Madrid", outcome=None, days=None) -> None:
        super().__init__(NOW, zone, days)
        self.triage = FakePass(self.db, outcome or triage_outcome())
        self.notifier = Notifier()
        self.languages = Languages()
        self.task = MemoryInboxTriageTask(
            store=self.repo, triage_pass=self.triage, lease=self.lease, task_queue=self.queue, user_repo=self.users,
            account_repo=self.accounts, owner_zones=self.owner_zones, languages=self.languages,
            notifications=self.notifier, run_timeout_s=900.0, clock=self.clock)

    async def run(self, payload=PAYLOAD):
        return await self.task(dict(payload), {})


async def test_a_run_without_a_user_is_a_bad_request():
    world = World()

    assert await world.run({"account_id": "a1"}) == ({"error": "missing user_id"}, 400)
    assert world.triage.requests == []


async def test_a_user_without_an_account_runs_nothing():
    world = World()
    world.users.accounts["u1"] = None

    assert await world.run() == ({"status": "failed"}, 200)
    assert world.triage.requests == [] and world.lease_doc is None


async def test_an_interval_not_elapsed_runs_nothing():
    world = World()
    await world.repo.record_triage("u1", True, datetime(2026, 11, 25, 7, 0, tzinfo=UTC))     # 08:00 Europe/Madrid, same day

    assert await world.run() == ({"status": "interval"}, 200)
    assert world.triage.requests == [] and world.lease_doc is None


async def test_the_interval_counts_the_owners_date():
    world = World(zone="Pacific/Auckland")                  # NOW is 00:30 on 11-26 there
    await world.repo.record_triage("u1", True, datetime(2026, 11, 25, 5, 0, tzinfo=UTC))      # 18:00 on 11-25 there

    assert await world.run() == ({"status": "completed"}, 200)
    assert len(world.triage.requests) == 1 and world.triage.requests[0].zone == "Pacific/Auckland"


async def test_a_longer_interval_waits_for_its_days():
    world = World(days=3)
    await world.repo.record_triage("u1", True, NOW - timedelta(days=2))

    assert (await world.run())[0] == {"status": "interval"}

    await world.repo.record_triage("u1", True, NOW - timedelta(days=3))

    assert (await world.run())[0] == {"status": "completed"}


async def test_a_busy_lease_defers_once_per_bucket():
    world = World()
    await world.hold_lease(NOW + timedelta(minutes=10))

    assert await world.run() == ({"status": "deferred"}, 200)
    assert await world.run() == ({"status": "deferred"}, 200)

    [task] = world.queue.created
    assert task["task_type"] == "memory_inbox_triage" and task["payload"] == {"user_id": "u1"}
    assert task["dedup_key"] == triage_defer_key("a1", NOW)
    assert task["delay_seconds"] == 120 and task["deadline_seconds"] == 1800
    assert len(world.queue.attempts) == 2 and world.triage.requests == []


async def test_the_lease_account_comes_from_the_profile():
    world = World()

    await world.run({"user_id": "u1", "account_id": "a2"})

    [request] = world.triage.requests
    assert request.account_id == "a1" and request.user_id == "u1"
    assert world.triage.leases_seen[0] is not None and "a2" not in world.db.collection("eval_memory_write_leases").docs


async def test_the_pass_gets_the_lease_end_the_owners_zone_and_language():
    world = World()
    world.languages.code = LanguageCode.FR

    await world.run()

    [request] = world.triage.requests
    assert request.now == NOW and request.zone == "Europe/Madrid" and request.language == "fr"
    assert request.deadline == world.triage.leases_seen[0]["lease_until"] == NOW + timedelta(seconds=960)
    assert world.lease_doc is None                        # released after the run


async def test_new_candidates_post_one_literal_notice():
    world = World(outcome=triage_outcome(candidates=3, sources=("Claude",)))

    assert await world.run() == ({"status": "completed"}, 200)

    assert world.notifier.posts == [("u1", "a1", UIMessage.MEMORY_INBOX_CANDIDATES_FROM,
                                     {"count": "3", "source": "Claude"})]


async def test_candidates_from_several_clients_post_the_generic_notice():
    world = World(outcome=triage_outcome(candidates=3, sources=("Claude", "Desk assistant", "Claude")))

    await world.run()

    assert world.notifier.posts == [("u1", "a1", UIMessage.MEMORY_INBOX_CANDIDATES, {"count": "3"})]


async def test_a_named_and_an_unnamed_client_post_the_generic_notice():
    world = World(outcome=triage_outcome(candidates=3, sources=("Claude", "")))

    await world.run()

    assert world.notifier.posts == [("u1", "a1", UIMessage.MEMORY_INBOX_CANDIDATES, {"count": "3"})]


async def test_no_candidates_post_nothing():
    world = World(outcome=triage_outcome(candidates=0))

    await world.run()

    assert world.notifier.posts == []


async def test_a_failed_notice_does_not_fail_the_run():
    world = World(outcome=triage_outcome(candidates=2, sources=("Claude",)))
    world.notifier.raises = True

    assert await world.run() == ({"status": "completed"}, 200)
    assert (await world.repo.state("u1")).last_triage_at == NOW


async def test_a_completed_run_marks_the_triage_and_an_empty_one_too():
    world = World(outcome=triage_outcome(status="empty"))

    assert await world.run() == ({"status": "empty"}, 200)

    state = await world.repo.state("u1")
    assert state.last_triage_at == NOW and state.last_triage_failed_at is None


async def test_a_failed_pass_marks_the_failure_and_releases_the_lease():
    world = World(outcome=triage_outcome(status="failed", candidates=1, sources=("Claude",)))
    earlier = NOW - timedelta(days=2)
    await world.repo.record_triage("u1", True, earlier)

    assert await world.run() == ({"status": "failed"}, 200)

    state = await world.repo.state("u1")
    assert state.last_triage_failed_at == NOW and state.last_triage_at == earlier
    assert world.lease_doc is None
    assert len(world.notifier.posts) == 1                  # the decisions it wrote before failing stand, and are announced


async def test_a_pass_that_raises_is_a_failed_run():
    world = World()
    world.triage.error = RuntimeError("coordinator down")

    assert await world.run() == ({"status": "failed"}, 200)

    state = await world.repo.state("u1")
    assert state.last_triage_failed_at == NOW and state.last_triage_at is None
    assert world.lease_doc is None
