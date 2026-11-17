"""ExecuteReminderTask: one claimed reminder fire, over the real note adapter on the H1 fake (composition-root refactor S5b).

Every branch of `__call__`, asserting the answer, the stored note and what reached the notifier and the ops sink.
"""
import inspect
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional

import pytest

from src.adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
from src.config.environment import EnvironmentConfig
from src.domain.agent import AgentStatus
from src.domain.agent_note import NoteCreate
from src.domain.complexity_settings import ComplexitySettings
from src.domain.notification_kind import NotificationKind
from src.domain.notify_result import NotifyResult, empty_answer_alert
from src.domain.reminder_alert import build_reminder_alert, build_reminder_alert_summary
from src.domain.task_complexity import TaskComplexity
from src.domain.user import PerformanceTier, UserBotConfig, UserProfile
from src.domain.worker_task import WorkerTask
from src.services.execute_reminder_task import ExecuteReminderTask
from src.adapters import firestore_agent_note_adapter
from tests.support.clock_instants import pin_datetime
from tests.support.firestore_eval import EvalDb
from tests.support.redelivery_world import ACCOUNT, T0, USER, AgentFactoryFake, UserDirectory

DELIVERED = NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS, sent=True)
EMPTY = NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error="empty_answer")


@dataclass
class Notifier:
    """`notify` records its keywords and the order against `ensure_agents`, and answers the scripted result."""
    result: NotifyResult
    order: List[str]
    calls: List[Dict[str, Any]] = field(default_factory=list)

    async def notify(self, **kwargs: Any) -> NotifyResult:
        self.order.append("notify")
        self.calls.append(kwargs)
        return self.result


class Sink:
    def __init__(self, fails: bool = False) -> None:
        self.posts: List[str] = []
        self._fails = fails

    async def post(self, text: str) -> None:
        if self._fails:
            raise RuntimeError("webhook down")
        self.posts.append(text)


class Rig:
    def __init__(self, result: NotifyResult = DELIVERED, sink: Optional[Sink] = None) -> None:
        self.db = EvalDb()
        self.env = EnvironmentConfig()
        self.notes = FirestoreAgentNoteAdapter(self.db, self.env)
        self.users = UserDirectory()
        self.order: List[str] = []
        self.factory = AgentFactoryFake()
        self.notifier = Notifier(result, self.order)
        self.sink = sink
        self.task = ExecuteReminderTask(
            notes_port=self.notes, user_repo=self.users, ensure_agents=self._ensure,
            notification_service=self.notifier, alert_sink=self.sink)

    async def _ensure(self, user_id: str) -> None:
        self.order.append("ensure")
        await self.factory.ensure_agents_for_user(user_id)

    async def seed(self, monkeypatch: Any, *, recurrence: Optional[str] = None,
                   complexity: Optional[TaskComplexity] = None, delivered: bool = False) -> str:
        pin_datetime(monkeypatch, firestore_agent_note_adapter, T0 - timedelta(days=1))
        note = await self.notes.create_note(NoteCreate(
            user_id=USER, text="water the plants", instruction="Remind the user to water the plants.",
            due=T0, recurrence=recurrence, complexity=complexity))
        if delivered:
            await self.notes.mark_fire_delivered(note_id=note.note_id, due_at=T0)
        return note.note_id

    def doc(self, note_id: str) -> Optional[dict]:
        docs = self.db.collection(self.env.orchestrator_notes_collection).docs
        return dict(docs[note_id]) if note_id in docs else None

    async def run(self, note_id: str, **overrides: Any):
        payload = {"task_type": "execute_reminder", "note_id": note_id, "user_id": USER, "due_at": T0.isoformat()}
        payload.update(overrides)
        return await self.task(payload, {})


def test_the_runner_is_called_as_a_worker_task():
    protocol = list(inspect.signature(WorkerTask.__call__).parameters)
    assert list(inspect.signature(ExecuteReminderTask.__call__).parameters) == protocol
    assert inspect.iscoroutinefunction(ExecuteReminderTask.__call__)


@pytest.mark.parametrize("missing", ["note_id", "user_id", "due_at"])
async def test_a_missing_field_is_a_400_and_nothing_runs(missing):
    rig = Rig()
    payload = {"note_id": "n1", "user_id": USER, "due_at": T0.isoformat()}
    payload[missing] = ""

    assert await rig.task(payload, {}) == ({"error": "missing note_id, user_id, or due_at"}, 400)
    assert rig.order == []


