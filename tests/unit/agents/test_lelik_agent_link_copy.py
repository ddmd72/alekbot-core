"""Links from ANY Lelik delegation reach chat structurally (RFC §4.10 rule 2, generalized) —
not only ask_alek's own copy, which AlekGatewayAgent already handles."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_agent import LelikAgent
from src.domain.agent import AgentResponse
from src.domain.messaging import SmartResponse
from src.infrastructure.agent_coordinator import AgentCoordinator


def _agent(delegation_result: str):
    prompt_builder = AsyncMock()
    persona = AsyncMock()
    persona.primary_channel.return_value = None
    notifications = AsyncMock()
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_u1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=prompt_builder, persona=persona, notifications=notifications,
    )
    coordinator = MagicMock()
    coordinator.CALL_CHAIN_KEY = AgentCoordinator.CALL_CHAIN_KEY
    coordinator.handle_delegation = AsyncMock(
        return_value=AgentResponse.success(task_id="t", agent_id="a", result=delegation_result),
    )
    agent.coordinator = coordinator
    return agent, notifications, coordinator


@pytest.mark.asyncio
async def test_search_findings_with_urls_post_one_chat_copy():
    # WebSearchAgent's findings shape (docs/.../task-A-brief.md); intent is deliberately
    # NOT "search_web" here — LELIK's descriptor fans that one out to Maps too
    # (DelegationEngine merges/labels the result), which is orthogonal to this feature
    # and already covered by test_lelik_agent_delegation.py.
    result_str = (
        '{"findings": [{"text": "Whey protein", "source": "Amazon.es listing", '
        '"url": "https://www.amazon.es/dp/B000QSNYGI"}]}'
    )
    agent, notifications, _ = _agent(result_str)

    output = await agent.delegate(
        user_id="u1", account_id="a1",
        arguments={"intent": "search_memory", "query": "amazon protein links"},
        call_context=[],
    )

    assert output == result_str
    notifications.notify_answer_copy.assert_awaited_once()
    call_args = notifications.notify_answer_copy.await_args
    assert call_args.args[0] == "u1" and call_args.args[1] == "a1"
    answer: SmartResponse = call_args.args[2]
    assert answer.link_list == [
        {"anchor": 1, "title": "Amazon.es listing", "url": "https://www.amazon.es/dp/B000QSNYGI"},
    ]
    assert answer.text == "[1] Amazon.es listing"


@pytest.mark.asyncio
async def test_result_without_urls_posts_no_copy():
    agent, notifications, _ = _agent("sunny, 24°C in Valencia")

    await agent.delegate(
        user_id="u1", account_id="a1",
        arguments={"intent": "search_memory", "query": "weather"},
        call_context=[],
    )

    notifications.notify_answer_copy.assert_not_awaited()


@pytest.mark.asyncio
async def test_ask_alek_never_double_posts_even_with_urls_present():
    """AlekGatewayAgent already runs notify_answer_copy for ask_alek's own structured answer."""
    result_str = '{"findings": [{"source": "x", "url": "https://example.com/x"}]}'
    agent, notifications, _ = _agent(result_str)

    output = await agent.delegate(
        user_id="u1", account_id="a1",
        arguments={"intent": "ask_alek", "query": "find me links"},
        call_context=[],
    )

    assert output == result_str
    notifications.notify_answer_copy.assert_not_awaited()


@pytest.mark.asyncio
async def test_copy_failure_is_logged_and_delegate_still_returns_result_str():
    result_str = '{"findings": [{"source": "x", "url": "https://example.com/x"}]}'
    agent, notifications, _ = _agent(result_str)
    notifications.notify_answer_copy.side_effect = RuntimeError("channel boom")

    output = await agent.delegate(
        user_id="u1", account_id="a1",
        arguments={"intent": "search_memory", "query": "find me links"},
        call_context=[],
    )

    assert output == result_str
    notifications.notify_answer_copy.assert_awaited_once()
