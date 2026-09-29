"""WebSearchLightAgent — one fast grounded lookup for Lelik (VOICE_COMPANION_RFC §4.15.1)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.web_search_light_agent import WebSearchLightAgent
from src.domain.agent import AgentIntent, AgentMessage, AgentStatus
from src.domain.llm import LLMResponse


def _agent(prompt="light prompt", prompt_error=None, llm_text="Sunny, 24 degrees."):
    prompt_builder = AsyncMock()
    prompt_builder.build_for_agent = AsyncMock(return_value=prompt, side_effect=prompt_error)
    execution_context = MagicMock(model_name="gemini-3.5-flash-lite")
    agent = WebSearchLightAgent(
        config=MagicMock(agent_id="web_search_light_agent_u1", agent_type="web_search_light", timeout_ms=20_000),
        execution_context=execution_context, prompt_builder=prompt_builder, user_id="u1",
    )
    agent._call_llm = AsyncMock(return_value=LLMResponse(text=llm_text))
    return agent, prompt_builder


def _message(query="[Sep 29, 07:14 UTC] weather in Valencia tomorrow"):
    return AgentMessage.create(sender="lelik", recipient="web_search_light_agent_u1", intent=AgentIntent.QUERY,
                               payload={"query": query, "intent": "search_web_light"},
                               context={"user_id": "u1", "account_id": "a1"})


@pytest.mark.asyncio
async def test_one_grounded_call_without_the_delegation_timestamp():
    agent, prompt_builder = _agent()

    response = await agent.execute(_message())

    assert response.status == AgentStatus.SUCCESS
    assert response.result == "Sunny, 24 degrees."
    request = agent._call_llm.await_args.args[0]
    assert request.use_grounding is True
    assert request.messages[0].parts[0].text == "weather in Valencia tomorrow"
    assert "light prompt" in request.system_instruction
    agent._call_llm.assert_awaited_once()
    assert prompt_builder.build_for_agent.await_args.kwargs["agent_type"] == "websearch_light"
    assert prompt_builder.build_for_agent.await_args.kwargs["routing_metadata"] is None
    # UAT 2026-09-29: the biography leaked into the search model and the directives made the
    # spoken answer chat-shaped (emojis, bold).
    assert prompt_builder.build_for_agent.await_args.kwargs["include_biographical"] is False
    assert prompt_builder.build_for_agent.await_args.kwargs["include_directives"] is False


@pytest.mark.asyncio
async def test_missing_prompt_is_a_failure_not_a_fallback():
    agent, _ = _agent(prompt_error=RuntimeError("no profile"))

    response = await agent.execute(_message())

    assert response.status != AgentStatus.SUCCESS
    agent._call_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_answer_says_nothing_was_found():
    agent, _ = _agent(llm_text="  ")

    response = await agent.execute(_message())

    assert response.result == "No relevant information found."


@pytest.mark.asyncio
async def test_llm_error_is_a_failure():
    agent, _ = _agent()
    agent._call_llm = AsyncMock(side_effect=RuntimeError("provider down"))

    response = await agent.execute(_message())

    assert response.status != AgentStatus.SUCCESS