async def test_a_bad_due_at_is_a_400(monkeypatch, caplog):
    rig = Rig()
    note_id = await rig.seed(monkeypatch)
    with caplog.at_level(logging.WARNING):
        answer = await rig.run(note_id, due_at="not-a-date")

    assert answer == ({"error": "bad due_at"}, 400)
    assert f"[Worker] execute_reminder: bad due_at 'not-a-date' for note={note_id}" in caplog.messages
    assert rig.order == [] and rig.doc(note_id).get("last_delivered_due") is None


async def test_a_note_that_is_gone_is_acked(caplog):
    rig = Rig()
    with caplog.at_level(logging.INFO):
        assert await rig.run("n-missing") == ({"status": "note_gone"}, 200)
    assert f"[Worker] execute_reminder: note_gone note=n-missing user={USER[:8]}" in caplog.messages
    assert rig.order == []


async def test_another_users_note_is_gone_for_this_user(monkeypatch):
    rig = Rig()
    note_id = await rig.seed(monkeypatch)

    assert await rig.run(note_id, user_id="u2") == ({"status": "note_gone"}, 200)
    assert rig.doc(note_id) is not None and rig.order == []


async def test_an_already_delivered_one_time_fire_is_cleaned_up_without_a_second_run(monkeypatch, caplog):
    rig = Rig()
    note_id = await rig.seed(monkeypatch, delivered=True)
    with caplog.at_level(logging.INFO):
        assert await rig.run(note_id) == ({"status": "already_delivered"}, 200)

    assert rig.doc(note_id) is None
    assert rig.order == []
    assert f"[Worker] execute_reminder: already_delivered note={note_id} due={T0.isoformat()}" in caplog.messages


async def test_an_already_delivered_recurring_fire_keeps_its_note(monkeypatch):
    rig = Rig()
    note_id = await rig.seed(monkeypatch, recurrence="FREQ=DAILY", delivered=True)

    assert await rig.run(note_id) == ({"status": "already_delivered"}, 200)
    assert rig.doc(note_id).get("last_delivered_due") == T0
    assert rig.order == []


async def test_a_fire_of_another_due_is_not_already_delivered(monkeypatch):
    rig = Rig()
    note_id = await rig.seed(monkeypatch, recurrence="FREQ=DAILY", delivered=True)
    later = T0 + timedelta(days=1)

    assert await rig.run(note_id, due_at=later.isoformat()) == ({"status": "ok"}, 200)
    assert rig.doc(note_id).get("last_delivered_due") == later


@pytest.mark.parametrize("profile", [None, UserProfile(user_id=USER, account_id="")], ids=["no-profile", "no-account"])
async def test_a_user_without_an_account_is_acked_without_a_run(monkeypatch, caplog, profile):
    rig = Rig()
    rig.users = UserDirectory()
    rig.users._profiles.pop(USER)
    if profile is not None:
        rig.users.put(profile)
    rig.task = ExecuteReminderTask(notes_port=rig.notes, user_repo=rig.users, ensure_agents=rig._ensure,
                                   notification_service=rig.notifier, alert_sink=None)
    note_id = await rig.seed(monkeypatch)
    with caplog.at_level(logging.WARNING):
        assert await rig.run(note_id) == ({"status": "no_user"}, 200)

    assert rig.order == [] and rig.doc(note_id).get("last_delivered_due") is None
    assert f"[Worker] execute_reminder: no_user user={USER[:8]} note={note_id}" in caplog.messages


async def test_a_delivered_one_time_fire_runs_the_users_agents_first_stamps_and_removes_the_note(monkeypatch, caplog):
    rig = Rig()
    note_id = await rig.seed(monkeypatch)
    note = await rig.notes.get_note(user_id=USER, note_id=note_id)
    with caplog.at_level(logging.INFO):
        assert await rig.run(note_id) == ({"status": "ok"}, 200)

    assert rig.order == ["ensure", "notify"] and rig.factory.ensured == [USER]
    assert rig.notifier.calls == [{
        "user_id": USER, "account_id": ACCOUNT,
        "system_alert": build_reminder_alert(note), "system_alert_summary": build_reminder_alert_summary(note),
        "kind": NotificationKind.REMINDER, "tier": None, "agent_id_override": f"smart_response_agent_{USER}",
        "task_complexity": "simple_analytics", "suppress_transient_retry": True,
        "origin_detail": "execute_reminder", "fire_key": f"reminder:{note_id}@{T0.isoformat()}",
    }]
    assert rig.doc(note_id) is None
    assert f"[Worker] execute_reminder: ok note={note_id} user={USER[:8]} due={T0.isoformat()}" in caplog.messages


