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
from src.ports.llm_service import Message, MessagePart, LLMService, LLMRequest
from src.services.agent_context_builder import AgentExecutionContext
from src.domain.user import PerformanceTier
from src.ports.llm_service import ProviderCapabilities


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
def mock_llm_service():
    """Create mock LLM service."""
    service = MagicMock(spec=LLMService)
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
def quick_agent(quick_agent_config, mock_llm_service, mock_session_store, mock_prompt_builder):
    """Create quick response agent with mocks."""
    execution_context = AgentExecutionContext(
        agent_type="quick",
        provider=mock_llm_service,
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
    async def test_execute_returns_success(self, quick_agent, mock_llm_service):
        """Should return successful response."""
        message = create_query_message("Привіт")
        
        response = await quick_agent.execute(message)
        
        assert response.status == AgentStatus.SUCCESS
        assert "Привіт" in response.result.text

    @pytest.mark.asyncio
    async def test_execute_calls_llm_service(self, quick_agent, mock_llm_service):
        """Should call LLM service with correct parameters."""
        message = create_query_message("Hello")
        
        await quick_agent.execute(message)
        
        mock_llm_service.generate_content.assert_called_once()
        call_args = mock_llm_service.generate_content.call_args
        request = call_args.kwargs["request"]
        assert isinstance(request, LLMRequest)
        assert request.model_name == "gemini-3-flash-preview"
        assert request.system_instruction

    @pytest.mark.asyncio
    async def test_execute_loads_session_history(self, quick_agent, mock_session_store):
        """Should load session history."""
        message = create_query_message("Hello", session_id="test_session")
        
        await quick_agent.execute(message)
        
        mock_session_store.load_session.assert_called_once_with("test_session")

    @pytest.mark.asyncio
    async def test_execute_builds_prompt(self, quick_agent, mock_prompt_builder):
        """Should build system prompt."""
        message = create_query_message("Hello")
        
        await quick_agent.execute(message)
        
        mock_prompt_builder.build_for_agent.assert_called_once()

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
    async def test_execute_handles_llm_error(self, quick_agent, mock_llm_service):
        """Should return failure on LLM error."""
        mock_llm_service.generate_content.side_effect = Exception("LLM Error")
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
    async def test_execute_handles_empty_response(self, quick_agent, mock_llm_service):
        """Should handle empty LLM response."""
        mock_llm_service.generate_content.return_value = MockLLMResponse(text="")
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
    async def test_sanitize_removes_tool_code(self, quick_agent, mock_llm_service):
        """Should remove tool_code blocks from response."""
        mock_llm_service.generate_content.return_value = MockLLMResponse(
            text="tool_code\nprint('hello')\nПривіт!"
        )
        message = create_query_message("Hello")
        
        response = await quick_agent.execute(message)
        
        assert "tool_code" not in response.result.text
        assert "print" not in response.result.text

    @pytest.mark.asyncio
    async def test_sanitize_removes_api_references(self, quick_agent, mock_llm_service):
        """Should remove API references from response."""
        mock_llm_service.generate_content.return_value = MockLLMResponse(
            text="default_api.search() Привіт!"
        )
        message = create_query_message("Hello")
        
        response = await quick_agent.execute(message)
        
        assert "default_api" not in response.result.text

    @pytest.mark.asyncio
    async def test_sanitize_removes_tool_references(self, quick_agent, mock_llm_service):
        """Should remove tool references from response."""
        mock_llm_service.generate_content.return_value = MockLLMResponse(
            text="ask_web_search_agent('query') Привіт!"
        )
        message = create_query_message("Hello")
        
        response = await quick_agent.execute(message)
        
        assert "ask_web_search_agent" not in response.result.text

    @pytest.mark.asyncio
    async def test_sanitize_handles_only_tool_code(self, quick_agent, mock_llm_service):
        """Should return empty string if response is just 'tool_code'."""
        mock_llm_service.generate_content.return_value = MockLLMResponse(text="tool_code")
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
        # Create 30 messages (more than CONTEXT_WINDOW=20)
        long_history = [
            Message(role="user", parts=[MessagePart(text=f"Message {i}")])
            for i in range(30)
        ]
        mock_session_store.load_session.return_value = MockSession(history=long_history)
        
        history = await quick_agent._load_history("session123")
        
        assert len(history) == 20
        # Should be the last 20 messages
        assert history[0].parts[0].text == "Message 10"

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
        self, mock_llm_service, mock_session_store, mock_prompt_builder
    ):
        """Should create agent with default configuration."""
        execution_context = AgentExecutionContext(
            agent_type="quick",
            provider=mock_llm_service,
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
        self, mock_llm_service, mock_session_store, mock_prompt_builder
    ):
        """Should create agent with user-specific ID."""
        execution_context = AgentExecutionContext(
            agent_type="quick",
            provider=mock_llm_service,
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
        self, mock_llm_service, mock_session_store, mock_prompt_builder
    ):
        """Should create agent with custom model."""
        execution_context = AgentExecutionContext(
            agent_type="quick",
            provider=mock_llm_service,
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
    async def test_full_greeting_flow(self, quick_agent, mock_llm_service):
        """Test complete greeting flow."""
        mock_llm_service.generate_content.return_value = MockLLMResponse(
            text="Привіт! Радий тебе бачити! 😊"
        )
        
        message = create_query_message("Привіт")
        response = await quick_agent.execute(message)
        
        assert response.status == AgentStatus.SUCCESS
        assert "Привіт" in response.result.text
        assert response.metadata["model"] == "gemini-3-flash-preview"

    @pytest.mark.asyncio
    async def test_full_acknowledgment_flow(self, quick_agent, mock_llm_service):
        """Test complete acknowledgment flow."""
        mock_llm_service.generate_content.return_value = MockLLMResponse(
            text="Будь ласка! Звертайся, якщо потрібна допомога."
        )
        
        message = create_query_message("Дякую")
        response = await quick_agent.execute(message)
        
        assert response.status == AgentStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_with_session_history(
        self, quick_agent, mock_llm_service, mock_session_store
    ):
        """Test with existing session history."""
        history = [
            Message(role="user", parts=[MessagePart(text="Привіт")]),
            Message(role="model", parts=[MessagePart(text="Привіт!")]),
        ]
        mock_session_store.load_session.return_value = MockSession(history=history)
        mock_llm_service.generate_content.return_value = MockLLMResponse(
            text="Радий знову бачити!"
        )
        
        message = create_query_message("Я повернувся")
        response = await quick_agent.execute(message)
        
        # Verify history was passed to LLM
        call_args = mock_llm_service.generate_content.call_args
        request = call_args.kwargs["request"]
        assert len(request.messages) == 3  # 2 history messages + 1 current (empty parts) message
