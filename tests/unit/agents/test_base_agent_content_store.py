"""
Unit tests for BaseAgent's LLM content capture hook.

After every _call_llm, the agent hands the turn to the injected
PromptContentStore via record_turn — best-effort, non-blocking. The adapter
builds the record; the agent only passes its native objects + identity. No store
injected → no call, and the LLM result is returned unchanged.
"""

from unittest.mock import AsyncMock

import pytest

from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentResponse
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart, UsageMetadata
from src.ports.prompt_content_store import PromptContentStore


class _StubAgent(BaseAgent):
    async def can_handle(self, message: AgentMessage) -> bool:  # pragma: no cover
        return True

    async def execute(self, message: AgentMessage) -> AgentResponse:  # pragma: no cover
        raise NotImplementedError


def _agent() -> _StubAgent:
    return _StubAgent(config=AgentConfig(agent_id="stub_u1", agent_type="stub"))


def _request() -> LLMRequest:
    return LLMRequest(
        model_name="claude-opus-4-8",
        system_instruction="you are a test",
        messages=[Message(role="user", parts=[MessagePart(text="hello")])],
    )


def _response() -> LLMResponse:
    return LLMResponse(
        text="hi there",
        usage_metadata=UsageMetadata(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


class TestContentCapture:
    async def test_call_llm_invokes_record_turn_with_identity(self):
        agent = _agent()
        store = AsyncMock(spec=PromptContentStore)
        agent._prompt_content_store = store
        agent._billing_account_id = "acct-1"
        agent.llm = AsyncMock()
        agent.llm.generate_content = AsyncMock(return_value=_response())

        resp = await agent._call_llm(_request(), turn=2)

        assert resp.text == "hi there"
        store.record_turn.assert_awaited_once()
        kwargs = store.record_turn.call_args.kwargs
        assert kwargs["agent_id"] == "stub_u1"
        assert kwargs["agent_type"] == "stub"
        assert kwargs["account_id"] == "acct-1"
        assert kwargs["turn"] == 2
        assert kwargs["provider"] == ""  # no execution context → empty provider
        assert kwargs["latency_ms"] is not None and kwargs["latency_ms"] >= 0
        assert kwargs["request"] is not None
        assert kwargs["response"].text == "hi there"

    async def test_no_store_means_no_capture(self):
        agent = _agent()
        agent._prompt_content_store = None
        agent.llm = AsyncMock()
        agent.llm.generate_content = AsyncMock(return_value=_response())

        resp = await agent._call_llm(_request())

        assert resp.text == "hi there"  # no error, result unchanged
