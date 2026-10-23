"""Quick and Smart on a REAL ClaudeAdapter end a turn on the synthesized ``deliver_response`` (CDR T6).

Real: the agent, ``BaseAgent._call_llm``, ``DelegationEngine`` and ``ClaudeAdapter``. Faked: the SDK boundary
(``client.messages.stream``, the helpers of the adapter wire tests) and the coordinator. Spec:
docs/04_solution_strategy/decisions/claude_deliver_response_string_tool.md (D1: an empty answer is a failure).
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.claude_adapter import ClaudeAdapter
from src.agents.core.quick_response_agent import QuickResponseAgent
from src.agents.core.smart_response_agent import SmartResponseAgent
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.domain.agent import AgentConfig, AgentStatus
from src.domain.user import PerformanceTier, UserBotConfig
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.ports.llm_port import ProviderCapabilities
from src.services.agent_context_builder import AgentExecutionContext
from tests.support.agent_answer_fakes import ENVELOPE
from tests.unit.adapters.test_claude_adapter_deliver_response_wire import (
    _install, _response, _text_block, _tool_block,
)
from tests.unit.agents.core.test_quick_response_agent import (  # noqa: F401  (fixtures)
    mock_session_store, mock_prompt_builder, create_query_message as quick_message,
)
from tests.unit.agents.core.test_smart_response_agent import create_query_message as smart_message

_MODEL = "claude-sonnet-5-5"


def _context(adapter, agent_type, tier):
    return AgentExecutionContext(
        agent_type=agent_type, provider=adapter, model_name=_MODEL, tier=tier,
        capabilities=ProviderCapabilities(), resilience_port=InMemoryProviderResilience(),
    )


def _build_quick(adapter, session_store, prompt_builder):
    config = AgentConfig(agent_id="quick_response_agent", agent_type="quick_response",
                         llm_model=_MODEL, timeout_ms=10000, capabilities=["fast_response"])
    return QuickResponseAgent(config=config, execution_context=_context(adapter, "quick", PerformanceTier.ECO),
                              session_store=session_store, prompt_builder=prompt_builder)


def _build_smart(adapter, session_store, prompt_builder):
    from src.infrastructure.task_execution_resolver import TaskExecutionResolver
    config = AgentConfig(agent_id="smart_response_agent", agent_type="smart_response",
                         llm_model=_MODEL, timeout_ms=60000,
                         capabilities=["complex_reasoning", "agent_delegation"])
    resolver = MagicMock(spec=TaskExecutionResolver)
    resolver.resolve.return_value = None
    coordinator = MagicMock(spec=AgentCoordinator)
    coordinator.route_message = AsyncMock()
    return SmartResponseAgent(
        config=config, execution_context=_context(adapter, "smart", PerformanceTier.BALANCED),
        session_store=session_store, prompt_builder=prompt_builder, resolver=resolver,
        user_config=UserBotConfig(), coordinator=coordinator)


@pytest.fixture(params=["quick", "smart"])
def setup(request, mock_session_store, mock_prompt_builder):
    adapter = ClaudeAdapter(api_key="k")
    if request.param == "quick":
        return adapter, _build_quick(adapter, mock_session_store, mock_prompt_builder), quick_message("hello")
    return adapter, _build_smart(adapter, mock_session_store, mock_prompt_builder), smart_message("complex")


async def test_deliver_response_ends_the_turn_after_one_llm_call(setup):
    adapter, agent, message = setup
    calls = _install(adapter, _response(
        _tool_block("deliver_response", {"response": json.dumps(ENVELOPE)}), stop_reason="tool_use"))

    r = await agent.execute(message)

    assert len(calls) == 1  # no "unknown tool" loop
    sent_tools = calls[0]["tools"]
    assert sent_tools[-1]["name"] == "deliver_response"
    assert len(sent_tools) > 1
    assert "output_config" not in calls[0] or "format" not in (calls[0]["output_config"] or {})
    assert r.status == AgentStatus.SUCCESS
    assert r.result.text == "Here is the answer."
    assert r.metadata["response_summary"] == "Short summary."
    assert r.result.structured_data.content_type == "table"
    assert r.result.structured_data.data["rows"] == [{"cells": ["a", "b"]}]


async def test_max_tokens_cut_deliver_call_is_an_empty_answer_failure(setup, caplog):
    adapter, agent, message = setup
    calls = _install(adapter, _response(
        _tool_block("deliver_response", {}), stop_reason="max_tokens"))

    r = await agent.execute(message)

    assert len(calls) == 1  # the adapter dropped the cut call: the engine had nothing to dispatch
    assert r.status == AgentStatus.FAILED
    assert r.error == "empty_answer"
    assert any(getattr(rec, "event", None) == "empty_answer" for rec in caplog.records)


async def test_text_beside_the_deliver_call_survives_when_the_answer_string_is_empty(setup):
    adapter, agent, message = setup
    _install(adapter, _response(
        _text_block("Said beside the call."), _tool_block("deliver_response", {"response": ""}),
        stop_reason="tool_use"))

    r = await agent.execute(message)

    assert r.status == AgentStatus.SUCCESS
    assert r.result.text == "Said beside the call."
