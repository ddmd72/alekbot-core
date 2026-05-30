"""
Span coverage tests for the delegation tracing tree.

Verifies the two врезки that give orchestrator→specialist hierarchy:
  - DelegationEngine.execute  → `delegation.loop` span (wraps the multi-turn run)
  - AgentCoordinator.handle_delegation → `delegation` span (wraps specialist dispatch)

Spans are captured via a real OTel in-memory exporter assigned to telemetry's
module-global tracer (avoids the process-wide set_tracer_provider one-shot).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import src.utils.telemetry as telem
from src.domain.agent import AgentResponse, AgentStatus
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_registry import AgentDescriptor, AgentRegistry, ExecutionMode
from src.infrastructure.delegation_engine import DelegationEngine


@pytest.fixture
def span_exporter(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telem, "_tracer", provider.get_tracer("test"))
    return exporter


def _spans_named(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == name]


class TestDelegationLoopSpan:
    async def test_execute_emits_delegation_loop_span(self, span_exporter):
        engine = DelegationEngine(coordinator=MagicMock())
        call_llm = AsyncMock(return_value=LLMResponse(text="done"))  # no tools → ends turn 1
        base_request = LLMRequest(
            model_name="m", messages=[Message(role="user", parts=[MessagePart(text="hi")])]
        )

        result = await engine.execute(
            call_llm=call_llm,
            base_request=base_request,
            context={},
            max_turns=3,
            calling_agent_id="smart_u1",
        )

        assert result.text == "done"
        loop_spans = _spans_named(span_exporter, "delegation.loop")
        assert len(loop_spans) == 1
        attrs = dict(loop_spans[0].attributes)
        assert attrs["delegation.agent_id"] == "smart_u1"
        assert attrs["delegation.max_turns"] == 3


class TestDelegationDispatchSpan:
    def _registry(self, intent="search_memory", mode=ExecutionMode.SYNC):
        registry = MagicMock(spec=AgentRegistry)
        desc = AgentDescriptor(
            agent_id="memory_agent",
            capabilities={intent: mode},
            dispatch_deadline_s=300,
        )
        registry.get_agent_for_intent = MagicMock(return_value=desc)
        registry.get_available_intents = MagicMock(return_value=[{"name": intent}])
        return registry

    def _agent(self, agent_id):
        agent = MagicMock()
        agent.agent_id = agent_id
        agent.process = AsyncMock(
            return_value=AgentResponse.success(task_id="t1", agent_id=agent_id, result={"a": 1})
        )
        agent.can_handle = AsyncMock(return_value=True)
        return agent

    async def test_sync_dispatch_emits_delegation_span(self, span_exporter):
        coord = AgentCoordinator(registry=self._registry())
        coord.register_agent(self._agent("memory_agent_user1"))

        response = await coord.handle_delegation(
            "search_memory", "find facts", {"user_id": "user1"}, calling_agent_id="smart_u1"
        )

        assert response.status == AgentStatus.SUCCESS
        spans = _spans_named(span_exporter, "delegation")
        assert len(spans) == 1
        attrs = dict(spans[0].attributes)
        assert attrs["delegation.intent"] == "search_memory"
        assert attrs["delegation.agent_id"] == "memory_agent"
        assert attrs["delegation.calling_agent"] == "smart_u1"

    async def test_unknown_intent_emits_no_span(self, span_exporter):
        registry = MagicMock(spec=AgentRegistry)
        registry.get_agent_for_intent = MagicMock(return_value=None)
        registry.get_available_intents = MagicMock(return_value=[])
        coord = AgentCoordinator(registry=registry)

        await coord.handle_delegation("ghost", "q", {"user_id": "u1"})

        # Error path returns before dispatch — no delegation span.
        assert _spans_named(span_exporter, "delegation") == []
