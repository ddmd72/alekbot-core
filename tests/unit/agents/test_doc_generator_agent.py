"""
Unit tests for DocGeneratorAgent.
"""

import base64
import json
from unittest.mock import AsyncMock

import pytest

from src.agents.doc_generator_agent import DocGeneratorAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.llm import LLMResponse, ToolCall
from src.domain.user import PerformanceTier
from src.ports.docx_runner_port import DocxRunnerError, DocxRunnerPort
from src.ports.llm_port import (
    AgentExecutionContext,
    LLMRequest,
    LLMPort,
    ProviderCapabilities,
)
from src.ports.prompt_builder_port import PromptBuilderPort


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_SPEC = {
    "status": "ready",
    "task_summary": "Test document",
    "doc_spec": {
        "document_type": "report",
        "title": "Test Report",
    },
}


def _make_execution_context(mock_llm) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="doc_generator",
        provider=mock_llm,
        model_name="claude-test",
        tier=PerformanceTier.PERFORMANCE,
        capabilities=ProviderCapabilities(),
    )


def _make_message(
    query: str = None,
    intent: AgentIntent = AgentIntent.QUERY,
) -> AgentMessage:
    if query is None:
        # Simulate the raw JSON string forwarded from DocPlannerAgent.
        query = json.dumps(_VALID_SPEC)
    return AgentMessage(
        intent=intent,
        payload={"query": query},
        sender="doc_planner_agent",
        recipient="doc_generator_agent",
        task_id="task_1",
        context={"user_id": "user123", "account_id": "acc1"},
    )


def _tool_call_response(js_code: str = "console.log('hi')") -> LLMResponse:
    return LLMResponse(
        text=None,
        tool_calls=[ToolCall(name="generate_docx", args={"js_code": js_code})],
    )


def _text_response(text: str = "Done") -> LLMResponse:
    return LLMResponse(text=text, tool_calls=[])


@pytest.fixture
def mock_llm():
    m = AsyncMock(spec=LLMPort)
    m.generate_content.return_value = _tool_call_response()
    return m


@pytest.fixture
def mock_prompt_builder():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "You are a DOC Generator..."
    return pb


@pytest.fixture
def mock_runner():
    return AsyncMock(spec=DocxRunnerPort)


_FAKE_DOCX_BYTES = b"PK\x03\x04fake-docx-content"


@pytest.fixture
def agent(mock_llm, mock_prompt_builder, mock_runner):
    mock_runner.run.return_value = _FAKE_DOCX_BYTES
    config = AgentConfig(agent_id="doc_generator_agent_user123", agent_type="doc_generator")
    return DocGeneratorAgent(
        config=config,
        execution_context=_make_execution_context(mock_llm),
        docx_runner=mock_runner,
        prompt_builder=mock_prompt_builder,
        user_id="user123",
    )


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------

class TestCanHandle:

    async def test_returns_true_with_valid_query(self, agent):
        msg = _make_message()
        assert await agent.can_handle(msg) is True

    async def test_returns_true_with_delegate_intent(self, agent):
        # AgentWorkerHandler sends DELEGATE when executing Cloud Tasks.
        assert await agent.can_handle(_make_message(intent=AgentIntent.DELEGATE)) is True

    async def test_returns_false_empty_query(self, agent):
        msg = _make_message(query="")
        assert await agent.can_handle(msg) is False

    async def test_returns_false_wrong_intent(self, agent):
        msg = _make_message(intent=AgentIntent.INFORM)
        assert await agent.can_handle(msg) is False


# ---------------------------------------------------------------------------
# execute — happy path
# ---------------------------------------------------------------------------

