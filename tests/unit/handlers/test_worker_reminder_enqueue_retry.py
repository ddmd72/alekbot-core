"""The claimed fire's `execute_reminder` create is retried under a name (P15-D3, owner decision O5), beyond the pin in
`test_worker_retry_after_effect.py` (a create that failed before landing). Redelivery world: the real `WorkerHandler`,
`RemindersService`, note store and `GcpTaskQueue` over the failing Cloud Tasks client fake on one `EvalDb`."""
import hashlib
from datetime import timedelta
from typing import Any

import pytest
from google.api_core.exceptions import DeadlineExceeded

from src.services import reminders_service
from tests.support.clock_instants import pin_datetime
from tests.support.redelivery_world import T0, USER, RedeliveryWorld, deliver


async def _due_note(world: RedeliveryWorld, monkeypatch: Any) -> str:
    note_id = await world.seed_note(monkeypatch, due=T0, created_at=T0 - timedelta(days=1))
    pin_datetime(monkeypatch, reminders_service, T0 + timedelta(seconds=30))
    monkeypatch.setattr(reminders_service, "_ENQUEUE_BACKOFF_S", 0)
    return note_id


async def test_a_create_that_landed_but_answered_an_error_is_not_doubled(monkeypatch):
    world = RedeliveryWorld()
    note_id = await _due_note(world, monkeypatch)
    world.client.fail_next(landed=True)

    answer = await deliver(world.handler, world.payload("fire_due_reminders"))

    assert answer == ({"enqueued": 1, "claim_lost": 0, "skipped": 0}, 200)
    assert world.client.failed_attempts == 1
    assert [(b["note_id"], b["user_id"], b["due_at"]) for b in world.bodies("execute_reminder")] == [
        (note_id, USER, T0.isoformat())]


async def test_the_fire_task_is_named_by_note_and_due(monkeypatch):
    world = RedeliveryWorld()
    note_id = await _due_note(world, monkeypatch)

    await deliver(world.handler, world.payload("fire_due_reminders"))

    digest = hashlib.sha256(f"execute_reminder:{note_id}@{T0.isoformat()}".encode()).hexdigest()[:32]
    assert [n.rsplit("/", 1)[-1] for n in world.client.names] == [f"execute_reminder-{digest}"]


async def test_two_failed_creates_are_retried_and_a_third_failure_raises(monkeypatch):
    world = RedeliveryWorld()
    note_id = await _due_note(world, monkeypatch)
    world.client.fail_next(times=2)

    assert await deliver(world.handler, world.payload("fire_due_reminders")) == (
        {"enqueued": 1, "claim_lost": 0, "skipped": 0}, 200)
    assert [b["note_id"] for b in world.bodies("execute_reminder")] == [note_id]

    later = RedeliveryWorld()
    await _due_note(later, monkeypatch)
    later.client.fail_next(times=3)
    with pytest.raises(DeadlineExceeded):
        await deliver(later.handler, later.payload("fire_due_reminders"))
    assert later.client.failed_attempts == 3 and later.bodies("execute_reminder") == []
