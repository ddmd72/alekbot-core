"""
Unit tests for PdfGeneratorAgent.

Covers:
- can_handle: QUERY/DELEGATE with query → True; empty query/wrong intent → False
- execute success: two delivery_items (HTML + PDF), correct types/content_types/filenames/labels
- execute success: HTML file_upload=False, PDF file_upload=True
- execute success: content_b64 decodes to original bytes
- execute success: filename derived from doc_spec.filename field
- execute success: filename fallback to document_type when filename absent
- execute success: display_name from doc_spec.title; fallback to document_type
- execute retry: PuppeteerRunnerError on turn 1 → retry → succeeds on turn 2
- execute: LLM finishes without tool call → failure (no captured PDF yet)
- execute: MAX_TURNS exhausted → failure
- execute: empty query → failure
- execute: prompt builder failure → failure
- execute: unknown tool name → error tool_response appended, loop continues
- execute: empty html_code arg → error tool_response appended
- LLM call: tools list includes generate_html; temperature and max_tokens match config
- _extract_filename: present field, sanitization, document_type fallback, empty spec
"""
import base64
import json
from unittest.mock import AsyncMock, patch

import pytest

from src.agents.pdf_generator_agent import PdfGeneratorAgent, _extract_filename
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.llm import LLMResponse, ToolCall
from src.domain.user import PerformanceTier
from src.ports.llm_port import (
    AgentExecutionContext,
    LLMPort,
    LLMRequest,
    ProviderCapabilities,
)
from src.ports.prompt_builder_port import PromptBuilderPort
from src.ports.puppeteer_runner_port import PuppeteerRunnerError, PuppeteerRunnerPort


# ============================================================================
# Helpers
# ============================================================================

_FAKE_PDF = b"%PDF-1.4 fake-pdf-content"
_FAKE_HTML = "<html><body><h1>Report</h1></body></html>"

_VALID_SPEC = {
    "status": "ready",
    "task_summary": "Q1 report",
    "doc_spec": {
        "document_type": "report",
        "title": "Q1 Sales Report",
        "filename": "q1_sales_report",
    },
}


def _make_execution_context(mock_llm) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="pdf_generator",
        provider=mock_llm,
        model_name="gemini-flash-test",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(),
    )


def _make_message(
    query: str = None,
    intent: AgentIntent = AgentIntent.DELEGATE,
) -> AgentMessage:
    if query is None:
        query = json.dumps(_VALID_SPEC)
    return AgentMessage(
        intent=intent,
        payload={"query": query},
        sender="pdf_planner_agent",
        recipient="pdf_generator_agent",
        task_id="task_gen_1",
        context={"user_id": "user123", "account_id": "acc1"},
    )


def _tool_call_response(html_code: str = _FAKE_HTML) -> LLMResponse:
    return LLMResponse(
        text=None,
        tool_calls=[ToolCall(name="generate_html", args={"html_code": html_code})],
    )


def _text_response(text: str = "Done") -> LLMResponse:
    return LLMResponse(text=text, tool_calls=[])


def _get_llm_request(mock_llm, call_index: int = 0) -> LLMRequest:
    return (
        mock_llm.generate_content.call_args_list[call_index].kwargs.get("request")
        or mock_llm.generate_content.call_args_list[call_index].args[0]
    )


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_llm():
    m = AsyncMock(spec=LLMPort)
    m.generate_content.return_value = _tool_call_response()
    return m


@pytest.fixture
def mock_prompt_builder():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "You are a PDF Generator..."
    return pb


@pytest.fixture
def mock_runner():
    r = AsyncMock(spec=PuppeteerRunnerPort)
    r.run.return_value = _FAKE_PDF
    return r


@pytest.fixture
def agent(mock_llm, mock_prompt_builder, mock_runner):
    config = AgentConfig(
        agent_id="pdf_generator_agent_user123",
        agent_type="pdf_generator",
    )
    return PdfGeneratorAgent(
        config=config,
        execution_context=_make_execution_context(mock_llm),
        pdf_runner=mock_runner,
        prompt_builder=mock_prompt_builder,
        user_id="user123",
    )


# ============================================================================
# can_handle
# ============================================================================

