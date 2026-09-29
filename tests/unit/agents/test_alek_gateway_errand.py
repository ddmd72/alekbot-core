"""tell_alek — an errand from a phone call (VOICE_COMPANION_RFC §4.15.2).

The caller is not waiting for it: the commission says so, and Alek's whole answer is posted to
chat, links or not — it is the only place the caller sees the outcome. The errand reaches the
gateway through the Cloud Task worker, as AgentIntent.DELEGATE.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.alek_gateway_agent import AlekGatewayAgent
from src.domain.agent import AgentIntent, AgentMessage, AgentResponse, AgentStatus
from src.domain.messaging import SmartResponse


def _message(intent_name="tell_alek", agent_intent=AgentIntent.DELEGATE):
    return AgentMessage.create(
        sender="worker", recipient="alek_agent_u1", intent=agent_intent,
        payload={"query": "[Sep 28, 10:20 UTC] Remind the user when to leave for the airport",
                 "intent": intent_name,
                 "call_context": [{"role": "user", "text": "Remind me when to leave to meet her"}]},
        context={"user_id": "u1", "account_id": "a1", "_call_chain": ["lelik_agent", "alek_agent"]},
    )


def _agent(answer):
    notifications = AsyncMock()
    agent = AlekGatewayAgent(config=MagicMock(agent_id="alek_agent_u1", timeout_ms=None),
                             notification_service=notifications)
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(return_value=AgentResponse.success(
        task_id="t", agent_id="smart", result=answer, metadata={}))
    agent.coordinator = coordinator
    return agent, coordinator, notifications


@pytest.mark.asyncio
async def test_the_worker_delegate_message_is_accepted():
    agent, *_ = _agent(SmartResponse(text="ok", structured_data=None, link_list=[]))

    assert await agent.can_handle(_message()) is True


@pytest.mark.asyncio
async def test_errand_commission_says_the_caller_is_not_waiting():
    agent, coordinator, _ = _agent(SmartResponse(text="ok", structured_data=None, link_list=[]))

    await agent.execute(_message())

    text = coordinator.route_message.await_args.args[0].payload["text"]
    assert "[Errand from Lelik during a phone call with the user." in text
    assert "Asked by Lelik" not in text


@pytest.mark.asyncio
async def test_errand_answer_without_links_is_still_posted_in_full():
    answer = SmartResponse(text="Two reminders set: 10:00 flight status, 13:30 leave.",
                           structured_data=None, link_list=[])
    agent, _, notifications = _agent(answer)

    response = await agent.execute(_message())

    assert response.status == AgentStatus.SUCCESS
    notifications.notify_answer_copy.assert_awaited_once_with("u1", "a1", answer)


@pytest.mark.asyncio
async def test_a_question_without_links_posts_nothing():
    agent, coordinator, notifications = _agent(SmartResponse(text="Nothing tomorrow.", structured_data=None, link_list=[]))

    await agent.execute(_message(intent_name="ask_alek", agent_intent=AgentIntent.QUERY))

    notifications.notify_answer_copy.assert_not_awaited()
    assert "Asked by Lelik" in coordinator.route_message.await_args.args[0].payload["text"]
