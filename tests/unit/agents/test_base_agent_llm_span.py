"""
Unit test for the llm.call tracing span emitted by BaseAgent._call_llm.

Uses a real OTel in-memory exporter wired through telemetry.get_tracer (via a
test tracer assigned to the module global, avoiding the process-wide
set_tracer_provider one-shot). Asserts the span carries metadata only — never
prompt/response content (that lives in the PromptContentStore).
"""

from unittest.mock import AsyncMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import src.utils.telemetry as telem
from src.agents.base_agent import BaseAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentResponse
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart, UsageMetadata


class _StubAgent(BaseAgent):
    async def can_handle(self, message: AgentMessage) -> bool:  # pragma: no cover
        return True

    async def execute(self, message: AgentMessage) -> AgentResponse:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def span_exporter(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telem, "_tracer", provider.get_tracer("test"))
    return exporter


def _request() -> LLMRequest:
    return LLMRequest(
        model_name="claude-opus-4-8",
        system_instruction="secret system prompt",
        messages=[Message(role="user", parts=[MessagePart(text="secret user text")])],
    )


def _response() -> LLMResponse:
    return LLMResponse(
        text="secret answer",
        usage_metadata=UsageMetadata(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


async def test_llm_call_span_emitted_with_metadata(span_exporter):
    agent = _StubAgent(config=AgentConfig(agent_id="s_u1", agent_type="smart"))
    agent.llm = AsyncMock()
    agent.llm.generate_content = AsyncMock(return_value=_response())

    await agent._call_llm(_request(), turn=3)

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "llm.call"
    attrs = dict(span.attributes)
    assert attrs["llm.model"] == "claude-opus-4-8"
    assert attrs["llm.agent_type"] == "smart"
    assert attrs["llm.turn"] == 3
    assert attrs["llm.tokens.total"] == 15
    assert attrs["llm.tokens.prompt"] == 10
    assert "llm.latency_ms" in attrs


async def test_span_never_carries_prompt_or_response_content(span_exporter):
    agent = _StubAgent(config=AgentConfig(agent_id="s_u1", agent_type="smart"))
    agent.llm = AsyncMock()
    agent.llm.generate_content = AsyncMock(return_value=_response())

    await agent._call_llm(_request())

    span = span_exporter.get_finished_spans()[0]
    blob = " ".join(str(v) for v in dict(span.attributes).values())
    assert "secret system prompt" not in blob
    assert "secret user text" not in blob
    assert "secret answer" not in blob


async def test_span_emission_failure_never_breaks_call(span_exporter, monkeypatch):
    # A broken tracer must not propagate out of _call_llm.
    broken = AsyncMock()
    broken.start_span = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tracer down"))
    monkeypatch.setattr(telem, "_tracer", broken)

    agent = _StubAgent(config=AgentConfig(agent_id="s_u1", agent_type="smart"))
    agent.llm = AsyncMock()
    agent.llm.generate_content = AsyncMock(return_value=_response())

    resp = await agent._call_llm(_request())
    assert resp.text == "secret answer"
