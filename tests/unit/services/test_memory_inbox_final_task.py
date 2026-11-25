"""MemoryInboxFinalTask (MCP_MEMORY_SUBMISSIONS_RFC §9.4, §10.3): the rhythm around the final pass, against a scripted
`FinalPass`, the real inbox store and lease adapter on the evaluating fake, and a recording queue."""
from datetime import datetime, timedelta

from src.domain.memory_inbox_task_keys import final_defer_key
from src.services.memory_inbox_final_task import MemoryInboxFinalTask
from tests.support.memory_inbox_rhythm import UTC, FakePass, ProfileStep, Rig, final_outcome

T0 = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)
PAYLOAD = {"task_type": "memory_inbox_final", "user_id": "u1", "account_id": "a1"}


class World(Rig):
    def __init__(self, outcome=None) -> None:
        super().__init__(T0)
        self.final = FakePass(self.db, outcome or final_outcome())
        self.profile = ProfileStep(self.db)
        self.task = MemoryInboxFinalTask(
            store=self.repo, final_pass=self.final, lease=self.lease, task_queue=self.queue, user_repo=self.users,
            owner_zones=self.owner_zones, profile_step=self.profile, run_timeout_s=900.0, clock=self.clock)

    async def run(self, payload=PAYLOAD):
        return await self.task(dict(payload), {})


async def test_a_run_without_a_user_is_a_bad_request():
    world = World()

    assert await world.run({"account_id": "a1"}) == ({"error": "missing user_id"}, 400)


async def test_a_task_before_final_due_does_nothing():
    world = World()
    await world.confirmed(T0)                               # final_due_at = T0 + 1 h
    world.clock.now = T0 + timedelta(minutes=59)

    assert await world.run() == ({"status": "not_due"}, 200)

    assert world.final.requests == [] and world.lease_doc is None and world.queue.attempts == []
    assert (await world.repo.state("u1")).final_due_at == T0 + timedelta(hours=1)


async def test_a_task_with_nothing_due_does_nothing():
    world = World()

    assert await world.run() == ({"status": "not_due"}, 200)
    assert world.final.requests == [] and world.lease_doc is None


async def test_a_burst_of_confirmations_runs_once():
    world = World()
    await world.confirmed(T0, "Alex trains with Robin")
    await world.confirmed(T0 + timedelta(minutes=10), "Maria likes tea")
    await world.confirmed(T0 + timedelta(minutes=20), "Clara booked the Coimbra weekend")
    results = []
    for minutes in (60, 70, 80):                            # the three tasks, each an hour after its confirmation
        world.clock.now = T0 + timedelta(minutes=minutes)
        results.append((await world.run())[0]["status"])

    assert results == ["not_due", "not_due", "completed"] and len(world.final.requests) == 1
    state = await world.repo.state("u1")
    assert state.final_due_at is None and state.last_final_at == T0 + timedelta(minutes=80)


async def test_a_confirmation_during_the_run_keeps_its_due_time():       # Review Focus 3
    world = World()
    await world.confirmed(T0)
    world.clock.now = T0 + timedelta(hours=1)
    late = T0 + timedelta(hours=1, minutes=5)

    async def another_confirmation():
        await world.confirmed(late, "Maria likes tea")

    world.final.during = another_confirmation

    assert await world.run() == ({"status": "completed"}, 200)

    state = await world.repo.state("u1")
    assert state.final_due_at == late + timedelta(hours=1)
    assert state.last_final_at is not None and world.lease_doc is None


async def test_the_pass_gets_the_lease_end_and_the_owners_zone():
    world = World()
    await world.confirmed(T0)
    world.clock.now = T0 + timedelta(hours=1)

    await world.run()

    [request] = world.final.requests
    assert request.user_id == "u1" and request.account_id == "a1" and request.zone == "Europe/Madrid"
    assert request.now == world.clock.now and request.deadline == world.clock.now + timedelta(seconds=960)
    assert world.final.leases_seen[0] is not None and world.lease_doc is None


