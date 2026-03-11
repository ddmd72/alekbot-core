"""
Unit tests for Agent Communication Protocol (ACP).
"""

import pytest
from datetime import datetime
from src.domain.agent import (
    AgentMessage,
    AgentResponse,
    AgentConfig,
    AgentIntent,
    AgentStatus
)


class TestAgentProtocol:
    """Test suite for Agent Communication Protocol."""

    def test_agent_message_creation(self):
        """Test creating an AgentMessage."""
        message = AgentMessage.create(
            sender="brain_service",
            recipient="memory_agent",
            intent=AgentIntent.QUERY,
            payload={"query": "test query"},
            context={"user_id": "user123"}
        )

        assert message.sender == "brain_service"
        assert message.recipient == "memory_agent"
        assert message.intent == AgentIntent.QUERY
        assert message.payload == {"query": "test query"}
        assert message.context == {"user_id": "user123"}
        assert message.task_id is not None
        assert isinstance(message.created_at, datetime)
        assert message.priority == 0
        assert message.timeout_ms is None

    def test_agent_message_defaults(self):
        """Test AgentMessage defaults."""
        message = AgentMessage.create(
            sender="sender",
            recipient="recipient",
            intent=AgentIntent.INFORM,
            payload={}
        )

        assert message.context == {}
        assert message.priority == 0
        assert message.timeout_ms is None

    def test_agent_response_success(self):
        """Test creating a successful AgentResponse."""
        response = AgentResponse.success(
            task_id="task123",
            agent_id="agent1",
            result={"data": "success"},
            confidence=0.9,
            metadata={"latency": 100}
        )

        assert response.task_id == "task123"
        assert response.agent_id == "agent1"
        assert response.status == AgentStatus.SUCCESS
        assert response.result == {"data": "success"}
        assert response.confidence == 0.9
        assert response.metadata == {"latency": 100}
        assert response.error is None

    def test_agent_response_failure(self):
        """Test creating a failed AgentResponse."""
        response = AgentResponse.failure(
            task_id="task123",
            agent_id="agent1",
            error="Something went wrong",
            suggestions=["try_again"]
        )

        assert response.task_id == "task123"
        assert response.agent_id == "agent1"
        assert response.status == AgentStatus.FAILED
        assert response.result is None
        assert response.confidence == 0.0
        assert response.error == "Something went wrong"
        assert response.suggestions == ["try_again"]

    def test_agent_response_cannot_handle(self):
        """Test creating a cannot_handle AgentResponse."""
        response = AgentResponse.cannot_handle(
            task_id="task123",
            agent_id="agent1",
            suggestions=["other_agent"]
        )

        assert response.task_id == "task123"
        assert response.agent_id == "agent1"
        assert response.status == AgentStatus.CANNOT_HANDLE
        assert response.result is None
        assert response.confidence == 0.0
        assert response.error == "Agent cannot handle this task type"
        assert response.suggestions == ["other_agent"]

    def test_agent_config_defaults(self):
        """Test AgentConfig defaults."""
        config = AgentConfig(
            agent_id="test_agent",
            agent_type="test_type"
        )

        assert config.agent_id == "test_agent"
        assert config.agent_type == "test_type"
        assert config.llm_model is None
        assert config.max_retries == 2
        assert config.timeout_ms is None
        assert config.circuit_breaker_threshold == 3
        assert config.circuit_breaker_recovery_ms == 300000
        assert config.capabilities == []
        assert config.metadata == {}

    def test_agent_intent_enum(self):
        """Test AgentIntent enum values."""
        assert AgentIntent.QUERY == "query"
        assert AgentIntent.DELEGATE == "delegate"
        assert AgentIntent.INFORM == "inform"
        assert AgentIntent.REQUEST_FEEDBACK == "request_feedback"

    def test_agent_status_enum(self):
        """Test AgentStatus enum values."""
        assert AgentStatus.SUCCESS == "success"
        assert AgentStatus.PARTIAL == "partial"
        assert AgentStatus.FAILED == "failed"
        assert AgentStatus.TIMEOUT == "timeout"
        assert AgentStatus.CANNOT_HANDLE == "cannot_handle"