class TestExecuteSuccess:

    async def test_returns_success_status(self, agent):
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.SUCCESS

    async def test_execute_with_delegate_intent_succeeds(self, agent):
        # Regression: AgentWorkerHandler dispatches Cloud Tasks with DELEGATE intent.
        # Generator must execute successfully — not return CANNOT_HANDLE.
        response = await agent.execute(_make_message(intent=AgentIntent.DELEGATE))
        assert response.status == AgentStatus.SUCCESS
        assert len(response.delivery_items) == 1

    async def test_response_has_delivery_items(self, agent):
        response = await agent.execute(_make_message())
        assert len(response.delivery_items) == 1

    async def test_delivery_item_type_is_file_upload(self, agent):
        response = await agent.execute(_make_message())
        assert response.delivery_items[0].type == "file_upload"

    async def test_file_bytes_b64_is_decodable(self, agent):
        response = await agent.execute(_make_message())
        b64 = response.delivery_items[0].data["file_bytes_b64"]
        decoded = base64.b64decode(b64)
        assert decoded == _FAKE_DOCX_BYTES

    async def test_filename_ends_with_docx(self, agent):
        response = await agent.execute(_make_message())
        filename = response.delivery_items[0].data["filename"]
        assert filename.endswith(".docx")

    async def test_filename_includes_document_type(self, agent):
        # doc_spec.document_type = "report" → filename starts with "report-"
        response = await agent.execute(_make_message())
        filename = response.delivery_items[0].data["filename"]
        assert filename.startswith("report-")

    async def test_metadata_contains_model(self, agent):
        response = await agent.execute(_make_message())
        assert response.metadata["model"] == "claude-test"

    async def test_metadata_contains_duration_ms(self, agent):
        response = await agent.execute(_make_message())
        assert "duration_ms" in response.metadata
        assert isinstance(response.metadata["duration_ms"], int)

    async def test_runner_receives_raw_query_as_spec(self, agent, mock_runner):
        # Generator passes raw_query (the planner JSON string) to the runner as spec_json.
        query = json.dumps(_VALID_SPEC)
        await agent.execute(_make_message(query=query))
        call_args = mock_runner.run.call_args
        spec_json_arg = call_args.args[1] if call_args.args else call_args.kwargs.get("spec_json")
        assert spec_json_arg == query


# ---------------------------------------------------------------------------
# execute — failure paths
# ---------------------------------------------------------------------------

class TestExecuteFailure:

    async def test_empty_query_returns_failure(self, agent):
        msg = _make_message(query="")
        response = await agent.execute(msg)
        assert response.status == AgentStatus.FAILED

    async def test_llm_no_tool_call_returns_failure(self, agent, mock_llm):
        mock_llm.generate_content.return_value = _text_response("I cannot do this.")
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.FAILED

    async def test_runner_always_fails_returns_failure(self, agent, mock_runner):
        mock_runner.run.side_effect = DocxRunnerError("node not found")
        # LLM always returns tool call → runner always fails → MAX_TURNS exhausted
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.FAILED

    async def test_prompt_builder_failure_returns_failure(self, agent, mock_prompt_builder):
        mock_prompt_builder.build_for_agent.side_effect = RuntimeError("Firestore error")
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.FAILED
        assert "system prompt" in response.error

    async def test_no_prompt_builder_returns_failure(self, mock_llm, mock_runner):
        config = AgentConfig(agent_id="doc_generator_agent_user123", agent_type="doc_generator")
        agent_no_pb = DocGeneratorAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            docx_runner=mock_runner,
            prompt_builder=None,
            user_id="user123",
        )
        response = await agent_no_pb.execute(_make_message())
        assert response.status == AgentStatus.FAILED


# ---------------------------------------------------------------------------
# execute — LLM call verification
# ---------------------------------------------------------------------------

class TestLLMCall:

    async def test_tools_include_generate_docx(self, agent, mock_llm):
        await agent.execute(_make_message())
        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        tool_names = [t["name"] for t in (req.tools or [])]
        assert "generate_docx" in tool_names

    async def test_temperature_matches_config(self, agent, mock_llm):
        from src.infrastructure.agent_config import DOC_GENERATOR
        await agent.execute(_make_message())
        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        assert req.temperature == DOC_GENERATOR.temperature

    async def test_max_tokens_matches_config(self, agent, mock_llm):
        """Regression test for R14.3: ensure DOC_GENERATOR.max_tokens (64K) is
        forwarded to LLMRequest.max_tokens. Commit ec34bbae (2026-03-16) renamed
        the kwarg to max_output_tokens, which Pydantic silently dropped, leaving
        DocGenerator running at provider defaults (Claude 16K, Gemini 8K) for
        ~46 days. ConfigDict(extra=forbid) on LLMRequest now blocks the same
        regression at construction time."""
        from src.infrastructure.agent_config import DOC_GENERATOR
        await agent.execute(_make_message())
        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        assert req.max_tokens == DOC_GENERATOR.max_tokens
        assert req.max_tokens == 64_000  # canary on the configured value itself

    async def test_raw_query_in_user_message(self, agent, mock_llm):
        # Raw planner JSON is injected into the system prompt as document_spec block.
        # The user message is a fixed "Generate." trigger — not the raw query.
        await agent.execute(_make_message())
        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        assert req.messages[0].parts[0].text == "Generate."
        # doc_spec lives in the system instruction, not the user message
        assert "doc_spec" in req.system_instruction

    async def test_system_prompt_from_builder(self, agent, mock_llm, mock_prompt_builder):
        # System instruction = document_spec block prepended + builder rules appended.
        await agent.execute(_make_message())
        req: LLMRequest = mock_llm.generate_content.call_args.kwargs.get("request") or \
                          mock_llm.generate_content.call_args.args[0]
        assert "document_spec {" in req.system_instruction
        assert "You are a DOC Generator..." in req.system_instruction
