"""An attachment whose upload raises (storage down): its slot carries the storage note, the turn goes on, ERROR line."""
import logging
from unittest.mock import AsyncMock, MagicMock

from tests.unit.handlers.test_conversation_handler_no_file_service import _channel, _ctx, _handler, _sent_texts


async def test_a_failed_upload_puts_the_note_in_the_slot_and_the_turn_continues(tmp_path, caplog):
    path = tmp_path / "upload.tmp"
    path.write_bytes(b"%PDF-1.4")
    service = MagicMock()
    service.process_attachment = AsyncMock(side_effect=RuntimeError("503 storage"))
    handler, coord = _handler(service)
    channel = _channel(str(path))
    with caplog.at_level(logging.DEBUG):
        await handler.handle_message(_ctx(), channel)
    coord.route_message.assert_awaited_once()
    sent = _sent_texts(coord)
    assert "invoice_painting_es.pdf" in sent and "cannot be stored or read" in sent
    records = [r for r in caplog.records if "upload failed" in r.getMessage()]
    assert records and records[0].levelno == logging.ERROR and "RuntimeError" in records[0].getMessage()
    assert "invoice_painting_es" not in records[0].getMessage()
