"""
Unit tests for RemindersService — Step #7 of NOTIFICATION_DELIVERY_REFACTOR_RFC.

The cron-side service no longer calls notify() synchronously. Tests pin
the new control flow:

  1. list_due_reminders → for each note:
  2. ATOMIC claim:
       recurrent → reschedule_if_due_at(expected_due=note.due, ...)
       one-time  → delete_if_due_at(expected_due=note.due, ...)
  3. On True (claim won) → enqueue execute_reminder Cloud Task.
     On False (concurrent tick won) → silently skip; counted in
     ``claim_lost``.
  4. Service NEVER calls notify or ensures agents (those move to the
     execute_reminder worker in Step #8).

Old behaviors REMOVED in this commit (regression-guarded by the new
test suite + the absence of fixtures for them):
  - _CRON_WINDOW_SECONDS idempotency guard (replaced by atomic claim).
  - Synchronous notify() from cron handler.
  - agent_factory.ensure_agents_for_user() inside cron.
  - notes_port.reschedule() (unconditional) — replaced by
    reschedule_if_due_at.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.domain.agent_note import AgentNote, ReminderRecurrence
from src.ports.agent_note_port import AgentNotePort
from src.services.reminders_service import (
    RemindersService,
    _compute_next_due,
    build_reminder_alert,
    build_reminder_alert_summary,
)
from src.services.task_dispatch_service import TaskDispatchService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "user-abc"
_ACCOUNT_ID = "acc-abc"
_NOTE_ID = "note-001"
_NOW = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
_DUE = _NOW - timedelta(minutes=5)


def _make_note(
    *,
    note_id: str = _NOTE_ID,
    user_id: str = _USER_ID,
    recurrence: ReminderRecurrence = None,
    last_fired: datetime = None,
    due: datetime = None,
) -> AgentNote:
    return AgentNote(
        note_id=note_id,
        user_id=user_id,
        text="check project status",
        instruction="Look at the project board and identify blockers.",
        due=due or _DUE,
        recurrence=recurrence,
        last_fired=last_fired,
        created_at=_NOW - timedelta(hours=1),
    )


def _make_profile(account_id: str = _ACCOUNT_ID, timezone: str = "UTC"):
    profile = MagicMock()
    profile.account_id = account_id
    profile.config.timezone = timezone
    return profile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def notes_port():
    p = AsyncMock(spec=AgentNotePort)
    p.list_due_reminders.return_value = []
    # Default: every claim succeeds. Tests override per-scenario.
    p.reschedule_if_due_at.return_value = True
    p.delete_if_due_at.return_value = True
    return p


@pytest.fixture
def user_repo():
    r = MagicMock()
    r.get_user = AsyncMock(return_value=_make_profile())
    return r


@pytest.fixture
def task_dispatch():
    t = MagicMock(spec=TaskDispatchService)
    t.enqueue_worker_task = AsyncMock(return_value="task-name-stub")
    return t


@pytest.fixture
def service(notes_port, user_repo, task_dispatch):
    return RemindersService(
        notes_port=notes_port,
        user_repo=user_repo,
        task_dispatch=task_dispatch,
    )


# ---------------------------------------------------------------------------
# No due reminders
# ---------------------------------------------------------------------------

class TestNoDueReminders:

    async def test_returns_zero_counts(self, service, notes_port):
        notes_port.list_due_reminders.return_value = []

        result, status = await service.fire_due_reminders(now_utc=_NOW)

        assert status == 200
        assert result == {"enqueued": 0, "claim_lost": 0, "skipped": 0}

    async def test_no_enqueue_calls(self, service, notes_port, task_dispatch):
        notes_port.list_due_reminders.return_value = []

        await service.fire_due_reminders(now_utc=_NOW)

        task_dispatch.enqueue_worker_task.assert_not_called()

    async def test_no_claim_attempts(self, service, notes_port):
        notes_port.list_due_reminders.return_value = []

        await service.fire_due_reminders(now_utc=_NOW)

        notes_port.reschedule_if_due_at.assert_not_called()
        notes_port.delete_if_due_at.assert_not_called()


# ---------------------------------------------------------------------------
# User resolution (skipped tally — neither claim nor enqueue happens)
# ---------------------------------------------------------------------------

class TestUserResolution:

    async def test_skips_when_user_not_found(
        self, service, notes_port, user_repo, task_dispatch,
    ):
        note = _make_note()
        notes_port.list_due_reminders.return_value = [note]
        user_repo.get_user.return_value = None

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result == {"enqueued": 0, "claim_lost": 0, "skipped": 1}
        notes_port.reschedule_if_due_at.assert_not_called()
        notes_port.delete_if_due_at.assert_not_called()
        task_dispatch.enqueue_worker_task.assert_not_called()

    async def test_skips_when_account_id_missing(
        self, service, notes_port, user_repo, task_dispatch,
    ):
        note = _make_note()
        notes_port.list_due_reminders.return_value = [note]
        profile = _make_profile()
        profile.account_id = None
        user_repo.get_user.return_value = profile

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result["skipped"] == 1
        assert result["enqueued"] == 0
        task_dispatch.enqueue_worker_task.assert_not_called()


# ---------------------------------------------------------------------------
# One-time reminder — claim via delete_if_due_at
# ---------------------------------------------------------------------------

class TestOneTimeReminder:

    async def test_successful_claim_enqueues_execute_reminder(
        self, service, notes_port, task_dispatch,
    ):
        note = _make_note(recurrence=None)
        notes_port.list_due_reminders.return_value = [note]
        notes_port.delete_if_due_at.return_value = True

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result["enqueued"] == 1
        notes_port.delete_if_due_at.assert_called_once_with(
            note_id=_NOTE_ID,
            user_id=_USER_ID,
            expected_due=note.due,
        )
        notes_port.reschedule_if_due_at.assert_not_called()
        task_dispatch.enqueue_worker_task.assert_called_once()

    async def test_failed_claim_skips_enqueue(
        self, service, notes_port, task_dispatch,
    ):
        """Concurrent cron tick won the race — silently skip, count as
        claim_lost. No enqueue, no log noise that resembles a failure."""
        note = _make_note(recurrence=None)
        notes_port.list_due_reminders.return_value = [note]
        notes_port.delete_if_due_at.return_value = False

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result == {"enqueued": 0, "claim_lost": 1, "skipped": 0}
        task_dispatch.enqueue_worker_task.assert_not_called()


# ---------------------------------------------------------------------------
# Recurrent reminder — claim via reschedule_if_due_at
# ---------------------------------------------------------------------------

class TestRecurrentReminder:

    async def test_successful_claim_enqueues_with_correct_payload(
        self, service, notes_port, task_dispatch,
    ):
        note = _make_note(recurrence=ReminderRecurrence(type="daily", interval=1))
        notes_port.list_due_reminders.return_value = [note]
        notes_port.reschedule_if_due_at.return_value = True

        await service.fire_due_reminders(now_utc=_NOW)

        # Claim attempted with the snapshot's due as expected_due.
        claim_kwargs = notes_port.reschedule_if_due_at.call_args.kwargs
        assert claim_kwargs["note_id"] == _NOTE_ID
        assert claim_kwargs["expected_due"] == note.due
        assert claim_kwargs["next_due"] > note.due
        assert claim_kwargs["last_fired"] == _NOW
        notes_port.delete_if_due_at.assert_not_called()

        # Enqueue carries (note_id, user_id, due_at) for the worker.
        task_dispatch.enqueue_worker_task.assert_called_once()
        enq_kwargs = task_dispatch.enqueue_worker_task.call_args.kwargs
        assert enq_kwargs["task_type"] == "execute_reminder"
        assert enq_kwargs["payload"] == {
            "note_id": _NOTE_ID,
            "user_id": _USER_ID,
            "due_at": note.due.isoformat(),
        }

    async def test_failed_claim_skips_enqueue(
        self, service, notes_port, task_dispatch,
    ):
        """Concurrent cron tick already rescheduled — atomic precondition
        fails → silently skip. This is the canonical fix for defect #3."""
        note = _make_note(recurrence=ReminderRecurrence(type="daily", interval=1))
        notes_port.list_due_reminders.return_value = [note]
        notes_port.reschedule_if_due_at.return_value = False

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result == {"enqueued": 0, "claim_lost": 1, "skipped": 0}
        task_dispatch.enqueue_worker_task.assert_not_called()


