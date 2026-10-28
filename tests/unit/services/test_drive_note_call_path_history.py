"""A drive-note receipt posted from a call keeps the note's words out of session history (final review L-I2, C-I1;
DRIVE_FILE_NOTES_RFC §1.4, §4.9): the history pair the consolidator would read carries the ref and a neutral line,
never the receipt; no channel is logged at ERROR (C-M10); the gateway's note posts share one timeout (C-M10)."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from src.domain.agent import DeliveryItem
from src.domain.companion_config import CompanionTextMode
from src.domain.consolidation_serialization import serialize_messages_for_consolidation
from src.domain.drive_note import DRIVE_NOTE_RECEIPT_DELIVERY

from .test_notification_drive_note_receipt import RECEIPT, _service

NOTE_WORDS = "Рахунок від підрядника"


async def test_the_history_pair_serialised_for_consolidation_holds_no_note_text():
    service, channel, store, _ = _service()
    await service.notify_drive_note_receipt("u1", "a1", DeliveryItem(type=DRIVE_NOTE_RECEIPT_DELIVERY,
                                                                     data=RECEIPT.delivery_data()))
    assert any(method == "literal" and NOTE_WORDS in text for method, text in channel.posted)   # the owner sees it
    messages = store.append_messages_batch.await_args.kwargs["messages"]
    for mode in (CompanionTextMode.SUMMARY, CompanionTextMode.FULL):
        serialized = repr(serialize_messages_for_consolidation(messages, mode))
        assert NOTE_WORDS not in serialized and "підрядника" not in serialized
        assert "ref=drive:a v2" in serialized
    assert all(NOTE_WORDS not in (p.full_text or "") + (p.text or "") for m in messages for p in m.parts)


async def test_no_channel_is_an_error():
    service, channel, _, _ = _service(channel=False)
    with patch("src.services.user_notification_service.logger") as log:
        await service.notify_drive_note_receipt("u1", "a1", DeliveryItem(type=DRIVE_NOTE_RECEIPT_DELIVERY,
                                                                         data=RECEIPT.delivery_data()))
    assert any("not posted" in c.args[0] for c in log.error.call_args_list)
    assert channel.posted == []


async def test_the_gateways_note_posts_share_one_timeout():
    from ..agents.test_alek_gateway_drive_note_receipt import ERASE, RECEIPT as ITEM, _agent, _message
    agent, notifications = _agent([ITEM, ERASE, ITEM])
    started = []

    async def slow(user_id, account_id, item):
        started.append(item)
        await asyncio.sleep(10)

    notifications.notify_drive_note_receipt.side_effect = slow
    with patch("src.agents.alek_gateway_agent._ANSWER_COPY_TIMEOUT_S", 0.05), \
            patch("src.agents.alek_gateway_agent.logger") as log:
        response = await agent.execute(_message("ask_alek"))
    assert response.result.text == "Noted."
    assert len(started) == 1        # one shared wait: the next notes are not each given their own timeout
    assert any("3 of 3 not posted" in c.args[0] for c in log.error.call_args_list)