async def test_a_busy_lease_defers_once_per_bucket_and_keeps_the_due_time():
    world = World()
    await world.confirmed(T0)
    world.clock.now = T0 + timedelta(hours=1)
    await world.hold_lease(world.clock.now + timedelta(minutes=10))

    assert await world.run() == ({"status": "deferred"}, 200)
    assert await world.run() == ({"status": "deferred"}, 200)

    [task] = world.queue.created
    assert task["task_type"] == "memory_inbox_final" and task["payload"] == {"user_id": "u1"}
    assert task["dedup_key"] == final_defer_key("a1", world.clock.now)
    assert task["delay_seconds"] == 120 and task["deadline_seconds"] == 1800
    assert world.final.requests == [] and (await world.repo.state("u1")).final_due_at == T0 + timedelta(hours=1)


async def test_a_failed_pass_retries_once_an_hour_later_up_to_three():
    world = World(outcome=final_outcome(status="failed"))
    await world.confirmed(T0)
    due = T0 + timedelta(hours=1)
    world.clock.now = due
    statuses = []
    for _ in range(4):
        statuses.append((await world.run())[0]["status"])
        world.clock.now += timedelta(hours=1)

    assert statuses == ["failed"] * 4
    retries = world.queue.created
    assert len(retries) == 2 and all(r["task_type"] == "memory_inbox_final" for r in retries)
    assert [r["delay_seconds"] for r in retries] == [3600, 3600] and retries[0]["dedup_key"] != retries[1]["dedup_key"]
    assert all(r["deadline_seconds"] == 1800 for r in retries)
    state = await world.repo.state("u1")
    assert state.final_due_at == due and state.final_attempts == 4      # left for the sweep's backstop
    assert world.lease_doc is None


async def test_a_partial_pass_is_retried_like_a_failed_one():
    world = World(outcome=final_outcome(status="partial", wrote=1))
    await world.confirmed(T0)
    world.clock.now = T0 + timedelta(hours=1)

    assert await world.run() == ({"status": "partial"}, 200)

    assert len(world.queue.created) == 1 and (await world.repo.state("u1")).final_due_at is not None


async def test_a_pass_that_raises_is_a_failed_run():
    world = World()
    await world.confirmed(T0)
    world.clock.now = T0 + timedelta(hours=1)
    world.final.error = RuntimeError("coordinator down")

    assert await world.run() == ({"status": "failed"}, 200)

    assert len(world.queue.created) == 1 and world.lease_doc is None
    assert (await world.repo.state("u1")).final_due_at is not None


async def test_an_empty_run_clears_the_due_time():
    world = World(outcome=final_outcome(status="empty", candidates=0))
    await world.confirmed(T0)
    world.clock.now = T0 + timedelta(hours=1)

    assert await world.run() == ({"status": "empty"}, 200)

    assert (await world.repo.state("u1")).final_due_at is None and world.queue.attempts == []


async def test_the_profile_step_runs_inside_the_lease_only_after_a_write():
    world = World(outcome=final_outcome(wrote=0))
    await world.confirmed(T0)
    world.clock.now = T0 + timedelta(hours=1)

    await world.run()

    assert world.profile.calls == []

    world.final.outcome = final_outcome(wrote=2)
    await world.confirmed(world.clock.now, "Maria likes tea")
    world.clock.now += timedelta(hours=1)
    await world.run()

    [(account_id, deadline)] = world.profile.calls
    assert account_id == "a1" and deadline == world.clock.now + timedelta(seconds=960)
    assert world.profile.leases_seen[0] is not None and world.lease_doc is None


async def test_a_failing_profile_step_does_not_change_the_result():
    world = World(outcome=final_outcome(wrote=1))
    world.profile.error = RuntimeError("profile down")
    await world.confirmed(T0)
    world.clock.now = T0 + timedelta(hours=1)

    assert await world.run() == ({"status": "completed"}, 200)

    assert (await world.repo.state("u1")).final_due_at is None and world.lease_doc is None