# ---------------------------------------------------------------------------
# Multiple notes — independent claim outcomes are tallied separately
# ---------------------------------------------------------------------------

class TestMultipleNotes:

    async def test_all_claim_won(self, service, notes_port, task_dispatch):
        notes = [_make_note(note_id=f"n{i}") for i in range(3)]
        notes_port.list_due_reminders.return_value = notes

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result["enqueued"] == 3
        assert task_dispatch.enqueue_worker_task.await_count == 3

    async def test_mixed_claim_outcomes(
        self, service, notes_port, task_dispatch,
    ):
        notes = [
            _make_note(note_id="n1"),  # claim won
            _make_note(note_id="n2"),  # claim lost
            _make_note(note_id="n3"),  # claim won
        ]
        notes_port.list_due_reminders.return_value = notes
        # First and third one-time deletes succeed; middle one fails.
        notes_port.delete_if_due_at.side_effect = [True, False, True]

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result == {"enqueued": 2, "claim_lost": 1, "skipped": 0}
        # Only the two winners enqueued tasks.
        assert task_dispatch.enqueue_worker_task.await_count == 2

    async def test_user_skipped_does_not_attempt_claim(
        self, service, notes_port, user_repo, task_dispatch,
    ):
        notes = [
            _make_note(note_id="n1"),
            _make_note(note_id="n2", user_id="user-gone"),
            _make_note(note_id="n3"),
        ]
        notes_port.list_due_reminders.return_value = notes

        # user-gone returns None from user_repo; everyone else gets default.
        async def get_user(uid):
            return None if uid == "user-gone" else _make_profile()
        user_repo.get_user = AsyncMock(side_effect=get_user)

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result == {"enqueued": 2, "claim_lost": 0, "skipped": 1}
        assert task_dispatch.enqueue_worker_task.await_count == 2


