"""
Unit tests for BaseAgent and CircuitBreaker.
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, MagicMock, patch
from src.agents.base_agent import BaseAgent, CircuitBreaker
from src.domain.agent import AgentConfig, AgentMessage, AgentResponse, AgentIntent, AgentStatus


class MockAgent(BaseAgent):
    """Mock implementation of BaseAgent for testing."""
    
    def __init__(self, config, circuit_breaker=None):
        super().__init__(config, circuit_breaker)
        self.can_handle_result = True
        self.execute_result = None
        self.execute_error = None
        self.execute_delay = 0
        self.execute_calls = 0

    async def can_handle(self, message: AgentMessage) -> bool:
        return self.can_handle_result

    async def execute(self, message: AgentMessage) -> AgentResponse:
        self.execute_calls += 1
        if self.execute_delay > 0:
            await asyncio.sleep(self.execute_delay)
        
        if self.execute_error:
            raise self.execute_error
            
        return self.execute_result or AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result="success"
        )


class TestCircuitBreaker:
    """Test suite for CircuitBreaker."""

    def test_initial_state(self):
        """Test initial state is closed."""
        cb = CircuitBreaker()
        assert not cb.is_open("agent1", threshold=3, recovery_ms=1000)
        status = cb.get_status("agent1")
        assert status["status"] == "closed"
        assert status["failures"] == 0

    def test_failure_counting(self):
        """Test recording failures."""
        cb = CircuitBreaker()
        cb.record_failure("agent1")
        
        status = cb.get_status("agent1")
        assert status["failures"] == 1
        
        cb.record_failure("agent1")
        status = cb.get_status("agent1")
        assert status["failures"] == 2

    def test_circuit_opening(self):
        """Test circuit opens after threshold."""
        cb = CircuitBreaker()
        threshold = 3
        
        # Record failures up to threshold
        for _ in range(threshold):
            assert not cb.is_open("agent1", threshold, 1000)
            cb.record_failure("agent1")
            
        # Should be open now
        assert cb.is_open("agent1", threshold, 1000)
        status = cb.get_status("agent1")
        assert status["status"] == "open"

    def test_success_resets_count(self):
        """Test success resets failure count."""
        cb = CircuitBreaker()
        cb.record_failure("agent1")
        cb.record_failure("agent1")
        
        cb.record_success("agent1")
        
        status = cb.get_status("agent1")
        assert status["failures"] == 0
        assert status["status"] == "closed"

    def test_auto_recovery(self):
        """Test circuit recovers after timeout."""
        cb = CircuitBreaker()
        threshold = 1
        recovery_ms = 100  # Short recovery for test
        
        cb.record_failure("agent1")
        assert cb.is_open("agent1", threshold, recovery_ms)
        
        # Wait for recovery
        import time
        time.sleep(0.2)
        
        assert not cb.is_open("agent1", threshold, recovery_ms)
        status = cb.get_status("agent1")
        assert status["failures"] == 0  # Should be reset


class TestBaseAgent:
    """Test suite for BaseAgent."""

    @pytest.fixture
    def config(self):
        return AgentConfig(
            agent_id="test_agent",
            agent_type="mock",
            max_retries=2,
            timeout_ms=1000,
            circuit_breaker_threshold=3,
            circuit_breaker_recovery_ms=1000
        )

    @pytest.fixture
    def message(self):
        return AgentMessage.create(
            sender="test",
            recipient="test_agent",
            intent=AgentIntent.QUERY,
            payload={}
        )

    @pytest.mark.asyncio
    async def test_process_success(self, config, message):
        """Test successful processing."""
        agent = MockAgent(config)
        response = await agent.process(message)
        
        assert response.status == AgentStatus.SUCCESS
        assert agent.execute_calls == 1
        
        # Verify circuit breaker recorded success
        status = agent.circuit_breaker.get_status(agent.agent_id)
        assert status["failures"] == 0

    @pytest.mark.asyncio
    async def test_process_cannot_handle(self, config, message):
        """Test when agent cannot handle message."""
        agent = MockAgent(config)
        agent.can_handle_result = False
        
        response = await agent.process(message)
        
        assert response.status == AgentStatus.CANNOT_HANDLE
        assert agent.execute_calls == 0

    @pytest.mark.asyncio
    async def test_retry_logic(self, config, message):
        """Test retry logic on failure."""
        agent = MockAgent(config)
        agent.execute_error = ValueError("Temporary error")
        
        # Mock sleep to speed up test
        with patch("asyncio.sleep", new_callable=AsyncMock):
            response = await agent.process(message)
        
        # Should try initial + max_retries
        assert agent.execute_calls == config.max_retries + 1
        assert response.status == AgentStatus.FAILED
        assert "Max retries exceeded" in response.error

    @pytest.mark.asyncio
    async def test_circuit_breaker_integration(self, config, message):
        """Test circuit breaker prevents execution."""
        agent = MockAgent(config)
        
        # Force open circuit
        for _ in range(config.circuit_breaker_threshold):
            agent.circuit_breaker.record_failure(agent.agent_id)
            
        response = await agent.process(message)
        
        assert response.status == AgentStatus.FAILED
        assert "Circuit breaker is open" in response.error
        assert agent.execute_calls == 0

    @pytest.mark.asyncio
    async def test_timeout_enforcement(self, config, message):
        """Test timeout enforcement."""
        agent = MockAgent(config)
        # Set delay longer than timeout
        agent.execute_delay = 2.0  # 2s > 1s timeout
        message.timeout_ms = 100   # 0.1s timeout

        # Mock sleep to avoid actual waiting but allow timeout logic
        # Note: We can't easily mock sleep inside wait_for, so we rely on
        # asyncio.wait_for raising TimeoutError correctly with real sleep
        # but we use a very short timeout in message

        # For this test, we need real sleep behavior for wait_for to work
        # so we don't mock sleep, but use small values

        response = await agent.process(message)

        assert response.status == AgentStatus.FAILED
        assert "Max retries exceeded" in response.error
        # Should have tried retries
        assert agent.execute_calls > 1

    @pytest.mark.asyncio
    async def test_can_handle_exception_returns_failure(self, config, message):
        """can_handle() raising an exception returns failure response."""
        agent = MockAgent(config)
        agent.can_handle_result = True

        async def boom(msg):
            raise RuntimeError("capability check failed")

        agent.can_handle = boom
        response = await agent.process(message)

        assert response.status == AgentStatus.FAILED
        assert "Capability check failed" in response.error
        assert agent.execute_calls == 0

    def test_get_status_returns_dict(self, config):
        agent = MockAgent(config)
        status = agent.get_status()
        assert status["agent_id"] == "test_agent"
        assert status["agent_type"] == "mock"
        assert "circuit_breaker" in status


class TestLoadConversationContext:
    """Tests for BaseAgent._load_conversation_context."""

    @pytest.fixture
    def agent(self):
        config = AgentConfig(agent_id="ctx_agent", agent_type="mock")
        return MockAgent(config)

    @pytest.mark.asyncio
    async def test_no_session_id_returns_current_message(self, agent):
        from src.ports.llm_port import MessagePart
        parts = [MessagePart(text="hello")]
        result = await agent._load_conversation_context(None, "", parts, 10)
        assert len(result) == 1
        assert result[0].role == "user"

    @pytest.mark.asyncio
    async def test_no_session_store_returns_current_message(self, agent):
        from src.ports.llm_port import MessagePart
        parts = [MessagePart(text="hello")]
        result = await agent._load_conversation_context(None, "sess-1", parts, 10)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_no_session_id_empty_parts_returns_empty(self, agent):
        result = await agent._load_conversation_context(None, "", [], 10)
        assert result == []

    @pytest.mark.asyncio
    async def test_with_session_store_normal_path(self, agent):
        from unittest.mock import AsyncMock, MagicMock
        from src.ports.llm_port import Message, MessagePart

        session_store = AsyncMock()
        prev_msg = Message(role="user", parts=[MessagePart(text="prev")])
        session = MagicMock()
        session.history = [prev_msg]
        session_store.load_session.return_value = session

        current_parts = [MessagePart(text="current")]
        result = await agent._load_conversation_context(session_store, "sess-1", current_parts, 10)
        assert len(result) == 2
        assert result[-1].role == "user"

    @pytest.mark.asyncio
    async def test_history_truncated_to_context_window(self, agent):
        from unittest.mock import AsyncMock, MagicMock
        from src.ports.llm_port import Message, MessagePart

        session_store = AsyncMock()
        session = MagicMock()
        session.history = [
            Message(role="user", parts=[MessagePart(text=f"msg {i}")])
            for i in range(20)
        ]
        session_store.load_session.return_value = session

        result = await agent._load_conversation_context(session_store, "sess-1", [], 5)
        assert len(result) == 5

    @pytest.mark.asyncio
    async def test_session_store_exception_falls_back_to_current(self, agent):
        from unittest.mock import AsyncMock
        from src.ports.llm_port import MessagePart

        session_store = AsyncMock()
        session_store.load_session.side_effect = RuntimeError("Firestore down")

        parts = [MessagePart(text="current")]
        result = await agent._load_conversation_context(session_store, "sess-1", parts, 10)
        assert len(result) == 1
        assert "current" in result[0].parts[0].text


class TestInjectTimestamps:
    """Tests for BaseAgent._inject_timestamps."""

    @pytest.fixture
    def agent(self):
        config = AgentConfig(agent_id="ts_agent", agent_type="mock")
        return MockAgent(config)

    def test_user_message_gets_timestamp_prefix(self, agent):
        """User messages with created_at get a timestamp prepended to the first part."""
        from src.ports.llm_port import Message, MessagePart
        msg = Message(role="user", parts=[MessagePart(text="hello")], created_at=1700000000)
        result = agent._inject_timestamps([msg])
        assert "hello" in result[0].parts[0].text
        assert result[0].parts[0].text != "hello"  # timestamp was prepended

    def test_model_message_unchanged(self, agent):
        from src.ports.llm_port import Message, MessagePart
        msg = Message(role="model", parts=[MessagePart(text="response")], created_at=1700000000)
        result = agent._inject_timestamps([msg])
        assert result[0].parts[0].text == "response"

    def test_user_message_multiple_parts_only_first_gets_stamp(self, agent):
        from src.ports.llm_port import Message, MessagePart
        msg = Message(
            role="user",
            parts=[MessagePart(text="first"), MessagePart(text="second")],
            created_at=1700000000,
        )
        result = agent._inject_timestamps([msg])
        # Second part should be unchanged
        assert result[0].parts[1].text == "second"


class TestApplyHistoryTier:
    """Tests for BaseAgent._apply_history_tier."""

    @pytest.fixture
    def agent(self):
        config = AgentConfig(agent_id="tier_agent", agent_type="mock")
        a = MockAgent(config)
        a.HISTORY_FULL_TURNS = 1
        return a

    def test_recent_model_turn_uses_full_text(self, agent):
        from src.ports.llm_port import Message, MessagePart
        msg = Message(
            role="model",
            parts=[MessagePart(text="summary", full_text="full response")],
        )
        result = agent._apply_history_tier([msg], max_full_turns=1)
        assert result[0].parts[0].text == "full response"

    def test_older_model_turn_uses_summary(self, agent):
        from src.ports.llm_port import Message, MessagePart
        old = Message(role="model", parts=[MessagePart(text="old summary", full_text="old full")])
        recent = Message(role="model", parts=[MessagePart(text="new summary", full_text="new full")])
        # max_full_turns=0: only the very last turn (model_turns_from_end==0) is full
        result = agent._apply_history_tier([old, recent], max_full_turns=0)
        # recent (index 1) should be full (model_turns_from_end was 0 when processed)
        assert result[1].parts[0].text == "new full"
        # old (index 0) should pass through as-is (model_turns_from_end==1 > 0)
        assert result[0].parts[0].text == "old summary"

    def test_user_message_with_file_uses_full_text_when_recent(self, agent):
        from src.ports.llm_port import Message, MessagePart
        msg = Message(
            role="user",
            parts=[MessagePart(text="stub", full_text="full file content")],
        )
        result = agent._apply_history_tier([msg], max_full_turns=2)
        assert result[0].parts[0].text == "full file content"

    def test_user_message_with_file_uses_stub_when_older(self, agent):
        from src.ports.llm_port import Message, MessagePart
        file_msg = Message(
            role="user",
            parts=[MessagePart(text="stub", full_text="full file content")],
        )
        model_msg1 = Message(role="model", parts=[MessagePart(text="r1")])
        model_msg2 = Message(role="model", parts=[MessagePart(text="r2")])
        # 2 model turns after file_msg → file_msg is beyond max_full_turns=1
        result = agent._apply_history_tier([file_msg, model_msg1, model_msg2], max_full_turns=1)
        assert result[0].parts[0].text == "stub"


class TestOnDelegation:
    """Tests for BaseAgent._on_delegation."""

    def test_short_query_logged_unchanged(self):
        config = AgentConfig(agent_id="orch", agent_type="orchestrator")
        agent = MockAgent(config)
        # Should not raise
        agent._on_delegation("search_memory", "short query")

    def test_long_query_truncated_in_log(self):
        config = AgentConfig(agent_id="orch", agent_type="orchestrator")
        agent = MockAgent(config)
        long_query = "a" * 100
        # Should not raise; internally truncates to 60 chars
        agent._on_delegation("search_memory", long_query)


class TestBuildDelegateToolDeclaration:
    """Tests for BaseAgent._build_delegate_tool_declaration."""

    def test_no_intents_produces_no_available_message(self):
        result = BaseAgent._build_delegate_tool_declaration([])
        assert "(no specialist agents registered)" in result["description"]

    def test_intents_listed_in_description(self):
        intents = [{"name": "search_memory", "description": "Search memory"}]
        result = BaseAgent._build_delegate_tool_declaration(intents)
        assert "search_memory" in result["description"]

    def test_context_schema_fields_added_to_context_param(self):
        intents = [
            {
                "name": "get_email_details",
                "description": "Get email",
                "context_schema": {"email_id": "The email ID to fetch"},
            }
        ]
        result = BaseAgent._build_delegate_tool_declaration(intents)
        context_param = result["parameters"]["properties"]["context"]
        assert "properties" in context_param
        assert "email_id" in context_param["properties"]

    def test_no_context_schema_no_properties(self):
        intents = [{"name": "search_web", "description": "Search", "context_schema": {}}]
        result = BaseAgent._build_delegate_tool_declaration(intents)
        context_param = result["parameters"]["properties"]["context"]
        assert "properties" not in context_param


class TestFormatHistoryForDebug:
    """Tests for BaseAgent._format_history_for_debug."""

    def test_text_part(self):
        from src.ports.llm_port import Message, MessagePart
        msg = Message(role="user", parts=[MessagePart(text="hello")])
        result = BaseAgent._format_history_for_debug([msg])
        assert "hello" in result
        assert "USER" in result

    def test_tool_call_part(self):
        from src.ports.llm_port import Message, MessagePart, ToolCall
        tc = ToolCall(name="search_memory", args={"query": "test"})
        msg = Message(role="model", parts=[MessagePart(tool_call=tc)])
        result = BaseAgent._format_history_for_debug([msg])
        assert "tool_call" in result
        assert "search_memory" in result

    def test_tool_response_dict(self):
        from src.ports.llm_port import Message, MessagePart
        msg = Message(role="user", parts=[MessagePart(tool_response={"name": "search_memory", "response": {}})])
        result = BaseAgent._format_history_for_debug([msg])
        assert "tool_response" in result
        assert "search_memory" in result

    def test_file_data_part(self):
        from src.ports.llm_port import Message, MessagePart
        msg = Message(role="user", parts=[MessagePart(file_data={"uri": "gs://bucket/f", "mime_type": "text/plain"})])
        result = BaseAgent._format_history_for_debug([msg])
        assert "file_data" in result

    def test_empty_part_shows_raw_content(self):
        from src.ports.llm_port import Message, MessagePart
        part = MessagePart()
        msg = Message(role="model", parts=[part])
        result = BaseAgent._format_history_for_debug([msg])
        assert "raw_content" in result

    def test_multiple_messages_separated_by_dashes(self):
        from src.ports.llm_port import Message, MessagePart
        msgs = [
            Message(role="user", parts=[MessagePart(text="q")]),
            Message(role="model", parts=[MessagePart(text="a")]),
        ]
        result = BaseAgent._format_history_for_debug(msgs)
        assert "---" in result


class TestCallLlmNoLlm:
    """Tests for BaseAgent._call_llm when no LLM is configured."""

    @pytest.mark.asyncio
    async def test_no_llm_raises_runtime_error(self):
        config = AgentConfig(agent_id="bare_agent", agent_type="mock")
        agent = MockAgent(config)
        # No self.llm or self._llm set
        from src.ports.llm_port import LLMRequest
        request = LLMRequest(model_name="test", messages=[])
        with pytest.raises(RuntimeError, match="no LLM service configured"):
            await agent._call_llm(request)


class TestDebugMethods:
    """Tests for _debug_prompt, _debug_response, _debug_llm_response."""

    @pytest.fixture
    def agent(self):
        config = AgentConfig(agent_id="debug_agent", agent_type="mock")
        return MockAgent(config)

    def test_debug_prompt_disabled_noop(self, agent):
        with patch("src.agents.base_agent.get_debug_logger") as mock_gdl:
            mock_gdl.return_value.enabled = False
            agent._debug_prompt("system", "content", turn=1, model="test")
        mock_gdl.return_value.log_prompt.assert_not_called()

    def test_debug_prompt_enabled_calls_log_prompt(self, agent):
        with patch("src.agents.base_agent.get_debug_logger") as mock_gdl:
            mock_logger = MagicMock()
            mock_logger.enabled = True
            mock_gdl.return_value = mock_logger
            agent._debug_prompt("system instr", "user content", turn=2, model="gemini")
        mock_logger.log_prompt.assert_called_once()
        call_kwargs = mock_logger.log_prompt.call_args.kwargs
        assert call_kwargs.get("system_instruction") == "system instr"
        assert call_kwargs.get("metadata", {}).get("model") == "gemini"

    def test_debug_response_disabled_noop(self, agent):
        with patch("src.agents.base_agent.get_debug_logger") as mock_gdl:
            mock_gdl.return_value.enabled = False
            agent._debug_response("text", tokens=10, turn=1)
        mock_gdl.return_value.log_response.assert_not_called()

    def test_debug_response_enabled_calls_log_response(self, agent):
        with patch("src.agents.base_agent.get_debug_logger") as mock_gdl:
            mock_logger = MagicMock()
            mock_logger.enabled = True
            mock_gdl.return_value = mock_logger
            agent._debug_response("response text", tokens=50, turn=3)
        mock_logger.log_response.assert_called_once()

    def test_debug_llm_response_disabled_noop(self, agent):
        from src.ports.llm_port import LLMResponse
        resp = MagicMock(spec=LLMResponse)
        with patch("src.agents.base_agent.get_debug_logger") as mock_gdl:
            mock_gdl.return_value.enabled = False
            agent._debug_llm_response(resp, turn=1)
        mock_gdl.return_value.log_response.assert_not_called()

    def test_debug_llm_response_with_tool_calls_and_usage(self, agent):
        from src.ports.llm_port import LLMResponse, ToolCall, UsageMetadata
        resp = MagicMock()
        resp.text = "Answer."
        resp.tool_calls = [ToolCall(name="search_memory", args={"q": "test"})]
        resp.usage_metadata = MagicMock()
        resp.usage_metadata.total_tokens = 100
        with patch("src.agents.base_agent.get_debug_logger") as mock_gdl:
            mock_logger = MagicMock()
            mock_logger.enabled = True
            mock_gdl.return_value = mock_logger
            agent._debug_llm_response(resp, turn=2)
        mock_logger.log_response.assert_called_once()
        logged_text = mock_logger.log_response.call_args.kwargs["response"]
        import json
        data = json.loads(logged_text)
        assert "tool_calls" in data
        assert data["tokens"] == 100


# =============================================================================
# Abstract method bodies (lines 170, 186)
# =============================================================================

class TestAbstractMethodBodies:
    """Call abstract method bodies directly via unbound invocation."""

    @pytest.mark.asyncio
    async def test_abstract_can_handle_body_returns_none(self):
        config = AgentConfig(agent_id="a", agent_type="mock")
        agent = MockAgent(config)
        message = AgentMessage.create(
            sender="t", recipient="a", intent=AgentIntent.QUERY, payload={}
        )
        # Call the abstract body directly — returns None (pass)
        result = await BaseAgent.can_handle(agent, message)
        assert result is None

    @pytest.mark.asyncio
    async def test_abstract_execute_body_returns_none(self):
        config = AgentConfig(agent_id="a", agent_type="mock")
        agent = MockAgent(config)
        message = AgentMessage.create(
            sender="t", recipient="a", intent=AgentIntent.QUERY, payload={}
        )
        result = await BaseAgent.execute(agent, message)
        assert result is None


# =============================================================================
# _apply_history_tier — user plain text (line 322)
# =============================================================================

class TestApplyHistoryTierPlainUser:

    def test_plain_user_message_inserted_unchanged(self):
        """User message with no full_text → inserted as-is (line 322)."""
        from src.ports.llm_port import Message, MessagePart
        config = AgentConfig(agent_id="a", agent_type="mock")
        agent = MockAgent(config)
        agent.HISTORY_FULL_TURNS = 1
        msg = Message(role="user", parts=[MessagePart(text="plain question")])
        result = agent._apply_history_tier([msg], max_full_turns=1)
        assert len(result) == 1
        assert result[0].parts[0].text == "plain question"


# =============================================================================
# _execute_with_timeout — no timeout (line 458)
# =============================================================================

class TestExecuteWithTimeoutNoTimeout:

    @pytest.mark.asyncio
    async def test_no_timeout_executes_directly(self):
        """Both message.timeout_ms and config.timeout_ms are None → execute directly (line 458)."""
        config = AgentConfig(agent_id="notimeout_agent", agent_type="mock", timeout_ms=None)
        agent = MockAgent(config)
        message = AgentMessage.create(
            sender="t", recipient="notimeout_agent", intent=AgentIntent.QUERY, payload={}
        )
        response = await agent._execute_with_timeout(message)
        assert response.status == AgentStatus.SUCCESS


# =============================================================================
# _on_agent_start (lines 486-487)
# =============================================================================

class TestOnAgentStart:

    def test_short_text_no_truncation(self):
        config = AgentConfig(agent_id="a", agent_type="mock")
        agent = MockAgent(config)
        # Should not raise; text ≤ 60 chars
        agent._on_agent_start("short text")

    def test_long_text_truncated_in_preview(self):
        config = AgentConfig(agent_id="a", agent_type="mock")
        agent = MockAgent(config)
        # text > 60 chars → preview truncated
        agent._on_agent_start("x" * 80)


# =============================================================================
# _on_agent_success (lines 495-504)
# =============================================================================

class TestOnAgentSuccess:

    def test_with_token_count_logs_tokens(self):
        config = AgentConfig(agent_id="a", agent_type="mock")
        agent = MockAgent(config)
        agent._on_agent_success(char_count=100, token_count=50)

    def test_without_token_count_logs_chars_only(self):
        config = AgentConfig(agent_id="a", agent_type="mock")
        agent = MockAgent(config)
        agent._on_agent_success(char_count=100, token_count=0)

    def test_with_output_text_calls_debug_logger(self):
        config = AgentConfig(agent_id="a", agent_type="mock")
        agent = MockAgent(config)
        with patch("src.agents.base_agent.get_debug_logger") as mock_gdl:
            mock_logger = MagicMock()
            mock_logger.enabled = True
            mock_gdl.return_value = mock_logger
            agent._on_agent_success(char_count=20, token_count=10, output_text="hello")
        mock_logger.log_response.assert_called_once()


# =============================================================================
# _flush_billing (lines 510-516)
# =============================================================================

class TestFlushBilling:

    @pytest.mark.asyncio
    async def test_flush_billing_creates_task_with_coordinator(self):
        """coordinator + account_id + tokens → asyncio.create_task called (lines 510-516)."""
        config = AgentConfig(agent_id="a", agent_type="mock")
        agent = MockAgent(config)
        agent.coordinator = MagicMock()
        agent.coordinator.route_message = AsyncMock(return_value=None)
        agent._billing_account_id = "acc1"
        agent._billing_prompt_tokens = 100
        agent._billing_completion_tokens = 50

        with patch("src.agents.base_agent.asyncio.create_task") as mock_create_task:
            await agent._flush_billing()
        mock_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_flush_billing_noop_without_coordinator(self):
        """No coordinator → no-op (early return)."""
        config = AgentConfig(agent_id="a", agent_type="mock")
        agent = MockAgent(config)
        agent._billing_account_id = "acc1"
        agent._billing_prompt_tokens = 100
        # No coordinator set — should return silently
        with patch("src.agents.base_agent.asyncio.create_task") as mock_create_task:
            await agent._flush_billing()
        mock_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_flush_billing_noop_without_tokens(self):
        """coordinator + account_id but no tokens → no-op (early return)."""
        config = AgentConfig(agent_id="a", agent_type="mock")
        agent = MockAgent(config)
        agent.coordinator = MagicMock()
        agent._billing_account_id = "acc1"
        # all billing token counts remain 0
        with patch("src.agents.base_agent.asyncio.create_task") as mock_create_task:
            await agent._flush_billing()
        mock_create_task.assert_not_called()


# =============================================================================
# _on_agent_error (lines 545-548)
# =============================================================================

class TestOnAgentError:

    def test_llm_rate_limit_error_logs_without_exc_info(self):
        from src.domain.exceptions import LLMRateLimitError
        config = AgentConfig(agent_id="a", agent_type="mock")
        agent = MockAgent(config)
        # Should not raise; logs without exc_info
        agent._on_agent_error(LLMRateLimitError("rate limit"), context="test")

    def test_llm_unavailable_error_logs_without_exc_info(self):
        from src.domain.exceptions import LLMUnavailableError
        config = AgentConfig(agent_id="a", agent_type="mock")
        agent = MockAgent(config)
        agent._on_agent_error(LLMUnavailableError("unavailable"), context="test")

    def test_generic_error_logs_with_exc_info(self):
        config = AgentConfig(agent_id="a", agent_type="mock")
        agent = MockAgent(config)
        agent._on_agent_error(ValueError("bad value"), context="test")


# =============================================================================
# _debug_raw_turn (lines 715-716)
# =============================================================================

class TestDebugRawTurn:

    def test_debug_raw_turn_logs_summary(self):
        config = AgentConfig(agent_id="a", agent_type="mock")
        agent = MockAgent(config)
        # Should not raise
        agent._debug_raw_turn(
            system_blocks=[{"text": "system instruction"}],
            user_content="user message",
            response_texts=["response text 1", "response text 2"],
            tokens=150,
            turn=0,
            model="claude-sonnet",
        )


# =============================================================================
# _set_execution_context (line 728)
# =============================================================================

class TestSetExecutionContext:

    def test_sets_context_attribute(self):
        from src.ports.llm_port import AgentExecutionContext, ProviderCapabilities, LLMPort
        from src.domain.user import PerformanceTier
        config = AgentConfig(agent_id="a", agent_type="mock")
        agent = MockAgent(config)
        mock_provider = MagicMock(spec=LLMPort)
        ctx = AgentExecutionContext(
            agent_type="mock",
            provider=mock_provider,
            model_name="gemini",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities(),
        )
        agent._set_execution_context(ctx)
        assert agent._agent_execution_context is ctx


# =============================================================================
# _call_llm — full paths (lines 748-785)
# =============================================================================

class TestCallLlmFullPaths:
    """Tests for _call_llm when LLM is configured."""

    def _make_agent_with_llm(self, mock_llm):
        config = AgentConfig(agent_id="llm_agent", agent_type="mock")
        agent = MockAgent(config)
        agent.llm = mock_llm
        return agent

    @pytest.mark.asyncio
    async def test_normal_success_accumulates_billing_tokens(self):
        """Normal LLM call → usage_metadata billed (lines 776-784)."""
        from src.ports.llm_port import LLMRequest, LLMResponse, UsageMetadata
        mock_llm = MagicMock()
        mock_llm.generate_content = AsyncMock(return_value=LLMResponse(
            text="answer",
            usage_metadata=UsageMetadata(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        ))
        agent = self._make_agent_with_llm(mock_llm)
        request = LLMRequest(model_name="test", messages=[])
        response = await agent._call_llm(request)
        assert response.text == "answer"
        assert agent._billing_prompt_tokens == 10
        assert agent._billing_completion_tokens == 5

    @pytest.mark.asyncio
    async def test_debug_enabled_logs_request(self):
        """debug.enabled=True → log_llm_request called (lines 748-754)."""
        from src.ports.llm_port import LLMRequest, LLMResponse
        mock_llm = MagicMock()
        mock_llm.generate_content = AsyncMock(return_value=LLMResponse(text="ok"))
        agent = self._make_agent_with_llm(mock_llm)
        request = LLMRequest(model_name="test", messages=[])
        with patch("src.agents.base_agent.get_debug_logger") as mock_gdl:
            mock_logger = MagicMock()
            mock_logger.enabled = True
            mock_gdl.return_value = mock_logger
            await agent._call_llm(request)
        mock_logger.log_llm_request.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limit_error_uses_fallback_provider(self):
        """LLMRateLimitError + fallback configured → retries with fallback (lines 757-772)."""
        from src.domain.exceptions import LLMRateLimitError
        from src.ports.llm_port import LLMRequest, LLMResponse, AgentExecutionContext, ProviderCapabilities, LLMPort
        from src.domain.user import PerformanceTier

        mock_primary = MagicMock(spec=LLMPort)
        mock_primary.generate_content = AsyncMock(side_effect=LLMRateLimitError("rate limit"))
        mock_fallback = MagicMock(spec=LLMPort)
        mock_fallback.generate_content = AsyncMock(return_value=LLMResponse(text="fallback answer"))

        agent = self._make_agent_with_llm(mock_primary)
        ctx = AgentExecutionContext(
            agent_type="mock",
            provider=mock_primary,
            model_name="primary-model",
            tier=PerformanceTier.ECO,
            capabilities=ProviderCapabilities(),
            fallback_provider=mock_fallback,
            fallback_model_name="fallback-model",
            fallback_provider_name="fallback",
        )
        agent._set_execution_context(ctx)

        request = LLMRequest(model_name="primary-model", messages=[])
        response = await agent._call_llm(request)
        assert response.text == "fallback answer"
        mock_fallback.generate_content.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limit_error_reraises_without_fallback(self):
        """LLMRateLimitError + no fallback → re-raises (lines 773-774)."""
        from src.domain.exceptions import LLMRateLimitError
        from src.ports.llm_port import LLMRequest

        mock_llm = MagicMock()
        mock_llm.generate_content = AsyncMock(side_effect=LLMRateLimitError("rate limit"))
        agent = self._make_agent_with_llm(mock_llm)
        # No execution context set → fallback_provider is None

        request = LLMRequest(model_name="test", messages=[])
        with pytest.raises(LLMRateLimitError):
            await agent._call_llm(request)

