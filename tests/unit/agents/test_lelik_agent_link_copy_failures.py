"""A failed delegation result never posts a chat copy, even when its error string contains a
URL (e.g. an OpenAI 429 pointing at platform.openai.com) — VOICE_COMPANION_RFC §4.10 rule 2,
Important finding 1."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_agent import LelikAgent
from src.domain.agent import AgentResponse
from src.infrastructure.agent_coordinator import AgentCoordinator


def _agent():
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
    agent.coordinator = coordinator
    return agent, notifications, coordinator


@pytest.mark.asyncio
async def test_rejected_result_with_a_url_in_the_error_posts_no_copy():
    agent, notifications, coordinator = _agent()
    coordinator.handle_delegation = AsyncMock(return_value=AgentResponse.failure(
        task_id="t", agent_id="a",
        error="rate limited, see https://platform.openai.com/account/limits",
    ))

    output = await agent.delegate(
        user_id="u1", account_id="a1",
        arguments={"intent": "search_memory", "query": "q"}, call_context=[],
    )

    assert "platform.openai.com" in output  # the spoken/returned result is untouched
    notifications.notify_answer_copy.assert_not_awaited()


@pytest.mark.asyncio
async def test_fanout_primary_rejected_posts_no_copy_even_with_a_secondary_link():
    """search_web fans out to maps_query through LELIK's own descriptor; the primary section
    is what decides failed, but a URL could land in either merged section."""
    agent, notifications, coordinator = _agent()

    async def handle_delegation(intent, **kwargs):
        if intent == "search_web":
            return AgentResponse.failure(task_id="t", agent_id="a", error="down")
        return AgentResponse.success(task_id="t", agent_id="a", result="See https://maps.example.com/x")

    coordinator.handle_delegation = AsyncMock(side_effect=handle_delegation)

    await agent.delegate(
        user_id="u1", account_id="a1",
        arguments={"intent": "search_web", "query": "coffee nearby"}, call_context=[],
    )

    assert {c.kwargs["intent"] for c in coordinator.handle_delegation.await_args_list} == {"search_web", "maps_query"}
    notifications.notify_answer_copy.assert_not_awaited()


@pytest.mark.asyncio
async def test_fanout_primary_ok_with_real_merge_format_still_copies_the_right_links():
    """Runs through the REAL _merge_fanout_results (via DelegationEngine.dispatch), not a
    reimplementation: the merged string carries the "SYSTEM:" preamble + "[Primary specialist: ...]"
    brackets, and the copy must still extract exactly the primary's link."""
    agent, notifications, coordinator = _agent()

    async def handle_delegation(intent, **kwargs):
        if intent == "search_web":
            return AgentResponse.success(
                task_id="t", agent_id="a",
                result='{"findings": [{"source": "Example", "url": "https://example.com/x"}]}',
            )
        return AgentResponse.failure(task_id="t", agent_id="a", error="maps down")

    coordinator.handle_delegation = AsyncMock(side_effect=handle_delegation)

    output = await agent.delegate(
        user_id="u1", account_id="a1",
        arguments={"intent": "search_web", "query": "coffee nearby"}, call_context=[],
    )

    assert "SYSTEM:" in output and "[Primary specialist: Web Search]" in output
    notifications.notify_answer_copy.assert_awaited_once()
    answer = notifications.notify_answer_copy.await_args.args[2]
    assert answer.link_list == [{"anchor": 1, "title": "Example", "url": "https://example.com/x"}]
