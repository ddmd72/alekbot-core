"""
Unit tests for PdfPlannerAgent.

Covers:
- can_handle: QUERY/DELEGATE with query → True; empty query/wrong intent → False
- execute: success returns SUCCESS with ack message, no delivery_items
- execute: delegates with GENERATE_PDF_CODE intent, raw LLM text as query, context forwarded
- execute: empty query → failure
- execute: prompt builder failure → failure
- execute: no prompt builder → failure
- LLM call: temperature, max_tokens, response_mime_type, response_schema, model_name, thinking
- Payload merging: extra string payload fields appended to LLM query; intent key excluded
"""
import json
from unittest.mock import AsyncMock

import pytest

from src.agents.pdf_planner_agent import PdfPlannerAgent
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


# ============================================================================
# Helpers
# ============================================================================

_VALID_SPEC = {
    "status": "ready",
    "task_summary": "Q1 Sales PDF report",
    "doc_spec": {
        "document_type": "report",
        "title": "Q1 Sales Report",
        "filename": "q1_sales_report",
    },
}


def _make_execution_context(mock_llm) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="doc_planner_pdf",
        provider=mock_llm,
        model_name="gemini-pro-test",
        tier=PerformanceTier.PERFORMANCE,
        capabilities=ProviderCapabilities(),
    )


def _make_message(
    query: str = "Create a Q1 sales PDF report",
    intent: AgentIntent = AgentIntent.QUERY,
) -> AgentMessage:
    return AgentMessage(
        intent=intent,
        payload={"query": query},
        sender="smart_response_agent",
        recipient="pdf_planner_agent",
        task_id="task_pdf_1",
        context={"user_id": "user123", "account_id": "acc1"},
    )


def _json_llm_response(spec: dict = None) -> LLMResponse:
    return LLMResponse(text=json.dumps(spec or _VALID_SPEC), tool_calls=[])


def _get_llm_request(mock_llm) -> LLMRequest:
    return (
        mock_llm.generate_content.call_args.kwargs.get("request")
        or mock_llm.generate_content.call_args.args[0]
    )


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_llm():
    m = AsyncMock(spec=LLMPort)
    m.generate_content.return_value = _json_llm_response()
    return m


@pytest.fixture
def mock_prompt_builder():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "You are a PDF Layout Planner..."
    return pb


@pytest.fixture
def mock_coordinator():
    c = AsyncMock(spec=AgentCoordinator)
    c.handle_delegation.return_value = AgentResponse.success(
        task_id="gen_task",
        agent_id="coordinator",
        result={"status": "started"},
    )
    return c


@pytest.fixture
def agent(mock_llm, mock_prompt_builder, mock_coordinator):
    config = AgentConfig(
        agent_id="pdf_planner_agent_user123",
        agent_type="doc_planner_pdf",
    )
    return PdfPlannerAgent(
        config=config,
        execution_context=_make_execution_context(mock_llm),
        coordinator=mock_coordinator,
        prompt_builder=mock_prompt_builder,
        user_id="user123",
    )


# ============================================================================
# can_handle
# ============================================================================

class TestCanHandle:

    async def test_returns_true_with_query_intent(self, agent):
        assert await agent.can_handle(_make_message()) is True

    async def test_returns_true_with_delegate_intent(self, agent):
        # AgentWorkerHandler sends DELEGATE for ASYNC Cloud Task execution.
        assert await agent.can_handle(_make_message(intent=AgentIntent.DELEGATE)) is True

    async def test_returns_false_empty_query(self, agent):
        assert await agent.can_handle(_make_message(query="")) is False

    async def test_returns_false_wrong_intent(self, agent):
        assert await agent.can_handle(_make_message(intent=AgentIntent.INFORM)) is False

    async def test_returns_false_missing_query_key(self, agent):
        msg = _make_message()
        msg.payload = {}
        assert await agent.can_handle(msg) is False


# ============================================================================
# execute — success path
# ============================================================================

class TestExecuteSuccess:

    async def test_returns_success_status(self, agent):
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.SUCCESS

    async def test_result_is_ack_message(self, agent):
        # Planner returns an ack; PdfGenerator delivers the document independently.
        response = await agent.execute(_make_message())
        assert isinstance(response.result, str)
        assert len(response.result) > 0

    async def test_delivery_items_empty(self, agent):
        # Fire-and-forget: PdfPlanner does not return delivery_items.
        response = await agent.execute(_make_message())
        assert response.delivery_items == []

    async def test_metadata_contains_model(self, agent):
        response = await agent.execute(_make_message())
        assert response.metadata["model"] == "gemini-pro-test"

    async def test_metadata_contains_duration_ms(self, agent):
        response = await agent.execute(_make_message())
        assert "duration_ms" in response.metadata
        assert isinstance(response.metadata["duration_ms"], int)


# ============================================================================
# execute — coordinator delegation
# ============================================================================

