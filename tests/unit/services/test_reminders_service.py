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
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.adapters.dateutil_recurrence_adapter import DateutilRecurrenceAdapter
from src.domain.agent_note import AgentNote
from src.ports.agent_note_port import AgentNotePort
from src.services.reminders_service import (
    RemindersService,
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
    recurrence: str = None,
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
    p.claim_one_time_if_due_at.return_value = True
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
        # Real evaluator: rescheduling is what this service exists to do, and a
        # stubbed next-due would assert nothing about the schedule it produces.
        recurrence=DateutilRecurrenceAdapter(),
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
# One-time reminder — claim via claim_one_time_if_due_at (marks fired, no delete)
# ---------------------------------------------------------------------------

class TestOneTimeReminder:

    async def test_successful_claim_enqueues_execute_reminder(
        self, service, notes_port, task_dispatch,
    ):
        note = _make_note(recurrence=None)
        notes_port.list_due_reminders.return_value = [note]
        notes_port.claim_one_time_if_due_at.return_value = True

        result, _ = await service.fire_due_reminders(now_utc=_NOW)

        assert result["enqueued"] == 1
        # One-time claim marks fired (last_fired=now) WITHOUT deleting, so the
        # worker can still read the note's content to build the alert.
        notes_port.claim_one_time_if_due_at.assert_called_once_with(
            note_id=_NOTE_ID,
            user_id=_USER_ID,
            expected_due=note.due,
            last_fired=_NOW,
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
        notes_port.claim_one_time_if_due_at.return_value = False

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
        note = _make_note(recurrence="FREQ=DAILY")
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
        note = _make_note(recurrence="FREQ=DAILY")
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
        # First and third one-time claims succeed; middle one fails.
        notes_port.claim_one_time_if_due_at.side_effect = [True, False, True]

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
        """The rule verbatim, not a phrasing: this alert is read by the orchestrator,
        which needs the exact RRULE to edit the schedule. Human wording is a
        user-facing concern (RecurrencePort.describe)."""
        note = _make_note(recurrence="FREQ=DAILY;INTERVAL=2")
        alert = build_reminder_alert(note)
        assert "FREQ=DAILY;INTERVAL=2" in alert

    def test_self_reminder_framing(self):
        note = _make_note()
        alert = build_reminder_alert(note)
        assert "SELF-REMINDER" in alert
        assert "you wrote" in alert.lower() or "your own" in alert.lower()


# ---------------------------------------------------------------------------
# Schedule arithmetic
# ---------------------------------------------------------------------------
#
# TestComputeNextDue lived here until 2026-07-30, when the schedule algebra moved
# behind RecurrencePort (RRULE). Its cases now live — as RRULE equivalents — in
# tests/unit/adapters/test_dateutil_recurrence_adapter.py. This service is left
# owning the claim/enqueue flow only.


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


# ---------------------------------------------------------------------------
# list_due_reminders timing
#
# 2026-08-13: a fire_due_reminders tick took 81s against Cloud Scheduler's 60s
# attempt deadline (DEADLINE_EXCEEDED / 504) and left nothing behind — no span in
# Logfire, no log line in the gap, no exception. Cloud Run itself answered 200 at
# 80.97s, so the request completed; only the scheduler gave up. The pre-existing
# log line could not localize the stall because `now` is captured BEFORE the query
# and printed after it. Timing the query is what splits "Firestore stalled" from
# "the runtime froze somewhere else".
# ---------------------------------------------------------------------------

class TestListDueRemindersTiming:

    async def test_log_line_reports_the_query_duration(self, service, caplog):
        with caplog.at_level(logging.INFO):
            await service.fire_due_reminders()

        line = next(r for r in caplog.records if "fire_due_reminders:" in r.getMessage())
        assert "list_due_reminders" in line.getMessage()
        assert "ms" in line.getMessage()

    async def test_duration_reflects_a_slow_query(self, service, notes_port, caplog):
        # The whole point: an 80s stall inside the port must be visible in the line.
        async def slow_list(*_args, **_kwargs):
            await asyncio.sleep(0.05)
            return []

        notes_port.list_due_reminders.side_effect = slow_list

        with caplog.at_level(logging.INFO):
            await service.fire_due_reminders()

        line = next(r for r in caplog.records if "fire_due_reminders:" in r.getMessage())
        measured = float(re.search(r"list_due_reminders (\d+)ms", line.getMessage()).group(1))
        assert measured >= 50

    async def test_fast_query_is_reported_as_such(self, service, caplog):
        # The other branch of the fork: a fast query means the time went elsewhere.
        with caplog.at_level(logging.INFO):
            await service.fire_due_reminders()

        line = next(r for r in caplog.records if "fire_due_reminders:" in r.getMessage())
        measured = float(re.search(r"list_due_reminders (\d+)ms", line.getMessage()).group(1))
        assert measured < 50

    async def test_note_count_and_timestamp_still_reported(self, service, notes_port, caplog):
        notes_port.list_due_reminders.return_value = [
            _make_note(note_id="n1"), _make_note(note_id="n2"),
        ]

        with caplog.at_level(logging.INFO):
            await service.fire_due_reminders(now_utc=_NOW)

        msg = next(
            r for r in caplog.records if "fire_due_reminders:" in r.getMessage()
        ).getMessage()
        assert "2 due note(s)" in msg
        assert _NOW.isoformat() in msg


# --------------------------------------------------------------------------- #
# Cloud Tasks dispatch deadline                                                #
#                                                                              #
# Enqueued without one, the per-fire task gets the Cloud Tasks default of 600s.
# A deep_reasoning fire runs at PERFORMANCE with a 1500s budget, so the dispatch
# is cut mid-run and — because a cut dispatch reads as failure — retried whole.
# Three such retries opened Smart's circuit breaker on 2026-08-15.
# --------------------------------------------------------------------------- #

class TestDispatchDeadline:
    async def test_injected_deadline_is_forwarded_to_the_queue(
        self, notes_port, user_repo, task_dispatch,
    ):
        service = RemindersService(
            notes_port=notes_port,
            user_repo=user_repo,
            task_dispatch=task_dispatch,
            recurrence=DateutilRecurrenceAdapter(),
            dispatch_deadline_s=1620,
        )
        notes_port.list_due_reminders.return_value = [_make_note(recurrence="FREQ=DAILY")]
        notes_port.reschedule_if_due_at.return_value = True

        await service.fire_due_reminders(now_utc=_NOW)

        enq_kwargs = task_dispatch.enqueue_worker_task.call_args.kwargs
        assert enq_kwargs["deadline_seconds"] == 1620

    async def test_composition_root_value_exceeds_the_cloud_tasks_default(self):
        """The value main.py injects must actually beat the 600s it exists to fix.

        Services may not import the infrastructure layer (REQ-ARCH-22), so the
        derivation is asserted here against the same function the composition root
        calls.
        """
        from src.domain.notification_kind import NotificationKind
        from src.infrastructure.notification_sla import dispatch_deadline_s

        assert dispatch_deadline_s(NotificationKind.REMINDER) > 600

    async def test_unset_deadline_passes_none(
        self, service, notes_port, task_dispatch,
    ):
        """Default construction sends None — the queue then applies the Cloud Tasks
        default. Documented as test-only; the composition root always injects."""
        notes_port.list_due_reminders.return_value = [_make_note(recurrence="FREQ=DAILY")]
        notes_port.reschedule_if_due_at.return_value = True

        await service.fire_due_reminders(now_utc=_NOW)

        enq_kwargs = task_dispatch.enqueue_worker_task.call_args.kwargs
        assert enq_kwargs["deadline_seconds"] is None
