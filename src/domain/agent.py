"""
Agent Communication Protocol (ACP)
Unified protocol for inter-agent communication in multi-agent architecture.
"""

from enum import Enum
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


class AgentIntent(str, Enum):
    """Intent type for agent messages."""
    QUERY = "query"              # Request information
    DELEGATE = "delegate"        # Delegate task execution
    INFORM = "inform"            # Share information
    REQUEST_FEEDBACK = "request_feedback"  # Ask for validation


@dataclass
class RoutingMetadata:
    """Typed routing metadata derived from triage classification."""
    user_tone: str
    complexity_score: int
    confidence: float
    needs_tools: List[str]
    reasoning: str
    semantic_lens: List[str] = field(default_factory=list)
    llm_target_agent: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_tone": self.user_tone,
            "complexity_score": self.complexity_score,
            "confidence": self.confidence,
            "needs_tools": self.needs_tools,
            "reasoning": self.reasoning,
            "semantic_lens": self.semantic_lens,
            "llm_target_agent": self.llm_target_agent
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RoutingMetadata":
        return cls(
            user_tone=data.get("user_tone", "friendly"),
            complexity_score=int(data.get("complexity_score", 5)),
            confidence=float(data.get("confidence", 0.5)),
            needs_tools=list(data.get("needs_tools", [])),
            reasoning=data.get("reasoning", ""),
            semantic_lens=list(data.get("semantic_lens", [])),
            llm_target_agent=data.get("llm_target_agent", "")
        )


class AgentStatus(str, Enum):
    """Status of agent response."""
    SUCCESS = "success"          # Task completed successfully
    PARTIAL = "partial"          # Partial results (e.g., some sources failed)
    FAILED = "failed"            # Task failed
    TIMEOUT = "timeout"          # Task exceeded timeout
    CANNOT_HANDLE = "cannot_handle"  # Agent cannot process this task


@dataclass
class AgentMessage:
    """
    Universal message for inter-agent communication.
    
    Represents a task or query sent from one agent to another.
    """
    task_id: str
    sender: str  # Agent ID or "brain_service"
    recipient: str  # Agent ID or "broadcast" for auto-routing
    intent: AgentIntent
    payload: Dict[str, Any]  # Task data (query, parameters, etc.)
    context: Dict[str, Any]  # Context (user_id, session_id, etc.)
    priority: int = 0  # 0 (low) - 10 (critical)
    timeout_ms: Optional[int] = None  # Explicit timeout (None = inherit from agent config)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @classmethod
    def create(
        cls,
        sender: str,
        recipient: str,
        intent: AgentIntent,
        payload: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
        priority: int = 0,
        timeout_ms: Optional[int] = None
    ) -> "AgentMessage":
        """Factory method for creating agent messages."""
        return cls(
            task_id=str(uuid4()),
            sender=sender,
            recipient=recipient,
            intent=intent,
            payload=payload,
            context=context or {},
            priority=priority,
            timeout_ms=timeout_ms
        )


@dataclass
class AgentResponse:
    """
    Response from an agent after processing a task.
    
    Contains result, status, confidence, and metadata.
    """
    task_id: str  # Links back to original AgentMessage
    agent_id: str  # ID of the agent that processed the task
    status: AgentStatus
    result: Any  # Actual result data
    confidence: float  # 0.0-1.0 confidence score
    metadata: Dict[str, Any] = field(default_factory=dict)  # tokens_used, latency_ms, etc.
    error: Optional[str] = None  # Error message if failed
    suggestions: Optional[List[str]] = None  # Alternative actions/agents
    
    @classmethod
    def success(
        cls,
        task_id: str,
        agent_id: str,
        result: Any,
        confidence: float = 1.0,
        metadata: Optional[Dict[str, Any]] = None
    ) -> "AgentResponse":
        """Factory method for successful responses."""
        return cls(
            task_id=task_id,
            agent_id=agent_id,
            status=AgentStatus.SUCCESS,
            result=result,
            confidence=confidence,
            metadata=metadata or {}
        )
    
    @classmethod
    def failure(
        cls,
        task_id: str,
        agent_id: str,
        error: str,
        suggestions: Optional[List[str]] = None
    ) -> "AgentResponse":
        """Factory method for failed responses."""
        return cls(
            task_id=task_id,
            agent_id=agent_id,
            status=AgentStatus.FAILED,
            result=None,
            confidence=0.0,
            error=error,
            suggestions=suggestions
        )
    
    @classmethod
    def cannot_handle(
        cls,
        task_id: str,
        agent_id: str,
        suggestions: Optional[List[str]] = None
    ) -> "AgentResponse":
        """Factory method for cannot handle responses."""
        return cls(
            task_id=task_id,
            agent_id=agent_id,
            status=AgentStatus.CANNOT_HANDLE,
            result=None,
            confidence=0.0,
            error="Agent cannot handle this task type",
            suggestions=suggestions
        )


@dataclass
class AgentConfig:
    """Configuration for an agent."""
    agent_id: str
    agent_type: str  # "memory_search", "web_search", "observation", etc.
    llm_model: Optional[str] = None  # Model to use (None = no LLM needed)
    max_retries: int = 2
    timeout_ms: Optional[int] = None  # Explicit timeout per agent (None = no timeout)
    circuit_breaker_threshold: int = 3  # Failures before opening circuit
    circuit_breaker_recovery_ms: int = 300000  # 5 minutes
    capabilities: List[str] = field(default_factory=list)  # What can this agent do
    metadata: Dict[str, Any] = field(default_factory=dict)