class TestCoordinatorDelegation:

    async def test_handle_delegation_called_once(self, agent, mock_coordinator):
        await agent.execute(_make_message())
        mock_coordinator.handle_delegation.assert_called_once()

    async def test_delegation_uses_generate_pdf_code_intent(self, agent, mock_coordinator):
        await agent.execute(_make_message())
        call_kwargs = mock_coordinator.handle_delegation.call_args.kwargs
        assert call_kwargs.get("intent") == Intent.GENERATE_PDF_CODE

    async def test_delegation_query_is_raw_llm_output(self, agent, mock_coordinator):
        # Planner forwards the raw LLM JSON string as-is to the generator.
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

    async def test_delegation_calling_agent_id_set(self, agent, mock_coordinator):
        await agent.execute(_make_message())
        call_kwargs = mock_coordinator.handle_delegation.call_args.kwargs
        assert call_kwargs.get("calling_agent_id") == agent.agent_id


# ============================================================================
# execute — failure paths
# ============================================================================

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
        config = AgentConfig(
            agent_id="pdf_planner_agent_user123",
            agent_type="doc_planner_pdf",
        )
        agent_no_pb = PdfPlannerAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            coordinator=mock_coordinator,
            prompt_builder=None,
            user_id="user123",
        )
        response = await agent_no_pb.execute(_make_message())
        assert response.status == AgentStatus.FAILED


# ============================================================================
# execute — LLM call verification
# ============================================================================

class TestLLMCall:

    async def test_temperature_matches_config(self, agent, mock_llm):
        from src.infrastructure.agent_config import PDF_PLANNER
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert req.temperature == PDF_PLANNER.temperature

    async def test_max_tokens_matches_config(self, agent, mock_llm):
        from src.infrastructure.agent_config import PDF_PLANNER
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert req.max_tokens == PDF_PLANNER.max_tokens

    async def test_response_mime_type_is_json(self, agent, mock_llm):
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert req.response_mime_type == "application/json"

    async def test_response_schema_enforces_required_fields(self, agent, mock_llm):
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert req.response_schema is not None
        assert "status" in req.response_schema.get("required", [])
        assert "task_summary" in req.response_schema.get("required", [])
        assert "doc_spec" in req.response_schema.get("required", [])

    async def test_model_name_passed_to_llm(self, agent, mock_llm):
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert req.model_name == "gemini-pro-test"

    async def test_thinking_matches_config(self, agent, mock_llm):
        from src.infrastructure.agent_config import PDF_PLANNER
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert req.thinking == (PDF_PLANNER.thinking_effort or None)

    async def test_query_included_in_user_message(self, agent, mock_llm):
        await agent.execute(_make_message(query="Create a technical report"))
        req = _get_llm_request(mock_llm)
        user_text = req.messages[0].parts[0].text
        assert "Create a technical report" in user_text

    async def test_system_prompt_from_builder(self, agent, mock_llm):
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert req.system_instruction == "You are a PDF Layout Planner..."


# ============================================================================
# execute — payload merging
# ============================================================================

class TestPayloadMerging:

    def _get_llm_query(self, mock_llm) -> str:
        req = _get_llm_request(mock_llm)
        return req.messages[0].parts[0].text

    def _make_message_with_extra(self, **extra_fields) -> AgentMessage:
        return AgentMessage(
            intent=AgentIntent.DELEGATE,
            payload={"query": "Create a PDF report", "intent": Intent.CREATE_PDF, **extra_fields},
            sender="worker",
            recipient="pdf_planner_agent",
            task_id="task_x",
            context={"user_id": "user123", "account_id": "acc1"},
        )

    async def test_extra_string_payload_appended_to_query(self, agent, mock_llm):
        msg = self._make_message_with_extra(research_content="Research data here...")
        await agent.execute(msg)
        query = self._get_llm_query(mock_llm)
        assert "Create a PDF report" in query
        assert "Research data here..." in query

    async def test_multiple_extra_fields_all_appended(self, agent, mock_llm):
        msg = self._make_message_with_extra(section_a="Part A", section_b="Part B")
        await agent.execute(msg)
        query = self._get_llm_query(mock_llm)
        assert "Part A" in query
        assert "Part B" in query

    async def test_non_string_extra_fields_excluded(self, agent, mock_llm):
        # Only string values are forwarded to LLM; ints/dicts are silently skipped.
        msg = self._make_message_with_extra(count=42, meta={"key": "val"})
        await agent.execute(msg)
        query = self._get_llm_query(mock_llm)
        assert "42" not in query

    async def test_intent_field_excluded_from_merge(self, agent, mock_llm):
        # "intent" key must never be forwarded to the LLM as content.
        msg = self._make_message_with_extra()
        await agent.execute(msg)
        query = self._get_llm_query(mock_llm)
        assert Intent.CREATE_PDF not in query
