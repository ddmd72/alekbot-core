"""Three older `notify_raw` callers send literally (T12d group C, owner rule): the reminder receipts (the reminder's words),
the drive receipts (a path) and the deep research copy line (the report's drive path, which holds the model's title).
Each carries text the bot did not write, so the platform shows it and obeys nothing in it.

Real `UserNotificationService` over a real `SlackResponseChannel`, whose `slack_sdk` client posts to a local HTTP server:
the assertion is on the bytes the Slack Web API would receive."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from src.adapters.slack.response_channel import SlackResponseChannel
from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentStatus
from src.domain.agent_note import AgentNote
from src.domain.deep_research_drive_copy import DEEP_RESEARCH_REPORT_CONTEXT_KEY
from src.domain.language import LanguageCode
from src.domain.notification import NotificationChannel
from src.domain.user_drive import DeleteOutcome, DriveItem
from src.infrastructure.agent_manifest import Intent
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.ports.file_storage_port import FileStoragePort
from src.services.deep_research_drive_copy_service import DeepResearchDriveCopyService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from src.services.user_notification_service import UserNotificationService
from tests.unit.adapters.slack.test_response_channel_send_literal import _wire_channel
from tests.unit.agents.test_file_management_agent_drive import _msg
from tests.unit.agents.test_notes_agent_workspace_file import CREATE, DUE, NOTE_ID, UPDATE, USER, _agent, _message, _tools, _text
from tests.unit.handlers.test_agent_worker_handler_deep_research_drive_copy import (
    _handler, _html_item, _html_payload, _success,
)

HOSTILE = "<!channel> & <@U1ABC>"
ESCAPED = "&lt;!channel&gt; &amp; &lt;@U1ABC&gt;"


def _notifications(channel: SlackResponseChannel) -> UserNotificationService:
    state = AsyncMock(spec=NotificationStatePort)
    state.get_primary.return_value = None
    state.get.return_value = NotificationChannel(user_id=USER, platform="slack", channel_id="D1",
                                                 updated_at=datetime(2026, 1, 1))
    factory = MagicMock(spec=NotificationChannelFactoryPort)
    factory.create.return_value = channel
    return UserNotificationService(state_repo=state, channel_factory=factory, coordinator=MagicMock(), notification_sla={})


def _shown_not_obeyed(post: dict) -> None:
    assert "<!channel>" not in post["text"] and "<@U1ABC>" not in post["text"]
    assert ESCAPED in post["text"]
    assert str(post["mrkdwn"]).lower() == "false"


async def test_a_reminder_receipt_shows_the_reminders_words_and_obeys_none_of_them():
    channel, api, server = await _wire_channel()
    try:
        agent, port, _ = _agent()
        agent._notification_service = _notifications(channel)
        for call, method in ((CREATE, port.create_note), (UPDATE, port.update_note)):
            method.return_value = AgentNote(note_id=NOTE_ID, user_id=USER, text=f"Tell {HOSTILE}", instruction="x",
                                            created_at=datetime(2030, 3, 9, tzinfo=timezone.utc), due=DUE)
            agent._call_llm = AsyncMock(side_effect=[_tools(call), _text()])
            assert (await agent.execute(_message())).status == AgentStatus.SUCCESS
    finally:
        await server.close()

    assert len(api.posts) == 2
    assert api.posts[0]["text"].startswith("📌 Reminder set") and api.posts[1]["text"].startswith("📝 Reminder updated")
    for post in api.posts:
        _shown_not_obeyed(post)


async def test_a_drive_receipt_shows_the_path_and_obeys_none_of_it():
    channel, api, server = await _wire_channel()
    try:
        localization = MagicMock()
        localization.get_ui_string.side_effect = lambda lang, m: "🗑️ Deleted from the drive: {path}"
        language = AsyncMock()
        language.resolve_ui_language.return_value = LanguageCode.EN
        drive = AsyncMock(spec=UserDriveService)
        item = DriveItem(item_id="f1", name="x.pdf", path=f"Inbox/{HOSTILE}.pdf", is_folder=False)
        drive.delete.return_value = DeleteOutcome(item=item, file_count=1)
        agent = FileManagementAgent(
            config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management",
                               capabilities={}, metadata={}),
            conversion_service=AsyncMock(spec=FileConversionService), storage=AsyncMock(spec=FileStoragePort),
            notification=_notifications(channel), drive_service=drive, localization=localization,
            language_service=language,
        )
        resp = await agent.execute(_msg(Intent.DELETE_FILE_FROM_DRIVE, file_ref="drive:f1"))
    finally:
        await server.close()

    assert resp.status == AgentStatus.SUCCESS
    assert len(api.posts) == 1 and api.posts[0]["text"].startswith("🗑️ Deleted from the drive: Inbox/")
    _shown_not_obeyed(api.posts[0])


async def test_the_deep_research_copy_line_shows_the_path_and_obeys_none_of_it():
    channel, api, server = await _wire_channel()
    try:
        copy = AsyncMock(spec=DeepResearchDriveCopyService)
        copy.copy_report.return_value = f"📁 Copied to your drive: Deep research/2026-10-14 {HOSTILE}.html"
        handler, coordinator, _, _ = _handler(copy=copy)
        handler._notification = _notifications(channel)
        handler._notification.notify_document_link = AsyncMock()
        coordinator.route_message.return_value = _success([_html_item()])
        result = await handler.handle_task(_html_payload(**{DEEP_RESEARCH_REPORT_CONTEXT_KEY: "q"}))
    finally:
        await server.close()

    assert result["status"] == "success"
    assert len(api.posts) == 1 and api.posts[0]["text"].startswith("📁 Copied to your drive: Deep research/")
    _shown_not_obeyed(api.posts[0])
