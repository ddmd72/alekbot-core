"""ConversationHandler posts drive-note deliveries (DRIVE_FILE_NOTES_RFC §4.6, §4.8): the receipt is the shared
renderer's text in the channel's language; an erase request is its line and then the command alone; a failed post is
logged and never raised, and an erase line that failed sends no command."""
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.agent import AgentResponse, DeliveryItem
from src.domain.drive_note import (
    DRIVE_NOTE_ERASE_DELIVERY, DRIVE_NOTE_RECEIPT_DELIVERY, STATUS_OTHER_DRIVE, EraseRequestReceipt, NoteReceipt,
)
from src.domain.drive_note_receipt import render_drive_note_erase, render_drive_note_receipt
from src.domain.messaging import MessageContext, SmartResponse
from src.domain.settings import ConsolidationSettings
from src.handlers.conversation_handler import ConversationHandler
from src.locales import fr
from src.services.localization_service import LocalizationService

RECEIPT = NoteReceipt(action="set", note_id="n1", ref="drive:a", path="Docs/a.pdf", version=2,
                      text="L'estimation de l'entrepreneur {x}", doc_type="contract", status=STATUS_OTHER_DRIVE)
ERASE = EraseRequestReceipt(scope="one", code="c0ffee", note_count=1, path="Docs/a.pdf", version_count=2)


def _fr(message):
    return fr.UI_STRINGS[message.value]


def _handler(response=None):
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()
    session_store.load_session = AsyncMock(return_value=None)
    factory = MagicMock()
    factory.ensure_agents_for_user = AsyncMock()
    factory.get_session_store = MagicMock(return_value=session_store)
    factory.user_repo.get_user = AsyncMock(return_value=None)
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(return_value=response)
    return ConversationHandler(coordinator=coordinator, agent_factory=factory, file_service=MagicMock(),
                               global_config=ConsolidationSettings(threshold=50, batch_size=30),
                               localization=LocalizationService(FileLocalizationAdapter()))


def _channel(language="fr"):
    ch = MagicMock()
    ch.channel_id = "C-001"
    ch.platform = "slack"
    ch.language = language
    for name in ("send_status", "send_message", "send_chunked_message", "update_message", "send_rich_content",
                 "update_status_with_phrase_and_dots", "send_file"):
        setattr(ch, name, AsyncMock())
    ch.send_status_with_phrase = AsyncMock(return_value=("m1", "thinking"))
    ch.get_status_phrase = AsyncMock(return_value="processing")
    ch.max_message_length = 4000
    ch.supports_message_editing = True
    ch.posted = []      # every post in order: (method, text, thread); the receipt and the erase line go out literally

    async def literal(text, to_message_id=None):
        ch.posted.append(("literal", text, to_message_id))

    async def message(text, thread_id=None, link_list=None):
        ch.posted.append(("message", text, thread_id))

    ch.send_literal = AsyncMock(side_effect=literal)
    ch.send_message = AsyncMock(side_effect=message)
    return ch


def _sent(channel):
    return [text for _, text, _ in channel.posted]


async def test_the_receipt_is_the_shared_renderer_text_in_the_channel_language():
    channel = _channel()
    item = DeliveryItem(type=DRIVE_NOTE_RECEIPT_DELIVERY, data=RECEIPT.delivery_data())
    await _handler()._deliver_item(item, channel, "th1", "u")
    assert channel.posted == [("literal", render_drive_note_receipt(RECEIPT.delivery_data(), _fr), "th1")]


async def test_an_erase_request_is_its_line_then_the_command_alone():
    channel = _channel()
    item = DeliveryItem(type=DRIVE_NOTE_ERASE_DELIVERY, data=ERASE.delivery_data())
    await _handler()._deliver_item(item, channel, None, "u")
    line, command = render_drive_note_erase(ERASE.delivery_data(), _fr)
    assert channel.posted == [("literal", line, None), ("message", command, None)]
    assert command == "`$drive-notes erase c0ffee`"


async def test_a_failed_post_is_logged_and_the_command_is_not_sent():
    channel = _channel()
    channel.send_literal = AsyncMock(side_effect=RuntimeError("slack down"))
    item = DeliveryItem(type=DRIVE_NOTE_ERASE_DELIVERY, data=ERASE.delivery_data())
    with patch("src.handlers.conversation_handler.logger") as log:
        await _handler()._deliver_item(item, channel, None, "u")
    assert channel.send_literal.await_count == 1 and channel.send_message.await_count == 0   # the line failed: no command
    assert any("drive_note_erase" in str(c.args) for c in log.error.call_args_list)


async def test_a_turn_with_a_receipt_posts_it_after_the_answer():
    response = AgentResponse.success(task_id="t", agent_id="smart", result=SmartResponse(text="Noted."),
                                     delivery_items=[DeliveryItem(type=DRIVE_NOTE_RECEIPT_DELIVERY,
                                                                  data=RECEIPT.delivery_data())])
    handler = _handler(response)
    channel = _channel()
    context = MessageContext(text="c'est l'estimation de l'entrepreneur", session_id="s", user_id="u", account_id="a",
                             attachments=[], metadata={})
    with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
        await handler.handle_message(context, channel)
    receipt = render_drive_note_receipt(RECEIPT.delivery_data(), _fr)
    assert receipt in _sent(channel) and ("literal", receipt) in [(m, t) for m, t, _ in channel.posted]
