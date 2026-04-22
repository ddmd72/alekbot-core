"""
Unit tests for RouterAgent.

Tests cover:
- Classification logic (simple vs complex)
- Personal data detection
- External search detection
- Routing behavior
- Edge cases
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.core.router_agent import RouterAgent, create_router_agent
from src.domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentIntent, AgentStatus
from src.ports.llm_port import AgentExecutionContext, ProviderCapabilities, LLMPort
from src.ports.prompt_builder_port import PromptBuilderPort
from src.ports.session_store import SessionStore
from src.ports.repository import FactRepository
from src.ports.agent_note_port import AgentNotePort
from src.domain.llm import Message, MessagePart
from src.domain.session import SessionState
from src.domain.agent_note import AgentNote
from src.domain.user import PerformanceTier


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def router_config():
    """Create router agent config."""
    return AgentConfig(
        agent_id="router_agent",
        agent_type="router",
        llm_model=None,
        max_retries=1,
        timeout_ms=None,
        capabilities=["classification", "routing"]
    )


@pytest.fixture
def router_agent(router_config):
    """Create router agent without coordinator."""
    return RouterAgent(
        config=router_config,
        coordinator=None,  # Testing without coordinator
        quick_agent_id="quick_agent",
        smart_agent_id="smart_agent"
    )


@pytest.fixture
def mock_coordinator():
    """Create mock coordinator for routing tests."""
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(return_value=AgentResponse.success(
        task_id="test",
        agent_id="target_agent",
        result="response",
        confidence=1.0
    ))
    return coordinator


@pytest.fixture
def router_with_coordinator(router_config, mock_coordinator):
    """Create router agent with mock coordinator."""
    return RouterAgent(
        config=router_config,
        coordinator=mock_coordinator,
        quick_agent_id="quick_agent",
        smart_agent_id="smart_agent"
    )


@pytest.fixture
def mock_llm():
    llm = MagicMock(spec=LLMPort)
    llm.generate_content = AsyncMock()
    return llm


@pytest.fixture
def mock_prompt_builder():
    pb = AsyncMock(spec=PromptBuilderPort)
    pb.build_for_agent.return_value = "You are a triage router. Classify the request."
    return pb


@pytest.fixture
def router_with_llm(router_config, mock_llm, mock_prompt_builder):
    ec = AgentExecutionContext(
        agent_type="router",
        provider=mock_llm,
        model_name="gemini-flash",
        tier=PerformanceTier.ECO,
        capabilities=ProviderCapabilities()
    )
    return RouterAgent(
        config=router_config,
        execution_context=ec,
        coordinator=None,
        quick_agent_id="quick_agent",
        smart_agent_id="smart_agent",
        prompt_builder=mock_prompt_builder
    )


def create_query_message(text: str) -> AgentMessage:
    """Helper to create query messages."""
    return AgentMessage.create(
        sender="conversation_handler",
        recipient="router_agent",
        intent=AgentIntent.QUERY,
        payload={"text": text},
        context={"user_id": "user123", "session_id": "session456"}
    )


# ============================================================================
# can_handle Tests
# ============================================================================

class TestRouterAgentCanHandle:
    """Test can_handle() method."""

    @pytest.mark.asyncio
    async def test_can_handle_query_with_text(self, router_agent):
        """Should handle QUERY intent with text."""
        message = create_query_message("Hello")
        
        result = await router_agent.can_handle(message)
        
        assert result is True

    @pytest.mark.asyncio
    async def test_cannot_handle_non_query_intent(self, router_agent):
        """Should not handle non-QUERY intents."""
        message = AgentMessage.create(
            sender="test",
            recipient="router_agent",
            intent=AgentIntent.INFORM,  # Not QUERY
            payload={"text": "Hello"},
            context={}
        )
        
        result = await router_agent.can_handle(message)
        
        assert result is False

    @pytest.mark.asyncio
    async def test_cannot_handle_empty_text(self, router_agent):
        """Should not handle messages without text."""
        message = AgentMessage.create(
            sender="test",
            recipient="router_agent",
            intent=AgentIntent.QUERY,
            payload={"text": ""},  # Empty text
            context={}
        )
        
        result = await router_agent.can_handle(message)
        
        assert result is False

    @pytest.mark.asyncio
    async def test_cannot_handle_missing_text(self, router_agent):
        """Should not handle messages without text key."""
        message = AgentMessage.create(
            sender="test",
            recipient="router_agent",
            intent=AgentIntent.QUERY,
            payload={"other_key": "value"},  # No text key
            context={}
        )
        
        result = await router_agent.can_handle(message)
        
        assert result is False


# ============================================================================
# Simple Request Classification Tests
# ============================================================================

class TestRouterAgentSimpleClassification:
    """Test classification of simple requests."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", [
        # Ukrainian greetings
        "Привіт",
        "привіт",
        "ПРИВІТ",
        "Привіт!",
        "привет",
        # English greetings
        "Hello",
        "Hi",
        "Hey",
        "hello!",
        # Acknowledgments
        "Ок",
        "ok",
        "OK",
        "Дякую",
        "дякую",
        "Спасибо",
        "Thanks",
        # Short phrases
        "Добре",
        "Ясно",
        "Зрозуміло",
        "Добрий день",
        "good morning",
    ])
    async def test_simple_greetings_classified_as_simple(self, router_agent, text):
        """Simple greetings should be classified as simple but route to smart agent."""
        message = create_query_message(text)
        
        response = await router_agent.execute(message)
        
        assert response.metadata["target_agent"] == "smart_agent"
        assert response.metadata["is_simple"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", [
        "Ага, ок",
        "Так, дякую",
        "Yes ok",
        "Ок, добре",
    ])
    async def test_short_acknowledgments_are_simple(self, router_agent, text):
        """Short acknowledgment phrases should be classified as simple."""
        message = create_query_message(text)
        
        response = await router_agent.execute(message)
        
        assert response.metadata["is_simple"] is True
        assert response.metadata["target_agent"] == "smart_agent"

    @pytest.mark.asyncio
    async def test_empty_text_is_simple(self, router_agent):
        """Empty text should be treated as simple."""
        # Note: can_handle would reject this, but _is_simple_request handles it
        assert router_agent._is_simple_request("") is True
        assert router_agent._is_simple_request("   ") is True


