"""
Unit tests for BaseAgent and CircuitBreaker.
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
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
