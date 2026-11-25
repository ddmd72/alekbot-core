"""MemoryInboxSweepTask (MCP_MEMORY_SUBMISSIONS_RFC §10.1): the daily tick over the real inbox store on the evaluating
fake, with a recording queue that keeps Cloud Tasks' name semantics."""
from datetime import datetime, timedelta

from src.domain.memory_inbox import ANSWER_WINDOW
from src.domain.memory_inbox_task_keys import final_key, triage_key
from src.services.memory_inbox_sweep_task import MemoryInboxSweepTask
from tests.support.memory_inbox_rhythm import UTC, Rig

NOW = datetime(2026, 11, 25, 7, 30, tzinfo=UTC)         # 08:30 Europe/Madrid


class World(Rig):
    def __init__(self, zone: str = "Europe/Madrid", days=None) -> None:
        super().__init__(NOW, zone, days)
        self.sweep = MemoryInboxSweepTask(store=self.repo, task_queue=self.queue, user_repo=self.users,
                                          account_repo=self.accounts, owner_zones=self.owner_zones, clock=self.clock)

    async def run(self):
        return await self.sweep({"task_type": "memory_inbox_sweep"}, {})


async def test_the_sweep_expires_triages_and_backstops():
    world = World()
    await world.candidate("Maria likes tea", at=NOW - ANSWER_WINDOW - timedelta(days=1))          # unanswered: expires
    await world.confirmed(NOW - timedelta(hours=3))                      # final_due_at two hours ago: a lost final
    await world.received("Clara booked the Coimbra weekend")

    body, status = await world.run()

    assert status == 200 and body == {"expired": 1, "triage_enqueued": 1, "final_backstop": 1}
    due = NOW - timedelta(hours=2)
    assert world.queue.keys == {triage_key("u1", NOW, "Europe/Madrid"), final_key("u1", due)}
    by_type = {call["task_type"]: call for call in world.queue.created}
    assert by_type["memory_inbox_triage"]["payload"] == {"user_id": "u1"}
    assert by_type["memory_inbox_final"]["payload"] == {"user_id": "u1"}
    assert all(call["deadline_seconds"] == 1800 and call["delay_seconds"] == 0 for call in world.queue.created)
    assert len(world.inbox.of_status("expired")) == 1


async def test_a_user_whose_interval_has_not_elapsed_is_not_enqueued():
    world = World()
    await world.received()
    await world.repo.record_triage("u1", True, datetime(2026, 11, 25, 0, 30, tzinfo=UTC))        # earlier the same Europe/Madrid day

    assert (await world.run())[0] == {"expired": 0, "triage_enqueued": 0, "final_backstop": 0}
    assert world.queue.attempts == []


async def test_the_interval_counts_the_owners_date_and_the_users_own_days():
    world = World(zone="Pacific/Auckland")                  # NOW is 20:30 on 11-25 there
    await world.received()
    await world.repo.record_triage("u1", True, datetime(2026, 11, 24, 12, 0, tzinfo=UTC))        # 01:00 on 11-25 there

    assert (await world.run())[0]["triage_enqueued"] == 0

    world = World(zone="Pacific/Auckland")
    await world.received()
    await world.repo.record_triage("u1", True, datetime(2026, 11, 24, 10, 0, tzinfo=UTC))        # 23:00 on 11-24 there

    assert (await world.run())[0]["triage_enqueued"] == 1
    assert world.queue.keys == {triage_key("u1", NOW, "Pacific/Auckland")}
    assert triage_key("u1", NOW, "Pacific/Auckland").endswith("2026-11-25")

    world = World(days=3)
    await world.received()
    await world.repo.record_triage("u1", True, NOW - timedelta(days=2))

    assert (await world.run())[0]["triage_enqueued"] == 0


async def test_a_final_due_within_the_grace_hour_is_left_alone():
    world = World()
    await world.confirmed(NOW - timedelta(minutes=90))      # final_due_at 30 minutes ago: its own task is still due to run

    assert (await world.run())[0] == {"expired": 0, "triage_enqueued": 0, "final_backstop": 0}
    assert world.queue.attempts == []


async def test_a_user_without_confirmed_candidates_gets_no_backstop():
    world = World()
    cid = await world.confirmed(NOW - timedelta(hours=3))
    await world.repo.settle_final([cid], True, NOW - timedelta(hours=2))        # consolidated, but the due time stayed

    assert (await world.run())[0]["final_backstop"] == 0


async def test_a_second_sweep_the_same_day_enqueues_the_same_keys():
    world = World()
    await world.received()
    await world.confirmed(NOW - timedelta(hours=3))

    first = (await world.run())[0]
    again = (await world.run())[0]

    assert first == {"expired": 0, "triage_enqueued": 1, "final_backstop": 1}
    assert again == {"expired": 0, "triage_enqueued": 0, "final_backstop": 0}
    assert len(world.queue.attempts) == 4
    assert [c["dedup_key"] for c in world.queue.attempts[:2]] == [c["dedup_key"] for c in world.queue.attempts[2:]]


async def test_a_user_without_an_account_is_skipped_and_the_others_are_not():
    world = World()
    world.users.accounts["u2"] = None
    await world.received("Maria likes tea", user="u2", account="a2")
    await world.received("Alex trains with Robin")

    assert (await world.run())[0]["triage_enqueued"] == 1
    assert world.queue.keys == {triage_key("u1", NOW, "Europe/Madrid")}


async def test_a_failed_enqueue_is_logged_and_the_sweep_still_answers():
    world = World()
    await world.received()
    world.queue.failing = True

    assert await world.run() == ({"expired": 0, "triage_enqueued": 0, "final_backstop": 0}, 200)
