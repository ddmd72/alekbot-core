"""
Base Agent Infrastructure
=========================

Provides abstract base class and utilities for all agents.
"""

import time
import random
import asyncio
from datetime import datetime
from abc import ABC, abstractmethod
from typing import ClassVar, Dict, Optional, List, TYPE_CHECKING
from ..domain.agent import AgentMessage, AgentResponse, AgentConfig
from ..domain.exceptions import (
    _ERROR_TYPE_LOG_LABEL,
    BothProvidersUnavailableError,
    FAILOVER_TRIGGER_TYPES,
    LLMRateLimitError,
    LLMUnavailableError,
    ProviderBreakerOpenError,
    TranscriptLockedError,
    TRANSIENT_RETRY_TYPES,
)
from ..domain.retry_policy import DEFAULT_RETRY_POLICY, NO_RETRY_POLICY, RetryPolicy
from ..ports.llm_port import Message, MessagePart
from ..ports.session_store import SessionStore
from ..utils.logger import logger
from ..utils.retry import retry_async
from ..utils.telemetry import get_tracer

if TYPE_CHECKING:
    from ..domain.llm import LLMRequest, LLMResponse
    from ..ports.llm_port import LLMPort, AgentExecutionContext


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

    # Same-provider retry budget for a transient FAILOVER error hit on a
    # provider-locked multi-turn transcript (see ``_call_llm`` transcript
    # invariant). Kept small: a transient blip clears in a few seconds, and the
    # path is user-facing — on exhaustion we fail cleanly to Smart→Quick.
    _SAME_PROVIDER_RETRY_ATTEMPTS: ClassVar[int] = 2

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
        # Queryable LLM content store (BigQuery). Injected post-construction by
        # UserAgentFactory, like coordinator. None → capture skipped. The adapter
        # owns record building + the background-task set (best-effort, non-blocking).
        self._prompt_content_store = None
        # Durable usage sink (QuotaService). Injected post-construction by
        # UserAgentFactory, like coordinator. None → billing skipped.
        self._quota_service = None
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

        # Typed retry via the shared executor (src/utils/retry.py). Retries only
        # TRANSIENT_RETRY_TYPES (LLMRateLimitError / LLMUnavailableError) with the
        # agent's RetryPolicy; everything else propagates out of retry_async and is
        # turned into a terminal outcome below. The surrounding circuit-breaker,
        # billing flush and timeout/cancelled handling are agent-level concerns and
        # stay here — only the backoff loop + transient classification are shared.
        #
        # Per-message suppression: when this execution is already backed by an outer
        # retry layer (Cloud Tasks re-running the whole /worker task — the reminder
        # and daily-email-review handlers return 5xx on failure), in-process retry
        # would multiply with it (layer1 × layer2). Those callers set
        # context["suppress_transient_retry"] so the same agent that retries on the
        # interactive path stays single-attempt under a Cloud Task. See
        # docs/04_solution_strategy/decisions/typed_retry_policy.md.
        policy = (
            NO_RETRY_POLICY
            if message.context.get("suppress_transient_retry")
            else self.retry_policy
        )
        last_error: Optional[str] = None

        async def _attempt() -> AgentResponse:
            logger.info(f"🔧 {self.agent_id} executing task {message.task_id[:8]}...")
            return await self._execute_with_timeout(message)

        def _on_retry(e: BaseException, attempt: int, backoff: float) -> None:
            error_type = "rate_limit" if isinstance(e, LLMRateLimitError) else "unavailable"
            logger.warning(
                f"⏳ {self.agent_id} transient error, retrying "
                f"(error_type={error_type}, "
                f"http_status={getattr(e, 'http_status', None)}, "
                f"attempt={attempt}, backoff={backoff:.2f}s): {e}"
            )

        try:
            response = await retry_async(
                _attempt,
                policy=policy,
                retryable=tuple(TRANSIENT_RETRY_TYPES),
                on_retry=_on_retry,
            )
            self.circuit_breaker.record_success(self.agent_id)
            await self._flush_billing()
            logger.info(
                f"✅ {self.agent_id} completed task {message.task_id[:8]} "
                f"(status={response.status}, confidence={response.confidence:.2f})"
            )
            return response

        except asyncio.CancelledError:
            # External cancellation — never swallow, never retry.
            self.circuit_breaker.record_failure(self.agent_id)
            await self._flush_billing()
            raise

        except (LLMRateLimitError, LLMUnavailableError) as e:
            # Transient retries exhausted.
            error_type = "rate_limit" if isinstance(e, LLMRateLimitError) else "unavailable"
            last_error = f"{error_type}: {e}"
            logger.warning(
                f"❌ {self.agent_id} transient error exhausted retries ({error_type}): {e}"
            )

        except asyncio.TimeoutError:
            # Structural budget mismatch — running again inside the same timeout
            # cannot help. Surfaced as failure, never retried.
            last_error = "Task execution timeout"
            logger.warning(
                f"⏱️ {self.agent_id} timeout (no retry — budget mismatch, not transient failure)"
            )

        except Exception as e:
            # Deterministic by assumption — retry would only delay the failure
            # and obscure the bug from logs.
            last_error = str(e)
            logger.warning(
                f"❌ {self.agent_id} failed (no retry — non-transient): {e}"
            )

        # All non-success paths converge here.
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

    async def _flush_billing(self) -> None:
        """Durably record this execution's usage. No-op if quota_service/account_id
        unset or no tokens accrued.

        Awaited, NOT fire-and-forget: the write must land while the request still
        holds CPU. On Cloud Run a task detached after the request returns is starved
        by CPU throttling and lost when the instance recycles (the old path also
        buffered in-memory before writing — doubly lossy). Best-effort: the quota
        service swallows write errors, so billing never breaks the agent response.
        """
        if not self._quota_service or not self._billing_account_id:
            return
        if not (self._billing_prompt_tokens or self._billing_completion_tokens
                or self._billing_cache_read_tokens or self._billing_cache_creation_tokens):
            return
        from ..domain.billing import calculate_cost
        model = getattr(self, "model_name", None) or self.config.llm_model or "unknown"
        tokens = (self._billing_prompt_tokens + self._billing_completion_tokens
                  + self._billing_cache_read_tokens + self._billing_cache_creation_tokens)
        cost = calculate_cost(
            model=model,
            prompt_tokens=self._billing_prompt_tokens,
            completion_tokens=self._billing_completion_tokens,
            cache_read_tokens=self._billing_cache_read_tokens,
            cache_creation_tokens=self._billing_cache_creation_tokens,
        )
        await self._quota_service.record_usage(
            account_id=self._billing_account_id,
            model=model,
            tokens=tokens,
            cost=cost,
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
    # ``_debug_raw_turn`` (below) is the only live debug helper: a summary-only
    # ``logger.info`` line for agents that bypass ``LLMPort`` (e.g.
    # ``ClaudeDeepResearchRunnerAgent``). The legacy GCS prompt-dump
    # (``PromptDebugLogger``) was removed — content capture now goes to the
    # BigQuery store via ``_call_llm`` → ``PromptContentStore.record_turn``.

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
        future agent that must bypass LLMPort (e.g. native built-in tools) and therefore
        never reaches the BigQuery capture in _call_llm. This is the only debug helper —
        the legacy GCS prompt-dump was removed (TD-1).

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

        **Transcript-integrity invariant (one transcript = one provider).** Steps
        4–6 (cross-provider failover) run ONLY when ``request.messages`` carries no
        provider-locked turn. If it does (any ``tool_call``/``tool_response`` part,
        or any ``raw_content`` — i.e. a multi-turn delegation transcript), a
        primary FAILOVER error is handled WITHOUT switching providers: retry the
        same provider up to ``_SAME_PROVIDER_RETRY_ATTEMPTS`` times for transient
        errors, else raise the terminal ``TranscriptLockedError`` (caught upstream
        → clean Smart→Quick fallback). Mixing providers mid-transcript corrupts it
        (tool_use ids / thinking / cache). See
        decisions/transcript_integrity_one_provider.md.
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
        ctx = (
            fallback_ctx_override
            if fallback_ctx_override is not None
            else self._agent_execution_context
        )
        primary_name = ctx.provider_name if ctx else ""
        resilience = ctx.resilience_port if ctx else None
        failover_tuple = tuple(FAILOVER_TRIGGER_TYPES)

        _t0 = time.perf_counter()
        _t0_ns = time.time_ns()
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

            # ── Transcript-integrity invariant: one transcript = one provider ──
            # A multi-turn transcript is provider-specific (tool_use ids, thinking
            # blocks, raw_content, cache). Re-serving a turn on the FALLBACK
            # provider mid-loop corrupts the next turn (2026-06-29 orphan
            # tool_use_id → 400). When the transcript is provider-locked we never
            # cross-provider-failover: retry the SAME provider for transient
            # errors, else raise the terminal TranscriptLockedError (which flows
            # to the existing Smart→Quick fallback with a clean transcript).
            # tool-part presence catches the orphan-id bug; raw_content presence
            # independently catches thinking-replay (a model turn can carry
            # raw_content without a tool_call). raw_content is an SDK object never
            # persisted to session history — set in-loop by build_tool_turn — so
            # turn-1 / single-call requests are unlocked and behave as before.
            # See decisions/transcript_integrity_one_provider.md.
            transcript_locked = any(
                part.tool_call or part.tool_response
                for msg in request.messages
                for part in msg.parts
            ) or any(msg.raw_content is not None for msg in request.messages)

            if transcript_locked:
                # Same-provider retry for transient mid-loop errors. Skipped for
                # ProviderBreakerOpenError — the breaker is open, retrying the
                # same provider is pointless; go straight to terminal.
                retry_error = e
                retried_ok = False
                if not isinstance(e, ProviderBreakerOpenError):
                    policy = self.retry_policy
                    for attempt in range(1, self._SAME_PROVIDER_RETRY_ATTEMPTS + 1):
                        backoff = policy.transient_backoff_base_seconds * (
                            2 ** (attempt - 1)
                        )
                        if policy.transient_jitter_seconds > 0:
                            backoff += random.uniform(0, policy.transient_jitter_seconds)
                        logger.warning(
                            "llm_same_provider_retry %s attempt=%d/%d cause=%s http=%s: %s",
                            primary_name, attempt, self._SAME_PROVIDER_RETRY_ATTEMPTS,
                            _ERROR_TYPE_LOG_LABEL[type(retry_error)],
                            retry_error.http_status, str(retry_error)[:300],
                            extra={
                                "event": "llm_same_provider_retry",
                                "agent_type": self.config.agent_type,
                                "primary_provider": primary_name,
                                "attempt": attempt,
                                "error_type": _ERROR_TYPE_LOG_LABEL[type(retry_error)],
                                "http_status": retry_error.http_status,
                            },
                        )
                        await asyncio.sleep(backoff)
                        try:
                            response = await llm.generate_content(request=request)
                            if resilience and primary_name:
                                resilience.record_success(primary_name)
                            retried_ok = True
                            break
                        except failover_tuple as retry_e:
                            retry_error = retry_e
                            if (
                                resilience
                                and primary_name
                                and not isinstance(retry_e, ProviderBreakerOpenError)
                            ):
                                resilience.record_failure(primary_name)
                if not retried_ok:
                    logger.warning(
                        # Cause folded into the message (extra fields don't reach Cloud Logging).
                        "llm_transcript_locked %s cause=%s http=%s turn=%d: %s",
                        primary_name, _ERROR_TYPE_LOG_LABEL[type(retry_error)],
                        retry_error.http_status, turn, str(retry_error)[:300],
                        extra={
                            "event": "llm_transcript_locked",
                            "agent_type": self.config.agent_type,
                            "primary_provider": primary_name,
                            "error_type": _ERROR_TYPE_LOG_LABEL[type(retry_error)],
                            "http_status": retry_error.http_status,
                            "turn": turn,
                        },
                    )
                    raise TranscriptLockedError(
                        provider_name=primary_name,
                        cause=retry_error,
                        turn=turn,
                    ) from retry_error
                # retried_ok: response is set → fall through to the success tail.
            else:
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
                        # Cause folded into the message: StructuredLogHandler only serializes
                        # record.labels, so `extra` fields never reach Cloud Logging.
                        "%s %s→%s cause=%s http=%s: %s",
                        "llm_both_open" if fallback_open else "llm_no_fallback",
                        primary_name, fallback_name,
                        _ERROR_TYPE_LOG_LABEL[type(e)], e.http_status, str(e)[:300],
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
                    # Cause folded into the message (extra fields don't reach Cloud Logging).
                    "llm_fallback %s→%s cause=%s http=%s: %s",
                    primary_name, fallback_name,
                    _ERROR_TYPE_LOG_LABEL[type(e)], e.http_status, str(e)[:300],
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
                        # Cause folded into the message (extra fields don't reach Cloud Logging).
                        "llm_fallback_failed %s→%s primary=%s fallback=%s: %s",
                        primary_name, fallback_name,
                        _ERROR_TYPE_LOG_LABEL[type(e)], _ERROR_TYPE_LOG_LABEL[type(fb_e)],
                        str(fb_e)[:300],
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
        if response.usage_metadata:
            m = response.usage_metadata
            try:
                self._billing_prompt_tokens += m.prompt_tokens
                self._billing_completion_tokens += m.completion_tokens
                self._billing_cache_read_tokens += getattr(m, "cache_read_tokens", 0)
                self._billing_cache_creation_tokens += getattr(m, "cache_creation_tokens", 0)
            except (TypeError, AttributeError):
                logger.debug("Non-conforming usage_metadata (e.g. test mock) — skipping billing accumulation")
        latency_ms = (time.perf_counter() - _t0) * 1000.0
        self._emit_llm_span(request, response, turn, latency_ms, primary_name, _t0_ns)
        # Best-effort content capture. record_turn builds the record + schedules
        # the write inside the adapter and returns immediately — zero latency,
        # never raises. No-op when no store is configured.
        if self._prompt_content_store is not None:
            await self._prompt_content_store.record_turn(
                request=request,
                response=response,
                agent_id=self.agent_id,
                agent_type=self.agent_type,
                account_id=self._billing_account_id,
                turn=turn,
                latency_ms=latency_ms,
                provider=primary_name,
            )
        return response

    def _emit_llm_span(
        self,
        request: "LLMRequest",
        response: "LLMResponse",
        turn: int,
        latency_ms: float,
        provider: str,
        start_ns: int,
    ) -> None:
        """Emit a leaf ``llm.call`` span (metadata only — never content).

        Nests under the active request span (e.g. ``conversation.agent_response``)
        so the LLM call shows up as a timed node in the trace. Carries token
        counts and latency, but never the prompt/response text — that lives only
        in the content store (PromptContentStore), joined by trace_id. Best-effort.
        """
        try:
            tracer = get_tracer()
            span = tracer.start_span("llm.call", start_time=start_ns)
            try:
                span.set_attribute("llm.model", request.model_name or "")
                span.set_attribute("llm.agent_type", self.agent_type)
                span.set_attribute("llm.turn", turn)
                span.set_attribute("llm.latency_ms", latency_ms)
                if provider:
                    span.set_attribute("llm.provider", provider)
                m = response.usage_metadata
                if m:
                    span.set_attribute("llm.tokens.prompt", getattr(m, "prompt_tokens", 0))
                    span.set_attribute("llm.tokens.completion", getattr(m, "completion_tokens", 0))
                    span.set_attribute("llm.tokens.total", getattr(m, "total_tokens", 0))
                    span.set_attribute("llm.tokens.cache_read", getattr(m, "cache_read_tokens", 0))
                span.set_attribute("llm.tool_calls", len(response.tool_calls))
            finally:
                span.end()
        except Exception as e:  # tracing must never break the LLM path
            logger.debug("llm.call span emission skipped: %s", e)

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