class TestCanHandle:

    async def test_returns_true_with_delegate_intent(self, agent):
        assert await agent.can_handle(_make_message()) is True

    async def test_returns_true_with_query_intent(self, agent):
        assert await agent.can_handle(_make_message(intent=AgentIntent.QUERY)) is True

    async def test_returns_false_empty_query(self, agent):
        assert await agent.can_handle(_make_message(query="")) is False

    async def test_returns_false_wrong_intent(self, agent):
        msg = _make_message()
        msg.intent = AgentIntent.INFORM
        assert await agent.can_handle(msg) is False

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

    async def test_returns_two_delivery_items(self, agent):
        response = await agent.execute(_make_message())
        assert len(response.delivery_items) == 2

    async def test_first_item_is_html_document(self, agent):
        response = await agent.execute(_make_message())
        html_item = response.delivery_items[0]
        assert html_item.type == "document"
        assert html_item.data["content_type"] == "text/html; charset=utf-8"

    async def test_second_item_is_pdf_document(self, agent):
        response = await agent.execute(_make_message())
        pdf_item = response.delivery_items[1]
        assert pdf_item.type == "document"
        assert pdf_item.data["content_type"] == "application/pdf"

    async def test_html_item_file_upload_false(self, agent):
        response = await agent.execute(_make_message())
        assert response.delivery_items[0].data["file_upload"] is False

    async def test_pdf_item_file_upload_true(self, agent):
        response = await agent.execute(_make_message())
        assert response.delivery_items[1].data["file_upload"] is True

    async def test_html_content_b64_decodes_to_original(self, agent):
        response = await agent.execute(_make_message())
        html_b64 = response.delivery_items[0].data["content_b64"]
        decoded = base64.b64decode(html_b64).decode("utf-8")
        assert decoded == _FAKE_HTML

    async def test_pdf_content_b64_decodes_to_original(self, agent):
        response = await agent.execute(_make_message())
        pdf_b64 = response.delivery_items[1].data["content_b64"]
        decoded = base64.b64decode(pdf_b64)
        assert decoded == _FAKE_PDF

    async def test_html_filename_uses_doc_spec_filename(self, agent):
        response = await agent.execute(_make_message())
        filename = response.delivery_items[0].data["filename"]
        assert filename == "q1_sales_report.html"

    async def test_pdf_filename_uses_doc_spec_filename(self, agent):
        response = await agent.execute(_make_message())
        filename = response.delivery_items[1].data["filename"]
        assert filename == "q1_sales_report.pdf"

    async def test_label_includes_display_name_from_title(self, agent):
        response = await agent.execute(_make_message())
        pdf_label = response.delivery_items[1].data["label"]
        assert "Q1 Sales Report" in pdf_label

    async def test_html_label_ends_with_dot_html(self, agent):
        response = await agent.execute(_make_message())
        assert response.delivery_items[0].data["label"].endswith(".html")

    async def test_pdf_label_ends_with_dot_pdf(self, agent):
        response = await agent.execute(_make_message())
        assert response.delivery_items[1].data["label"].endswith(".pdf")

    async def test_metadata_contains_model(self, agent):
        response = await agent.execute(_make_message())
        assert response.metadata["model"] == "gemini-flash-test"

    async def test_metadata_contains_duration_ms(self, agent):
        response = await agent.execute(_make_message())
        assert "duration_ms" in response.metadata
        assert isinstance(response.metadata["duration_ms"], int)

    async def test_delegate_intent_succeeds(self, agent):
        # Regression: AgentWorkerHandler dispatches Cloud Tasks with DELEGATE intent.
        response = await agent.execute(_make_message(intent=AgentIntent.DELEGATE))
        assert response.status == AgentStatus.SUCCESS

    async def test_runner_receives_html_from_tool_call(self, agent, mock_runner):
        await agent.execute(_make_message())
        call_args = mock_runner.run.call_args
        html_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("html_code")
        assert html_arg == _FAKE_HTML

    async def test_runner_called_once_on_first_success(self, agent, mock_runner):
        await agent.execute(_make_message())
        mock_runner.run.assert_called_once()


# ============================================================================
# execute — filename fallback
# ============================================================================

