"""RemindersService.fire_due_reminders: a recurrent note whose rule cannot be rescheduled is left alone.

Two ways to get there (both reach `skipped`, neither may touch the note or enqueue a fire):
  - the rule yields no next occurrence at all (corrupted or hand-edited rule);
  - the rule is more than `_MAX_CATCH_UP_STEPS` occurrences behind `now`, so the catch-up loop gives up."""
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent_note import AgentNote
from src.ports.agent_note_port import AgentNotePort
from src.ports.recurrence_port import RecurrencePort
from src.services import reminders_service
from src.services.reminders_service import RemindersService
from src.services.task_dispatch_service import TaskDispatchService

_NOW = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
_DUE = _NOW - timedelta(minutes=5)


def _recurrent_note(note_id: str = "note-rec") -> AgentNote:
    return AgentNote(
        note_id=note_id,
        user_id="user-abc",
        text="check project status",
        instruction="Look at the project board.",
        due=_DUE,
        recurrence="FREQ=DAILY",
        created_at=_NOW - timedelta(hours=1),
    )


@pytest.fixture
def notes_port():
    port = AsyncMock(spec=AgentNotePort)
    port.reschedule_if_due_at.return_value = True
    port.claim_one_time_if_due_at.return_value = True
    return port


@pytest.fixture
def task_dispatch():
    dispatch = MagicMock(spec=TaskDispatchService)
    dispatch.enqueue_worker_task = AsyncMock(return_value="task-name-stub")
    return dispatch


@pytest.fixture
def recurrence():
    return MagicMock(spec=RecurrencePort)


@pytest.fixture
def service(notes_port, task_dispatch, recurrence):
    profile = MagicMock()
    profile.account_id = "acc-abc"
    profile.config.timezone = "UTC"
    user_repo = MagicMock()
    user_repo.get_user = AsyncMock(return_value=profile)
    return RemindersService(
        notes_port=notes_port, user_repo=user_repo, task_dispatch=task_dispatch, recurrence=recurrence,
    )


async def test_a_rule_with_no_next_occurrence_is_skipped_untouched(
    service, notes_port, task_dispatch, recurrence, caplog,
):
    recurrence.next_occurrence.return_value = None
    notes_port.list_due_reminders.return_value = [_recurrent_note()]

    with caplog.at_level(logging.ERROR):
        result, status = await service.fire_due_reminders(now_utc=_NOW)

    assert status == 200
    assert result == {"enqueued": 0, "claim_lost": 0, "skipped": 1}
    notes_port.reschedule_if_due_at.assert_not_awaited()
    notes_port.claim_one_time_if_due_at.assert_not_awaited()
    notes_port.delete_if_due_at.assert_not_awaited()
    task_dispatch.enqueue_worker_task.assert_not_awaited()
    assert any("yields no next occurrence" in r.getMessage() and "note-rec" in r.getMessage() for r in caplog.records)


async def test_a_skipped_note_does_not_stop_the_others(service, notes_port, task_dispatch, recurrence):
    broken, healthy = _recurrent_note("note-broken"), _recurrent_note("note-ok")
    calls = {"n": 0}

    def next_occurrence(rule, after, tz):
        calls["n"] += 1
        return None if calls["n"] == 1 else _NOW + timedelta(days=1)

    recurrence.next_occurrence.side_effect = next_occurrence
    notes_port.list_due_reminders.return_value = [broken, healthy]

    result, _ = await service.fire_due_reminders(now_utc=_NOW)

    assert result == {"enqueued": 1, "claim_lost": 0, "skipped": 1}
    assert notes_port.reschedule_if_due_at.await_count == 1
    assert notes_port.reschedule_if_due_at.await_args.kwargs["note_id"] == "note-ok"
    assert task_dispatch.enqueue_worker_task.await_args.kwargs["payload"]["note_id"] == "note-ok"


async def test_a_rule_that_never_gets_past_now_gives_up_after_the_catch_up_cap(
    service, notes_port, task_dispatch, recurrence, caplog,
):
    recurrence.next_occurrence.return_value = _NOW - timedelta(minutes=1)  # always in the past
    notes_port.list_due_reminders.return_value = [_recurrent_note()]

    with caplog.at_level(logging.ERROR):
        result, status = await service.fire_due_reminders(now_utc=_NOW)

    assert status == 200
    assert result == {"enqueued": 0, "claim_lost": 0, "skipped": 1}
    # one call for the first step after `due`, then one per catch-up iteration
    assert recurrence.next_occurrence.call_count == 1 + reminders_service._MAX_CATCH_UP_STEPS
    notes_port.reschedule_if_due_at.assert_not_awaited()
    task_dispatch.enqueue_worker_task.assert_not_awaited()
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("occurrences behind" in m and "not rescheduled" in m for m in messages)
