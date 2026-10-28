"""UserNotificationService.notify_drive_note_receipt, every failure path (DRIVE_FILE_NOTES_RFC §4.9): each one is
logged at its level, posts nothing it must not, writes no history pair it must not, and never raises."""
import logging
from unittest.mock import AsyncMock

import pytest

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.drive_note_receipt import render_drive_note_receipt
from src.domain.language import LanguageCode
from src.locales import en
from src.ports.language_service_port import LanguageServicePort
from src.services.localization_service import LocalizationService

from src.domain.agent import AgentMessage, AgentResponse, DeliveryItem
from src.domain.drive_note import DRIVE_NOTE_ERASE_DELIVERY, DRIVE_NOTE_RECEIPT_DELIVERY
from src.services.user_notification_service import MessageRouter
from tests.unit.services.test_notification_drive_note_receipt import ERASE, RECEIPT, _service, _strings

LOGGER = "src.services.user_notification_service"
RECEIPT_ITEM = DeliveryItem(type=DRIVE_NOTE_RECEIPT_DELIVERY, data=RECEIPT.delivery_data())
ERASE_ITEM = DeliveryItem(type=DRIVE_NOTE_ERASE_DELIVERY, data=ERASE.delivery_data())


@pytest.fixture(autouse=True)
def _capture(caplog):
    caplog.set_level(logging.DEBUG)


def _records(caplog, level):
    return [r.getMessage() for r in caplog.records if r.levelno == level]


async def test_an_item_that_cannot_be_rendered_is_logged_and_nothing_else_happens(caplog):
    service, channel, store, factory = _service()
    bad = DeliveryItem(type=DRIVE_NOTE_RECEIPT_DELIVERY, data=None)
    await service.notify_drive_note_receipt("u1-user-id", "a1", bad)
    errors = _records(caplog, logging.ERROR)
    assert len(errors) == 1 and "could not be rendered" in errors[0] and "u1-user-" in errors[0]
    assert channel.posted == []
    factory.create.assert_not_called()
    store.append_messages_batch.assert_not_called()


async def test_an_erase_item_that_cannot_be_rendered_is_logged_the_same_way(caplog):
    service, channel, store, _ = _service()
    await service.notify_drive_note_receipt("u1", "a1", DeliveryItem(type=DRIVE_NOTE_ERASE_DELIVERY, data=None))
    assert any("could not be rendered" in m for m in _records(caplog, logging.ERROR))
    assert channel.posted == []
    store.append_messages_batch.assert_not_called()


async def test_no_resolved_channel_is_an_error_for_the_operator_and_posts_nothing(caplog):
    service, channel, store, factory = _service(channel=False)
    await service.notify_drive_note_receipt("u1", "a1", RECEIPT_ITEM)
    errors = _records(caplog, logging.ERROR)
    assert len(errors) == 1 and "No channel" in errors[0] and DRIVE_NOTE_RECEIPT_DELIVERY in errors[0]
    factory.create.assert_not_called()
    assert channel.posted == []
    store.append_messages_batch.assert_not_called()


async def test_a_channel_factory_that_yields_nothing_is_a_warning_and_writes_no_history(caplog):
    service, channel, store, factory = _service()
    factory.create.return_value = None
    await service.notify_drive_note_receipt("u1", "a1", RECEIPT_ITEM)
    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1 and "Cannot create channel" in warnings[0] and "telegram" in warnings[0]
    assert channel.posted == []
    store.append_messages_batch.assert_not_called()


async def test_a_failed_post_is_logged_with_the_platform_and_no_history_pair_follows(caplog):
    service, channel, store, _ = _service()
    channel.send_literal.side_effect = RuntimeError("telegram down")
    await service.notify_drive_note_receipt("u1", "a1", RECEIPT_ITEM)
    errors = _records(caplog, logging.ERROR)
    assert len(errors) == 1 and "delivery failed" in errors[0] and "telegram down" in errors[0]
    assert not any("delivered to" in r.getMessage() for r in caplog.records)
    store.append_messages_batch.assert_not_called()


async def test_a_failed_command_post_of_an_erase_request_also_stops_before_history(caplog):
    service, channel, store, _ = _service()
    channel.send_message.side_effect = RuntimeError("slack down")
    await service.notify_drive_note_receipt("u1", "a1", ERASE_ITEM)
    assert [kind for kind, _ in channel.posted] == ["literal"]
    assert any("delivery failed" in m for m in _records(caplog, logging.ERROR))
    store.append_messages_batch.assert_not_called()


async def test_without_a_session_store_the_receipt_is_delivered_and_no_history_is_attempted(caplog):
    service, channel, store, _ = _service()
    service._session_store = None
    await service.notify_drive_note_receipt("u1", "a1", RECEIPT_ITEM)
    assert [kind for kind, _ in channel.posted] == ["literal"]
    assert any("delivered to telegram" in m for m in _records(caplog, logging.INFO))
    store.append_messages_batch.assert_not_called()
    assert _records(caplog, logging.ERROR) == []


async def test_a_history_write_failure_is_logged_and_never_raises(caplog):
    service, channel, store, _ = _service()
    store.append_messages_batch.side_effect = RuntimeError("firestore down")
    await service.notify_drive_note_receipt("u1", "a1", RECEIPT_ITEM)
    assert [kind for kind, _ in channel.posted] == ["literal"]
    assert any("history pair failed" in m for m in _records(caplog, logging.ERROR))


async def test_a_message_router_implementation_can_defer_to_the_protocol_stub():
    class Router(MessageRouter):
        async def route_message(self, message: AgentMessage) -> AgentResponse:
            return await super().route_message(message)

    assert await Router().route_message(None) is None  # type: ignore[arg-type]


async def test_a_service_wired_without_a_language_service_posts_in_english():
    service, channel, _, _ = _service()
    service._language = None
    await service.notify_drive_note_receipt("u1", "a1", RECEIPT_ITEM)
    assert channel.posted == [("literal", render_drive_note_receipt(RECEIPT.delivery_data(), _strings(en)))]


async def test_attach_localization_late_wires_strings_and_language():
    service, channel, _, _ = _service(localization=False)
    await service.notify_drive_note_receipt("u1", "a1", RECEIPT_ITEM)
    assert channel.posted == []
    lang = AsyncMock(spec=LanguageServicePort)
    lang.resolve_ui_language.return_value = LanguageCode.EN
    service.attach_localization(LocalizationService(FileLocalizationAdapter()), lang)
    await service.notify_drive_note_receipt("u1", "a1", RECEIPT_ITEM)
    assert channel.posted == [("literal", render_drive_note_receipt(RECEIPT.delivery_data(), _strings(en)))]
