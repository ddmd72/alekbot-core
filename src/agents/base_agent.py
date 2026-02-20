"""
Base Agent Infrastructure
=========================

Provides abstract base class and utilities for all agents.
"""

import time
import asyncio
from datetime import datetime, timezone
from abc import ABC, abstractmethod
from typing import Dict, Optional, List
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentStatus
from ..ports.llm_service import Message, MessagePart
from ..ports.session_store import SessionStore
from ..utils.logger import logger


class CircuitBreaker:
    """
    Circuit Breaker pattern implementation.
    
    Prevents cascading failures by temporarily disabling failing agents.
    After threshold failures, circuit "opens" and blocks requests for recovery period.
    """
    
    def __init__(self):
        # agent_id -> (failure_count, last_failure_timestamp)
        self._failures: Dict[str, tuple[int, float]] = {}
    
    def is_open(self, agent_id: str, threshold: int, recovery_ms: int) -> bool:
        """
        Check if circuit breaker is open for this agent.
        
        Args:
            agent_id: Agent identifier
            threshold: Number of failures before opening circuit
            recovery_ms: Recovery timeout in milliseconds
            
        Returns:
            True if circuit is open (agent should not be called)
        """
        if agent_id not in self._failures:
            return False
        
        failure_count, last_failure = self._failures[agent_id]
        
        # Auto-recovery after timeout
        current_time = time.time()
        recovery_seconds = recovery_ms / 1000
        
        if current_time - last_failure > recovery_seconds:
            # Circuit closed - remove from failures
            del self._failures[agent_id]
            logger.info(f"🔓 Circuit breaker CLOSED for {agent_id} (auto-recovery)")
            return False
        
        # Check if threshold exceeded
        is_open = failure_count >= threshold
        if is_open:
            logger.warning(
                f"⚡ Circuit breaker OPEN for {agent_id} "
                f"({failure_count}/{threshold} failures)"
            )
        
        return is_open
    
    def record_failure(self, agent_id: str):
        """Record a failure for this agent."""
        if agent_id in self._failures:
            count, _ = self._failures[agent_id]
            self._failures[agent_id] = (count + 1, time.time())
        else:
            self._failures[agent_id] = (1, time.time())
        
        count, _ = self._failures[agent_id]
        logger.warning(f"❌ Failure recorded for {agent_id} (total: {count})")
    
    def record_success(self, agent_id: str):
        """Record a success - resets failure counter."""
        if agent_id in self._failures:
            del self._failures[agent_id]
            logger.debug(f"✅ Success recorded for {agent_id} - failures reset")
    
    def get_status(self, agent_id: str) -> Dict[str, any]:
        """Get current circuit breaker status for agent."""
        if agent_id not in self._failures:
            return {"status": "closed", "failures": 0}
        
        count, last_failure = self._failures[agent_id]
        return {
            "status": "open" if count >= 3 else "half-open",
            "failures": count,
            "last_failure": last_failure
        }


