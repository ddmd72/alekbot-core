"""A reminder's run reaches the queue (EXTERNAL_TASK_CONFIRMATION_AND_ALERTS; EXTERNAL_TASK_QUEUE_RFC §5.3).

The real stack end to end: `ExecuteReminderTask` over the real note adapter, the real `UserNotificationService`, the real
coordinator and registry, a real Smart and the lazily built `ExternalTaskAgent` over the real `ExternalTaskService`
(the chain of `test_external_task_specialist_chain.py`); Smart and the specialist are scripted models. The task is queued
at once, with no receipt and no command, and its origin is the channel the reminder spoke in. Alex Doe is `usr-1`.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.adapters import firestore_agent_note_adapter
from src.adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
from src.config.environment import EnvironmentConfig
from src.domain.agent_note import NoteCreate
from src.domain.external_task import ExternalTaskStatus
from src.domain.external_task_entry import REMINDER_RUN_ORIGIN
from src.domain.external_task_texts import NOTE_QUEUED
from src.domain.notification import NotificationChannel
from src.domain.user import UserBotConfig
from src.infrastructure.notification_sla import NOTIFICATION_SLA
from src.services.background_empty_answer_reporter import BackgroundEmptyAnswerReporter
from src.services.execute_reminder_task import ExecuteReminderTask
from src.services.inactive_user_check import InactiveUserCheck
from src.services.user_notification_service import UserNotificationService
from tests.support.clock_instants import pin_datetime
from tests.support.external_task_world import ORIGIN, TEXT, TITLE, USER_ID, ExternalTaskWorld
from tests.support.firestore_eval import EvalDb
from tests.support.scripted_llm import ScriptedLLM, text, tool_results, tools
from tests.support.tool_e2e import smart_rig
from tests.unit.infrastructure.test_external_task_specialist_chain import CREATE, HAND_OFF, _chain, _sessions
from tests.unit.services.test_execute_reminder_task import DELIVERED, Rig
from tests.support.redelivery_world import T0

S = ExternalTaskStatus


class Channel:
    def __init__(self) -> None:
        self.posts = []

    async def send_long_text(self, text: str, link_list=None) -> None:
        self.posts.append(text)


class Sessions:
    def __init__(self) -> None:
        self.appended = []

    async def append_messages_batch(self, *, session_id: str, owner_id: str, messages: list) -> None:
        self.appended.append((session_id, messages))


async def test_the_run_origin_constant_is_the_one_the_reminder_runner_stamps(monkeypatch):
    rig = Rig(DELIVERED)
    note_id = await rig.seed(monkeypatch)
    await rig.run(note_id)

    [call] = rig.notifier.calls
    assert REMINDER_RUN_ORIGIN == f"background:{call['origin_detail']}"      # notify stamps `background:<origin_detail>`


async def test_a_reminder_fire_hands_a_task_to_the_queue_with_no_confirmation_and_no_receipt(monkeypatch):
    world = await ExternalTaskWorld().seed()
    notes = FirestoreAgentNoteAdapter(EvalDb(), EnvironmentConfig())
    pin_datetime(monkeypatch, firestore_agent_note_adapter, T0)
    note = await notes.create_note(NoteCreate(
        user_id=USER_ID, text="irrigation comparison", instruction=f"Hand this to research: {TEXT}", due=T0))

    smart_llm = ScriptedLLM([tools(HAND_OFF), text("The irrigation comparison went to research.")])
    specialist_llm = ScriptedLLM([tools(CREATE), text("Queued.")])
    coordinator = _chain(world, specialist_llm)
    smart = smart_rig(smart_llm, user_id=USER_ID, coordinator=coordinator, session_store=SimpleNamespace(
        load_session=_sessions([])), user_config=UserBotConfig(external_tasks_enabled=True))
    coordinator.register_agent(smart)
    state_repo = AsyncMock()
    state_repo.get_primary.return_value = NotificationChannel(
        user_id=USER_ID, platform=ORIGIN.platform, channel_id=ORIGIN.channel_id, updated_at=datetime.now(timezone.utc))
    channel, sessions, factory = Channel(), Sessions(), MagicMock()
    factory.create.return_value = channel
    notifications = UserNotificationService(
        state_repo=state_repo, channel_factory=factory, coordinator=coordinator, notification_sla=NOTIFICATION_SLA,
        session_store=sessions, inactive_users=InactiveUserCheck(world.users))
    runner = ExecuteReminderTask(
        notes_port=notes, user_repo=world.users, ensure_agents=AsyncMock(), notification_service=notifications,
        empty_answer_reporter=BackgroundEmptyAnswerReporter(alert_sink=None))

    answer = await runner({"task_type": "execute_reminder", "note_id": note.note_id, "user_id": USER_ID,
                           "due_at": T0.isoformat()}, {})

    assert answer[1] == 200
    for llm in (smart_llm, specialist_llm):
        assert llm.unanswered == 0 and llm.pending() == 0
    [task] = world.tasks.tasks.values()
    assert task.status is S.QUEUED and task.hold is None and task.text == TEXT and task.title == TITLE
    assert task.origin.platform == ORIGIN.platform and task.origin.channel_id == ORIGIN.channel_id   # where it spoke
    [(_, created)] = tool_results(specialist_llm)
    assert created == NOTE_QUEUED.format(task_id=task.task_id, worker="research", title=TITLE)
    assert channel.posts == ["The irrigation comparison went to research."]                            # text only
    assert [j["kind"] for j in world.jobs("external_task_timer")] == ["deadline"]