async def test_a_delivered_recurring_fire_is_stamped_and_kept(monkeypatch):
    rig = Rig()
    note_id = await rig.seed(monkeypatch, recurrence="FREQ=DAILY")

    assert await rig.run(note_id) == ({"status": "ok"}, 200)
    assert rig.doc(note_id).get("last_delivered_due") == T0


async def test_the_sla_tier_follows_the_users_override_of_the_notes_complexity(monkeypatch):
    rig = Rig()
    rig.users.put(UserProfile(user_id=USER, account_id=ACCOUNT, config=UserBotConfig(complexity_settings_overrides={
        TaskComplexity.SIMPLE_ANALYTICS: ComplexitySettings(tier=PerformanceTier.PERFORMANCE)})))
    note_id = await rig.seed(monkeypatch, complexity=TaskComplexity.SIMPLE_ANALYTICS)

    await rig.run(note_id)

    assert (rig.notifier.calls[0]["tier"], rig.notifier.calls[0]["task_complexity"]) == (
        PerformanceTier.PERFORMANCE, "simple_analytics")


async def test_a_complexity_with_no_settings_leaves_the_tier_to_the_kind(monkeypatch):
    rig = Rig()
    note_id = await rig.seed(monkeypatch, complexity=TaskComplexity.DEEP_REASONING)
    monkeypatch.setattr("src.services.execute_reminder_task.resolve_complexity_settings", lambda complexity, config: None)

    await rig.run(note_id)

    assert (rig.notifier.calls[0]["tier"], rig.notifier.calls[0]["task_complexity"]) == (None, "deep_reasoning")


@pytest.mark.parametrize("error,body_error", [("agent_timeout", "agent_timeout"), (None, "delivery_failed")])
async def test_a_failed_delivery_is_a_500_and_leaves_the_note_for_the_retry(monkeypatch, caplog, error, body_error):
    rig = Rig(NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error=error), sink=Sink())
    note_id = await rig.seed(monkeypatch)
    with caplog.at_level(logging.WARNING):
        answer = await rig.run(note_id)

    assert answer == ({"error": body_error, "agent_status": AgentStatus.FAILED.value}, 500)
    assert rig.doc(note_id).get("last_delivered_due") is None and rig.sink.posts == []
    assert (f"[Worker] execute_reminder: delivery_failed note={note_id} user={USER[:8]} "
            f"(agent_status={AgentStatus.FAILED}, error={error})") in caplog.messages


async def test_an_empty_answer_is_final_one_error_line_one_alert_and_the_note_is_done(monkeypatch, caplog):
    rig = Rig(EMPTY, sink=Sink())
    note_id = await rig.seed(monkeypatch)
    with caplog.at_level(logging.INFO):
        assert await rig.run(note_id) == ({"status": "empty_answer_final"}, 200)

    records = [r for r in caplog.records if getattr(r, "event", None) == "background_empty_answer"]
    assert [(r.levelno, r.getMessage(), r.path, r.note_id, r.user_id) for r in records] == [
        (logging.ERROR, f"[Worker] background_empty_answer path=execute_reminder note={note_id} user={USER[:8]}",
         "execute_reminder", note_id, USER[:8])]
    assert rig.sink.posts == [empty_answer_alert("execute_reminder", USER, note_id)]
    assert rig.doc(note_id) is None
    assert f"[Worker] execute_reminder: empty_answer_final note={note_id} user={USER[:8]} due={T0.isoformat()}" \
        in caplog.messages


async def test_an_empty_answer_with_no_sink_still_finishes(monkeypatch):
    rig = Rig(EMPTY, sink=None)
    note_id = await rig.seed(monkeypatch, recurrence="FREQ=DAILY")

    assert await rig.run(note_id) == ({"status": "empty_answer_final"}, 200)
    assert rig.doc(note_id).get("last_delivered_due") == T0


async def test_a_failing_sink_does_not_break_the_final_path(monkeypatch, caplog):
    rig = Rig(EMPTY, sink=Sink(fails=True))
    note_id = await rig.seed(monkeypatch)
    with caplog.at_level(logging.WARNING):
        assert await rig.run(note_id) == ({"status": "empty_answer_final"}, 200)

    assert "[Worker] background_empty_answer alert failed: webhook down" in caplog.messages
    assert rig.doc(note_id) is None
