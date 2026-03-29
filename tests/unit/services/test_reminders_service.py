"""
Unit tests for RemindersService.

Covers: fire_due_reminders lifecycle, idempotency guard, recurrence scheduling,
one-time deletion, notify-failure resilience, _build_reminder_alert, _compute_next_due.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.domain.agent_note import AgentNote, ReminderRecurrence
from src.ports.agent_note_port import AgentNotePort
from src.services.reminders_service import (
    RemindersService,
    _build_reminder_alert,
    _compute_next_due,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "user-abc"
_ACCOUNT_ID = "acc-abc"
_NOTE_ID = "note-001"
_NOW = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)


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
        due=due or _NOW - timedelta(minutes=5),
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
    p.reschedule.return_value = None
    p.delete_note.return_value = None
    return p


@pytest.fixture
def user_repo():
    r = MagicMock()
    r.get_user = AsyncMock(return_value=_make_profile())
    return r


@pytest.fixture
def notification():
    n = MagicMock()
    n.notify = AsyncMock()
    return n


@pytest.fixture
def agent_factory():
    f = MagicMock()
    f.ensure_agents_for_user = AsyncMock()
    return f


@pytest.fixture
def service(notes_port, user_repo, notification, agent_factory):
    return RemindersService(
        notes_port=notes_port,
        user_repo=user_repo,
        notification_service=notification,
        agent_factory=agent_factory,
    )


# ---------------------------------------------------------------------------
# No due reminders
# ---------------------------------------------------------------------------

class TestNoDueReminders:

    async def test_returns_zero_counts(self, service, notes_port):
        notes_port.list_due_reminders.return_value = []

        result, status = await service.fire_due_reminders(now_utc=_NOW)

        assert status == 200
        assert result == {"fired": 0, "skipped": 0}

    async def test_no_notify_calls(self, service, notes_port, notification):
        notes_port.list_due_reminders.return_value = []

        await service.fire_due_reminders(now_utc=_NOW)

        notification.notify.assert_not_called()


# ---------------------------------------------------------------------------
# Idempotency guard
# ---------------------------------------------------------------------------

class TestIdempotencyGuard:

    async def test_skips_recently_fired_note(self, service, notes_port):
        # last_fired 2 minutes ago — within 4-min window
        note = _make_note(last_fired=_NOW - timedelta(minutes=2))
        notes_port.list_due_reminders.return_value = [note]

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result["skipped"] == 1
        assert result["fired"] == 0

    async def test_fires_note_outside_window(self, service, notes_port):
        # last_fired 5 minutes ago — outside window
        note = _make_note(last_fired=_NOW - timedelta(minutes=5))
        notes_port.list_due_reminders.return_value = [note]

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result["fired"] == 1

    async def test_fires_note_with_no_last_fired(self, service, notes_port):
        note = _make_note(last_fired=None)
        notes_port.list_due_reminders.return_value = [note]

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result["fired"] == 1


# ---------------------------------------------------------------------------
# User resolution
# ---------------------------------------------------------------------------

class TestUserResolution:

    async def test_skips_when_user_not_found(self, service, notes_port, user_repo, notification):
        note = _make_note()
        notes_port.list_due_reminders.return_value = [note]
        user_repo.get_user.return_value = None

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result["skipped"] == 1
        notification.notify.assert_not_called()

    async def test_skips_when_account_id_missing(self, service, notes_port, user_repo, notification):
        note = _make_note()
        notes_port.list_due_reminders.return_value = [note]
        profile = _make_profile()
        profile.account_id = None
        user_repo.get_user.return_value = profile

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result["skipped"] == 1
        notification.notify.assert_not_called()


# ---------------------------------------------------------------------------
# One-time reminder
# ---------------------------------------------------------------------------

class TestOneTimeReminder:

    async def test_note_deleted_after_firing(self, service, notes_port):
        note = _make_note(recurrence=None)
        notes_port.list_due_reminders.return_value = [note]

        await service.fire_due_reminders(now_utc=_NOW)

        notes_port.delete_note.assert_called_once_with(_NOTE_ID, _USER_ID)
        notes_port.reschedule.assert_not_called()

    async def test_notify_called_with_correct_user(self, service, notes_port, notification):
        note = _make_note(recurrence=None)
        notes_port.list_due_reminders.return_value = [note]

        await service.fire_due_reminders(now_utc=_NOW)

        _, kwargs = notification.notify.call_args
        assert kwargs["user_id"] == _USER_ID
        assert kwargs["account_id"] == _ACCOUNT_ID

    async def test_ensure_agents_called_before_notify(self, service, notes_port, notification, agent_factory):
        note = _make_note()
        notes_port.list_due_reminders.return_value = [note]
        call_order = []
        agent_factory.ensure_agents_for_user.side_effect = lambda uid: call_order.append("ensure")
        notification.notify.side_effect = lambda **kw: call_order.append("notify")

        await service.fire_due_reminders(now_utc=_NOW)

        assert call_order == ["ensure", "notify"]

    async def test_agent_id_override_targets_smart_agent(self, service, notes_port, notification):
        note = _make_note()
        notes_port.list_due_reminders.return_value = [note]

        await service.fire_due_reminders(now_utc=_NOW)

        _, kwargs = notification.notify.call_args
        assert kwargs["agent_id_override"] == f"smart_response_agent_{_USER_ID}"


# ---------------------------------------------------------------------------
# Recurrent reminder
# ---------------------------------------------------------------------------

class TestRecurrentReminder:

    async def test_note_rescheduled_not_deleted(self, service, notes_port):
        note = _make_note(recurrence=ReminderRecurrence(type="daily", interval=1))
        notes_port.list_due_reminders.return_value = [note]

        await service.fire_due_reminders(now_utc=_NOW)

        notes_port.reschedule.assert_called_once()
        notes_port.delete_note.assert_not_called()

    async def test_reschedule_receives_note_id_and_future_date(self, service, notes_port):
        note = _make_note(
            due=_NOW - timedelta(minutes=5),
            recurrence=ReminderRecurrence(type="daily", interval=1),
        )
        notes_port.list_due_reminders.return_value = [note]

        await service.fire_due_reminders(now_utc=_NOW)

        call_args = notes_port.reschedule.call_args
        next_due = call_args[0][1]
        assert call_args[0][0] == _NOTE_ID
        assert next_due > note.due  # must be in the future relative to due


# ---------------------------------------------------------------------------
# Notify failure resilience
# ---------------------------------------------------------------------------

class TestNotifyFailure:

    async def test_deletion_still_called_after_notify_error(self, service, notes_port, notification):
        note = _make_note(recurrence=None)
        notes_port.list_due_reminders.return_value = [note]
        notification.notify.side_effect = RuntimeError("Slack unavailable")

        result, status = await service.fire_due_reminders(now_utc=_NOW)

        # Fired (lifecycle completed) even though notify failed
        assert result["fired"] == 1
        notes_port.delete_note.assert_called_once()

    async def test_reschedule_still_called_after_notify_error(self, service, notes_port, notification):
        note = _make_note(recurrence=ReminderRecurrence(type="daily", interval=1))
        notes_port.list_due_reminders.return_value = [note]
        notification.notify.side_effect = RuntimeError("timeout")

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result["fired"] == 1
        notes_port.reschedule.assert_called_once()


# ---------------------------------------------------------------------------
# Multiple notes
# ---------------------------------------------------------------------------

class TestMultipleNotes:

    async def test_counts_all_fired(self, service, notes_port):
        notes = [
            _make_note(note_id="n1"),
            _make_note(note_id="n2"),
            _make_note(note_id="n3"),
        ]
        notes_port.list_due_reminders.return_value = notes

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result["fired"] == 3
        assert result["skipped"] == 0

    async def test_mix_of_fired_and_skipped(self, service, notes_port):
        notes = [
            _make_note(note_id="n1"),  # will fire
            _make_note(note_id="n2", last_fired=_NOW - timedelta(seconds=60)),  # idempotency skip
        ]
        notes_port.list_due_reminders.return_value = notes

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result["fired"] == 1
        assert result["skipped"] == 1


# ---------------------------------------------------------------------------
# _build_reminder_alert
# ---------------------------------------------------------------------------

class TestBuildReminderAlert:

    def test_contains_note_text(self):
        note = _make_note()
        alert = _build_reminder_alert(note)
        assert "check project status" in alert

    def test_contains_instruction(self):
        note = _make_note()
        alert = _build_reminder_alert(note)
        assert "Look at the project board and identify blockers." in alert

    def test_contains_note_id(self):
        note = _make_note()
        alert = _build_reminder_alert(note)
        assert _NOTE_ID in alert

    def test_one_time_schedule_label(self):
        note = _make_note(recurrence=None)
        alert = _build_reminder_alert(note)
        assert "one-time" in alert

    def test_recurrent_schedule_label(self):
        note = _make_note(recurrence=ReminderRecurrence(type="daily", interval=2))
        alert = _build_reminder_alert(note)
        assert "2 daily" in alert

    def test_self_reminder_framing(self):
        note = _make_note()
        alert = _build_reminder_alert(note)
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
