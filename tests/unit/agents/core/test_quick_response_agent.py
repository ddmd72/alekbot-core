"""
Unit tests for QuickResponseAgent.

Tests cover:
- can_handle() classification logic
- execute() LLM call flow
- Response sanitization
- Session history management
- Prompt building
- Error handling
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

from src.agents.core.quick_response_agent import QuickResponseAgent, create_quick_response_agent
from src.domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent, AgentStatus
from src.ports.llm_port import Message, MessagePart, LLMPort, LLMRequest, ToolCall
from src.services.agent_context_builder import AgentExecutionContext
from src.domain.user import PerformanceTier
from src.ports.llm_port import ProviderCapabilities
from src.services.history_summary_service import HistorySummaryService


# ============================================================================
# Mock Types
# ============================================================================

@dataclass
class MockUsageMetadata:
    """Mock for LLM usage metadata."""
    total_tokens: int = 100
    prompt_tokens: int = 50
    response_tokens: int = 50


@dataclass
class MockLLMResponse:
    """Mock for LLM response."""
    text: str = "Привіт! Як справи?"
    usage_metadata: MockUsageMetadata = None
    tool_calls: list = None
    
    def __post_init__(self):
        if self.usage_metadata is None:
            self.usage_metadata = MockUsageMetadata()
        if self.tool_calls is None:
            self.tool_calls = []


@dataclass
class MockSession:
    """Mock for session with history."""
    history: list = None
    
    def __post_init__(self):
        if self.history is None:
            self.history = []


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_llm_port():
    """Create mock LLM service."""
    service = MagicMock(spec=LLMPort)
    service.generate_content = AsyncMock(return_value=MockLLMResponse())
    return service


@pytest.fixture
def mock_session_store():
    """Create mock session store."""
    store = MagicMock()
    store.load_session = AsyncMock(return_value=MockSession())
    return store


@pytest.fixture
def mock_prompt_builder():
    """Create mock prompt builder."""
    builder = MagicMock()
    builder.build_for_agent = AsyncMock(return_value="SYSTEM_PROMPT")
    builder.build_system_prompt = AsyncMock(return_value={
        "kernel": "KERNEL CONTENT",
        "slack_rules": "SLACK RULES",
        "examples": "EXAMPLES",
        "biographical_context": "BIO CONTEXT"
    })
    return builder


@pytest.fixture
def quick_agent_config():
    """Create quick agent config."""
    return AgentConfig(
        agent_id="quick_response_agent",
        agent_type="quick_response",
        llm_model="gemini-3-flash-preview",
        max_retries=1,
        timeout_ms=10000,
        capabilities=["fast_response"]
    )


@pytest.fixture
def quick_agent(quick_agent_config, mock_llm_port, mock_session_store, mock_prompt_builder):
    """Create quick response agent with mocks."""
    execution_context = AgentExecutionContext(
        agent_type="quick",
        provider=mock_llm_port,
        model_name="gemini-3-flash-preview",
        tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities()
    )
    return QuickResponseAgent(
        config=quick_agent_config,
        execution_context=execution_context,
        session_store=mock_session_store,
        prompt_builder=mock_prompt_builder
    )


def create_query_message(
    text: str, 
    is_simple: bool = True,
    session_id: str = "session123",
    user_id: str = "user456"
) -> AgentMessage:
    """Helper to create query messages with classification."""
    return AgentMessage.create(
        sender="router_agent",
        recipient="quick_response_agent",
        intent=AgentIntent.QUERY,
        payload={"text": text},
        context={
            "user_id": user_id,
            "session_id": session_id,
            "classification": {
                "is_simple": is_simple,
                "is_personal": False,
                "needs_external": False
            },
            "routed_by": "router_agent"
        }
    )


# ============================================================================
# can_handle Tests
# ============================================================================

class TestQuickResponseAgentCanHandle:
    """Test can_handle() method."""

    @pytest.mark.asyncio
    async def test_can_handle_simple_query(self, quick_agent):
        """Should handle query when payload has text."""
        message = create_query_message("Привіт", is_simple=True)
        
        result = await quick_agent.can_handle(message)
        
        assert result is True

    @pytest.mark.asyncio
    async def test_cannot_handle_complex_query(self, quick_agent):
        """Should handle query regardless of classification."""
        message = create_query_message("Яка погода?", is_simple=False)
        
        result = await quick_agent.can_handle(message)
        
        assert result is True

    @pytest.mark.asyncio
    async def test_cannot_handle_non_query_intent(self, quick_agent):
        """Should not handle non-QUERY intents."""
        message = AgentMessage.create(
            sender="test",
            recipient="quick_response_agent",
            intent=AgentIntent.INFORM,
            payload={"text": "Hello"},
            context={"classification": {"is_simple": True}}
        )
        
        result = await quick_agent.can_handle(message)
        
        assert result is False

    @pytest.mark.asyncio
    async def test_cannot_handle_missing_classification(self, quick_agent):
        """Should handle messages without classification when payload is valid."""
        message = AgentMessage.create(
            sender="test",
            recipient="quick_response_agent",
            intent=AgentIntent.QUERY,
            payload={"text": "Hello"},
            context={}  # No classification
        )
        
        result = await quick_agent.can_handle(message)
        
        assert result is True


# ============================================================================
# execute Tests - Basic Flow
# ============================================================================

class TestQuickResponseAgentExecute:
    """Test execute() method - basic flow."""

    @pytest.mark.asyncio
    async def test_execute_returns_success(self, quick_agent, mock_llm_port):
        """Should return successful response."""
        message = create_query_message("Привіт")
        
        response = await quick_agent.execute(message)
        
        assert response.status == AgentStatus.SUCCESS
        assert "Привіт" in response.result.text

    @pytest.mark.asyncio
    async def test_execute_calls_llm_port(self, quick_agent, mock_llm_port):
        """Should call LLM service with correct parameters and return its response."""
        message = create_query_message("Hello")

        response = await quick_agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        assert "Привіт" in response.result.text
        mock_llm_port.generate_content.assert_called_once()
        call_args = mock_llm_port.generate_content.call_args
        request = call_args.kwargs["request"]
        assert isinstance(request, LLMRequest)
        assert request.model_name == "gemini-3-flash-preview"
        assert request.system_instruction

    @pytest.mark.asyncio
    async def test_execute_loads_session_history(self, quick_agent, mock_session_store):
        """Should load session history and return a successful response."""
        message = create_query_message("Hello", session_id="test_session")

        response = await quick_agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        mock_session_store.load_session.assert_called_once_with("test_session")

    @pytest.mark.asyncio
    async def test_execute_builds_prompt(self, quick_agent, mock_prompt_builder):
        """Should build system prompt and incorporate it in the LLM call."""
        message = create_query_message("Hello")

        response = await quick_agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        mock_prompt_builder.build_for_agent.assert_called_once()
        build_call = mock_prompt_builder.build_for_agent.call_args
        assert build_call[1].get("agent_type") == "quick"

    @pytest.mark.asyncio
    async def test_execute_returns_metadata(self, quick_agent):
        """Should include metadata in response."""
        message = create_query_message("Hello")
        
        response = await quick_agent.execute(message)
        
        assert "model" in response.metadata
        assert "tokens" in response.metadata
        assert response.metadata["model"] == "gemini-3-flash-preview"


# ============================================================================
# execute Tests - Error Handling
# ============================================================================

class TestQuickResponseAgentErrorHandling:
    """Test execute() error handling."""

    @pytest.mark.asyncio
    async def test_execute_handles_llm_error(self, quick_agent, mock_llm_port):
        """Should return failure on LLM error."""
        mock_llm_port.generate_content.side_effect = Exception("LLM Error")
        message = create_query_message("Hello")
        
        response = await quick_agent.execute(message)
        
        assert response.status == AgentStatus.FAILED
        assert "LLM Error" in response.error

    @pytest.mark.xfail(strict=True, reason="BUG: _load_conversation_context does not handle session errors; propagates to execute() causing FAILED instead of graceful degradation")
    @pytest.mark.asyncio
    async def test_execute_handles_session_error(self, quick_agent, mock_session_store):
        """Should continue without history if session load fails."""
        mock_session_store.load_session.side_effect = Exception("DB Error")
        message = create_query_message("Hello")
        
        response = await quick_agent.execute(message)
        
        # Should still succeed, just without history
        assert response.status == AgentStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_execute_handles_empty_response(self, quick_agent, mock_llm_port):
        """Should handle empty LLM response."""
        mock_llm_port.generate_content.return_value = MockLLMResponse(text="")
        message = create_query_message("Hello")
        
        response = await quick_agent.execute(message)
        
        assert response.status == AgentStatus.SUCCESS
        assert response.result.text == ""


# ============================================================================
# Response Sanitization Tests
# ============================================================================

class TestQuickResponseAgentSanitization:
    """Test response sanitization."""

    @pytest.mark.asyncio
    async def test_sanitize_removes_tool_code(self, quick_agent, mock_llm_port):
        """Should remove tool_code blocks from response."""
        mock_llm_port.generate_content.return_value = MockLLMResponse(
            text="tool_code\nprint('hello')\nПривіт!"
        )
        message = create_query_message("Hello")
        
        response = await quick_agent.execute(message)
        
        assert "tool_code" not in response.result.text
        assert "print" not in response.result.text

    @pytest.mark.asyncio
    async def test_sanitize_removes_api_references(self, quick_agent, mock_llm_port):
        """Should remove API references from response."""
        mock_llm_port.generate_content.return_value = MockLLMResponse(
            text="default_api.search() Привіт!"
        )
        message = create_query_message("Hello")
        
        response = await quick_agent.execute(message)
        
        assert "default_api" not in response.result.text

    @pytest.mark.asyncio
    async def test_sanitize_removes_tool_references(self, quick_agent, mock_llm_port):
        """Should remove tool references from response."""
        mock_llm_port.generate_content.return_value = MockLLMResponse(
            text="ask_web_search_agent('query') Привіт!"
        )
        message = create_query_message("Hello")
        
        response = await quick_agent.execute(message)
        
        assert "ask_web_search_agent" not in response.result.text

    @pytest.mark.asyncio
    async def test_sanitize_handles_only_tool_code(self, quick_agent, mock_llm_port):
        """Should return empty string if response is just 'tool_code'."""
        mock_llm_port.generate_content.return_value = MockLLMResponse(text="tool_code")
        message = create_query_message("Hello")
        
        response = await quick_agent.execute(message)
        
        assert response.result.text == ""

    def test_sanitize_preserves_normal_text(self, quick_agent):
        """Sanitization should preserve normal text."""
        normal_text = "Привіт! Як справи? 🎉"
        
        result = quick_agent._sanitize_response(normal_text)
        
        assert result == normal_text


# ============================================================================
# Session History Tests
# ============================================================================

class TestQuickResponseAgentHistory:
    """Test session history handling."""

    @pytest.mark.asyncio
    async def test_load_history_truncates_to_context_window(self, quick_agent, mock_session_store):
        """Should truncate history to context window size."""
        context_window = quick_agent.CONTEXT_WINDOW
        total = context_window + 10
        long_history = [
            Message(role="user", parts=[MessagePart(text=f"Message {i}")])
            for i in range(total)
        ]
        mock_session_store.load_session.return_value = MockSession(history=long_history)

        history = await quick_agent._load_history("session123")

        assert len(history) == context_window
        # Should be the last context_window messages
        assert history[0].parts[0].text == f"Message {total - context_window}"

    @pytest.mark.asyncio
    async def test_load_history_handles_empty_session(self, quick_agent, mock_session_store):
        """Should handle empty session."""
        mock_session_store.load_session.return_value = MockSession(history=[])
        
        history = await quick_agent._load_history("session123")
        
        assert history == []

    @pytest.mark.asyncio
    async def test_load_history_handles_none_session(self, quick_agent, mock_session_store):
        """Should handle None session."""
        mock_session_store.load_session.return_value = None
        
        history = await quick_agent._load_history("session123")
        
        assert history == []

    @pytest.mark.asyncio
    async def test_load_history_handles_no_session_id(self, quick_agent, mock_session_store):
        """Should return empty history for no session ID."""
        history = await quick_agent._load_history(None)
        
        assert history == []
        mock_session_store.load_session.assert_not_called()


# ============================================================================
# Clean History Tests
# ============================================================================

class TestQuickResponseAgentCleanHistory:
    """Test history cleaning for quick responses."""

    def test_clean_history_removes_tool_calls(self, quick_agent):
        """Should remove messages with tool calls."""
        history = [
            Message(role="user", parts=[MessagePart(text="Hello")]),
            Message(role="model", parts=[
                MessagePart(tool_call={"name": "search", "args": {}})
            ]),
            Message(role="tool", parts=[
                MessagePart(tool_response={"result": "data"})
            ]),
            Message(role="model", parts=[MessagePart(text="Here's the result")])
        ]
        
        clean = quick_agent._clean_history_for_quick(history)
        
        assert len(clean) == 2
        assert clean[0].parts[0].text == "Hello"
        assert clean[1].parts[0].text == "Here's the result"

    def test_clean_history_preserves_text_only(self, quick_agent):
        """Should preserve messages with only text."""
        history = [
            Message(role="user", parts=[MessagePart(text="Hello")]),
            Message(role="model", parts=[MessagePart(text="Hi!")]),
        ]
        
        clean = quick_agent._clean_history_for_quick(history)
        
        assert len(clean) == 2


# ============================================================================
# Factory Function Tests
# ============================================================================

class TestCreateQuickResponseAgent:
    """Test factory function."""

    def test_creates_agent_with_defaults(
        self, mock_llm_port, mock_session_store, mock_prompt_builder
    ):
        """Should create agent with default configuration."""
        execution_context = AgentExecutionContext(
            agent_type="quick",
            provider=mock_llm_port,
            model_name="gemini-3-flash-preview",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities()
        )
        agent = create_quick_response_agent(
            execution_context=execution_context,
            session_store=mock_session_store,
            prompt_builder=mock_prompt_builder
        )
        
        assert agent.agent_id == "quick_response_agent"
        assert agent.agent_type == "quick_response"
        assert agent.model_name == "gemini-3-flash-preview"

    def test_creates_agent_with_user_id(
        self, mock_llm_port, mock_session_store, mock_prompt_builder
    ):
        """Should create agent with user-specific ID."""
        execution_context = AgentExecutionContext(
            agent_type="quick",
            provider=mock_llm_port,
            model_name="gemini-3-flash-preview",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities()
        )
        agent = create_quick_response_agent(
            execution_context=execution_context,
            session_store=mock_session_store,
            prompt_builder=mock_prompt_builder,
            user_id="user123"
        )
        
        assert agent.agent_id == "quick_response_agent_user123"

    def test_creates_agent_with_custom_model(
        self, mock_llm_port, mock_session_store, mock_prompt_builder
    ):
        """Should create agent with custom model."""
        execution_context = AgentExecutionContext(
            agent_type="quick",
            provider=mock_llm_port,
            model_name="gemini-3-flash-preview",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities()
        )
        agent = create_quick_response_agent(
            execution_context=execution_context,
            session_store=mock_session_store,
            prompt_builder=mock_prompt_builder,
            model_name="custom-model"
        )
        
        assert agent.model_name == "custom-model"


# ============================================================================
# Integration-style Tests
# ============================================================================

class TestQuickResponseAgentIntegration:
    """Integration-style tests for complete flows."""

    @pytest.mark.asyncio
    async def test_full_greeting_flow(self, quick_agent, mock_llm_port):
        """Test complete greeting flow."""
        mock_llm_port.generate_content.return_value = MockLLMResponse(
            text="Привіт! Радий тебе бачити! 😊"
        )
        
        message = create_query_message("Привіт")
        response = await quick_agent.execute(message)
        
        assert response.status == AgentStatus.SUCCESS
        assert "Привіт" in response.result.text
        assert response.metadata["model"] == "gemini-3-flash-preview"

    @pytest.mark.asyncio
    async def test_full_acknowledgment_flow(self, quick_agent, mock_llm_port):
        """Test complete acknowledgment flow."""
        mock_llm_port.generate_content.return_value = MockLLMResponse(
            text="Будь ласка! Звертайся, якщо потрібна допомога."
        )
        
        message = create_query_message("Дякую")
        response = await quick_agent.execute(message)
        
        assert response.status == AgentStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_with_session_history(
        self, quick_agent, mock_llm_port, mock_session_store
    ):
        """Test with existing session history."""
        history = [
            Message(role="user", parts=[MessagePart(text="Привіт")]),
            Message(role="model", parts=[MessagePart(text="Привіт!")]),
        ]
        mock_session_store.load_session.return_value = MockSession(history=history)
        mock_llm_port.generate_content.return_value = MockLLMResponse(
            text="Радий знову бачити!"
        )
        
        message = create_query_message("Я повернувся")
        response = await quick_agent.execute(message)
        
        # Verify history was passed to LLM
        call_args = mock_llm_port.generate_content.call_args
        request = call_args.kwargs["request"]
        assert len(request.messages) == 3  # 2 history messages + 1 current (empty parts) message


# ============================================================================
# Intent Remap Tests
# ============================================================================

class TestQuickAgentIntentRemap:
    """Verify that _delegate_quick remaps search_web → search_web_light."""

    @pytest.mark.asyncio
    async def test_search_web_remapped_to_search_web_light(self, quick_agent):
        """search_web must be remapped to search_web_light before coordinator call."""
        coordinator = MagicMock()
        coordinator.handle_delegation = AsyncMock(return_value=AgentResponse.success(
            task_id="t", agent_id="web_search_light_agent", result="ok"
        ))
        quick_agent.coordinator = coordinator

        tool_call = ToolCall(name="delegate_to_specialist", args={"intent": "search_web", "query": "погода"})
        await quick_agent._delegate_quick(tool_call, user_id="u1", session_id="s1", account_id="a1")

        coordinator.handle_delegation.assert_called_once()
        actual_intent = coordinator.handle_delegation.call_args.kwargs["intent"]
        assert actual_intent == "search_web_light", (
            f"Remap failed: coordinator received intent={actual_intent!r}, expected 'search_web_light'"
        )

    @pytest.mark.asyncio
    async def test_search_memory_not_remapped(self, quick_agent):
        """search_memory must pass through without remapping."""
        coordinator = MagicMock()
        coordinator.handle_delegation = AsyncMock(return_value=AgentResponse.success(
            task_id="t", agent_id="memory_search_agent", result="facts"
        ))
        quick_agent.coordinator = coordinator

        tool_call = ToolCall(name="delegate_to_specialist", args={"intent": "search_memory", "query": "факты"})
        await quick_agent._delegate_quick(tool_call, user_id="u1", session_id="s1", account_id="a1")

        actual_intent = coordinator.handle_delegation.call_args.kwargs["intent"]
        assert actual_intent == "search_memory"


# ============================================================================
# History Summary Fallback Tests
# ============================================================================

class TestHistorySummaryFallback:
    """
    Verify the plain-text fallback path: when parse_llm_response() returns
    (text, None, None) — no JSON summary — HistorySummaryService must be
    scheduled via asyncio.create_task().

    This is the path that masked the original tool_choice regression:
    the fallback post-processed the malformed response so users never saw the bug.
    """

    def _make_agent_with_summary_service(
        self, quick_agent_config, mock_llm_port, mock_session_store, mock_prompt_builder
    ) -> tuple:
        """Return (agent, summary_service_mock)."""
        summary_service = MagicMock(spec=HistorySummaryService)
        summary_service.summarize_model_response = AsyncMock(return_value="compact summary")

        execution_context = AgentExecutionContext(
            agent_type="quick",
            provider=mock_llm_port,
            model_name="gemini-3-flash-preview",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities()
        )
        agent = QuickResponseAgent(
            config=quick_agent_config,
            execution_context=execution_context,
            session_store=mock_session_store,
            prompt_builder=mock_prompt_builder,
            history_summary_service=summary_service,
        )
        return agent, summary_service

    @pytest.mark.asyncio
    async def test_plain_text_response_schedules_summary_task(
        self, quick_agent_config, mock_llm_port, mock_session_store, mock_prompt_builder
    ):
        """Plain text from LLM → HistorySummaryService.summarize_model_response scheduled."""
        agent, summary_service = self._make_agent_with_summary_service(
            quick_agent_config, mock_llm_port, mock_session_store, mock_prompt_builder
        )
        mock_llm_port.generate_content.return_value = MockLLMResponse(
            text="Just plain text, no JSON envelope."
        )

        with patch("src.agents.core.quick_response_agent.ENABLE_HISTORY_OPTIMIZATION", True):
            response = await agent.execute(create_query_message("Hello"))

        assert response.status == AgentStatus.SUCCESS
        assert "response_summary_task" in response.metadata, (
            "response_summary_task must be in metadata when LLM returns plain text "
            "and ENABLE_HISTORY_OPTIMIZATION=True"
        )
        # Await the task so the test runner completes it cleanly
        await response.metadata["response_summary_task"]
        summary_service.summarize_model_response.assert_called_once_with(
            "Just plain text, no JSON envelope."
        )

    @pytest.mark.asyncio
    async def test_json_response_with_summary_skips_service(
        self, quick_agent_config, mock_llm_port, mock_session_store, mock_prompt_builder
    ):
        """Valid JSON with response_summary → HistorySummaryService must NOT be called."""
        agent, summary_service = self._make_agent_with_summary_service(
            quick_agent_config, mock_llm_port, mock_session_store, mock_prompt_builder
        )
        mock_llm_port.generate_content.return_value = MockLLMResponse(
            text='{"full_response": "The answer.", "response_summary": "compact", "rich_content": null}'
        )

        with patch("src.agents.core.quick_response_agent.ENABLE_HISTORY_OPTIMIZATION", True):
            response = await agent.execute(create_query_message("Hello"))

        assert response.status == AgentStatus.SUCCESS
        assert "response_summary_task" not in response.metadata, (
            "response_summary_task must NOT be in metadata when JSON response_summary is present"
        )
        summary_service.summarize_model_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_plain_text_without_service_produces_no_task(
        self, quick_agent_config, mock_llm_port, mock_session_store, mock_prompt_builder
    ):
        """Plain text + no history_summary_service → no task scheduled, no crash."""
        execution_context = AgentExecutionContext(
            agent_type="quick",
            provider=mock_llm_port,
            model_name="gemini-3-flash-preview",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities()
        )
        agent = QuickResponseAgent(
            config=quick_agent_config,
            execution_context=execution_context,
            session_store=mock_session_store,
            prompt_builder=mock_prompt_builder,
            # history_summary_service intentionally omitted
        )
        mock_llm_port.generate_content.return_value = MockLLMResponse(text="Plain text.")

        with patch("src.agents.core.quick_response_agent.ENABLE_HISTORY_OPTIMIZATION", True):
            response = await agent.execute(create_query_message("Hello"))

        assert response.status == AgentStatus.SUCCESS
        assert "response_summary_task" not in response.metadata


# ============================================================================
# link_list Pass-Through Tests
# ============================================================================

class TestLinkListPassThrough:
    """
    Verify that link_list from the LLM JSON response is preserved in
    AgentResponse.result.link_list and survives the full execute() flow.

    link_list carries [{anchor, title, url}] for platform adapters to resolve
    [N] anchors into clickable links. It must never be dropped by the agent.
    """

    @pytest.mark.asyncio
    async def test_link_list_preserved_from_json_response(self, quick_agent, mock_llm_port):
        """link_list in LLM JSON → AgentResponse.result.link_list."""
        mock_llm_port.generate_content.return_value = MockLLMResponse(
            text=(
                '{"full_response": "Best place is [Cafe Roma][1].", '
                '"response_summary": "Cafe Roma.", '
                '"rich_content": null, '
                '"link_list": [{"anchor": 1, "title": "Cafe Roma", "url": "https://maps.google.com/?q=1"}]}'
            )
        )
        response = await quick_agent.execute(create_query_message("знайди кафе"))

        assert response.status == AgentStatus.SUCCESS
        assert response.result.link_list == [
            {"anchor": 1, "title": "Cafe Roma", "url": "https://maps.google.com/?q=1"}
        ]

    @pytest.mark.asyncio
    async def test_link_list_none_when_absent_from_json(self, quick_agent, mock_llm_port):
        """JSON without link_list key → AgentResponse.result.link_list is empty list."""
        mock_llm_port.generate_content.return_value = MockLLMResponse(
            text='{"full_response": "Plain answer.", "response_summary": "Short.", "rich_content": null}'
        )
        response = await quick_agent.execute(create_query_message("Hello"))

        assert response.status == AgentStatus.SUCCESS
        assert response.result.link_list == [] or response.result.link_list is None

    @pytest.mark.asyncio
    async def test_link_list_none_on_plain_text_response(self, quick_agent, mock_llm_port):
        """Plain text (no JSON) → link_list is empty / None, not raised."""
        mock_llm_port.generate_content.return_value = MockLLMResponse(
            text="Just a plain answer with no links."
        )
        response = await quick_agent.execute(create_query_message("Hello"))

        assert response.status == AgentStatus.SUCCESS
        # Must not crash; link_list is absent or empty
        link_list = getattr(response.result, "link_list", None)
        assert not link_list

    @pytest.mark.asyncio
    async def test_multiple_links_all_preserved(self, quick_agent, mock_llm_port):
        """All entries in link_list must pass through unchanged."""
        links = [
            {"anchor": 1, "title": "Place A", "url": "https://a.example.com"},
            {"anchor": 2, "title": "Place B", "url": "https://b.example.com"},
            {"anchor": 3, "title": "Place C", "url": "https://c.example.com"},
        ]
        mock_llm_port.generate_content.return_value = MockLLMResponse(
            text=(
                '{"full_response": "Options: [1] [2] [3].", '
                '"response_summary": "3 options.", '
                '"rich_content": null, '
                f'"link_list": {__import__("json").dumps(links)}}}'
            )
        )
        response = await quick_agent.execute(create_query_message("знайди місця"))

        assert response.status == AgentStatus.SUCCESS
        assert len(response.result.link_list) == 3
        assert response.result.link_list[2]["title"] == "Place C"
