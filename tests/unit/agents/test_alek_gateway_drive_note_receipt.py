"""On a call, the Alek gateway posts Smart's drive-note receipts and erase requests to chat (DRIVE_FILE_NOTES_RFC §4.9,
D18): on ask_alek and on send_to_chat, each such item through notify_drive_note_receipt, independent of the answer copy;
a failed post is logged and the answer still returns; no item, no post."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.alek_gateway_agent import AlekGatewayAgent
from src.domain.agent import AgentIntent, AgentMessage, AgentResponse, AgentStatus, DeliveryItem
from src.domain.drive_note import DRIVE_NOTE_ERASE_DELIVERY, DRIVE_NOTE_RECEIPT_DELIVERY
from src.domain.messaging import SmartResponse

RECEIPT = DeliveryItem(type=DRIVE_NOTE_RECEIPT_DELIVERY, data={"action": "set", "path": "Docs/a.pdf", "version": 1})
ERASE = DeliveryItem(type=DRIVE_NOTE_ERASE_DELIVERY, data={"scope": "one", "command": "$drive-notes erase c1"})
OTHER = DeliveryItem(type="document", data={})


def _message(intent):
    return AgentMessage.create(
        sender="coordinator", recipient="alek_agent_u1", intent=AgentIntent.QUERY,
        payload={"query": "note the PDF as the contractor's invoice", "intent": intent},
        context={"user_id": "u1", "account_id": "a1", "session_id": "u1:D9"},
    )


def _agent(items):
    notifications = AsyncMock()
    agent = AlekGatewayAgent(config=MagicMock(agent_id="alek_agent_u1", timeout_ms=None),
                             notification_service=notifications)
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(return_value=AgentResponse.success(
        task_id="t", agent_id="smart", result=SmartResponse(text="Noted.", structured_data=None, link_list=[]),
        delivery_items=list(items)))
    agent.coordinator = coordinator
    return agent, notifications


@pytest.mark.parametrize("intent", ["ask_alek", "send_to_chat"])
async def test_each_note_item_is_posted_on_both_paths(intent):
    agent, notifications = _agent([RECEIPT, OTHER, ERASE])
    response = await agent.execute(_message(intent))
    assert response.status == AgentStatus.SUCCESS
    posted = [c.args for c in notifications.notify_drive_note_receipt.await_args_list]
    assert posted == [("u1", "a1", RECEIPT), ("u1", "a1", ERASE)]


async def test_posted_even_when_ask_alek_sends_no_answer_copy():
    agent, notifications = _agent([RECEIPT])
    await agent.execute(_message("ask_alek"))
    notifications.notify_answer_copy.assert_not_called()      # "Noted." has no link: no copy on a question
    notifications.notify_drive_note_receipt.assert_awaited_once()


async def test_a_failed_post_is_logged_and_the_answer_still_returns():
    agent, notifications = _agent([RECEIPT, ERASE])
    notifications.notify_drive_note_receipt.side_effect = [RuntimeError("slack down"), None]
    with patch("src.agents.alek_gateway_agent.logger") as log:
        response = await agent.execute(_message("send_to_chat"))
    assert response.status == AgentStatus.SUCCESS and response.result.text == "Noted."
    assert notifications.notify_drive_note_receipt.await_count == 2
    assert any("drive_note_receipt" in c.args[0] for c in log.error.call_args_list)


async def test_no_note_item_no_post():
    agent, notifications = _agent([OTHER])
    await agent.execute(_message("send_to_chat"))
    notifications.notify_drive_note_receipt.assert_not_called()