class TestFilenameFallback:

    async def test_filename_from_document_type_when_no_filename_field(
        self, mock_llm, mock_prompt_builder, mock_runner
    ):
        spec = {
            "status": "ready",
            "task_summary": "Report",
            "doc_spec": {"document_type": "proposal", "title": "My Proposal"},
        }
        config = AgentConfig(agent_id="pdf_gen", agent_type="pdf_generator")
        agent = PdfGeneratorAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            pdf_runner=mock_runner,
            prompt_builder=mock_prompt_builder,
            user_id="user123",
        )
        response = await agent.execute(_make_message(query=json.dumps(spec)))
        assert response.status == AgentStatus.SUCCESS
        pdf_filename = response.delivery_items[1].data["filename"]
        assert pdf_filename == "proposal.pdf"

    async def test_display_name_from_document_type_when_no_title(
        self, mock_llm, mock_prompt_builder, mock_runner
    ):
        spec = {
            "status": "ready",
            "task_summary": "Report",
            "doc_spec": {"document_type": "memo", "filename": "my_memo"},
        }
        config = AgentConfig(agent_id="pdf_gen", agent_type="pdf_generator")
        agent = PdfGeneratorAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            pdf_runner=mock_runner,
            prompt_builder=mock_prompt_builder,
            user_id="user123",
        )
        response = await agent.execute(_make_message(query=json.dumps(spec)))
        assert response.status == AgentStatus.SUCCESS
        pdf_label = response.delivery_items[1].data["label"]
        assert "Memo" in pdf_label or "memo" in pdf_label


# ============================================================================
# execute — retry on Puppeteer error
# ============================================================================

class TestRetry:

    async def test_retry_succeeds_on_second_turn(self, mock_llm, mock_prompt_builder, mock_runner):
        # Turn 1: runner fails. Turn 2: runner succeeds.
        mock_runner.run.side_effect = [
            PuppeteerRunnerError("Puppeteer crash turn 1"),
            _FAKE_PDF,
        ]
        config = AgentConfig(agent_id="pdf_gen", agent_type="pdf_generator")
        agent = PdfGeneratorAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            pdf_runner=mock_runner,
            prompt_builder=mock_prompt_builder,
            user_id="user123",
        )
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.SUCCESS
        assert len(response.delivery_items) == 2

    async def test_retry_sends_error_back_to_llm(self, mock_llm, mock_prompt_builder, mock_runner):
        # After runner failure, agent appends error tool_response to messages and calls LLM again.
        mock_runner.run.side_effect = [
            PuppeteerRunnerError("render failed"),
            _FAKE_PDF,
        ]
        config = AgentConfig(agent_id="pdf_gen", agent_type="pdf_generator")
        agent = PdfGeneratorAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            pdf_runner=mock_runner,
            prompt_builder=mock_prompt_builder,
            user_id="user123",
        )
        await agent.execute(_make_message())
        assert mock_llm.generate_content.call_count == 2

    async def test_max_turns_exhausted_returns_failure(
        self, mock_llm, mock_prompt_builder, mock_runner
    ):
        # Runner always fails → all MAX_TURNS exhausted.
        mock_runner.run.side_effect = PuppeteerRunnerError("always fails")
        config = AgentConfig(agent_id="pdf_gen", agent_type="pdf_generator")
        agent = PdfGeneratorAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            pdf_runner=mock_runner,
            prompt_builder=mock_prompt_builder,
            user_id="user123",
        )
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.FAILED


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

    async def test_no_prompt_builder_returns_failure(self, mock_llm, mock_runner):
        config = AgentConfig(agent_id="pdf_gen", agent_type="pdf_generator")
        agent_no_pb = PdfGeneratorAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            pdf_runner=mock_runner,
            prompt_builder=None,
            user_id="user123",
        )
        response = await agent_no_pb.execute(_make_message())
        assert response.status == AgentStatus.FAILED

    async def test_llm_no_tool_call_returns_failure(self, agent, mock_llm):
        # LLM returns text instead of tool call — failure (no captured PDF yet).
        mock_llm.generate_content.return_value = _text_response("I cannot do this.")
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.FAILED

    async def test_llm_no_tool_call_error_message_informative(self, agent, mock_llm):
        mock_llm.generate_content.return_value = _text_response("Done.")
        response = await agent.execute(_make_message())
        assert response.error and len(response.error) > 0

    async def test_unknown_tool_name_appends_error_to_messages(
        self, mock_llm, mock_prompt_builder, mock_runner
    ):
        # Turn 1: LLM calls unknown tool → error response appended.
        # Turn 2: LLM calls generate_html → success.
        mock_llm.generate_content.side_effect = [
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(name="unknown_tool", args={})],
            ),
            _tool_call_response(),
        ]
        config = AgentConfig(agent_id="pdf_gen", agent_type="pdf_generator")
        agent = PdfGeneratorAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            pdf_runner=mock_runner,
            prompt_builder=mock_prompt_builder,
            user_id="user123",
        )
        response = await agent.execute(_make_message())
        # Second turn should succeed
        assert response.status == AgentStatus.SUCCESS

    async def test_empty_html_code_appends_error_to_messages(
        self, mock_llm, mock_prompt_builder, mock_runner
    ):
        # Turn 1: tool call with empty html_code → error. Turn 2: valid html_code → success.
        mock_llm.generate_content.side_effect = [
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(name="generate_html", args={"html_code": ""})],
            ),
            _tool_call_response(),
        ]
        config = AgentConfig(agent_id="pdf_gen", agent_type="pdf_generator")
        agent = PdfGeneratorAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            pdf_runner=mock_runner,
            prompt_builder=mock_prompt_builder,
            user_id="user123",
        )
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.SUCCESS