class BaseAgent(ABC):
    """
    Abstract base class for all agents.
    
    Provides common functionality:
    - Circuit breaker for fault tolerance
    - Retry logic with exponential backoff
    - Standardized error handling
    - Logging and telemetry hooks
    - Conversation history composition helper
    
    Subclasses must implement:
    - can_handle(): Determine if agent can process message
    - execute(): Core agent logic
    """
    
    def __init__(self, config: AgentConfig, circuit_breaker: Optional[CircuitBreaker] = None):
        """
        Initialize base agent.
        
        Args:
            config: Agent configuration
            circuit_breaker: Shared circuit breaker instance (optional)
        """
        self.config = config
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        
        logger.info(
            f"🤖 Agent initialized: {config.agent_id} "
            f"(type={config.agent_type}, model={config.llm_model or 'none'})"
        )
    
    @property
    def agent_id(self) -> str:
        """Get agent identifier."""
        return self.config.agent_id
    
    @property
    def agent_type(self) -> str:
        """Get agent type."""
        return self.config.agent_type
    
    @abstractmethod
    async def can_handle(self, message: AgentMessage) -> bool:
        """
        Determine if this agent can handle the message.
        
        Args:
            message: Agent message to evaluate
            
        Returns:
            True if agent can process this message
        """
        pass
    
    @abstractmethod
    async def execute(self, message: AgentMessage) -> AgentResponse:
        """
        Execute the agent's core logic.
        
        Args:
            message: Agent message to process
            
        Returns:
            Agent response with result
            
        Raises:
            Exception: If execution fails
        """
        pass
    
    async def _load_conversation_context(
        self,
        session_store: SessionStore,
        session_id: str,
        current_message_parts: List[MessagePart],
        context_window: int
    ) -> List[Message]:
        """
        Load conversation history and build complete LLM context.
        
        This helper ensures consistent history composition across all agents:
        1. Load previous conversation from SessionStore (truncated to context_window)
        2. Append current message from AgentMessage payload
        3. Return ready-to-use history for LLM
        
        Why this pattern exists:
        - ConversationHandler uses batch write optimization (saves user+model messages AFTER response)
        - Therefore, SessionStore history does NOT include the current user message
        - Agents must compose: previous_history + current_message
        
        Args:
            session_store: SessionStore instance
            session_id: Session identifier
            current_message_parts: Current user message parts (from AgentMessage.context)
            context_window: Maximum number of messages to load
            
        Returns:
            Complete conversation history ready for LLM (previous + current)
        """
        if not session_id or not session_store:
            # No history available - return only current message
            result = [Message(role="user", parts=current_message_parts)] if current_message_parts else []
            return self._inject_timestamps(result)

        try:
            # Load previous conversation history
            session = await session_store.load_session(session_id)
            previous_history = session.history if session.history else []

            # Truncate to context window
            if len(previous_history) > context_window:
                previous_history = previous_history[-context_window:]

            # Append current message
            if current_message_parts:
                current_message = Message(role="user", parts=current_message_parts)
                result = previous_history + [current_message]
            else:
                result = previous_history
            return self._inject_timestamps(result)

        except Exception as e:
            logger.warning(f"⚠️ Failed to load conversation context: {e}")
            # Fallback: return only current message
            result = [Message(role="user", parts=current_message_parts)] if current_message_parts else []
            return self._inject_timestamps(result)
    
    HISTORY_FULL_TURNS = 5  # model turns to keep full content (model responses + file content)

    @staticmethod
    def _inject_timestamps(history: List[Message]) -> List[Message]:
        """Prepend UTC timestamp to each user message for LLM temporal awareness.

        Allows the model to distinguish a gap of 5 minutes from a gap of 5 days —
        critical for contextual responses (e.g., referencing yesterday's conversation).
        Only user messages are stamped; model responses are always immediate follow-ups.
        """
        result = []
        for msg in history:
            if msg.role == "user" and msg.created_at:
                ts = datetime.fromtimestamp(msg.created_at, tz=timezone.utc).strftime("[%b %d, %H:%M UTC]")
                new_parts = []
                for i, part in enumerate(msg.parts):
                    if i == 0 and part.text and not part.tool_call and not part.file_data:
                        new_parts.append(MessagePart(text=f"{ts} {part.text}"))
                    else:
                        new_parts.append(part)
                result.append(Message(role=msg.role, parts=new_parts, created_at=msg.created_at))
            else:
                result.append(msg)
        return result

    def _apply_history_tier(
        self,
        history: List[Message],
        max_full_turns: Optional[int] = None,
    ) -> List[Message]:
        """
        Apply tiered history loading: keep full content for recent turns, stub/summary for older.

        Handles two content types in one pass:
          - model messages: prefer full_text (detailed response) over text (summary)
          - user messages with files: prefer full_text (full file) over text (1 000-char stub)

        Both use the same distance threshold (max_full_turns).
        Agents override HISTORY_FULL_TURNS or pass max_full_turns directly.
        """
        turns = max_full_turns if max_full_turns is not None else self.HISTORY_FULL_TURNS
        result: List[Message] = []
        model_turns_from_end = 0

        for msg in reversed(history):
            use_full = model_turns_from_end <= turns

            if msg.role == "model":
                model_turns_from_end += 1
                if use_full:
                    new_parts = [
                        MessagePart(
                            text=part.full_text if part.full_text else part.text,
                            tool_call=part.tool_call,
                            tool_response=part.tool_response,
                            file_data=part.file_data,
                        )
                        for part in msg.parts
                    ]
                    result.insert(0, Message(role=msg.role, parts=new_parts, created_at=msg.created_at))
                else:
                    result.insert(0, msg)
            else:
                # User messages: apply tiering only to file content parts (have full_text set)
                has_file = any(p.full_text is not None for p in msg.parts)
                if has_file:
                    new_parts = [
                        MessagePart(
                            text=part.full_text if (part.full_text is not None and use_full) else part.text,
                            tool_call=part.tool_call,
                            tool_response=part.tool_response,
                            file_data=part.file_data,
                        )
                        for part in msg.parts
                    ]
                    result.insert(0, Message(role=msg.role, parts=new_parts, created_at=msg.created_at))
                else:
                    result.insert(0, msg)

        return result

    async def process(self, message: AgentMessage) -> AgentResponse:
        """
        Process message with retry logic and circuit breaker.

        This is the main entry point - wraps execute() with:
        - Circuit breaker check
        - can_handle() validation
        - Retry logic
        - Error handling
        
        Args:
            message: Agent message to process
            
        Returns:
            Agent response
        """
        logger.debug(
            f"🔄 [BaseAgent] {self.agent_id} processing message: "
            f"task_id={message.task_id[:8]}..., "
            f"intent={message.intent}, "
            f"payload keys={list(message.payload.keys())}"
        )
        
        # 1. Check circuit breaker
        if self.circuit_breaker.is_open(
            self.agent_id,
            self.config.circuit_breaker_threshold,
            self.config.circuit_breaker_recovery_ms
        ):
            logger.warning(f"⚡ [BaseAgent] {self.agent_id} circuit breaker is OPEN")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="Circuit breaker is open - agent temporarily disabled"
            )
        
        # 2. Validate capability
        try:
            logger.debug(f"🔍 [BaseAgent] {self.agent_id} checking can_handle()...")
            can_handle_result = await self.can_handle(message)
            logger.debug(
                f"🔍 [BaseAgent] {self.agent_id} can_handle() returned: {can_handle_result}"
            )
            
            if not can_handle_result:
                logger.debug(f"❌ [BaseAgent] {self.agent_id} cannot handle this message")
                return AgentResponse.cannot_handle(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    suggestions=self._get_alternative_agents()
                )
        except Exception as e:
            logger.error(
                f"❌ [BaseAgent] Error in can_handle() for {self.agent_id}: {e}",
                exc_info=True
            )
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Capability check failed: {str(e)}"
            )
        
        # 3. Execute with retry
        max_retries = self.config.max_retries
        last_error = None
        
        for attempt in range(max_retries + 1):
            try:
                logger.info(
                    f"🔧 {self.agent_id} executing task {message.task_id[:8]}... "
                    f"(attempt {attempt + 1}/{max_retries + 1})"
                )
                
                response = await self._execute_with_timeout(message)
                
                # Success - record and return
                self.circuit_breaker.record_success(self.agent_id)
                
                logger.info(
                    f"✅ {self.agent_id} completed task {message.task_id[:8]} "
                    f"(status={response.status}, confidence={response.confidence:.2f})"
                )
                
                return response
                
            except asyncio.TimeoutError:
                last_error = "Task execution timeout"
                logger.warning(
                    f"⏱️ {self.agent_id} timeout on attempt {attempt + 1}"
                )
                
            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"❌ {self.agent_id} failed on attempt {attempt + 1}: {e}"
                )
            
            # Exponential backoff before retry
            if attempt < max_retries:
                backoff_seconds = 2 ** attempt  # 1s, 2s, 4s...
                logger.debug(f"⏳ Waiting {backoff_seconds}s before retry...")
                await asyncio.sleep(backoff_seconds)
        
        # All retries exhausted - record failure
        self.circuit_breaker.record_failure(self.agent_id)
        
        return AgentResponse.failure(
            task_id=message.task_id,
            agent_id=self.agent_id,
            error=f"Max retries exceeded. Last error: {last_error}"
        )
    
    async def _execute_with_timeout(self, message: AgentMessage) -> AgentResponse:
        """
        Execute with timeout protection.

        Timeout resolution strategy:
        - message.timeout_ms overrides agent config
        - agent config applies when message timeout is None
        - if both None, execute without timeout (routing agents)
        """
        timeout_ms = message.timeout_ms if message.timeout_ms is not None else self.config.timeout_ms

        if timeout_ms is None:
            return await self.execute(message)

        timeout_seconds = timeout_ms / 1000

        try:
            return await asyncio.wait_for(
                self.execute(message),
                timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(
                f"Agent {self.agent_id} exceeded timeout of {timeout_seconds}s"
            )
    
    def _get_alternative_agents(self) -> Optional[list[str]]:
        """
        Get suggestions for alternative agents.
        
        Override in subclasses to provide intelligent fallback suggestions.
        """
        return None
    
    def get_status(self) -> Dict[str, any]:
        """
        Get agent status for monitoring.
        
        Returns:
            Status dictionary with agent info and circuit breaker state
        """
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "model": self.config.llm_model,
            "capabilities": self.config.capabilities,
            "circuit_breaker": self.circuit_breaker.get_status(self.agent_id)
        }