# ---------------------------------------------------------------------------
# now_utc default
# ---------------------------------------------------------------------------

class TestNowDefault:

    async def test_uses_datetime_now_when_not_provided(
        self, service, notes_port,
    ):
        """now_utc=None → service supplies datetime.now(UTC)."""
        notes_port.list_due_reminders.return_value = []

        # Should not raise.
        result, status = await service.fire_due_reminders()

        assert status == 200
        notes_port.list_due_reminders.assert_called_once()
        # The supplied as_of is some recent UTC datetime.
        called_as_of = notes_port.list_due_reminders.call_args.kwargs["as_of"]
        assert called_as_of.tzinfo is not None


# ---------------------------------------------------------------------------
# build_reminder_alert (public utility — consumed by Step #8 worker)
# ---------------------------------------------------------------------------

class TestBuildReminderAlert:

    def test_contains_note_text(self):
        note = _make_note()
        alert = build_reminder_alert(note)
        assert "check project status" in alert

    def test_contains_instruction(self):
        note = _make_note()
        alert = build_reminder_alert(note)
        assert "Look at the project board and identify blockers." in alert

    def test_contains_note_id(self):
        note = _make_note()
        alert = build_reminder_alert(note)
        assert _NOTE_ID in alert

    def test_one_time_schedule_label(self):
        note = _make_note(recurrence=None)
        alert = build_reminder_alert(note)
        assert "one-time" in alert

    def test_recurrent_schedule_label(self):
        note = _make_note(recurrence=ReminderRecurrence(type="daily", interval=2))
        alert = build_reminder_alert(note)
        assert "2 daily" in alert

    def test_self_reminder_framing(self):
        note = _make_note()
        alert = build_reminder_alert(note)
        assert "SELF-REMINDER" in alert
        assert "you wrote" in alert.lower() or "your own" in alert.lower()


# ---------------------------------------------------------------------------
# _compute_next_due
# ---------------------------------------------------------------------------

class TestComputeNextDue:

    _UTC = ZoneInfo("UTC")
    _KYIV = ZoneInfo("Europe/Kyiv")
    _BASE = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)

    def test_hourly_adds_one_hour(self):
        result = _compute_next_due(
            self._BASE, ReminderRecurrence(type="hourly", interval=1), self._UTC
        )
        assert result == self._BASE + timedelta(hours=1)

    def test_hourly_interval_2(self):
        result = _compute_next_due(
            self._BASE, ReminderRecurrence(type="hourly", interval=2), self._UTC
        )
        assert result == self._BASE + timedelta(hours=2)

    def test_daily_adds_one_day(self):
        result = _compute_next_due(
            self._BASE, ReminderRecurrence(type="daily", interval=1), self._UTC
        )
        assert result == self._BASE + timedelta(days=1)

    def test_weekly_adds_seven_days(self):
        result = _compute_next_due(
            self._BASE, ReminderRecurrence(type="weekly", interval=1), self._UTC
        )
        assert result == self._BASE + timedelta(weeks=1)

    def test_monthly_adds_one_month(self):
        base = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        result = _compute_next_due(
            base, ReminderRecurrence(type="monthly", interval=1), self._UTC
        )
        assert result.month == 2
        assert result.day == 15

    def test_monthly_end_of_month(self):
        base = datetime(2026, 1, 31, 10, 0, 0, tzinfo=timezone.utc)
        result = _compute_next_due(
            base, ReminderRecurrence(type="monthly", interval=1), self._UTC
        )
        # dateutil handles month-end clamping (Jan 31 + 1 month = Feb 28)
        assert result.month == 2

    def test_result_is_utc(self):
        result = _compute_next_due(
            self._BASE, ReminderRecurrence(type="daily", interval=1), self._KYIV
        )
        assert result.tzinfo == timezone.utc

    def test_unknown_type_defaults_to_daily(self):
        result = _compute_next_due(
            self._BASE, ReminderRecurrence(type="biannual", interval=1), self._UTC
        )
        assert result == self._BASE + timedelta(days=1)


class TestBuildReminderAlertSummary:

    def test_is_compact_label_name_only(self):
        # The compact label is what survives history tiering: the reminder name
        # only — no instruction body, no note_id, no framing lyrics.
        note = _make_note()
        summary = build_reminder_alert_summary(note)

        assert summary == 'Received reminder: "check project status"'
        assert "blockers" not in summary            # instruction body excluded
        assert _NOTE_ID not in summary              # no note_id
        assert len(summary) < len(build_reminder_alert(note))
