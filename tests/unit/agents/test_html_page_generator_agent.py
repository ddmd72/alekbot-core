"""
Unit tests for HtmlPageGeneratorAgent (single-LLM-call pipeline).

Covers:
- can_handle: QUERY/DELEGATE with query → True; empty query/wrong intent → False
- execute success: single LLM call, one delivery_item (HTML)
- execute success: HTML file_upload=False
- execute success: content_b64 decodes to original HTML
- execute success: filename and display_name extracted from <title> tag
- execute success: fallback filename "page" when <title> absent
- execute success: markdown fences stripped from LLM HTML response
- execute: LLM returns empty string → failure
- execute: empty query → failure
- execute: prompt_builder failure → failure
- LLM call: no tools, correct temperature/max_tokens/model_name
- _strip_markdown_fences: various fence patterns
- _extract_filename_from_html: title present, absent, special chars
"""

import base64
from unittest.mock import AsyncMock

import pytest

from src.agents.html_page_generator_agent import (
    HtmlPageGeneratorAgent,
    _extract_filename_from_html,
    _resolve_unsplash_placeholders,
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


# ============================================================================
# Helpers
# ============================================================================

_FAKE_HTML = "<!DOCTYPE html><html><head><title>Product Landing Page</title></head><body><h1>Hero</h1></body></html>"
_FAKE_HTML_NO_TITLE = "<!DOCTYPE html><html><body><h1>No Title</h1></body></html>"
_QUERY = "Create a landing page for a SaaS task management product"


def _make_execution_context(mock_llm) -> AgentExecutionContext:
    return AgentExecutionContext(
        agent_type="html_page",
        provider=mock_llm,
        model_name="gemini-pro-test",
        tier=PerformanceTier.PERFORMANCE,
        capabilities=ProviderCapabilities(),
    )


def _make_message(
    query: str = _QUERY,
    intent: AgentIntent = AgentIntent.DELEGATE,
) -> AgentMessage:
    return AgentMessage(
        intent=intent,
        payload={"query": query},
        sender="smart_response_agent",
        recipient="html_page_generator_agent",
        task_id="task_html_1",
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
    pb.build_for_agent.return_value = "You are an HTML Page Generator..."
    return pb


@pytest.fixture
def agent(mock_llm, mock_prompt_builder):
    config = AgentConfig(
        agent_id="html_page_generator_agent_user123",
        agent_type="html_page",
    )
    return HtmlPageGeneratorAgent(
        config=config,
        execution_context=_make_execution_context(mock_llm),
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

    async def test_returns_exactly_one_delivery_item(self, agent):
        response = await agent.execute(_make_message())
        assert len(response.delivery_items) == 1

    async def test_delivery_item_is_document_type(self, agent):
        response = await agent.execute(_make_message())
        assert response.delivery_items[0].type == "document"

    async def test_delivery_item_content_type_html(self, agent):
        response = await agent.execute(_make_message())
        assert response.delivery_items[0].data["content_type"] == "text/html; charset=utf-8"

    async def test_html_item_file_upload_false(self, agent):
        response = await agent.execute(_make_message())
        assert response.delivery_items[0].data["file_upload"] is False

    async def test_content_b64_decodes_to_original_html(self, agent):
        response = await agent.execute(_make_message())
        html_b64 = response.delivery_items[0].data["content_b64"]
        decoded = base64.b64decode(html_b64).decode("utf-8")
        assert decoded == _FAKE_HTML

    async def test_filename_extracted_from_title_tag(self, agent):
        response = await agent.execute(_make_message())
        filename = response.delivery_items[0].data["filename"]
        assert filename == "product_landing_page.html"

    async def test_label_uses_raw_title(self, agent):
        response = await agent.execute(_make_message())
        label = response.delivery_items[0].data["label"]
        assert "Product Landing Page" in label

    async def test_label_ends_with_dot_html(self, agent):
        response = await agent.execute(_make_message())
        assert response.delivery_items[0].data["label"].endswith(".html")

    async def test_metadata_contains_model(self, agent):
        response = await agent.execute(_make_message())
        assert response.metadata["model"] == "gemini-pro-test"

    async def test_metadata_contains_duration_ms(self, agent):
        response = await agent.execute(_make_message())
        assert "duration_ms" in response.metadata
        assert isinstance(response.metadata["duration_ms"], int)

    async def test_single_llm_call_only(self, agent, mock_llm):
        await agent.execute(_make_message())
        assert mock_llm.generate_content.call_count == 1

    async def test_prompt_builder_called_with_html_page_agent_type(self, agent, mock_prompt_builder):
        await agent.execute(_make_message())
        call_kwargs = mock_prompt_builder.build_for_agent.call_args.kwargs
        assert call_kwargs.get("agent_type") == "html_page"


# ============================================================================
# execute — fallback filename when no <title>
# ============================================================================

class TestFilenameFallback:

    async def test_fallback_filename_page_when_no_title(
        self, mock_llm, mock_prompt_builder
    ):
        mock_llm.generate_content.return_value = _html_response(_FAKE_HTML_NO_TITLE)
        config = AgentConfig(agent_id="html_page_gen", agent_type="html_page")
        agent = HtmlPageGeneratorAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            prompt_builder=mock_prompt_builder,
            user_id="user123",
        )
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.SUCCESS
        assert response.delivery_items[0].data["filename"] == "page.html"
        assert response.delivery_items[0].data["label"] == "Page.html"

    async def test_title_with_special_chars_sanitized_in_filename(
        self, mock_llm, mock_prompt_builder
    ):
        html = "<!DOCTYPE html><html><head><title>App: Launch 2025 (Beta)</title></head></html>"
        mock_llm.generate_content.return_value = _html_response(html)
        config = AgentConfig(agent_id="html_page_gen", agent_type="html_page")
        agent = HtmlPageGeneratorAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            prompt_builder=mock_prompt_builder,
            user_id="user123",
        )
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.SUCCESS
        filename = response.delivery_items[0].data["filename"]
        assert " " not in filename
        assert ":" not in filename
        assert "(" not in filename
        assert filename.endswith(".html")


# ============================================================================
# execute — markdown fence stripping
# ============================================================================

class TestMarkdownFenceStripping:

    async def test_fenced_html_is_decoded_correctly(
        self, mock_llm, mock_prompt_builder
    ):
        fenced = f"```html\n{_FAKE_HTML}\n```"
        mock_llm.generate_content.return_value = _html_response(fenced)
        config = AgentConfig(agent_id="html_page_gen", agent_type="html_page")
        agent = HtmlPageGeneratorAgent(
            config=config,
            execution_context=_make_execution_context(mock_llm),
            prompt_builder=mock_prompt_builder,
            user_id="user123",
        )
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.SUCCESS
        html_b64 = response.delivery_items[0].data["content_b64"]
        decoded = base64.b64decode(html_b64).decode("utf-8")
        assert "```" not in decoded


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

    async def test_prompt_builder_exception_returns_failure(self, agent, mock_prompt_builder):
        mock_prompt_builder.build_for_agent.side_effect = RuntimeError("Firestore unavailable")
        response = await agent.execute(_make_message())
        assert response.status == AgentStatus.FAILED

    async def test_failure_response_has_error_message(self, agent, mock_llm):
        mock_llm.generate_content.return_value = LLMResponse(text="", tool_calls=[])
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
        from src.infrastructure.agent_config import HTML_PAGE_GENERATOR
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert req.temperature == HTML_PAGE_GENERATOR.temperature

    async def test_max_tokens_matches_config(self, agent, mock_llm):
        from src.infrastructure.agent_config import HTML_PAGE_GENERATOR
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert req.max_tokens == HTML_PAGE_GENERATOR.max_tokens

    async def test_model_name_passed_to_llm(self, agent, mock_llm):
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert req.model_name == "gemini-pro-test"

    async def test_system_prompt_from_builder(self, agent, mock_llm):
        await agent.execute(_make_message())
        req = _get_llm_request(mock_llm)
        assert req.system_instruction == "You are an HTML Page Generator..."

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
        html = "<html><head><title>My Landing Page</title></head></html>"
        _, display_name = _extract_filename_from_html(html)
        assert display_name == "My Landing Page"

    def test_sanitizes_title_for_filename(self):
        html = "<html><head><title>App: Launch Page</title></head></html>"
        base_filename, _ = _extract_filename_from_html(html)
        assert " " not in base_filename
        assert ":" not in base_filename

    def test_filename_is_lowercase(self):
        html = "<html><head><title>My Page</title></head></html>"
        base_filename, _ = _extract_filename_from_html(html)
        assert base_filename == base_filename.lower()

    def test_fallback_when_no_title(self):
        html = "<html><body>no title here</body></html>"
        base_filename, display_name = _extract_filename_from_html(html)
        assert base_filename == "page"
        assert display_name == "Page"

    def test_fallback_when_empty_title(self):
        html = "<html><head><title></title></head></html>"
        base_filename, display_name = _extract_filename_from_html(html)
        assert base_filename == "page"
        assert display_name == "Page"

    def test_alphanumeric_and_hyphens_preserved(self):
        html = "<html><head><title>launch-2025_final</title></head></html>"
        base_filename, _ = _extract_filename_from_html(html)
        assert "launch" in base_filename
        assert "2025" in base_filename

    def test_no_consecutive_underscores(self):
        html = "<html><head><title>A: B: C</title></head></html>"
        base_filename, _ = _extract_filename_from_html(html)
        assert "__" not in base_filename


# ============================================================================
# _resolve_unsplash_placeholders
# ============================================================================

async def test_raw_url_with_existing_query_uses_ampersand():
    """raw_url from Unsplash API already has ?ixid=...&ixlib=... — must use & not ? for sizing."""
    from src.ports.image_search_port import ImageResult, ImageSearchPort
    mock = AsyncMock(spec=ImageSearchPort)
    raw_url = "https://images.unsplash.com/photo-abc?ixid=abc&ixlib=rb-4.1.0"
    mock.search.return_value = [ImageResult(
        url=raw_url, raw_url=raw_url,
        photographer="Jane", photographer_url="https://unsplash.com/@jane",
    )]
    html = '<img src="https://source.unsplash.com/150x150/?hacker,hoodie">'
    result = await _resolve_unsplash_placeholders(html, mock)
    assert "?w=" not in result
    assert "&w=150" in result
    assert "&h=150" in result
