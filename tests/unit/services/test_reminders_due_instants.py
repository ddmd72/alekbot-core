"""RemindersService.fire_due_reminders across DST transitions (P16-B).

Real `RemindersService`, real `FirestoreAgentNoteAdapter` on `EvalDb` inside `eval_transactions()`, the real
`DateutilRecurrenceAdapter`; the task dispatcher is a recording stand-in and the user repository an in-memory fake
with the user's `config.timezone`. A daily 09:00 reminder is due at the local 09:00 before each transition
(spring and fall, Berlin, Helsinki, New York): the fire enqueues one `execute_reminder` and moves `due` to the next
local 09:00, whose UTC hour differs from the fired one. A second fire at the same `now` enqueues nothing.

The gathered pair sets `yield_on_get = True` on the notes collection (the only collection the claim reads in its
transaction; the due-list query and the user lookup do not yield), so both ticks list the note before either claims.
"""

import asyncio
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pytest

from src.adapters import firestore_agent_note_adapter
from src.adapters.dateutil_recurrence_adapter import DateutilRecurrenceAdapter
from src.adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
from src.domain.agent_note import NoteCreate
from src.domain.user import UserBotConfig, UserProfile
from src.services.reminders_service import RemindersService
from tests.support.clock_instants import ZONES, pin_datetime, transition
from tests.support.firestore_eval import EvalDb, eval_transactions

UTC = timezone.utc
NOTES = "notes"
DAILY_9 = "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"


class RecordingTaskDispatch:
    """Stands in for `TaskDispatchService`: keeps every enqueued worker task."""

    def __init__(self) -> None:
        self.tasks: List[Dict[str, Any]] = []

    async def enqueue_worker_task(self, task_type: str, payload: dict, **kwargs: Any) -> None:
        self.tasks.append({"task_type": task_type, "payload": payload, **kwargs})


class InMemoryUsers:
    def __init__(self, *profiles: UserProfile) -> None:
        self._by_id = {p.user_id: p for p in profiles}

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        return self._by_id.get(user_id)


class World:
    def __init__(self, zone: str) -> None:
        self.db = EvalDb()
        self.notes = FirestoreAgentNoteAdapter(self.db, SimpleNamespace(orchestrator_notes_collection=NOTES))
        self.dispatch = RecordingTaskDispatch()
        users = InMemoryUsers(UserProfile(user_id="u1", account_id="a1", config=UserBotConfig(timezone=zone)))
        self.service = RemindersService(self.notes, users, self.dispatch, DateutilRecurrenceAdapter(),
                                        dispatch_deadline_s=1500)

    def stored_due(self, note_id: str) -> datetime:
        return self.db.collection(NOTES).docs[note_id]["due"].astimezone(UTC)


def _local_nine(zone: str, day) -> datetime:
    return datetime.combine(day, time(9), tzinfo=ZoneInfo(zone)).astimezone(UTC)


def _case(zone: str, kind: str) -> tuple:
    """The local 09:00 on the day before the transition, and the local 09:00 of the transition day."""
    day = transition(zone, kind, "T").utc.astimezone(ZoneInfo(zone)).date()
    return _local_nine(zone, day - timedelta(days=1)), _local_nine(zone, day)


_CASES = [pytest.param(z, k, id=f"{z}-{k}") for z in ZONES for k in ("spring", "fall")]


async def _seed(monkeypatch, w: World, due: datetime) -> str:
    pin_datetime(monkeypatch, firestore_agent_note_adapter, due - timedelta(days=1))
    note = await w.notes.create_note(NoteCreate(user_id="u1", text="water the ferns", instruction="remind to water",
                                                due=due, recurrence=DAILY_9))
    return note.note_id


class TestDailyNineAcrossTransitions:

    @pytest.mark.parametrize("zone, kind", _CASES)
    async def test_the_fire_enqueues_once_and_moves_due_to_the_next_local_nine(self, monkeypatch, zone, kind):
        due, next_due = _case(zone, kind)
        w = World(zone)
        note_id = await _seed(monkeypatch, w, due)
        now = due + timedelta(seconds=30)

        with eval_transactions():
            stats, status = await w.service.fire_due_reminders(now_utc=now)

        assert status == 200 and stats == {"enqueued": 1, "claim_lost": 0, "skipped": 0}
        assert [t["task_type"] for t in w.dispatch.tasks] == ["execute_reminder"]
        assert w.dispatch.tasks[0]["payload"] == {"note_id": note_id, "user_id": "u1", "due_at": due.isoformat()}
        assert w.dispatch.tasks[0]["deadline_seconds"] == 1500
        stored = w.stored_due(note_id)
        assert stored == next_due
        assert stored.astimezone(ZoneInfo(zone)).time() == time(9)
        assert stored.hour != due.hour, "the UTC hour of 09:00 local changes across the transition"
        assert w.db.collection(NOTES).docs[note_id]["last_fired"].astimezone(UTC) == now

    @pytest.mark.parametrize("zone, kind", _CASES)
    async def test_a_second_tick_at_the_same_now_enqueues_nothing(self, monkeypatch, zone, kind):
        """Sequential: after the first claim `due` is in the future, so the second tick does not even list the note
        (claim_lost stays 0; the card's `claim_lost == 1` holds only for the gathered pair below)."""
        due, next_due = _case(zone, kind)
        w = World(zone)
        note_id = await _seed(monkeypatch, w, due)
        now = due + timedelta(seconds=30)

        with eval_transactions():
            await w.service.fire_due_reminders(now_utc=now)
            stats, _ = await w.service.fire_due_reminders(now_utc=now)

        assert stats == {"enqueued": 0, "claim_lost": 0, "skipped": 0}
        assert len(w.dispatch.tasks) == 1
        assert w.stored_due(note_id) == next_due

    @pytest.mark.parametrize("zone, kind", _CASES)
    async def test_two_gathered_ticks_fire_once_and_one_loses_the_claim(self, monkeypatch, zone, kind):
        """Concurrent: both ticks list the note (notes collection yields on get); one commits the reschedule, the
        other's transaction re-reads the moved `due` and returns False → `claim_lost == 1`."""
        due, next_due = _case(zone, kind)
        w = World(zone)
        note_id = await _seed(monkeypatch, w, due)
        w.db.collection(NOTES).yield_on_get = True
        now = due + timedelta(seconds=30)

        with eval_transactions():
            results = await asyncio.gather(w.service.fire_due_reminders(now_utc=now),
                                           w.service.fire_due_reminders(now_utc=now))

        stats = sorted((r[0] for r in results), key=lambda s: s["enqueued"])
        assert stats == [{"enqueued": 0, "claim_lost": 1, "skipped": 0}, {"enqueued": 1, "claim_lost": 0, "skipped": 0}]
        assert len(w.dispatch.tasks) == 1
        assert w.stored_due(note_id) == next_due
