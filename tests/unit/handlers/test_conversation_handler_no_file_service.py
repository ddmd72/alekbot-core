"""Upload branch without a file service: the attachment slot carries a note, nothing is parsed in the handler, and the
failure is visible as an ERROR line (USER_FILE_PARSING_RFC §4.2, owner rule: graceful degradation + a visible signal)."""
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from src.domain.agent import AgentResponse
from src.domain.messaging import FileAttachment, MessageContext, SmartResponse
from src.domain.settings import ConsolidationSettings
from src.handlers.conversation_handler import ConversationHandler
from src.utils.file_conversion import storage_unavailable_alert


def _channel(path):
    ch = MagicMock()
    ch.channel_id = "C-001"
    ch.platform = "slack"
    ch.send_status_with_phrase = AsyncMock(return_value=("m1", "thinking"))
    for name in ("send_status", "send_message", "send_chunked_message", "update_message",
                 "send_rich_content", "update_status_with_phrase_and_dots"):
        setattr(ch, name, AsyncMock())
    ch.get_status_phrase = AsyncMock(return_value="processing")
    ch.download_file = AsyncMock(return_value=path)
    ch.max_message_length = 4000
    ch.supports_message_editing = True
    return ch


def _handler(file_conversion_service=None):
    coord = MagicMock()
    coord.route_message = AsyncMock(return_value=AgentResponse.success(
        task_id="t", agent_id="smart_response_agent_ann", result=SmartResponse(text="ok")))
    store = MagicMock()
    store.append_messages_batch = AsyncMock()
    store.load_session = AsyncMock(return_value=None)
    store.save_session = AsyncMock()
    factory = MagicMock()
    factory.ensure_agents_for_user = AsyncMock()
    factory.get_session_store = MagicMock(return_value=store)
    factory.invalidate_prompt_cache = MagicMock()
    factory.user_repo = MagicMock()
    factory.user_repo.get_user = AsyncMock(return_value=None)
    return ConversationHandler(
        coordinator=coord, agent_factory=factory, file_service=MagicMock(),
        global_config=ConsolidationSettings(threshold=50, batch_size=30),
        file_conversion_service=file_conversion_service,
    ), coord


def _ctx():
    att = FileAttachment(url="http://example.com/f", mime_type="application/pdf", filename="invoice_painting_es.pdf")
    return MessageContext(text="see this", session_id="s", user_id="ann", account_id="acc-ann", attachments=[att])


def _sent_texts(coord):
    message = coord.route_message.call_args[0][0]
    return repr(message)


async def test_without_a_file_service_the_slot_carries_the_note_and_nothing_is_parsed(tmp_path, caplog):
    path = tmp_path / "upload.tmp"
    path.write_bytes(b"%PDF-1.4")
    handler, coord = _handler()
    with patch("src.utils.file_conversion.convert_file_to_text", new_callable=AsyncMock) as converter, \
            caplog.at_level(logging.ERROR):
        await handler.handle_message(_ctx(), _channel(str(path)))
    converter.assert_not_awaited()
    sent = _sent_texts(coord)
    assert "invoice_painting_es.pdf" in sent and "cannot be stored or read" in sent
    assert any(r.levelno >= logging.ERROR and "No file service" in r.getMessage() for r in caplog.records)


async def test_with_a_file_service_a_ref_is_stored_as_before(tmp_path, caplog):
    from src.domain.llm import MessagePart
    path = tmp_path / "upload.tmp"
    path.write_bytes(b"x")
    service = MagicMock()
    service.process_attachment = AsyncMock(return_value=MessagePart(text="REF-invoice_painting_es"))
    handler, coord = _handler(service)
    with caplog.at_level(logging.ERROR):
        await handler.handle_message(_ctx(), _channel(str(path)))
    service.process_attachment.assert_awaited_once()
    sent = _sent_texts(coord)
    assert "REF-invoice_painting_es" in sent and "cannot be stored" not in sent
    assert not any("No file service" in r.getMessage() for r in caplog.records)


def test_the_note_names_the_file_and_forbids_guessing():
    note = storage_unavailable_alert("gym_pass_uk.md")
    assert "gym_pass_uk.md" in note and "Do not guess" in note and "try sending it again later" in note
