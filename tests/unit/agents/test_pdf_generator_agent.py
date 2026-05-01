"""
Unit tests for PdfGeneratorAgent (single-LLM-call pipeline).

Covers:
- can_handle: QUERY/DELEGATE with query → True; empty query/wrong intent → False
- execute success: single LLM call, two delivery_items (HTML + PDF)
- execute success: HTML file_upload=False, PDF file_upload=True
- execute success: content_b64 decodes to original bytes
- execute success: filename and display_name extracted from <title> tag
- execute success: fallback filename "document" when <title> absent
- execute success: markdown fences stripped from LLM HTML response
- execute: LLM returns empty string → failure
- execute: Puppeteer error → failure
- execute: empty query → failure
- LLM call: no tools, correct temperature/max_tokens/model_name
- _strip_markdown_fences: various fence patterns
- _extract_filename_from_html: title present, absent, special chars
"""

import base64
import re
from unittest.mock import AsyncMock

import pytest

from src.agents.pdf_generator_agent import (
    PdfGeneratorAgent,
    _extract_filename_from_html,
    _strip_markdown_fences,
)
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.llm import LLMResponse
from src.domain.user import PerformanceTier
from src.ports.llm_port import (
    AgentExecutionContext,
    LLMPort,
    LLMRequest,
    ProviderCapabilities,
)
from src.ports.prompt_builder_port import PromptBuilderPort
from src.ports.puppeteer_runner_port import PuppeteerRunnerError, PuppeteerRunnerPort
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience


# ============================================================================
# Helpers
# ============================================================================

_FAKE_PDF = b"%PDF-1.4 fake-pdf-content"
_FAKE_HTML = "<!DOCTYPE html><html><head><title>Q1 Sales Report</title></head><body><h1>Report</h1></body></html>"
_FAKE_HTML_NO_TITLE = "<!DOCTYPE html><html><body><h1>Report</h1></body></html>"
_QUERY = "Create a Q1 sales report PDF for Acme Corp"


def _make_execution_context(mock_llm) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="pdf_generator",
        provider=mock_llm,
        model_name="gemini-flash-test",
        tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(),
        resilience_port=InMemoryProviderResilience(),
    )


def _make_message(
    query: str = _QUERY,
    intent: AgentIntent = AgentIntent.DELEGATE,
) -> AgentMessage:
    return AgentMessage(
        intent=intent,
        payload={"query": query},
        sender="smart_response_agent",
        recipient="pdf_generator_agent",
        task_id="task_pdf_1",
        context={"user_id": "user123", "account_id": "acc1"},
    )


def _html_response(html: str = _FAKE_HTML) -> LLMResponse:
    return LLMResponse(text=html, tool_calls=[])


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
    m.generate_content.return_value = _html_response()
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

    async def test_filename_extracted_from_title_tag(self, agent):
        response = await agent.execute(_make_message())
        html_filename = response.delivery_items[0].data["filename"]
        pdf_filename = response.delivery_items[1].data["filename"]
        assert html_filename == "q1_sales_report.html"
        assert pdf_filename == "q1_sales_report.pdf"

    async def test_label_uses_raw_title(self, agent):
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
        response = await agent.execute(_make_message(intent=AgentIntent.DELEGATE))
        assert response.status == AgentStatus.SUCCESS

    async def test_single_llm_call_only(self, agent, mock_llm):
        await agent.execute(_make_message())
        assert mock_llm.generate_content.call_count == 1

    async def test_runner_receives_html_from_llm(self, agent, mock_runner):
        await agent.execute(_make_message())
        call_args = mock_runner.run.call_args
        html_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("html_code")
        assert html_arg == _FAKE_HTML

    async def test_runner_called_once(self, agent, mock_runner):
        await agent.execute(_make_message())
        mock_runner.run.assert_called_once()


# ============================================================================
# execute — fallback filename when no <title>
# ============================================================================

class TestFilenameFallback:

    async def test_fallback_filename_document_when_no_title(
        self, mock_llm, mock_prompt_builder, mock_runner
    ):
        mock_llm.generate_content.return_value = _html_response(_FAKE_HTML_NO_TITLE)
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
        assert response.delivery_items[1].data["filename"] == "document.pdf"
        assert response.delivery_items[1].data["label"] == "Document.pdf"

    async def test_title_with_special_chars_sanitized_in_filename(
        self, mock_llm, mock_prompt_builder, mock_runner
    ):
        html = "<!DOCTYPE html><html><head><title>Report: Q1 2025 (Draft)</title></head></html>"
        mock_llm.generate_content.return_value = _html_response(html)
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
        filename = response.delivery_items[1].data["filename"]
        # No spaces, colons, parens in filename
        assert " " not in filename
        assert ":" not in filename
        assert "(" not in filename
        assert filename.endswith(".pdf")


