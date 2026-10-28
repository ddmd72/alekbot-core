"""UserNotificationService.notify_drive_note_receipt (DRIVE_FILE_NOTES_RFC §4.9): the primary channel is resolved; the
posted text is the shared renderer's in the owner's UI language; the history pair carries the ref (and never an erase
code); an erase request posts its line then the command; no channel or no localization → logged, skipped."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.agent import DeliveryItem
from src.domain.drive_note import (
    DRIVE_NOTE_ERASE_DELIVERY, DRIVE_NOTE_RECEIPT_DELIVERY, STATUS_SOON, EraseRequestReceipt, NoteReceipt,
)
from src.domain.drive_note_receipt import render_drive_note_erase, render_drive_note_receipt
from src.domain.language import LanguageCode
from src.domain.notification import NotificationChannel
from src.locales import en, uk
from src.ports.language_service_port import LanguageServicePort
from src.services.localization_service import LocalizationService
from src.services.user_notification_service import UserNotificationService

T = datetime(2026, 10, 22, tzinfo=timezone.utc)
RECEIPT = NoteReceipt(action="set", note_id="n1", ref="drive:a", path="Docs/a.pdf", version=2,
                      text="Рахунок від підрядника", status=STATUS_SOON)
ERASE = EraseRequestReceipt(scope="one", code="c0ffee", note_count=1, path="Docs/a.pdf", version_count=2)


def _service(*, channel=True, localization=True, language=LanguageCode.UK, language_fails=False):
    state = AsyncMock()
    state.get_primary.return_value = (NotificationChannel(user_id="u1", platform="telegram", channel_id="777",
                                                               updated_at=T)
                                      if channel else None)
    state.get.return_value = None
    response_channel = MagicMock()
    response_channel.posted = []    # every post in order: (method, text); the receipt and the erase line go out literally

    async def literal(text, to_message_id=None):
        response_channel.posted.append(("literal", text))

    async def message(text, thread_id=None, link_list=None):
        response_channel.posted.append(("message", text))

    response_channel.send_literal = AsyncMock(side_effect=literal)
    response_channel.send_message = AsyncMock(side_effect=message)
    factory = MagicMock()
    factory.create.return_value = response_channel
    store = MagicMock()
    store.append_messages_batch = AsyncMock()
    lang = AsyncMock(spec=LanguageServicePort)
    if language_fails:
        lang.resolve_ui_language.side_effect = RuntimeError("profile down")
    else:
        lang.resolve_ui_language.return_value = language
    service = UserNotificationService(
        state_repo=state, channel_factory=factory, coordinator=MagicMock(), notification_sla={}, session_store=store,
        localization=LocalizationService(FileLocalizationAdapter()) if localization else None, language=lang)
    return service, response_channel, store, factory


def _strings(module):
    return lambda m: module.UI_STRINGS[m.value]


def _posted(channel):
    return [text for _, text in channel.posted]


async def test_the_receipt_goes_to_the_primary_channel_in_the_owner_language_with_a_history_pair():
    service, channel, store, factory = _service()
    await service.notify_drive_note_receipt("u1", "a1", DeliveryItem(type=DRIVE_NOTE_RECEIPT_DELIVERY,
                                                                     data=RECEIPT.delivery_data()))
    factory.create.assert_called_once_with(platform="telegram", channel_id="777")
    text = render_drive_note_receipt(RECEIPT.delivery_data(), _strings(uk))
    assert channel.posted == [("literal", text)]
    kwargs = store.append_messages_batch.await_args.kwargs
    assert kwargs["session_id"] == "u1:777" and kwargs["owner_id"] == "u1"
    user, model = kwargs["messages"]
    assert user.parts[0].text == "[System: drive note saved during a call with Lelik: Docs/a.pdf ref=drive:a v2]"
    # final review L-I2 / C-I1: the model turn is a neutral line, never the receipt (no note words in history)
    neutral = "[drive note receipt posted to the chat: Docs/a.pdf v2]"
    assert model.parts[0].text == neutral and model.parts[0].full_text in (None, "", neutral)


async def test_an_erase_request_posts_line_and_command_and_keeps_the_code_out_of_history():
    service, channel, store, _ = _service()
    await service.notify_drive_note_receipt("u1", "a1", DeliveryItem(type=DRIVE_NOTE_ERASE_DELIVERY,
                                                                     data=ERASE.delivery_data()))
    line, command = render_drive_note_erase(ERASE.delivery_data(), _strings(uk))
    assert channel.posted == [("literal", line), ("message", command)]
    history = " ".join(p.text for m in store.append_messages_batch.await_args.kwargs["messages"] for p in m.parts)
    assert "c0ffee" not in history


async def test_no_channel_is_logged_and_skipped():
    service, channel, store, _ = _service(channel=False)
    await service.notify_drive_note_receipt("u1", "a1", DeliveryItem(type=DRIVE_NOTE_RECEIPT_DELIVERY,
                                                                     data=RECEIPT.delivery_data()))
    assert channel.posted == []
    store.append_messages_batch.assert_not_called()


async def test_without_localization_nothing_is_posted_and_an_error_is_logged():
    service, channel, _, _ = _service(localization=False)
    with patch("src.services.user_notification_service.logger") as log:
        await service.notify_drive_note_receipt("u1", "a1", DeliveryItem(type=DRIVE_NOTE_RECEIPT_DELIVERY,
                                                                         data=RECEIPT.delivery_data()))
    assert channel.posted == []
    assert log.error.called


async def test_a_language_lookup_failure_falls_back_to_english():
    service, channel, _, _ = _service(language_fails=True)
    await service.notify_drive_note_receipt("u1", "a1", DeliveryItem(type=DRIVE_NOTE_RECEIPT_DELIVERY,
                                                                     data=RECEIPT.delivery_data()))
    assert channel.posted == [("literal", render_drive_note_receipt(RECEIPT.delivery_data(), _strings(en)))]


async def test_a_failed_post_writes_no_history_and_never_raises():
    service, channel, store, _ = _service()
    channel.send_literal.side_effect = RuntimeError("telegram down")
    await service.notify_drive_note_receipt("u1", "a1", DeliveryItem(type=DRIVE_NOTE_RECEIPT_DELIVERY,
                                                                     data=RECEIPT.delivery_data()))
    store.append_messages_batch.assert_not_called()
