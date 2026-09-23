import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.alek_gateway_agent import AlekGatewayAgent
from src.domain.agent import AgentIntent, AgentMessage, AgentResponse, AgentStatus
from src.domain.messaging import SmartResponse


def _message(**payload):
    return AgentMessage.create(
        sender="coordinator", recipient="alek_agent_u1", intent=AgentIntent.QUERY,
        payload={"query": "[Sep 23, 10:00 UTC] what's in my inbox today", "intent": "ask_alek", **payload},
        context={"user_id": "u1", "account_id": "a1", "session_id": "u1:D9",
                 "origin_channel_id": "D9", "origin_platform": "slack",
                 "_call_chain": ["lelik_agent", "alek_agent"]},
    )


def _agent(answer=None, status_ok=True, metadata=None):
    notifications = AsyncMock()
    agent = AlekGatewayAgent(config=MagicMock(agent_id="alek_agent_u1", timeout_ms=None), notification_service=notifications)
    coordinator = MagicMock()
    answer = answer or SmartResponse(text="Three emails, one from the bank.", structured_data=None, link_list=[])
    coordinator.route_message = AsyncMock(return_value=(
        AgentResponse.success(task_id="t", agent_id="smart", result=answer, metadata=metadata or {})
        if status_ok else AgentResponse.failure(task_id="t", agent_id="router", error="Smart timed out")
    ))
    agent.coordinator = coordinator
    return agent, coordinator, notifications


@pytest.mark.asyncio
async def test_routes_to_the_router_not_smart():
    agent, coordinator, _ = _agent()
    await agent.execute(_message())
    routed = coordinator.route_message.await_args.args[0]
    assert routed.recipient == "router_agent_u1"


@pytest.mark.asyncio
async def test_forwards_context_with_chain_session_and_current_message():
    agent, coordinator, _ = _agent()
    await agent.execute(_message(call_context=[{"role": "user", "text": "anything urgent?"}], reasoning="caller is driving"))
    routed = coordinator.route_message.await_args.args[0]
    assert routed.context["_call_chain"] == ["lelik_agent", "alek_agent"]
    assert routed.context["session_id"] == "u1:D9"
    text = routed.payload["text"]
    assert "what's in my inbox today" in text
    assert "user: anything urgent?" in text and "caller is driving" in text
    [part] = routed.context["current_message_parts"]
    assert part.text == text  # Smart builds its user turn from these parts only


@pytest.mark.asyncio
async def test_returns_smarts_full_answer():
    agent, *_ = _agent()
    response = await agent.execute(_message())
    assert response.status == AgentStatus.SUCCESS
    assert response.result.text == "Three emails, one from the bank."


@pytest.mark.asyncio
async def test_plain_answer_posts_nothing_to_chat():
    agent, _, notifications = _agent()
    await agent.execute(_message())
    notifications.notify_answer_copy.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    SmartResponse(text="t [1]", structured_data=None, link_list=[{"anchor": 1, "url": "https://x"}]),
    SmartResponse(text="t", structured_data=MagicMock(), link_list=[]),
])
async def test_links_or_tables_are_copied_to_chat(answer):
    agent, _, notifications = _agent(answer=answer)
    await agent.execute(_message())
    notifications.notify_answer_copy.assert_awaited_once_with("u1", "a1", answer)


@pytest.mark.asyncio
async def test_chat_copy_failure_does_not_lose_the_spoken_answer():
    answer = SmartResponse(text="t", structured_data=None, link_list=[{"anchor": 1}])
    agent, _, notifications = _agent(answer=answer)
    notifications.notify_answer_copy.side_effect = RuntimeError("slack")
    response = await agent.execute(_message())
    assert response.status == AgentStatus.SUCCESS


@pytest.mark.asyncio
async def test_unused_history_summary_is_cancelled():
    task = asyncio.ensure_future(asyncio.sleep(10))
    agent, *_ = _agent(metadata={"response_summary_task": task})
    await agent.execute(_message())
    await asyncio.sleep(0)
    assert task.cancelled()


@pytest.mark.asyncio
async def test_router_failure_is_a_failure_with_the_reason():
    agent, *_ = _agent(status_ok=False)
    response = await agent.execute(_message())
    assert response.status != AgentStatus.SUCCESS
    assert "Smart timed out" in response.error


def test_never_retried():
    from src.domain.retry_policy import NO_RETRY_POLICY
    assert AlekGatewayAgent.RETRY_POLICY is NO_RETRY_POLICY