# ============================================================================
# execute — markdown fence stripping
# ============================================================================

class TestMarkdownFenceStripping:

    async def test_fenced_html_is_rendered_correctly(
        self, mock_llm, mock_prompt_builder, mock_runner
    ):
        fenced = f"```html\n{_FAKE_HTML}\n```"
        mock_llm.generate_content.return_value = _html_response(fenced)
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
        # Puppeteer received clean HTML, not fenced
        call_args = mock_runner.run.call_args
        html_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("html_code")
        assert "```" not in html_arg


# ============================================================================
# execute — failure paths
# ============================================================================

class TestExecuteFailure:

    async def test_empty_query_returns_failure(self, agent):
        response = await agent.execute(_make_message(query=""))
        assert response.status == AgentStatus.FAILED

    async def test_llm_empty_response_returns_failure(self, agent, mock_llm):
        mock_llm.generate_content.return_value = LLMResponse(text="", tool_calls=[])
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.FAILED

    async def test_llm_whitespace_only_response_returns_failure(self, agent, mock_llm):
        mock_llm.generate_content.return_value = LLMResponse(text="   \n  ", tool_calls=[])
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.FAILED

    async def test_puppeteer_error_returns_failure(self, agent, mock_runner):
        mock_runner.run.side_effect = PuppeteerRunnerError("Puppeteer crashed")
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.FAILED

    async def test_puppeteer_error_message_in_response(self, agent, mock_runner):
        mock_runner.run.side_effect = PuppeteerRunnerError("render timeout")
        response = await agent.execute(_make_message())
        assert response.error and len(response.error) > 0


# ============================================================================
# execute — LLM call verification
# ============================================================================

class TestLLMCall:

    async def test_no_tools_in_request(self, agent, mock_llm):
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert not req.tools

    async def test_no_response_mime_type(self, agent, mock_llm):
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert not req.response_mime_type

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

    async def test_system_prompt_from_builder_when_available(self, agent, mock_llm):
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert req.system_instruction == "You are a PDF Generator..."

    async def test_query_included_in_user_message(self, agent, mock_llm):
        await agent.execute(_make_message(query=_QUERY))
        req = _get_llm_request(mock_llm)
        user_text = req.messages[0].parts[0].text
        assert _QUERY in user_text


# ============================================================================
# _strip_markdown_fences (unit)
# ============================================================================

class TestStripMarkdownFences:

    def test_no_fences_unchanged(self):
        html = "<!DOCTYPE html><html></html>"
        assert _strip_markdown_fences(html) == html

    def test_strips_triple_backtick_html(self):
        html = "<!DOCTYPE html><html></html>"
        fenced = f"```html\n{html}\n```"
        assert _strip_markdown_fences(fenced) == html

    def test_strips_triple_backtick_no_lang(self):
        html = "<!DOCTYPE html><html></html>"
        fenced = f"```\n{html}\n```"
        result = _strip_markdown_fences(fenced)
        assert "```" not in result

    def test_returns_stripped_content(self):
        html = "<html><body>content</body></html>"
        result = _strip_markdown_fences(f"```html\n{html}\n```")
        assert "html>" in result


# ============================================================================
# _extract_filename_from_html (unit)
# ============================================================================

class TestExtractFilenameFromHtml:

    def test_extracts_title_as_display_name(self):
        html = "<html><head><title>Q1 Report</title></head></html>"
        _, display_name = _extract_filename_from_html(html)
        assert display_name == "Q1 Report"

    def test_sanitizes_title_for_filename(self):
        html = "<html><head><title>Q1 Report: Final</title></head></html>"
        base_filename, _ = _extract_filename_from_html(html)
        assert " " not in base_filename
        assert ":" not in base_filename

    def test_filename_is_lowercase(self):
        html = "<html><head><title>My Report</title></head></html>"
        base_filename, _ = _extract_filename_from_html(html)
        assert base_filename == base_filename.lower()

    def test_fallback_when_no_title(self):
        html = "<html><body>no title here</body></html>"
        base_filename, display_name = _extract_filename_from_html(html)
        assert base_filename == "document"
        assert display_name == "Document"

    def test_fallback_when_empty_title(self):
        html = "<html><head><title></title></head></html>"
        base_filename, display_name = _extract_filename_from_html(html)
        assert base_filename == "document"
        assert display_name == "Document"

    def test_alphanumeric_and_hyphens_preserved(self):
        html = "<html><head><title>report-2025_final</title></head></html>"
        base_filename, _ = _extract_filename_from_html(html)
        assert "report" in base_filename
        assert "2025" in base_filename

    def test_no_consecutive_underscores(self):
        html = "<html><head><title>A: B: C</title></head></html>"
        base_filename, _ = _extract_filename_from_html(html)
        assert "__" not in base_filename
