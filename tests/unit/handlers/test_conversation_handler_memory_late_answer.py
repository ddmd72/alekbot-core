"""Memory preview on the long-turn late-answer path, and malformed preview items
(MEMORY_FACT_CONFIRMATION_RFC). Reuses the long-turn harness."""
import asyncio

import pytest

from src.domain.agent import AgentResponse, AgentStatus, DeliveryItem
from src.domain.memory_fact import MEMORY_PREVIEW_DELIVERY
from src.domain.messaging import SmartResponse
from src.domain.ui_messages import UIMessage
from tests.unit.handlers.test_conversation_handler_long_turn import _build

FACT = {"text": "User's passport expires on 2031-03-04.", "source": "passport.pdf"}


@pytest.fixture
def lt():
    return _build(notice_after_s=0.05, heartbeat_s=0.02)


async def test_late_answer_delivers_the_memory_preview_file_then_the_command(lt):
    async def _route(message):
        await asyncio.sleep(0.2)
        return AgentResponse(
            task_id="t", agent_id="smart", status=AgentStatus.SUCCESS,
            result=SmartResponse(text="proposed"), confidence=1.0, metadata={},
            delivery_items=[DeliveryItem(type=MEMORY_PREVIEW_DELIVERY,
                                         data={"facts": [FACT], "command": "$memory save ab12"})])
    lt.coordinator.route_message.side_effect = _route
    order = []
    lt.channel.send_late_answer.side_effect = lambda *a, **k: order.append("late")
    lt.channel.send_file.side_effect = lambda **k: order.append("file")
    lt.channel.send_message.side_effect = lambda text, thread=None: order.append(text)

    await lt.handle()

    assert order[0] == "late" and order[1] == "file" and order[-1] == "`$memory save ab12`"
    assert lt.channel.send_file.await_args.kwargs["thread_id"] is None


async def test_a_malformed_preview_item_sends_the_failure_notice_not_an_exception(lt):
    ch = lt.channel
    for bad in ({"command": "$memory save ab12"}, {"facts": [{"nope": 1}], "command": "$memory save ab12"}):
        ch.send_message.reset_mock()
        item = DeliveryItem(type=MEMORY_PREVIEW_DELIVERY, data=bad)

        await lt.handler._deliver_item(item, ch, None, "u")

        sent = [c.args[0] for c in ch.send_message.await_args_list]
        assert sent == [lt.handler._ui_string_for_language(
            getattr(ch, "language", None), UIMessage.MEMORY_PREVIEW_DELIVERY_FAILED)]


@pytest.mark.parametrize("code", ["AB12", "abc", "ab123", "zzzz", "../x"])
async def test_a_code_that_is_not_four_lowercase_hex_chars_never_reaches_the_service(lt, code):
    from unittest.mock import AsyncMock, MagicMock
    service = MagicMock()
    service.confirm = AsyncMock()
    lt.handler._memory_facts = service
    ctx = lt.make_context()

    await lt.handler._handle_memory_command(f"memory save {code}", ctx, lt.channel)

    service.confirm.assert_not_awaited()
    assert lt.channel.send_message.await_args.args[0] == lt.handler._ui_string(
        ctx, UIMessage.MEMORY_DRAFT_NOT_FOUND, code=code)
