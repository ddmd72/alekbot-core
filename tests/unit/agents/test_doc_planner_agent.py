"""
Unit tests for DocPlannerAgent.
"""

import json
from unittest.mock import AsyncMock

import pytest

from src.agents.doc_planner_agent import DocPlannerAgent
from src.domain.agent import (
    AgentConfig,
    AgentIntent,
    AgentMessage,
    AgentResponse,
    AgentStatus,
)
from src.domain.llm import LLMResponse
from src.domain.user import PerformanceTier
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import Intent
from src.ports.llm_port import (
    AgentExecutionContext,
    LLMPort,
    LLMRequest,
    ProviderCapabilities,
)
from src.ports.prompt_builder_port import PromptBuilderPort


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_SPEC = {
    "status": "ready",
    "task_summary": "Quarterly sales report",
    "doc_spec": {
        "document_type": "report",
        "title": "Sales Report Q1",
    },
}


def _make_execution_context(mock_llm) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="doc_planner",
        provider=mock_llm,
        model_name="gemini-test",
        tier=PerformanceTier.PERFORMANCE,
        capabilities=ProviderCapabilities(),
    )


def _make_message(
    query: str = "Create a quarterly sales report",
    intent: AgentIntent = AgentIntent.QUERY,
) -> AgentMessage:
    return AgentMessage(
        intent=intent,
        payload={"query": query},
        sender="quick_response_agent",
        recipient="doc_planner_agent",
        task_id="task_1",
        context={"user_id": "user123", "account_id": "acc1"},
    )


def _json_llm_response(spec: dict = None) -> LLMResponse:
    if spec is None:
        spec = _VALID_SPEC
    return LLMResponse(text=json.dumps(spec), tool_calls=[])


@pytest.fixture
def mock_llm():
    m = AsyncMock(spec=LLMPort)
    m.generate_content.return_value = _json_llm_response()
    return m


@pytest.fixture
def mock_prompt_builder():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "You are a DOC Layout Planner..."
    return pb


@pytest.fixture
def mock_coordinator():
    c = AsyncMock(spec=AgentCoordinator)
    # Planner enqueues generator fire-and-forget; return value is not used.
    c.handle_delegation.return_value = None
    return c


@pytest.fixture
def agent(mock_llm, mock_prompt_builder, mock_coordinator):
    config = AgentConfig(agent_id="doc_planner_agent_user123", agent_type="doc_planner")
    return DocPlannerAgent(
        config=config,
        execution_context=_make_execution_context(mock_llm),
        coordinator=mock_coordinator,
        prompt_builder=mock_prompt_builder,
        user_id="user123",
    )


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------

class TestCanHandle:

    async def test_returns_true_with_query(self, agent):
        assert await agent.can_handle(_make_message()) is True

    async def test_returns_true_with_delegate_intent(self, agent):
        # DELEGATE is sent by AgentWorkerHandler for ASYNC Cloud Task execution.
        assert await agent.can_handle(_make_message(intent=AgentIntent.DELEGATE)) is True

    async def test_returns_false_empty_query(self, agent):
        assert await agent.can_handle(_make_message(query="")) is False

    async def test_returns_false_wrong_intent(self, agent):
        assert await agent.can_handle(_make_message(intent=AgentIntent.INFORM)) is False


# ---------------------------------------------------------------------------
# execute — happy path
# ---------------------------------------------------------------------------

class TestExecuteSuccess:

    async def test_returns_success_status(self, agent):
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.SUCCESS

    async def test_result_indicates_generation_started(self, agent):
        response = await agent.execute(_make_message())
        assert "generation started" in response.result.lower()

    async def test_no_delivery_items(self, agent):
        # Planner returns immediately — DOCX delivery comes from generator Cloud Task.
        response = await agent.execute(_make_message())
        assert response.delivery_items == []

    async def test_metadata_contains_model(self, agent):
        response = await agent.execute(_make_message())
        assert response.metadata["model"] == "gemini-test"

    async def test_metadata_contains_duration_ms(self, agent):
        response = await agent.execute(_make_message())
        assert "duration_ms" in response.metadata
        assert isinstance(response.metadata["duration_ms"], int)


# ---------------------------------------------------------------------------
# execute — coordinator delegation
# ---------------------------------------------------------------------------

class TestCoordinatorDelegation:

    async def test_handle_delegation_called_once(self, agent, mock_coordinator):
        await agent.execute(_make_message())
        mock_coordinator.handle_delegation.assert_called_once()

    async def test_delegation_uses_generate_docx_intent(self, agent, mock_coordinator):
        await agent.execute(_make_message())
        call_kwargs = mock_coordinator.handle_delegation.call_args.kwargs
        assert call_kwargs.get("intent") == Intent.GENERATE_DOCX_CODE

    async def test_delegation_query_is_raw_llm_output(self, agent, mock_coordinator):
        # Planner forwards the raw LLM text (JSON string) as-is to the generator.
        await agent.execute(_make_message())
        call_kwargs = mock_coordinator.handle_delegation.call_args.kwargs
        delegated_query = call_kwargs.get("query", "")
        parsed = json.loads(delegated_query)
        assert parsed["status"] == "ready"
        assert "doc_spec" in parsed

    async def test_delegation_passes_context(self, agent, mock_coordinator):
        await agent.execute(_make_message())
        call_kwargs = mock_coordinator.handle_delegation.call_args.kwargs
        assert "account_id" in call_kwargs.get("context", {})


# ---------------------------------------------------------------------------
# execute — failure paths
# ---------------------------------------------------------------------------

class TestExecuteFailure:

    async def test_empty_query_returns_failure(self, agent):
        response = await agent.execute(_make_message(query=""))
        assert response.status == AgentStatus.FAILED

    async def test_prompt_builder_failure_returns_failure(self, agent, mock_prompt_builder):
        mock_prompt_builder.build_for_agent.side_effect = RuntimeError("Firestore down")
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.FAILED
        assert "system prompt" in response.error

    async def test_no_prompt_builder_returns_failure(self, mock_llm, mock_coordinator):
        config = AgentConfig(agent_id="doc_planner_agent_user123", agent_type="doc_planner")
        agent_no_pb = DocPlannerAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            coordinator=mock_coordinator,
            prompt_builder=None,
            user_id="user123",
        )
        response = await agent_no_pb.execute(_make_message())
        assert response.status == AgentStatus.FAILED


# ---------------------------------------------------------------------------
# execute — LLM call verification
# ---------------------------------------------------------------------------

class TestLLMCall:

    async def test_temperature_matches_config(self, agent, mock_llm):
        from src.infrastructure.agent_config import DOC_PLANNER
        await agent.execute(_make_message())
        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        assert req.temperature == DOC_PLANNER.temperature

    async def test_response_mime_type_is_json(self, agent, mock_llm):
        await agent.execute(_make_message())
        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        assert req.response_mime_type == "application/json"

    async def test_response_schema_is_set(self, agent, mock_llm):
        await agent.execute(_make_message())
        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        assert req.response_schema is not None
        assert req.response_schema.get("required") == ["status", "task_summary", "doc_spec"]

    async def test_query_in_user_message(self, agent, mock_llm):
        await agent.execute(_make_message(query="Write a proposal"))
        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        user_text = req.messages[0].parts[0].text
        assert "Write a proposal" in user_text

    async def test_system_prompt_from_builder(self, agent, mock_llm):
        await agent.execute(_make_message())
        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        assert req.system_instruction == "You are a DOC Layout Planner..."
