"""
Base Agent Infrastructure
=========================

Provides abstract base class and utilities for all agents.
"""

import json
import random
import time
import asyncio
from datetime import datetime, timezone
from abc import ABC, abstractmethod
from typing import ClassVar, Dict, Optional, List
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig, AgentStatus
from ..domain.exceptions import (
    _ERROR_TYPE_LOG_LABEL,
    BothProvidersUnavailableError,
    FAILOVER_TRIGGER_TYPES,
    LLMError,
    LLMRateLimitError,
    LLMUnavailableError,
    ProviderBreakerOpenError,
)
from ..domain.retry_policy import DEFAULT_RETRY_POLICY, RetryPolicy
from ..ports.llm_port import Message, MessagePart
from ..ports.session_store import SessionStore
from ..utils.logger import logger
from ..utils.debug_logger import get_debug_logger


# ---------------------------------------------------------------------------
# Behavioral system anchor injected into the LATEST user message of every
# Quick / Smart turn (in-memory only — never persisted to session history).
#
# Two rules applied as a procedural gate before generation:
#   1. Information-gap rule — every user message is a request for information
#      the user does not yet have, even when phrased as chat. Find the gap
#      and close it via tools / memory / context. Tone is not the test.
#   2. Posture rule — be proactive, write the next scene, do not just mirror,
#      manipulate the user (in the screenwriter / narrative-pull sense, NOT
#      the clinician sense — see PSYCHIATRIST history note below).
#
# History notes — failure modes encountered during iteration. Do not repeat:
#
#   - NAMED ROLES TENDED TO COLLAPSE INTO STEREOTYPES. Earlier versions used
#     named cognitive lenses (PSYCHIATRIST, SCOUT, CHIEF OF STAFF, INTERPRETER).
#     Each name dragged in unwanted associations: PSYCHIATRIST → therapy mode;
#     CHIEF OF STAFF → corporate assistant tone. Final form drops named lenses
#     in favor of direct imperative verbs.
#
#   - PSYCHIATRIST lens (removed) framed the USER as the object of analysis;
#     the LLM became Freudian and over-therapized everything. Lesson: never
#     write a lens whose action verbs target the human ("model the user",
#     "diagnose", "what they avoid"). Target the artifact (request, ask).
#
#   - PRISM lens (removed) instructed the LLM to "refract through the voice
#     and character you were given" — the LLM interpreted that meta-reference
#     as pointing at THIS anchor itself, making the anchor the new persona
#     and overriding the actual configured character. Lesson: never add a
#     meta-instruction inside the anchor that references "your character" /
#     "your voice" / "the persona you were given".
#
#   - SCREENWRITER (earlier version) had a "sometimes nothing beyond a warm
#     reply" escape clause. The LLM grabbed this clause on every conversational
#     message and skipped tool calls. Lesson: never give the bot permission to
#     default to no-work on conversational tone. Hence "Tone is not the test".
#
# Lives here (not in src/infrastructure/agent_config.py) because base_agent.py
# cannot import from infrastructure/ — circular import via agent_coordinator.py
# → agents/__init__.py → base_agent.py. Same constraint as HISTORY_FULL_TURNS.
# ---------------------------------------------------------------------------
USER_TURN_SYSTEM_ANCHOR = (
    "System anchors. Apply both before sending any response.\n\n"
    "1. Every user message is a request for information the user does not yet "
    "have — even when phrased as chat. Find the gap (pending stuff, current data, "
    "real-world facts) and close it. Do not ignore your tools. Tone is not the test.\n\n"
    "2. Be proactive. Write the scene. Do not just mirror. Manipulate the user."
)


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

    Class-level flags:
    - forwards_language_preference: When True the agent extracts preferred_language
      and agent_mirror from message.context["metadata"] and passes them to
      prompt_builder.build_for_agent(). Only orchestrators (Quick, Smart) set this True.
      Internal specialist agents leave it False — they inherit user language from the
      system prompt assembled by their caller, not from per-call params.
    """

    forwards_language_preference: ClassVar[bool] = False

    # Per-agent retry policy. Subclasses override this class attribute
    # (see e.g. RouterAgent → NO_RETRY_POLICY, DocPlannerAgent →
    # NO_RETRY_POLICY for ASYNC paths). Constructor argument
    # ``retry_policy=`` overrides the class default at instance level.
    # See domain/retry_policy.py and
    # docs/04_solution_strategy/decisions/typed_retry_policy.md.
    RETRY_POLICY: ClassVar[RetryPolicy] = DEFAULT_RETRY_POLICY

    def __init__(
        self,
        config: AgentConfig,
        circuit_breaker: Optional[CircuitBreaker] = None,
        retry_policy: Optional[RetryPolicy] = None,
    ):
        """
        Initialize base agent.
        
        Args:
            config: Agent configuration
            circuit_breaker: Shared circuit breaker instance (optional)
        """
        self.config = config
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        # Instance retry policy. None → use class-level RETRY_POLICY.
        # Read via ``self.retry_policy`` property to keep the lookup clear.
        self._retry_policy_override: Optional[RetryPolicy] = retry_policy
        self._agent_execution_context = None  # set via _set_execution_context()
        self.coordinator = None  # injected post-construction by UserAgentFactory for all agents
        self._user_timezone: str = "UTC"  # overridden by subclasses that receive user_timezone
        self._billing_account_id: Optional[str] = None
        self._billing_prompt_tokens: int = 0
        self._billing_completion_tokens: int = 0
        self._billing_cache_read_tokens: int = 0
        self._billing_cache_creation_tokens: int = 0

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

    @property
    def retry_policy(self) -> RetryPolicy:
        """Effective retry policy: instance override or class default."""
        return self._retry_policy_override or self.RETRY_POLICY
    
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
    
    HISTORY_FULL_TURNS = 2  # edit in src/infrastructure/agent_config.py → BaseAgentConfig.history_full_turns

    def _inject_timestamps(self, history: List[Message]) -> List[Message]:
        """Prepend timestamp in user's local timezone to each user message.

        Allows the model to distinguish a gap of 5 minutes from a gap of 5 days —
        critical for contextual responses (e.g., referencing yesterday's conversation).
        Only user messages are stamped; model responses are always immediate follow-ups.

        Uses self._user_timezone (IANA, e.g. "Europe/Madrid") set by subclass constructors.
        Falls back to UTC when timezone is unavailable or invalid.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            tz = ZoneInfo(self._user_timezone or "UTC")
        except (ZoneInfoNotFoundError, KeyError):
            tz = ZoneInfo("UTC")
        tz_abbr = datetime.now(tz).strftime("%Z") or self._user_timezone or "UTC"

        result = []
        for msg in history:
            if msg.role == "user" and msg.created_at:
                local_dt = datetime.fromtimestamp(msg.created_at, tz=tz)
                ts = local_dt.strftime(f"[%b %d, %H:%M {tz_abbr}]")
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

    def _inject_user_turn_anchor(self, history: List[Message]) -> List[Message]:
        """Prepend USER_TURN_SYSTEM_ANCHOR to the LATEST user message only.

        Two-rule procedural gate (information-gap rule + posture rule) injected
        right before LLM generation. Targets only history[-1] so historical user
        messages stay clean — the anchor frames the current turn, not past ones.

        In-memory only — never persisted to session history. ConversationHandler
        saves the original message_parts (see clean_message_parts in
        conversation_handler.py:640-660), not this in-memory copy.

        Used by Quick / Smart orchestrators only. Specialists, Router, and
        background agents do not call this — anchor is for fresh user-turn framing.

        Order relative to _inject_timestamps: anchor goes BEFORE the timestamp
        in the final text-part. Call this AFTER _inject_timestamps so the anchor
        prepends cleanly:
            "<ANCHOR>\\n\\n[Apr 10, 14:30 CEST] <user text>"
        """
        if not history or history[-1].role != "user":
            return history

        last = history[-1]
        new_parts: List[MessagePart] = []
        injected = False
        for part in last.parts:
            if not injected and part.text and not part.tool_call and not part.file_data:
                new_parts.append(MessagePart(
                    text=f"{USER_TURN_SYSTEM_ANCHOR}\n\n{part.text}",
                    full_text=part.full_text,
                    file_data=part.file_data,
                    tool_call=part.tool_call,
                    tool_response=part.tool_response,
                    consolidation_text=part.consolidation_text,
                ))
                injected = True
            else:
                new_parts.append(part)

        return history[:-1] + [Message(
            role=last.role,
            parts=new_parts,
            created_at=last.created_at,
        )]

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
        
        # 3. Execute with typed retry — see domain/retry_policy.py.
        # Only LLMRateLimitError / LLMUnavailableError are retried (with
        # exponential backoff + jitter). asyncio.TimeoutError is fixed-
        # never-retry (structural budget mismatch — same call inside the
        # same budget cannot succeed). asyncio.CancelledError is honored
        # outright. Any other Exception is treated as deterministic and
        # surfaced immediately.
        self._billing_account_id = message.context.get("account_id")
        self._billing_prompt_tokens = 0
        self._billing_completion_tokens = 0
        self._billing_cache_read_tokens = 0
        self._billing_cache_creation_tokens = 0

        policy = self.retry_policy
        # Total tries = 1 initial attempt + N transient retries.
        max_attempts = policy.transient_max_attempts + 1
        attempt = 0
        last_error: Optional[str] = None

        while attempt < max_attempts:
            attempt += 1
            try:
                logger.info(
                    f"🔧 {self.agent_id} executing task {message.task_id[:8]}... "
                    f"(attempt {attempt}/{max_attempts})"
                )

                response = await self._execute_with_timeout(message)

                # Success - record and return
                self.circuit_breaker.record_success(self.agent_id)
                await self._flush_billing()

                logger.info(
                    f"✅ {self.agent_id} completed task {message.task_id[:8]} "
                    f"(status={response.status}, confidence={response.confidence:.2f})"
                )

                return response

            except (LLMRateLimitError, LLMUnavailableError) as e:
                error_type = "rate_limit" if isinstance(e, LLMRateLimitError) else "unavailable"
                last_error = f"{error_type}: {e}"
                if attempt >= max_attempts:
                    logger.warning(
                        f"❌ {self.agent_id} transient error exhausted retries "
                        f"({error_type}, attempt {attempt}/{max_attempts}): {e}"
                    )
                    break
                # Exponential backoff with jitter — defends against
                # synchronised retry storms when many agents hit the same
                # provider rate-limit window simultaneously.
                backoff = policy.transient_backoff_base_seconds * (2 ** (attempt - 1))
                if policy.transient_jitter_seconds > 0:
                    backoff += random.uniform(0, policy.transient_jitter_seconds)
                logger.warning(
                    f"⏳ {self.agent_id} transient error, retrying "
                    f"(error_type={error_type}, "
                    f"http_status={getattr(e, 'http_status', None)}, "
                    f"attempt={attempt}/{max_attempts}, "
                    f"backoff={backoff:.2f}s): {e}"
                )
                await asyncio.sleep(backoff)
                continue

            except asyncio.TimeoutError:
                # Structural budget mismatch — running again inside the
                # same timeout cannot help. Surface immediately.
                last_error = "Task execution timeout"
                logger.warning(
                    f"⏱️ {self.agent_id} timeout on attempt {attempt}/{max_attempts} "
                    f"(no retry — timeout indicates budget mismatch, not transient failure)"
                )
                break

            except asyncio.CancelledError:
                # External cancellation — never swallow, never retry.
                self.circuit_breaker.record_failure(self.agent_id)
                await self._flush_billing()
                raise

            except Exception as e:
                # Deterministic by assumption — retry would only delay
                # the failure and obscure the bug from logs.
                last_error = str(e)
                logger.warning(
                    f"❌ {self.agent_id} failed on attempt {attempt}/{max_attempts} "
                    f"(no retry — non-transient): {e}"
                )
                break

        # All paths past the loop are non-success.
        self.circuit_breaker.record_failure(self.agent_id)
        await self._flush_billing()

        return AgentResponse.failure(
            task_id=message.task_id,
            agent_id=self.agent_id,
            error=f"Agent failed. Last error: {last_error}"
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
    
    # ---------------------------------------------------------------------- #
    # Lifecycle hooks                                                        #
    # ---------------------------------------------------------------------- #
    #
    # Agents call these at fixed lifecycle points instead of writing
    # logger.info/error directly.  Changing logging infrastructure
    # (format, fields, telemetry) requires editing only these methods.
    #
    # Override any hook in a subclass to add custom behaviour for a specific
    # agent.  The default implementations log via the shared logger.
    # ---------------------------------------------------------------------- #

    def _on_agent_start(self, text: str = "") -> None:
        """Lifecycle hook: called at the top of execute() with the user text."""
        preview = f"'{text[:60]}...'" if len(text) > 60 else f"'{text}'"
        logger.info(f"[{self.agent_id}] start → {preview}")

    def _on_agent_success(self, char_count: int = 0, token_count: int = 0, output_text: str = "") -> None:
        """Lifecycle hook: called before returning a successful AgentResponse.

        output_text: final text shown to the user. When provided and DEBUG_PROMPTS
        is enabled, it is written to the debug bucket as type=output.
        """
        if token_count:
            logger.info(f"✅ [{self.agent_id}] done ({char_count} chars, {token_count} tokens)")
        else:
            logger.info(f"✅ [{self.agent_id}] done ({char_count} chars)")
        if output_text:
            from ..domain.billing import calculate_cost
            model = getattr(self, "model_name", None) or self.config.llm_model or "unknown"
            meta: dict = {
                "type": "output",
                "model": model,
                "tokens": token_count,
                "prompt_tokens": self._billing_prompt_tokens,
                "completion_tokens": self._billing_completion_tokens,
                "chars": char_count,
            }
            if self._billing_cache_read_tokens:
                meta["cache_read_tokens"] = self._billing_cache_read_tokens
            if self._billing_cache_creation_tokens:
                meta["cache_creation_tokens"] = self._billing_cache_creation_tokens
            meta["cost"] = calculate_cost(
                model=model,
                prompt_tokens=self._billing_prompt_tokens,
                completion_tokens=self._billing_completion_tokens,
                cache_read_tokens=self._billing_cache_read_tokens,
                cache_creation_tokens=self._billing_cache_creation_tokens,
            )
            get_debug_logger().log_response(
                agent_name=self.agent_type or self.agent_id,
                response=output_text,
                metadata=meta,
            )

    async def _flush_billing(self) -> None:
        """Fire-and-forget usage report to billing_agent. No-op if coordinator/account_id not set."""
        if not self.coordinator or not self._billing_account_id:
            return
        if not (self._billing_prompt_tokens or self._billing_completion_tokens
                or self._billing_cache_read_tokens or self._billing_cache_creation_tokens):
            return
        from ..domain.billing import calculate_cost
        from ..domain.agent import AgentMessage, AgentIntent
        model = getattr(self, "model_name", None) or self.config.llm_model or "unknown"
        asyncio.create_task(
            self.coordinator.route_message(
                AgentMessage.create(
                    sender=self.agent_id,
                    recipient="billing_agent",
                    intent=AgentIntent.INFORM,
                    payload={
                        "account_id": self._billing_account_id,
                        "tokens": (self._billing_prompt_tokens + self._billing_completion_tokens
                                   + self._billing_cache_read_tokens + self._billing_cache_creation_tokens),
                        "cost": calculate_cost(
                            model=model,
                            prompt_tokens=self._billing_prompt_tokens,
                            completion_tokens=self._billing_completion_tokens,
                            cache_read_tokens=self._billing_cache_read_tokens,
                            cache_creation_tokens=self._billing_cache_creation_tokens,
                        ),
                        "model": model,
                    },
                    context={},
                )
            )
        )

    def _on_agent_error(self, error: Exception, context: str = "execute") -> None:
        """Lifecycle hook: called in the except block of execute().

        context: optional label for WHERE the error occurred (default: "execute").
        """
        if isinstance(error, (LLMUnavailableError, LLMRateLimitError)):
            logger.error(f"❌ [{self.agent_id}] error in {context}: {error}")
        else:
            logger.error(f"❌ [{self.agent_id}] error in {context}: {error}", exc_info=True)

    def _on_delegation(self, intent: str, query: str = "") -> None:
        """Lifecycle hook: called before each specialist delegation.

        Used by orchestrator agents (Quick, Smart) that delegate to specialists.
        """
        preview = f"'{query[:60]}...'" if len(query) > 60 else f"'{query}'"
        logger.info(f"[{self.agent_id}] → delegate: intent={intent} query={preview}")

    @staticmethod
    def _build_tool_turn(response, tool_results: list) -> list:
        """Build message history entries from an LLM response with tool calls + results.

        Delegates to domain-level build_tool_turn(). See its docstring for details.
        """
        from ..domain.llm import build_tool_turn
        return build_tool_turn(response, tool_results)

    @staticmethod
    def _build_delegate_tool_declaration(available_intents: list) -> dict:
        """Build the delegate_to_specialist tool declaration for LLM APIs.

        Collects context_schema fields from all available intents and exposes
        them as typed properties on the context parameter — so Gemini can
        populate them from the conversation instead of generating an empty {}.
        """
        intents_description = "\n".join(
            f"- {i['name']}: {i['description']}" for i in available_intents
        ) or "(no specialist agents registered)"

        context_properties: dict = {}
        for intent in available_intents:
            for field_name, field_desc in intent.get("context_schema", {}).items():
                if field_name not in context_properties:
                    context_properties[field_name] = {
                        "type": "string",
                        "description": field_desc,
                    }

        context_param: dict = {
            "type": "object",
            "description": (
                "Structured parameters for intents that require them. "
                "See agents_registry in your system prompt for required fields per intent."
            ),
        }
        if context_properties:
            context_param["properties"] = context_properties

        return {
            "name": "delegate_to_specialist",
            "description": (
                "Send a task to a specialist agent in the network. "
                "The specialist executes autonomously and returns results.\n\n"
                f"Available intents:\n{intents_description}\n\n"
                "See agents_registry in your system prompt for per-intent "
                "query formulation rules and required context fields."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "Target agent intent (from available intents list)",
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Task for the specialist. "
                            "Formulate per agents_registry rules for the chosen intent."
                        ),
                    },
                    "context": context_param,
                },
                "required": ["intent", "query"],
            },
        }

    # ---------------------------------------------------------------------- #
    # Debug logging helpers                                                  #
    # ---------------------------------------------------------------------- #
    #
    # Thin wrappers around PromptDebugLogger that:
    #   • guard on debug.enabled (no-op when DEBUG_PROMPTS is off)
    #   • standardise agent_name (agent_type or agent_id)
    #   • format List[Message] history to a readable string
    #
    # Agents call self._debug_prompt / self._debug_response instead of
    # importing get_debug_logger() themselves.
    # ---------------------------------------------------------------------- #

    def _debug_prompt(self, system: str, content, turn: int = 0, model: str = "") -> None:
        """Log what was sent to the LLM (no-op when DEBUG_PROMPTS is off).

        content: either a pre-formatted str or List[Message].
        system:  system instruction (may be empty).
        """
        debug = get_debug_logger()
        if not debug.enabled:
            return
        prompt_str = content if isinstance(content, str) else self._format_history_for_debug(content)
        meta = {}
        if model:
            meta["model"] = model
        if turn:
            meta["turn"] = turn
        debug.log_prompt(
            agent_name=self.agent_type or self.agent_id,
            prompt=prompt_str,
            system_instruction=system or None,
            metadata=meta or None,
        )

    def _debug_response(self, text: str, tokens: int = 0, turn: int = 0) -> None:
        """Log what the LLM returned (no-op when DEBUG_PROMPTS is off)."""
        debug = get_debug_logger()
        if not debug.enabled:
            return
        meta = {}
        if tokens:
            meta["tokens"] = tokens
        if turn:
            meta["turn"] = turn
        debug.log_response(
            agent_name=self.agent_type or self.agent_id,
            response=text,
            metadata=meta or None,
        )

    def _debug_llm_response(self, response: "LLMResponse", turn: int = 0) -> None:
        """Log full LLMResponse as JSON (no-op when DEBUG_PROMPTS is off).

        Serialises text + tool_calls + tokens so the debug bucket always
        contains the complete model output, not just the text fragment.
        """
        debug = get_debug_logger()
        if not debug.enabled:
            return
        data: dict = {"text": response.text or ""}
        if response.tool_calls:
            data["tool_calls"] = [
                {"name": tc.name, "args": tc.args}
                for tc in response.tool_calls
            ]
        if response.usage_metadata:
            m = response.usage_metadata
            data["tokens"] = m.total_tokens
            try:
                data["prompt_tokens"] = int(m.prompt_tokens or 0)
                data["completion_tokens"] = int(m.completion_tokens or 0)
                cr = int(m.cache_read_tokens or 0)
                cc = int(m.cache_creation_tokens or 0)
                if cr or cc:
                    data["cache"] = {"read": cr, "creation": cc}
            except (TypeError, ValueError):
                logger.debug("Non-numeric tokens in usage_metadata — skipping detailed debug")
        meta = {"turn": turn} if turn else None
        debug.log_response(
            agent_name=self.agent_type or self.agent_id,
            response=json.dumps(data, ensure_ascii=False, indent=2),
            metadata=meta,
        )

    def _debug_raw_turn(
        self,
        system_blocks: list[dict],
        user_content: str,
        response_texts: list[str],
        tokens: int,
        turn: int,
        model: str = "",
    ) -> None:
        """Debug logging for agents that call the LLM SDK directly (not via LLMPort).

        Intended as an explicit escape hatch for ClaudeDeepResearchRunnerAgent and any
        future agent that must bypass LLMPort (e.g. native built-in tools).
        Do NOT call _debug_prompt/_debug_response from such agents — use this instead.

        system_blocks: raw system list[dict] as sent to the API (each has "text" key).
        user_content:  user message sent in messages[].
        response_texts: text blocks extracted from the response content.
        tokens:        input + output tokens for this turn.
        turn:          0-based turn index.
        model:         model name for metadata.
        """
        response_chars = sum(len(t) for t in response_texts)
        logger.info(
            "[debug_raw_turn] agent=%s model=%s turn=%d tokens=%d response_chars=%d",
            self.agent_type or self.agent_id, model, turn, tokens, response_chars,
        )

    def _set_execution_context(self, context: "AgentExecutionContext") -> None:
        """Store the AgentExecutionContext for fallback provider access in _call_llm.

        Called by orchestrator agents (Router, Quick, Smart) after receiving their
        execution context. Enables transparent fallback to a secondary provider on
        LLMRateLimitError / LLMUnavailableError without any agent-level awareness.
        """
        self._agent_execution_context = context

    async def _call_llm(
        self,
        request: "LLMRequest",
        turn: int = 0,
        *,
        llm_override: Optional["LLMPort"] = None,
        fallback_ctx_override: Optional["AgentExecutionContext"] = None,
    ) -> "LLMResponse":
        """Invoke the agent LLM and auto-log the full response to the debug bucket.

        All agents MUST call this instead of self.llm / self._llm directly.
        Changing debug logging = edit this method only.

        Resolves the LLM service via (in priority order):
          1. ``llm_override`` keyword argument (per-call override; used by
             agents that resolve their execution context per-message and
             must not mutate ``self.llm``).
          2. ``self.llm`` (Quick/Smart/Router orchestrators).
          3. ``self._llm`` (specialist agents).

        Resilience flow (when ``ctx.resilience_port`` and ``ctx.provider_name`` set):
          1. **Pre-call check.** If the resilience port reports the primary
             breaker open, raise ``ProviderBreakerOpenError`` (consequence
             of past failures, not a new failure — no ``record_failure``).
          2. **Try primary.** On success, ``record_success(primary)``.
          3. **On any FAILOVER_TRIGGER_TYPES.** ``record_failure(primary)``
             unless the error is itself ``ProviderBreakerOpenError`` (already
             accounted for). Then dispatch fallback.
          4. **Pre-fallback check.** If no fallback configured, OR fallback
             breaker is open, raise ``BothProvidersUnavailableError`` carrying
             the primary cause. Log ``event="llm_both_open"``. Terminal: NOT
             a FAILOVER trigger — caller must wait for cooldown.
          5. **Try fallback.** On success, return — but do NOT call
             ``record_success(fallback)``: full-reset semantics on
             ``InMemoryProviderResilience`` (`adapter:72-74`) would erase
             accumulated fallback failures after one lucky call. Asymmetry
             is intentional; see `decisions/provider_resilience_port.md`.
          6. **On fallback failure (FAILOVER_TRIGGER_TYPES).**
             ``record_failure(fallback)``, then raise
             ``BothProvidersUnavailableError`` with primary cause.
        """
        llm = llm_override if llm_override is not None else (
            getattr(self, "llm", None) or getattr(self, "_llm", None)
        )
        if llm is None:
            raise RuntimeError(
                f"{self.agent_id}: no LLM service configured "
                f"(set self.llm or self._llm before calling _call_llm, "
                f"or pass llm_override=)"
            )
        debug = get_debug_logger()
        if debug.enabled:
            debug.log_llm_request(
                agent_name=self.agent_type or self.agent_id,
                request=request,
                turn=turn,
            )
        ctx = (
            fallback_ctx_override
            if fallback_ctx_override is not None
            else self._agent_execution_context
        )
        primary_name = ctx.provider_name if ctx else ""
        resilience = ctx.resilience_port if ctx else None
        failover_tuple = tuple(FAILOVER_TRIGGER_TYPES)

        try:
            if resilience and primary_name and resilience.is_provider_open(primary_name):
                # Short-circuit before primary call. Routes to fallback via the
                # same except path as adapter-translated failures — uniform flow.
                raise ProviderBreakerOpenError(primary_name)
            response = await llm.generate_content(request=request)
            if resilience and primary_name:
                resilience.record_success(primary_name)
        except failover_tuple as e:
            # Account real failures only. ProviderBreakerOpenError is the
            # consequence of past record_failure calls; counting it again
            # would create a self-amplifying loop on the open breaker.
            if (
                resilience
                and primary_name
                and not isinstance(e, ProviderBreakerOpenError)
            ):
                resilience.record_failure(primary_name)

            fallback_provider = ctx.fallback_provider if ctx else None
            fallback_name = ctx.fallback_provider_name if ctx else None

            # Pre-fallback breaker check + missing-fallback check collapse to
            # the same terminal outcome.
            fallback_open = bool(
                resilience
                and fallback_name
                and resilience.is_provider_open(fallback_name)
            )
            if fallback_provider is None or fallback_open:
                logger.warning(
                    "llm_both_open" if fallback_open else "llm_no_fallback",
                    extra={
                        "event": "llm_both_open" if fallback_open else "llm_no_fallback",
                        "agent_type": self.config.agent_type,
                        "primary_provider": primary_name,
                        "fallback_provider": fallback_name,
                        "error_type": _ERROR_TYPE_LOG_LABEL[type(e)],
                        "http_status": e.http_status,
                    },
                )
                raise BothProvidersUnavailableError(
                    primary_name=primary_name,
                    fallback_name=fallback_name,
                    primary_cause=e,
                ) from e

            logger.warning(
                "llm_fallback",
                extra={
                    "event": "llm_fallback",
                    "agent_type": self.config.agent_type,
                    "primary_provider": primary_name,
                    "fallback_provider": fallback_name,
                    "error_type": _ERROR_TYPE_LOG_LABEL[type(e)],
                    "http_status": e.http_status,
                },
            )
            fallback_request = request.model_copy(
                update={"model_name": ctx.fallback_model_name}
            )
            try:
                response = await fallback_provider.generate_content(
                    request=fallback_request
                )
                # Asymmetry by design — see method docstring step 5.
            except failover_tuple as fb_e:
                if resilience and fallback_name:
                    resilience.record_failure(fallback_name)
                logger.warning(
                    "llm_fallback_failed",
                    extra={
                        "event": "llm_fallback_failed",
                        "agent_type": self.config.agent_type,
                        "primary_provider": primary_name,
                        "fallback_provider": fallback_name,
                        "primary_error_type": _ERROR_TYPE_LOG_LABEL[type(e)],
                        "fallback_error_type": _ERROR_TYPE_LOG_LABEL[type(fb_e)],
                    },
                )
                raise BothProvidersUnavailableError(
                    primary_name=primary_name,
                    fallback_name=fallback_name,
                    primary_cause=e,
                ) from fb_e
        self._debug_llm_response(response, turn=turn)
        if response.usage_metadata:
            m = response.usage_metadata
            try:
                self._billing_prompt_tokens += m.prompt_tokens
                self._billing_completion_tokens += m.completion_tokens
                self._billing_cache_read_tokens += getattr(m, "cache_read_tokens", 0)
                self._billing_cache_creation_tokens += getattr(m, "cache_creation_tokens", 0)
            except (TypeError, AttributeError):
                logger.debug("Non-conforming usage_metadata (e.g. test mock) — skipping billing accumulation")
        return response

    @staticmethod
    def _format_history_for_debug(history: List[Message]) -> str:
        """Render List[Message] as a human-readable string for debug logs."""
        sections = []
        for msg in history:
            parts_strs = []
            for p in msg.parts:
                if p.text:
                    parts_strs.append(p.text)
                elif p.tool_call:
                    parts_strs.append(f"[tool_call: {p.tool_call.name} args={p.tool_call.args}]")
                elif p.tool_response:
                    name = (
                        p.tool_response.get("name", "?")
                        if isinstance(p.tool_response, dict) else str(p.tool_response)
                    )
                    parts_strs.append(f"[tool_response: {name}]")
                elif p.file_data:
                    parts_strs.append("[file_data]")
                else:
                    parts_strs.append("[raw_content]")
            sections.append(f"[{msg.role.upper()}]\n" + "\n".join(parts_strs))
        return "\n---\n".join(sections)

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