# ============================================================================
# execute — LLM call verification
# ============================================================================

class TestLLMCall:

    async def test_tools_include_generate_html(self, agent, mock_llm):
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        tool_names = [t["name"] for t in (req.tools or [])]
        assert "generate_html" in tool_names

    async def test_temperature_matches_config(self, agent, mock_llm):
        from src.infrastructure.agent_config import PDF_GENERATOR
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert req.temperature == PDF_GENERATOR.temperature

    async def test_max_tokens_matches_config(self, agent, mock_llm):
        from src.infrastructure.agent_config import PDF_GENERATOR
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert req.max_tokens == PDF_GENERATOR.max_tokens

    async def test_model_name_passed_to_llm(self, agent, mock_llm):
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert req.model_name == "gemini-flash-test"

    async def test_system_prompt_from_builder(self, agent, mock_llm):
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert req.system_instruction == "You are a PDF Generator..."

    async def test_spec_query_included_in_user_message(self, agent, mock_llm):
        spec_json = json.dumps(_VALID_SPEC)
        await agent.execute(_make_message(query=spec_json))
        req = _get_llm_request(mock_llm)
        user_text = req.messages[0].parts[0].text
        assert "doc_spec" in user_text

    async def test_no_response_mime_type_set(self, agent, mock_llm):
        # Generator uses tool call, not JSON mode — response_mime_type must NOT be set.
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert not req.response_mime_type


# ============================================================================
# _extract_filename (unit)
# ============================================================================

class TestExtractFilename:

    def test_uses_filename_field_when_present(self):
        spec = {"filename": "q1_sales_report", "document_type": "report"}
        assert _extract_filename(spec) == "q1_sales_report"

    def test_sanitizes_spaces_to_underscores(self):
        spec = {"filename": "my report doc"}
        result = _extract_filename(spec)
        assert " " not in result
        assert "_" in result

    def test_sanitizes_special_chars(self):
        spec = {"filename": "report@2026!final"}
        result = _extract_filename(spec)
        for ch in "@!":
            assert ch not in result

    def test_allows_alphanumeric_underscore_hyphen(self):
        spec = {"filename": "report_2026-final"}
        assert _extract_filename(spec) == "report_2026-final"

    def test_falls_back_to_document_type_when_filename_absent(self):
        spec = {"document_type": "proposal"}
        assert _extract_filename(spec) == "proposal"

    def test_falls_back_to_document_type_when_filename_empty(self):
        spec = {"filename": "", "document_type": "memo"}
        assert _extract_filename(spec) == "memo"

    def test_document_type_spaces_replaced_with_underscores(self):
        spec = {"document_type": "business plan"}
        assert _extract_filename(spec) == "business_plan"

    def test_document_type_slashes_replaced(self):
        spec = {"document_type": "report/summary"}
        result = _extract_filename(spec)
        assert "/" not in result

    def test_empty_spec_returns_document(self):
        assert _extract_filename({}) == "document"

    def test_filename_field_takes_priority_over_document_type(self):
        spec = {"filename": "custom_name", "document_type": "report"}
        assert _extract_filename(spec) == "custom_name"
