"""Drive-note receipts go out literally (DRIVE_FILE_NOTES_RFC §4.6, §4.8, §4.9; design §5 "Literal receipts", Δ10, Δ11).

The note's words and a file path are text the bot did not write, so the platform shows them and obeys nothing in them.
Both owner paths, on a real `SlackResponseChannel` whose `slack_sdk` client posts to a local HTTP server (the pattern of
`test_literal_receipts_on_the_wire.py`; assertion on the bytes Slack would receive):
  - the chat turn: `ConversationHandler._deliver_drive_note_receipt`;
  - the call and the late receipt: `UserNotificationService.notify_drive_note_receipt`, where a late receipt on a CHAT
    turn goes to the run's origin channel with chat wording, and a call's receipt keeps the call path.
The erase COMMAND is the bot's own text: it is sent as a message (copyable, backtick wrapper kept for the adapters).
Synthetic data only: the bathroom-repair invoice."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.agent import DeliveryItem
from src.domain.drive_note import (
    DRIVE_NOTE_ERASE_DELIVERY, DRIVE_NOTE_RECEIPT_DELIVERY, EraseRequestReceipt, NoteReceipt,
)
from src.domain.language import LanguageCode
from src.domain.notification import NotificationChannel
from src.domain.settings import ConsolidationSettings
from src.handlers.conversation_handler import ConversationHandler
from src.ports.language_service_port import LanguageServicePort
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.services.localization_service import LocalizationService
from src.services.user_notification_service import UserNotificationService
from tests.unit.adapters.slack.test_response_channel_send_literal import _wire_channel

HOSTILE = "<!channel> & <@U1ABC>"
ESCAPED = "&lt;!channel&gt; &amp; &lt;@U1ABC&gt;"
REPAIR = f"the bathroom repair invoice from Reformas Exemplo {HOSTILE}"
RECEIPT = NoteReceipt(action="set", note_id="n1", ref="drive:a", path="Docs/a.pdf", version=1, text=REPAIR)
ERASE = EraseRequestReceipt(scope="one", code="c0ffee", note_count=1, path=f"Docs/{HOSTILE}.pdf", version_count=2)
ITEM = DeliveryItem(type=DRIVE_NOTE_RECEIPT_DELIVERY, data=RECEIPT.delivery_data())
ERASE_ITEM = DeliveryItem(type=DRIVE_NOTE_ERASE_DELIVERY, data=ERASE.delivery_data())
T = datetime(2026, 10, 28, tzinfo=timezone.utc)
USER = "u1"


def _service(channel, *, primary=("slack", "D-PRIMARY")):
    state = AsyncMock(spec=NotificationStatePort)
    state.get_primary.return_value = NotificationChannel(user_id=USER, platform=primary[0], channel_id=primary[1],
                                                         updated_at=T)
    state.get.return_value = None
    factory = MagicMock(spec=NotificationChannelFactoryPort)
    factory.create.return_value = channel
    store = MagicMock()
    store.append_messages_batch = AsyncMock()
    language = AsyncMock(spec=LanguageServicePort)
    language.resolve_ui_language.return_value = LanguageCode.EN
    service = UserNotificationService(
        state_repo=state, channel_factory=factory, coordinator=MagicMock(), notification_sla={}, session_store=store,
        localization=LocalizationService(FileLocalizationAdapter()), language=language)
    return service, factory, store


def _shown_not_obeyed(post: dict) -> None:
    assert "<!channel>" not in post["text"] and "<@U1ABC>" not in post["text"]
    assert ESCAPED in post["text"]
    assert str(post["mrkdwn"]).lower() == "false"


def _handler() -> ConversationHandler:
    return ConversationHandler(coordinator=MagicMock(), agent_factory=MagicMock(), file_service=MagicMock(),
                               global_config=ConsolidationSettings(threshold=50, batch_size=30),
                               localization=LocalizationService(FileLocalizationAdapter()))


async def test_on_a_chat_turn_the_receipt_shows_the_notes_words_and_obeys_none_of_them():
    channel, api, server = await _wire_channel()
    try:
        await _handler()._deliver_drive_note_receipt(ITEM, channel, None)
    finally:
        await server.close()
    assert len(api.posts) == 1 and api.posts[0]["text"].startswith("📝")
    _shown_not_obeyed(api.posts[0])


async def test_on_a_chat_turn_an_erase_request_is_a_literal_line_and_the_command_as_a_message():
    channel, api, server = await _wire_channel()
    try:
        await _handler()._deliver_drive_note_receipt(ERASE_ITEM, channel, None)
    finally:
        await server.close()
    assert len(api.posts) == 2
    _shown_not_obeyed(api.posts[0])               # the line carries the path
    assert "c0ffee" in api.posts[1]["text"] and "<" not in api.posts[1]["text"].replace("<!", "")
    assert str(api.posts[1]["mrkdwn"]).lower() != "false"      # the bot's own command, formatted as ever


async def test_on_a_call_the_receipt_is_literal_and_goes_to_the_primary_channel():
    channel, api, server = await _wire_channel()
    try:
        service, factory, store = _service(channel)
        await service.notify_drive_note_receipt(USER, "a1", ITEM)
    finally:
        await server.close()
    factory.create.assert_called_once_with(platform="slack", channel_id="D-PRIMARY")
    assert len(api.posts) == 1
    _shown_not_obeyed(api.posts[0])
    user = store.append_messages_batch.await_args.kwargs["messages"][0].parts[0].text
    assert user == "[System: drive note saved during a call with Lelik: Docs/a.pdf ref=drive:a v1]"


async def test_on_a_call_an_erase_request_is_a_literal_line_then_the_command_as_a_message():
    channel, api, server = await _wire_channel()
    try:
        service, _, store = _service(channel)
        await service.notify_drive_note_receipt(USER, "a1", ERASE_ITEM)
    finally:
        await server.close()
    assert len(api.posts) == 2
    _shown_not_obeyed(api.posts[0])
    assert "c0ffee" in api.posts[1]["text"]
    history = " ".join(p.text for m in store.append_messages_batch.await_args.kwargs["messages"] for p in m.parts)
    assert "c0ffee" not in history


async def test_a_late_receipt_on_a_chat_turn_goes_to_the_origin_channel_with_chat_wording_and_literally():
    channel, api, server = await _wire_channel()
    try:
        service, factory, store = _service(channel, primary=("telegram", "777"))
        await service.notify_drive_note_receipt(USER, "a1", ITEM, call=False, channel_id_override="C-ORIGIN",
                                                platform_override="slack")
    finally:
        await server.close()
    factory.create.assert_called_once_with(platform="slack", channel_id="C-ORIGIN")      # not the primary
    assert len(api.posts) == 1
    _shown_not_obeyed(api.posts[0])
    kwargs = store.append_messages_batch.await_args.kwargs
    assert kwargs["session_id"] == f"{USER}:C-ORIGIN"
    user, model = kwargs["messages"]
    assert user.parts[0].text == "[System: drive note saved after the answer was sent: Docs/a.pdf ref=drive:a v1]"
    assert "Lelik" not in user.parts[0].text and "call" not in user.parts[0].text
    assert model.parts[0].text == "[drive note receipt posted to the chat: Docs/a.pdf v1]"
    assert "bathroom repair" not in repr(kwargs["messages"])


async def test_a_late_erase_request_on_a_chat_turn_has_chat_wording_and_no_code_in_history():
    channel, api, server = await _wire_channel()
    try:
        service, _, store = _service(channel)
        await service.notify_drive_note_receipt(USER, "a1", ERASE_ITEM, call=False, channel_id_override="C-ORIGIN",
                                                platform_override="slack")
    finally:
        await server.close()
    assert len(api.posts) == 2
    user = store.append_messages_batch.await_args.kwargs["messages"][0].parts[0].text
    assert "Lelik" not in user and "after the answer" in user and "c0ffee" not in user
