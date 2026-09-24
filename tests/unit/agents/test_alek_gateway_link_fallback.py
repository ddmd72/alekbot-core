"""AlekGatewayAgent M1 (bounded chat-copy wait) + M2 (link fallback from plain text) —
VOICE_COMPANION_RFC §4.10 rule 2, one shared builder with LelikAgent's own copy."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.agents.alek_gateway_agent as gateway_module
from src.agents.alek_gateway_agent import AlekGatewayAgent
from src.domain.agent import AgentIntent, AgentMessage, AgentResponse, AgentStatus
from src.domain.messaging import SmartResponse


def _message(**payload):
    return AgentMessage.create(
        sender="coordinator", recipient="alek_agent_u1", intent=AgentIntent.QUERY,
        payload={"query": "q", "intent": "ask_alek", **payload},
        context={"user_id": "u1", "account_id": "a1"},
    )


def _agent(answer, notifications=None):
    notifications = notifications or AsyncMock()
    agent = AlekGatewayAgent(
        config=MagicMock(agent_id="alek_agent_u1", timeout_ms=None), notification_service=notifications,
    )
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(return_value=AgentResponse.success(
        task_id="t", agent_id="smart", result=answer, metadata={},
    ))
    agent.coordinator = coordinator
    return agent, notifications


@pytest.mark.asyncio
async def test_hung_chat_copy_does_not_block_the_spoken_answer(monkeypatch):
    monkeypatch.setattr(gateway_module, "_ANSWER_COPY_TIMEOUT_S", 0.05)
    answer = SmartResponse(text="See https://example.com/x", structured_data=None, link_list=[])

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(10)

    notifications = AsyncMock()
    notifications.notify_answer_copy.side_effect = hang
    agent, _ = _agent(answer, notifications)

    response = await asyncio.wait_for(agent.execute(_message()), timeout=1.0)
    assert response.status == AgentStatus.SUCCESS


@pytest.mark.asyncio
async def test_raw_urls_with_empty_link_list_get_one_bare_anchor_copy():
    answer = SmartResponse(text="Check https://example.com/x for details.", structured_data=None, link_list=[])
    agent, notifications = _agent(answer)

    await agent.execute(_message())

    notifications.notify_answer_copy.assert_awaited_once()
    posted = notifications.notify_answer_copy.await_args.args[2]
    assert posted.text == "[1]"
    assert posted.link_list == [{"anchor": 1, "title": "example.com", "url": "https://example.com/x"}]


@pytest.mark.asyncio
async def test_link_list_answer_posts_only_the_original_no_second_copy():
    answer = SmartResponse(
        text="Full report [1]", structured_data=None,
        link_list=[{"anchor": 1, "url": "https://x", "title": "x"}],
    )
    agent, notifications = _agent(answer)

    await agent.execute(_message())

    notifications.notify_answer_copy.assert_awaited_once_with("u1", "a1", answer)


@pytest.mark.asyncio
async def test_plain_text_with_no_links_still_posts_nothing():
    answer = SmartResponse(text="No links here, just words.", structured_data=None, link_list=[])
    agent, notifications = _agent(answer)

    await agent.execute(_message())

    notifications.notify_answer_copy.assert_not_awaited()