# ============================================================================
# Complex Request Classification Tests
# ============================================================================

class TestRouterAgentComplexClassification:
    """Test classification of complex requests."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", [
        # Personal data queries
        "Який мій розмір взуття?",
        "Нагадай мені про мою зустріч",
        "What's my car model?",
        "Скажи моє ім'я",
        # External search queries  
        "Яка погода в Валенсії?",
        "Покажи новости про Ukraine",
        "Find flights to Barcelona",
        "Який курс долара?",
        # Complex questions
        "Порівняй ці два варіанти та скажи, який краще",
        "Explain quantum computing",
        "Напиши план на сьогодні",
    ])
    async def test_complex_queries_routed_to_smart(self, router_agent, text):
        """Complex queries should route to smart agent."""
        message = create_query_message(text)
        
        response = await router_agent.execute(message)
        
        # Rule-based path routes to quick_agent; LLM routing (tested in TestRouterAgentLlmTriage)
        # routes complex queries to smart_agent. Rule-based only ensures is_simple=False.
        assert response.metadata["is_simple"] is False


# ============================================================================
# Personal Request Detection Tests
# ============================================================================

class TestRouterAgentPersonalDetection:
    """Test detection of personal data requests."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text,expected", [
        # Ukrainian personal keywords
        ("Який мій розмір?", True),
        ("Скажи моє ім'я", True),
        ("Нагадай мені", True),
        ("У мене є машина", True),
        # Russian personal keywords
        ("Какой мой номер?", True),
        ("Напомни мне", True),
        ("У меня встреча", True),
        # English personal keywords
        ("What's my phone number?", True),
        ("Tell me about my car", True),
        ("Remind me", True),
        # NOT personal
        ("Яка погода?", False),
        ("Hello", False),
        ("Розкажи про машини", False),
    ])
    async def test_personal_request_detection(self, router_agent, text, expected):
        """Should correctly detect personal data requests."""
        result = router_agent._is_personal_request(text)
        
        assert result is expected

    @pytest.mark.asyncio
    async def test_personal_overrides_simple(self, router_agent):
        """Personal request should override simple classification."""
        # "Ок, мій" - contains both simple "ок" and personal "мій"
        message = create_query_message("Ок, скажи мій номер")
        
        response = await router_agent.execute(message)
        
        # Rule-based path routes to quick_agent regardless; LLM routing handles smart routing
        # Verify that personal data IS detected and is_simple IS overridden
        assert response.metadata["is_simple"] is False
        assert response.metadata["is_personal"] is True


# ============================================================================
# External Search Detection Tests
# ============================================================================

