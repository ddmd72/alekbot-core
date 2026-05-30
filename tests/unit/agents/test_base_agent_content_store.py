"""
Unit tests for BaseAgent's fire-and-forget LLM content capture.

After every _call_llm, the agent schedules a PromptContentRecord write to the
injected PromptContentStore (BigQuery). The write is non-blocking, carries the
request/response + token metadata, and is skipped entirely when no store is
injected. These tests exercise the врезка in isolation with a mock LLM + store.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentResponse
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart, UsageMetadata
from src.domain.observability import PromptContentRecord
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
        usage_metadata=UsageMetadata(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        ),
    )


async def _drain(agent: _StubAgent) -> None:
    """Await any scheduled fire-and-forget content tasks."""
    await asyncio.gather(*list(agent._content_tasks))


class TestContentCapture:
    async def test_record_scheduled_with_expected_fields(self):
        agent = _agent()
        store = AsyncMock(spec=PromptContentStore)
        agent._prompt_content_store = store
        agent._billing_account_id = "acct-1"
        agent.llm = AsyncMock()
        agent.llm.generate_content = AsyncMock(return_value=_response())

        resp = await agent._call_llm(_request(), turn=2)
        await _drain(agent)

        assert resp.text == "hi there"
        store.store.assert_awaited_once()
        record = store.store.call_args.args[0]
        assert isinstance(record, PromptContentRecord)
        assert record.agent_id == "stub_u1"
        assert record.agent_type == "stub"
        assert record.model == "claude-opus-4-8"
        assert record.turn == 2
        assert record.account_id == "acct-1"
        assert record.response_text == "hi there"
        assert record.prompt_tokens == 10
        assert record.completion_tokens == 5
        assert record.total_tokens == 15
        assert record.latency_ms is not None and record.latency_ms >= 0
        # Request text captures both system instruction and message history.
        assert "you are a test" in record.request_text
        assert "hello" in record.request_text

    async def test_no_store_means_no_scheduling(self):
        agent = _agent()
        agent._prompt_content_store = None
        agent.llm = AsyncMock()
        agent.llm.generate_content = AsyncMock(return_value=_response())

        resp = await agent._call_llm(_request())

        assert resp.text == "hi there"
        assert agent._content_tasks == set()

    async def test_missing_usage_metadata_records_zero_tokens(self):
        agent = _agent()
        store = AsyncMock(spec=PromptContentStore)
        agent._prompt_content_store = store
        agent.llm = AsyncMock()
        agent.llm.generate_content = AsyncMock(return_value=LLMResponse(text="ok"))

        await agent._call_llm(_request())
        await _drain(agent)

        store.store.assert_awaited_once()
        record = store.store.call_args.args[0]
        assert record.prompt_tokens == 0
        assert record.total_tokens == 0
        assert record.response_text == "ok"

    async def test_tool_calls_serialized_to_json(self):
        from src.domain.llm import ToolCall

        agent = _agent()
        store = AsyncMock(spec=PromptContentStore)
        agent._prompt_content_store = store
        agent.llm = AsyncMock()
        agent.llm.generate_content = AsyncMock(
            return_value=LLMResponse(
                text=None,
                tool_calls=[ToolCall(name="search_memory", args={"q": "x"})],
            )
        )

        await agent._call_llm(_request())
        await _drain(agent)

        record = store.store.call_args.args[0]
        assert record.tool_calls is not None
        assert "search_memory" in record.tool_calls
