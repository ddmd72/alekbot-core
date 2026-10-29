"""A late note receipt follows the run's origin (arbiter Δ10; design U2-5 follow-up, U2-9).

A write cut by the soft deadline still lands and its receipt is posted late through
`UserNotificationService.notify_drive_note_receipt`. On a CHAT turn that receipt goes to the run's origin channel with
chat wording (`call=False`, the origin channel and platform from the run's context); on a call with Lelik it keeps the
existing call path (`call=True`, no override: the primary channel). Synthetic data only: the bathroom-repair invoice."""
import asyncio
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from src.domain import drive_note_tool_texts as nt
from src.domain.drive_note import DRIVE_NOTE_RECEIPT_DELIVERY
from src.services.user_notification_service import UserNotificationService
from tests.unit.infrastructure.test_drive_note_tools_specialist import (
    REPAIR, OWNER, _call, _ops, _receipt, _service, _tools,
)


async def _cut_write(context: Dict[str, Any]) -> AsyncMock:
    gate = asyncio.Event()
    service = _service()

    async def slow_create(*args, **kwargs):
        await gate.wait()
        return _receipt()
    service.create.side_effect = slow_create
    notification = AsyncMock(spec=UserNotificationService)
    notes, _, _ = _tools(service, ops=_ops(notification), context=context)
    call = asyncio.ensure_future(_call(notes, nt.CREATE_NOTE, item_ref="drive:a", text=REPAIR))
    await asyncio.sleep(0.01)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
    return notification


async def test_a_late_receipt_on_a_chat_turn_goes_to_the_origin_channel_with_chat_wording():
    notification = await _cut_write({**OWNER, "origin_channel_id": "C1", "origin_platform": "slack"})
    notification.notify_drive_note_receipt.assert_awaited_once()
    args, kwargs = notification.notify_drive_note_receipt.await_args
    assert args[2].type == DRIVE_NOTE_RECEIPT_DELIVERY
    assert kwargs == {"call": False, "channel_id_override": "C1", "platform_override": "slack"}


async def test_a_late_receipt_on_a_chat_turn_without_an_origin_channel_falls_to_the_primary_with_chat_wording():
    notification = await _cut_write(dict(OWNER))
    _, kwargs = notification.notify_drive_note_receipt.await_args
    assert kwargs == {"call": False, "channel_id_override": None, "platform_override": None}


@pytest.mark.parametrize("origin", ["voice:ask_alek", "voice:send_to_chat"])
async def test_a_late_receipt_on_a_call_keeps_the_call_path_and_ignores_any_origin_channel(origin):
    notification = await _cut_write({**OWNER, "run_origin": origin, "origin_channel_id": "C1",
                                     "origin_platform": "slack"})
    notification.notify_drive_note_receipt.assert_awaited_once()
    _, kwargs = notification.notify_drive_note_receipt.await_args
    assert kwargs == {"call": True, "channel_id_override": None, "platform_override": None}