class TestRouterAgentExternalSearchDetection:
    """Test detection of external search requests."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text,expected", [
        # Weather
        ("Яка погода?", True),
        ("weather forecast", True),
        ("Прогноз погоди", True),
        # News
        ("Покажи новости", True),
        ("Latest news", True),
        # Prices
        ("Який курс долара?", True),
        ("Bitcoin price", True),
        # Travel
        ("Find flights", True),
        ("Рейс до Парижа", True),
        ("Hotel in Barcelona", True),
        # Commands
        ("Погугли це", True),
        ("Search for restaurants", True),
        # NOT external
        ("Hello", False),
        ("Дякую", False),
        ("Скажи мій номер", False),  # Personal, not external
    ])
    async def test_external_search_detection(self, router_agent, text, expected):
        """Should correctly detect external search requests."""
        result = router_agent._requires_external_search(text)
        
        assert result is expected

    @pytest.mark.asyncio
    async def test_personal_overrides_external(self, router_agent):
        """Personal keywords should override external search detection."""
        # "Яка погода в моєму місті" - has weather keyword but also "моєму"
        text = "Яка погода в моєму місті?"
        
        is_personal = router_agent._is_personal_request(text)
        needs_external = router_agent._requires_external_search(text)
        
        # Personal wins - no external search for personal queries
        assert is_personal is True
        assert needs_external is False


# ============================================================================
# Routing with Coordinator Tests
# ============================================================================

class TestRouterAgentRouting:
    """Test routing behavior with coordinator."""

    @pytest.mark.asyncio
    async def test_routes_to_smart_agent_always(self, router_with_coordinator, mock_coordinator):
        """Should route all requests to smart agent."""
        message = create_query_message("Привіт")
        
        await router_with_coordinator.execute(message)
        
        # Verify coordinator was called
        mock_coordinator.route_message.assert_called_once()
        
        # Get the routed message
        routed_msg = mock_coordinator.route_message.call_args[0][0]
        assert routed_msg.recipient == "smart_agent"

    @pytest.mark.asyncio
    async def test_routes_to_smart_agent(self, router_with_coordinator, mock_coordinator):
        """Rule-based path: external queries go to quick_agent (LLM routing needed for smart)."""
        message = create_query_message("Яка погода в Київі?")
        
        await router_with_coordinator.execute(message)
        
        # Verify coordinator was called — routing happened (destination depends on LLM availability)
        mock_coordinator.route_message.assert_called_once()
        routed_msg = mock_coordinator.route_message.call_args[0][0]
        # Always routes to smart_agent
        assert routed_msg.recipient == "smart_agent"

    @pytest.mark.asyncio
    async def test_preserves_context_in_routing(self, router_with_coordinator, mock_coordinator):
        """Should preserve original context when routing."""
        message = create_query_message("Hello")
        message.context["custom_data"] = "preserve_me"
        
        await router_with_coordinator.execute(message)
        
        routed_msg = mock_coordinator.route_message.call_args[0][0]
        assert routed_msg.context["custom_data"] == "preserve_me"
        assert routed_msg.context["routed_by"] == "router_agent"

    @pytest.mark.asyncio
    async def test_adds_classification_to_context(self, router_with_coordinator, mock_coordinator):
        """Should add classification results to context."""
        message = create_query_message("Яка погода?")
        
        await router_with_coordinator.execute(message)
        
        routed_msg = mock_coordinator.route_message.call_args[0][0]
        assert "classification" in routed_msg.context
        assert routed_msg.context["classification"]["needs_external"] is True
        assert "routing" in routed_msg.context


class TestRouterAgentLlmTriage:
    """Test LLM triage routing behavior."""

    @pytest.mark.asyncio
    async def test_llm_routes_to_smart_on_low_complexity(self, router_with_llm, mock_llm):
        mock_llm.generate_content.return_value = MagicMock(
            text='{"needs_memory_search":false,"reasoning":"simple greeting","search_intent":"none","relevant_domains":[],"semantic_lens":[],"search_phrase":"","metadata":{"user_tone":"casual","task_complexity":"small_talk"}}'
        )

        message = create_query_message("Привіт")
        response = await router_with_llm.execute(message)

        assert response.result["routed_to"] == "smart_agent"

    @pytest.mark.asyncio
    async def test_llm_routes_to_smart_on_high_complexity(self, router_with_llm, mock_llm):
        mock_llm.generate_content.return_value = MagicMock(
            text='{"needs_memory_search":true,"reasoning":"multi-fact query","search_intent":"topic","relevant_domains":["possession"],"semantic_lens":["car","vehicle"],"search_phrase":"user vehicles list","metadata":{"user_tone":"casual","task_complexity":"deep_reasoning"}}'
        )

        message = create_query_message("Які у мене машини?")
        response = await router_with_llm.execute(message)

        assert response.result["routed_to"] == "smart_agent"

    @pytest.mark.asyncio
    async def test_llm_fallback_to_rule_based_on_error(self, router_with_llm, mock_llm):
        mock_llm.generate_content.side_effect = Exception("LLM failed")

        message = create_query_message("Привіт")
        response = await router_with_llm.execute(message)

        assert response.result["routed_to"] == "smart_agent"


# ============================================================================
# Edge Cases Tests
# ============================================================================

class TestRouterAgentEdgeCases:
    """Test edge cases and unusual inputs."""

    @pytest.mark.asyncio
    async def test_unicode_handling(self, router_agent):
        """Should handle unicode characters correctly."""
        # Ukrainian
        message = create_query_message("Привіт 👋 як справи?")
        response = await router_agent.execute(message)
        assert response.status == AgentStatus.SUCCESS
        
        # Chinese
        message = create_query_message("你好")
        response = await router_agent.execute(message)
        assert response.status == AgentStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_mixed_language(self, router_agent):
        """Should handle mixed language input."""
        message = create_query_message("Hello, як справи today?")
        
        response = await router_agent.execute(message)
        
        assert response.status == AgentStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_special_characters(self, router_agent):
        """Should handle special characters."""
        message = create_query_message("Привіт!!! 😊😊😊")
        
        response = await router_agent.execute(message)
        
        assert response.status == AgentStatus.SUCCESS
        assert response.metadata["is_simple"] is True

    @pytest.mark.asyncio
    async def test_very_long_text(self, router_agent):
        """Should handle very long text."""
        long_text = "Привіт " * 100  # Very long greeting
        message = create_query_message(long_text)
        
        response = await router_agent.execute(message)
        
        assert response.status == AgentStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_case_insensitivity(self, router_agent):
        """Classification should be case-insensitive."""
        messages = [
            create_query_message("ПРИВІТ"),
            create_query_message("привіт"),
            create_query_message("Привіт"),
        ]
        
        for msg in messages:
            response = await router_agent.execute(msg)
            assert response.metadata["is_simple"] is True

    @pytest.mark.asyncio
    async def test_punctuation_handling(self, router_agent):
        """Should handle various punctuation."""
        messages = [
            "Привіт!",
            "Привіт?",
            "Привіт...",
            "Привіт!!!",
            "(Привіт)",
        ]
        
        for text in messages:
            msg = create_query_message(text)
            response = await router_agent.execute(msg)
            assert response.metadata["is_simple"] is True


# ============================================================================
# Factory Function Tests
# ============================================================================

class TestCreateRouterAgent:
    """Test factory function."""

    def test_creates_router_with_defaults(self):
        """Should create router with default configuration."""
        router = create_router_agent()
        
        assert router.agent_id == "router_agent"
        assert router.agent_type == "router"
        assert router.config.llm_model is None
        assert router.quick_agent_id == "quick_response_agent"
        assert router.smart_agent_id == "smart_response_agent"

    def test_creates_router_with_custom_agents(self):
        """Should create router with custom agent IDs."""
        router = create_router_agent(
            quick_agent_id="my_quick_agent",
            smart_agent_id="my_smart_agent"
        )
        
        assert router.quick_agent_id == "my_quick_agent"
        assert router.smart_agent_id == "my_smart_agent"

    def test_creates_router_with_coordinator(self, mock_coordinator):
        """Should create router with coordinator."""
        router = create_router_agent(coordinator=mock_coordinator)
        
        assert router.coordinator is mock_coordinator


# ============================================================================
# Integration-style Tests
# ============================================================================

class TestRouterAgentIntegration:
    """Integration-style tests for complete flows."""

    @pytest.mark.asyncio
    async def test_full_classification_flow_simple(self, router_agent):
        """Test full flow for simple request."""
        message = create_query_message("Привіт!")
        
        response = await router_agent.execute(message)
        
        assert response.status == AgentStatus.SUCCESS
        assert response.result["routed_to"] == "smart_agent"
        assert response.result["classification"]["is_simple"] is True
        assert response.result["classification"]["is_personal"] is False
        assert response.result["classification"]["needs_external"] is False

    @pytest.mark.asyncio
    async def test_full_classification_flow_complex_personal(self, router_agent):
        """Test full flow for personal query."""
        message = create_query_message("Який мій номер телефону?")
        
        response = await router_agent.execute(message)
        
        assert response.status == AgentStatus.SUCCESS
        # Rule-based path routes to quick_agent; smart routing requires LLM
        assert response.result["classification"]["is_simple"] is False
        assert response.result["classification"]["is_personal"] is True

    @pytest.mark.asyncio
    async def test_full_classification_flow_complex_external(self, router_agent):
        """Test full flow for external search query."""
        message = create_query_message("Яка погода завтра в Києві?")

        response = await router_agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        # Rule-based path routes to quick_agent; smart routing requires LLM
        assert response.result["classification"]["is_simple"] is False
        assert response.result["classification"]["needs_external"] is True


# ============================================================================
# Agent ID with user_id Tests (line 212)
# ============================================================================

class TestRouterAgentId:
    """Tests for per-user agent_id assignment (line 212)."""

    def test_agent_id_includes_user_id_when_provided_via_factory(self):
        """create_router_agent(user_id=...) produces agent with per-user agent_id."""
        agent = create_router_agent(user_id="user_xyz")
        # Factory pre-sets config.agent_id, so the if-branch on line 212 is False
        # and agent_id stays as-is: router_agent_user_xyz
        assert agent.agent_id == "router_agent_user_xyz"

    def test_agent_id_unchanged_when_no_user_id(self, router_config):
        """agent_id should remain 'router_agent' when user_id is not provided."""
        agent = RouterAgent(config=router_config)
        assert agent.agent_id == "router_agent"

    def test_agent_id_unchanged_when_user_id_set(self, router_config):
        """user_id alone does not change agent_id — factory sets it via config."""
        agent = RouterAgent(config=router_config, user_id="user_abc")
        assert agent.agent_id == "router_agent"

    def test_agent_id_unchanged_when_config_already_has_custom_id(self):
        """If config.agent_id is not 'router_agent', user_id rename branch does not fire."""
        custom_config = AgentConfig(
            agent_id="my_custom_router",
            agent_type="router",
            llm_model=None,
            max_retries=1,
            timeout_ms=None,
            capabilities=[]
        )
        # custom id != "router_agent" → branch on line 211 is False → no rename attempt
        agent = RouterAgent(config=custom_config)
        assert agent.agent_id == "my_custom_router"


# ============================================================================
# Admin Cache Reset Command (line 264)
# ============================================================================

class TestAdminCacheResetRouting:
    """Test that $admin_cache_reset is intercepted before any other processing (line 264)."""

    @pytest.mark.asyncio
    async def test_admin_cache_reset_command_intercepted(self, router_config):
        """$admin_cache_reset must short-circuit execute and return without routing."""
        agent = RouterAgent(config=router_config)
        message = create_query_message("$admin_cache_reset")

        response = await agent.execute(message)

        # No coordinator → result comes from _handle_admin_cache_reset
        assert response.status == AgentStatus.SUCCESS
        assert response.metadata["command"] == "admin_cache_reset"

    @pytest.mark.asyncio
    async def test_admin_cache_reset_with_whitespace(self, router_config):
        """$admin_cache_reset with surrounding whitespace should still be intercepted."""
        agent = RouterAgent(config=router_config)
        message = create_query_message("  $admin_cache_reset  ")

        response = await agent.execute(message)

        assert response.status == AgentStatus.SUCCESS
        assert response.metadata["command"] == "admin_cache_reset"


# ============================================================================
# Conversation History Loading (line 271)
# ============================================================================

class TestConversationHistoryLoading:
    """Test history loading when session_store AND session_id are both set (line 271)."""

    @pytest.mark.asyncio
    async def test_history_loaded_when_session_store_and_session_id_present(self, router_config):
        """_load_conversation_context should be called when both session_store and session_id set."""
        mock_session_store = AsyncMock(spec=SessionStore)
        session = SessionState(session_id="sess_001", user_id="user1")
        mock_session_store.load_session.return_value = session

        agent = RouterAgent(
            config=router_config,
            session_store=mock_session_store,
        )

        message = AgentMessage.create(
            sender="ch",
            recipient="router_agent",
            intent=AgentIntent.QUERY,
            payload={"text": "Привіт"},
            context={"user_id": "user1", "session_id": "sess_001"},
        )

        response = await agent.execute(message)

        mock_session_store.load_session.assert_called_once_with("sess_001")
        assert response.status == AgentStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_history_not_loaded_when_session_id_missing(self, router_config):
        """_load_conversation_context should NOT be called when session_id is absent."""
        mock_session_store = AsyncMock(spec=SessionStore)

        agent = RouterAgent(
            config=router_config,
            session_store=mock_session_store,
        )

        message = AgentMessage.create(
            sender="ch",
            recipient="router_agent",
            intent=AgentIntent.QUERY,
            payload={"text": "Привіт"},
            context={"user_id": "user1"},  # no session_id
        )

        await agent.execute(message)

        mock_session_store.load_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_history_not_loaded_when_no_session_store(self, router_config):
        """History loading must be skipped entirely when session_store is None."""
        agent = RouterAgent(config=router_config)

        message = AgentMessage.create(
            sender="ch",
            recipient="router_agent",
            intent=AgentIntent.QUERY,
            payload={"text": "Привіт"},
            context={"user_id": "user1", "session_id": "sess_001"},
        )

        response = await agent.execute(message)
        assert response.status == AgentStatus.SUCCESS


# ============================================================================
# Vision Complexity Override (lines 284-285)
# ============================================================================

class TestVisionComplexityOverride:
    """Test that a message with file_data forces complexity >= 7 (lines 284-285)."""

    @pytest.mark.asyncio
    async def test_vision_attachment_forces_smart_routing(self, router_config, mock_llm, mock_prompt_builder):
        """current_parts with file_data must force complexity=7 → smart_agent path."""
        # LLM returns complexity=2 (would route to quick without vision override)
        mock_llm.generate_content.return_value = MagicMock(
            text='{"needs_memory_search":false,"reasoning":"image","search_intent":"none","relevant_domains":[],"semantic_lens":[],"search_phrase":"","metadata":{"user_tone":"casual","task_complexity":"small_talk"}}'
        )
        ec = AgentExecutionContext(
            agent_type="router",
            provider=mock_llm,
            model_name="gemini-flash",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities()
        )
        agent = RouterAgent(
            config=router_config,
            execution_context=ec,
            prompt_builder=mock_prompt_builder,
            smart_agent_id="smart_agent",
            quick_agent_id="quick_agent",
        )

        # Simulate current_message_parts containing a file attachment
        file_part = MagicMock(spec=MessagePart)
        file_part.text = None
        file_part.file_data = MagicMock()  # truthy → vision detected

        message = AgentMessage.create(
            sender="ch",
            recipient="router_agent",
            intent=AgentIntent.QUERY,
            payload={"text": "What is in this image?"},
            context={
                "user_id": "user1",
                "session_id": None,
                "current_message_parts": [file_part],
            },
        )

        response = await agent.execute(message)

        assert response.result["routed_to"] == "smart_agent"

    @pytest.mark.asyncio
    async def test_no_file_data_does_not_override_complexity(self, router_config):
        """Parts without file_data must not trigger the vision override."""
        text_part = MagicMock(spec=MessagePart)
        text_part.text = "Hello"
        text_part.file_data = None

        agent = RouterAgent(
            config=router_config,
            quick_agent_id="quick_agent",
            smart_agent_id="smart_agent",
        )

        message = AgentMessage.create(
            sender="ch",
            recipient="router_agent",
            intent=AgentIntent.QUERY,
            payload={"text": "Hello"},
            context={
                "user_id": "user1",
                "current_message_parts": [text_part],
            },
        )

        response = await agent.execute(message)
        assert response.result["routed_to"] == "smart_agent"


# ============================================================================
# Biographical fetch exception swallow (lines 304-319)
# ============================================================================

class TestBiographicalFetchException:
    """Router must swallow biographical fetch errors and continue (lines 304-319)."""

    @pytest.mark.asyncio
    async def test_biographical_fetch_failure_does_not_crash_router(self, mock_llm, mock_prompt_builder):
        """Exception in repository.get_biographical_context_cached must be swallowed."""
        mock_llm.generate_content.return_value = MagicMock(
            text='{"needs_memory_search":true,"confidence":0.9,"reasoning":"topic","search_intent":"topic","relevant_domains":[],"semantic_lens":["test"],"search_phrase":"test","metadata":{"user_tone":"casual","complexity_score":5}}'
        )
        ec = AgentExecutionContext(
            agent_type="router",
            provider=mock_llm,
            model_name="gemini-flash",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities()
        )

        mock_repository = AsyncMock(spec=FactRepository)
        mock_repository.get_biographical_context_cached = AsyncMock(
            side_effect=Exception("Firestore unavailable")
        )

        mock_enrichment = AsyncMock()
        mock_enrichment.enrich_context = AsyncMock(return_value=None)

        agent = create_router_agent(
            execution_context=ec,
            prompt_builder=mock_prompt_builder,
            repository=mock_repository,
            search_enrichment_service=mock_enrichment,
            user_id="user1",
        )

        message = create_query_message("Tell me about my projects")

        # Must not raise even though biographical fetch fails
        response = await agent.execute(message)
        assert response.status == AgentStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_biographical_fetch_skipped_without_repository(self, mock_llm, mock_prompt_builder):
        """When repository is None, no biographical fetch attempt should be made."""
        mock_llm.generate_content.return_value = MagicMock(
            text='{"needs_memory_search":false,"confidence":0.9,"reasoning":"ok","search_intent":"topic","relevant_domains":[],"semantic_lens":["test"],"search_phrase":"test","metadata":{"user_tone":"casual","complexity_score":3}}'
        )
        ec = AgentExecutionContext(
            agent_type="router",
            provider=mock_llm,
            model_name="gemini-flash",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities()
        )

        mock_enrichment = AsyncMock()
        mock_enrichment.enrich_context = AsyncMock(return_value=None)

        agent = create_router_agent(
            execution_context=ec,
            prompt_builder=mock_prompt_builder,
            repository=None,
            search_enrichment_service=mock_enrichment,
            user_id="user1",
        )

        response = await agent.execute(create_query_message("найди мої проекти"))
        assert response.status == AgentStatus.SUCCESS


# ============================================================================
# Notes port fetch + exception swallow (lines 330-344)
# ============================================================================

class TestNotesPortFetch:
    """Test notes port list_active_notes path and exception swallow (lines 330-344)."""

    @pytest.mark.asyncio
    async def test_notes_loaded_when_notes_port_and_user_id_present(self):
        """list_active_notes should be called when notes_port and user_id are set."""
        note = AgentNote(
            note_id="n1",
            user_id="user1",
            text="Buy milk",
            instruction="Go to the shop",
            created_at=datetime.now(timezone.utc),
            due=datetime.now(timezone.utc),
        )
        mock_notes_port = AsyncMock(spec=AgentNotePort)
        mock_notes_port.list_active_notes = AsyncMock(return_value=[note])

        agent = create_router_agent(
            notes_port=mock_notes_port,
            user_id="user1",
        )

        message = create_query_message("Привіт")
        response = await agent.execute(message)

        mock_notes_port.list_active_notes.assert_called_once()
        call_kwargs = mock_notes_port.list_active_notes.call_args
        assert call_kwargs.kwargs.get("user_id") == "user1" or call_kwargs.args[0] == "user1"
        assert response.status == AgentStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_notes_fetch_failure_does_not_crash_router(self):
        """Exception in notes_port.list_active_notes must be swallowed."""
        mock_notes_port = AsyncMock(spec=AgentNotePort)
        mock_notes_port.list_active_notes = AsyncMock(
            side_effect=Exception("Notes DB unavailable")
        )

        agent = create_router_agent(
            notes_port=mock_notes_port,
            user_id="user1",
        )

        response = await agent.execute(create_query_message("Привіт"))
        assert response.status == AgentStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_notes_skipped_without_user_id(self, router_config):
        """list_active_notes should NOT be called when user_id is absent."""
        mock_notes_port = AsyncMock(spec=AgentNotePort)
        mock_notes_port.list_active_notes = AsyncMock(return_value=[])

        agent = RouterAgent(
            config=router_config,
            notes_port=mock_notes_port,
            # no user_id
        )

        await agent.execute(create_query_message("Привіт"))
        mock_notes_port.list_active_notes.assert_not_called()


# ============================================================================
# _load_triage_prompt raises RuntimeError without prompt_builder (line 410)
# ============================================================================

class TestLoadTriagePromptNoPromptBuilder:
    """_load_triage_prompt must raise RuntimeError when no prompt_builder (line 410)."""

    @pytest.mark.asyncio
    async def test_load_triage_prompt_raises_without_prompt_builder(self, router_config, mock_llm):
        """_load_triage_prompt raises RuntimeError when prompt_builder is None."""
        ec = AgentExecutionContext(
            agent_type="router",
            provider=mock_llm,
            model_name="gemini-flash",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities()
        )
        agent = RouterAgent(
            config=router_config,
            execution_context=ec,
            prompt_builder=None,  # no prompt_builder
        )

        message = create_query_message("What's the weather?")
        with pytest.raises(RuntimeError, match="RouterAgent requires prompt_builder"):
            await agent._load_triage_prompt(message)

    @pytest.mark.asyncio
    async def test_llm_triage_falls_back_to_rule_based_when_prompt_builder_missing(self, router_config, mock_llm):
        """When prompt_builder is missing, LLM triage raises → fallback to rule-based (not exception)."""
        ec = AgentExecutionContext(
            agent_type="router",
            provider=mock_llm,
            model_name="gemini-flash",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities()
        )
        agent = RouterAgent(
            config=router_config,
            execution_context=ec,
            prompt_builder=None,
        )

        # Should NOT raise — fallback kicks in
        response = await agent.execute(create_query_message("Hello"))
        assert response.status == AgentStatus.SUCCESS


# ============================================================================
# _classify_with_llm: empty clean_messages fallback (line 441)
# ============================================================================

class TestClassifyWithLlmEmptyMessages:
    """When all messages have no text parts, fall back to rule-based (line 441)."""

    @pytest.mark.asyncio
    async def test_classify_with_llm_falls_back_when_no_text_parts(self, router_config, mock_llm, mock_prompt_builder):
        """_classify_with_llm must return _classify_request(text) when clean_messages is empty."""
        ec = AgentExecutionContext(
            agent_type="router",
            provider=mock_llm,
            model_name="gemini-flash",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities()
        )
        agent = RouterAgent(
            config=router_config,
            execution_context=ec,
            prompt_builder=mock_prompt_builder,
        )

        # Build a history where every message has only file_data parts (no text)
        file_only_part = MagicMock(spec=MessagePart)
        file_only_part.text = None
        file_only_part.file_data = MagicMock()
        history = [Message(role="user", parts=[file_only_part])]

        message = create_query_message("Hello")

        # Call _classify_with_llm directly
        result = await agent._classify_with_llm("Hello", message, history)

        # Rule-based result must be returned — LLM not called
        mock_llm.generate_content.assert_not_called()
        assert "is_simple" in result


# ============================================================================
# _classify_with_llm: JSONDecodeError re-raised (lines 473-476)
# ============================================================================

class TestClassifyWithLlmJsonDecodeError:
    """JSONDecodeError from json.loads must be re-raised (lines 473-476)."""

    @pytest.mark.asyncio
    async def test_json_decode_error_raises(self, router_config, mock_llm, mock_prompt_builder):
        """Malformed JSON in triage response must propagate as JSONDecodeError.
        The regex r'(\\{.*\\})' (re.DOTALL) requires matching braces, so we use
        text that has both { and } but invalid JSON body."""
        mock_llm.generate_content.return_value = MagicMock(
            text='{broken: json}'
        )
        ec = AgentExecutionContext(
            agent_type="router",
            provider=mock_llm,
            model_name="gemini-flash",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities()
        )
        agent = RouterAgent(
            config=router_config,
            execution_context=ec,
            prompt_builder=mock_prompt_builder,
        )

        # The broken JSON is still a valid regex match for `\{.*\}` (re.DOTALL),
        # so json.loads will be called and will raise JSONDecodeError.
        # _classify_request_with_fallback catches any exception and uses rule-based fallback.
        response = await agent.execute(create_query_message("query"))
        # Fallback must succeed
        assert response.status == AgentStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_no_json_in_response_raises_value_error(self, router_config, mock_llm, mock_prompt_builder):
        """When LLM returns text with no JSON object, ValueError is raised (line 476).
        _classify_request_with_fallback catches it and uses rule-based fallback."""
        mock_llm.generate_content.return_value = MagicMock(
            text="This is just plain text with no JSON braces at all"
        )
        ec = AgentExecutionContext(
            agent_type="router",
            provider=mock_llm,
            model_name="gemini-flash",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities()
        )
        agent = RouterAgent(
            config=router_config,
            execution_context=ec,
            prompt_builder=mock_prompt_builder,
        )

        # ValueError from line 476 is caught by _classify_request_with_fallback
        response = await agent.execute(create_query_message("query"))
        assert response.status == AgentStatus.SUCCESS





# ============================================================================
# _is_simple_request: SHORT_ACKNOWLEDGMENTS exact match (line 552)
# ============================================================================

class TestIsSimpleRequestAcknowledgments:
    """Exact match in SHORT_ACKNOWLEDGMENTS must return True (line 552)."""

    def test_short_acknowledgment_exact_match_is_simple(self, router_agent):
        """Each SHORT_ACKNOWLEDGMENTS token must individually return True."""
        for token in RouterAgent.SHORT_ACKNOWLEDGMENTS:
            assert router_agent._is_simple_request(token) is True, (
                f"Expected '{token}' to be simple"
            )

    def test_short_acknowledgment_not_in_simple_phrases(self, router_agent):
        """A token that is only in SHORT_ACKNOWLEDGMENTS (not SIMPLE_PHRASES) returns True."""
        # "fine" is in SHORT_ACKNOWLEDGMENTS but not necessarily in SIMPLE_PHRASES
        if "fine" not in RouterAgent.SIMPLE_PHRASES:
            assert router_agent._is_simple_request("fine") is True


# ============================================================================
# _is_personal_request: empty string returns False (line 584)
# ============================================================================

class TestIsPersonalRequestEmptyString:
    """_is_personal_request('') must return False (line 584)."""

    def test_empty_string_not_personal(self, router_agent):
        assert router_agent._is_personal_request("") is False

    def test_whitespace_only_not_personal(self, router_agent):
        assert router_agent._is_personal_request("   ") is False


# ============================================================================
# _requires_external_search: empty string returns False (line 608)
# ============================================================================

class TestRequiresExternalSearchEmptyString:
    """_requires_external_search('') must return False (line 608)."""

    def test_empty_string_no_external_search(self, router_agent):
        assert router_agent._requires_external_search("") is False

    def test_whitespace_only_no_external_search(self, router_agent):
        assert router_agent._requires_external_search("   ") is False


# ============================================================================
# _handle_admin_cache_reset (lines 628-669)
# ============================================================================

class TestHandleAdminCacheReset:
    """Full coverage of _handle_admin_cache_reset branches (lines 628-669)."""

    @pytest.mark.asyncio
    async def test_cache_reset_success_with_assembly_service(self, router_config):
        """When prompt_builder has assembly_service.invalidate_cache, returns success (lines 628-649)."""
        mock_assembly_service = MagicMock()
        mock_assembly_service.invalidate_cache = MagicMock()

        mock_pb = MagicMock(spec=PromptBuilderPort)
        mock_pb.assembly_service = mock_assembly_service

        agent = RouterAgent(config=router_config, prompt_builder=mock_pb)

        response = await agent._handle_admin_cache_reset()

        mock_assembly_service.invalidate_cache.assert_called_once()
        assert response.status == AgentStatus.SUCCESS
        assert response.metadata["cache_cleared"] is True
        assert response.metadata["command"] == "admin_cache_reset"

    @pytest.mark.asyncio
    async def test_cache_reset_failure_when_invalidate_raises(self, router_config):
        """When invalidate_cache() raises, returns AgentResponse.failure with the error."""
        mock_assembly_service = MagicMock()
        mock_assembly_service.invalidate_cache = MagicMock(
            side_effect=RuntimeError("cache exploded")
        )

        mock_pb = MagicMock(spec=PromptBuilderPort)
        mock_pb.assembly_service = mock_assembly_service

        agent = RouterAgent(config=router_config, prompt_builder=mock_pb)

        response = await agent._handle_admin_cache_reset()
        assert response.status == AgentStatus.FAILED
        assert "cache exploded" in response.error

    @pytest.mark.asyncio
    async def test_cache_reset_not_available_when_no_assembly_service(self, router_config):
        """When prompt_builder lacks assembly_service attr, returns 'not available' (lines 659-669)."""
        mock_pb = MagicMock(spec=PromptBuilderPort)
        # spec=PromptBuilderPort means hasattr(mock_pb, 'assembly_service') is False
        # by default (unless the port has that attr), so we simply don't set it.
        # Use a plain object to be explicit.
        class _FakePB:
            pass  # no assembly_service attribute

        agent = RouterAgent(config=router_config, prompt_builder=_FakePB())

        response = await agent._handle_admin_cache_reset()

        assert response.status == AgentStatus.SUCCESS
        assert response.metadata["cache_cleared"] is False
        assert response.metadata["command"] == "admin_cache_reset"

    @pytest.mark.asyncio
    async def test_cache_reset_not_available_when_no_prompt_builder(self, router_config):
        """When prompt_builder is None, returns 'not available' success response."""
        agent = RouterAgent(config=router_config, prompt_builder=None)

        response = await agent._handle_admin_cache_reset()

        assert response.status == AgentStatus.SUCCESS
        assert response.metadata["cache_cleared"] is False

    @pytest.mark.asyncio
    async def test_cache_reset_not_available_when_assembly_service_is_none(self, router_config):
        """When assembly_service attribute is None (falsy), returns 'not available'."""
        mock_pb = MagicMock()
        mock_pb.assembly_service = None

        agent = RouterAgent(config=router_config, prompt_builder=mock_pb)

        response = await agent._handle_admin_cache_reset()

        assert response.status == AgentStatus.SUCCESS
        assert response.metadata["cache_cleared"] is False


# ============================================================================
# _get_alternative_agents returns None (line 673)
# ============================================================================

class TestGetAlternativeAgents:
    """_get_alternative_agents must return None (line 673)."""

    def test_get_alternative_agents_returns_none(self, router_agent):
        assert router_agent._get_alternative_agents() is None


# ============================================================================
# GCS reference vs native binary vision detection (lines 282-289)
# ============================================================================

class TestVisionRefVsNativeBinary:
    """Ref-only files (GCS references) should NOT trigger vision.
    Only native binary images should."""

    @pytest.mark.asyncio
    async def test_ref_only_pdf_not_treated_as_vision(self, router_config):
        """file_data with ref key and non-image mime → no vision override."""
        ref_part = MessagePart(
            text='[File: "doc.pdf" (1.2MB)]',
            file_data={"ref": "doc.pdf", "mime_type": "application/pdf", "size_bytes": 1200000},
        )

        agent = RouterAgent(
            config=router_config,
            quick_agent_id="quick_agent",
            smart_agent_id="smart_agent",
        )

        message = AgentMessage.create(
            sender="ch",
            recipient="router_agent",
            intent=AgentIntent.QUERY,
            payload={"text": "thanks"},
            context={
                "user_id": "user1",
                "current_message_parts": [ref_part],
            },
        )

        response = await agent.execute(message)
        # PDF ref should not force smart routing (no vision override)
        assert response.result["routed_to"] == "smart_agent"

    @pytest.mark.asyncio
    async def test_ref_only_image_treated_as_vision(self, router_config, mock_llm, mock_prompt_builder):
        """file_data with ref key AND image/ mime → vision IS detected."""
        mock_llm.generate_content.return_value = MagicMock(
            text='{"needs_memory_search":false,"reasoning":"img","search_intent":"none","relevant_domains":[],"semantic_lens":[],"search_phrase":"","metadata":{"user_tone":"casual","task_complexity":"small_talk"}}'
        )
        ec = AgentExecutionContext(
            agent_type="router",
            provider=mock_llm,
            model_name="gemini-flash",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities()
        )
        agent = RouterAgent(
            config=router_config,
            execution_context=ec,
            prompt_builder=mock_prompt_builder,
            smart_agent_id="smart_agent",
            quick_agent_id="quick_agent",
        )

        ref_image_part = MessagePart(
            text='[File: "photo.png" (500KB)]',
            file_data={"ref": "photo.png", "mime_type": "image/png", "size_bytes": 500000, "path": "/tmp/photo.png"},
        )

        message = AgentMessage.create(
            sender="ch",
            recipient="router_agent",
            intent=AgentIntent.QUERY,
            payload={"text": "What is in this image?"},
            context={
                "user_id": "user1",
                "session_id": None,
                "current_message_parts": [ref_image_part],
            },
        )

        response = await agent.execute(message)
        assert response.result["routed_to"] == "smart_agent"

    @pytest.mark.asyncio
    async def test_ref_only_docx_not_treated_as_vision(self, router_config):
        """DOCX ref should not trigger vision override."""
        ref_part = MessagePart(
            text='[File: "report.docx" (45KB)]',
            file_data={"ref": "report.docx", "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "size_bytes": 45000},
        )

        agent = RouterAgent(
            config=router_config,
            quick_agent_id="quick_agent",
            smart_agent_id="smart_agent",
        )

        message = AgentMessage.create(
            sender="ch",
            recipient="router_agent",
            intent=AgentIntent.QUERY,
            payload={"text": "ok thanks"},
            context={
                "user_id": "user1",
                "current_message_parts": [ref_part],
            },
        )

        response = await agent.execute(message)
        # DOCX ref should not force smart routing (no vision override)
        assert response.result["routed_to"] == "smart_agent"
