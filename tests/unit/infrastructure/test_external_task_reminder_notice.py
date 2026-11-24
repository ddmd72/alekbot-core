"""A task a reminder's run hands over is shown to the owner at once (owner's decision 2026-11-24): after the run's answer,
in the reminder's channel, the receipt file (front matter with the note, then the brief) and a short literal message
that it was handed over from a reminder; no command, no button, nothing to confirm. The task itself is queued at once,
as before.

The real stack of `test_external_task_reminder_chain.py`: `ExecuteReminderTask` over the real note adapter, the real
`UserNotificationService` with the real localization, the real coordinator and registry, a real Smart and the lazily
built `ExternalTaskAgent` over the real `ExternalTaskService`; Smart and the specialist are scripted models. The channel
records every post in order. Alex Doe is `usr-1`.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import yaml

from src.adapters import firestore_agent_note_adapter
from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
from src.config.environment import EnvironmentConfig
from src.domain.agent_note import NoteCreate
from src.domain.external_task import ExternalTaskStatus
from src.domain.external_task_entry import TaskEntry
from src.domain.notification import NotificationChannel
from src.domain.ui_messages import UIMessage
from src.domain.user import UserBotConfig
from src.infrastructure.notification_sla import NOTIFICATION_SLA
from src.locales import en
from src.services.background_empty_answer_reporter import BackgroundEmptyAnswerReporter
from src.services.execute_reminder_task import ExecuteReminderTask
from src.services.inactive_user_check import InactiveUserCheck
from src.services.localization_service import LocalizationService
from src.services.user_notification_service import UserNotificationService
from tests.support.clock_instants import pin_datetime
from tests.support.external_task_world import NOTE, ORIGIN, TEXT, TITLE, USER_ID, ExternalTaskWorld
from tests.support.firestore_eval import EvalDb
from tests.support.redelivery_world import T0
from tests.support.scripted_llm import ScriptedLLM, text, tools
from tests.support.tool_e2e import smart_rig
from tests.unit.infrastructure.test_external_task_specialist_chain import CREATE, HAND_OFF, _chain, _sessions

ANSWER = "The irrigation comparison went to research."


class Channel:
    """Records every post in order: the answer text, files, literal messages and plain messages."""

    def __init__(self) -> None:
        self.posts = []

    async def send_long_text(self, text: str, link_list=None) -> None:
        self.posts.append(("text", text))

    async def send_file(self, content: bytes, filename: str, title: str, thread_id=None) -> None:
        self.posts.append(("file", filename, title, content))

    async def send_literal(self, text: str, thread_id=None) -> None:
        self.posts.append(("literal", text))

    async def send_message(self, text: str, thread_id=None) -> None:
        self.posts.append(("message", text))


class Sessions:
    def __init__(self) -> None:
        self.appended = []

    async def append_messages_batch(self, *, session_id: str, owner_id: str, messages: list) -> None:
        self.appended.append((session_id, messages))


async def test_a_reminder_runs_task_is_shown_after_the_answer_as_a_notice_with_no_command(monkeypatch):
    world = await ExternalTaskWorld().seed()
    notes = FirestoreAgentNoteAdapter(EvalDb(), EnvironmentConfig())
    pin_datetime(monkeypatch, firestore_agent_note_adapter, T0)
    note = await notes.create_note(NoteCreate(
        user_id=USER_ID, text="irrigation comparison", instruction=f"Hand this to research: {TEXT}", due=T0))

    smart_llm = ScriptedLLM([tools(HAND_OFF), text(ANSWER)])
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
        session_store=sessions, localization=LocalizationService(FileLocalizationAdapter()),
        inactive_users=InactiveUserCheck(world.users))
    runner = ExecuteReminderTask(
        notes_port=notes, user_repo=world.users, ensure_agents=AsyncMock(), notification_service=notifications,
        empty_answer_reporter=BackgroundEmptyAnswerReporter(alert_sink=None))

    answer = await runner({"task_type": "execute_reminder", "note_id": note.note_id, "user_id": USER_ID,
                           "due_at": T0.isoformat()}, {})

    assert answer[1] == 200
    [task] = world.tasks.tasks.values()
    assert task.status is ExternalTaskStatus.QUEUED and (task.note, task.entry) == (NOTE, TaskEntry.REMINDER)
    factory.create.assert_called_with(platform=ORIGIN.platform, channel_id=ORIGIN.channel_id)   # the reminder's channel

    header = en.UI_STRINGS[UIMessage.EXTERNAL_TASK_QUEUED_FROM_REMINDER.value].format(worker="research", title=TITLE)
    [(said, answer_text), (filed, filename, title, content), (lit, message)] = channel.posts
    assert (said, answer_text) == ("text", ANSWER)                                     # the answer first
    assert (filed, filename, title) == ("file", f"task-{task.task_id}.txt", TITLE)
    assert content.startswith(b"---\n")
    head, _, body = content.decode("utf-8")[len("---\n"):].partition("\n---\n")
    assert yaml.safe_load(head) == {"title": TITLE, "worker": "research", "note": NOTE} and body == TEXT
    assert (lit, message) == ("literal", f"{header}\nTask id: `{task.task_id}`")
    assert "$task send" not in message                                                 # nothing to confirm, no button

    [(_, history)] = sessions.appended
    assert [m.role for m in history] == ["user", "model"]                              # the alert and the answer only
    assert all(NOTE not in (part.text or "") + (part.full_text or "") for m in history for part in m.parts)
