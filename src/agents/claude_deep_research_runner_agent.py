"""
ClaudeDeepResearchRunnerAgent — specialist agent that executes a Claude deep research loop.

Invoked via agent_execution Cloud Task from ClaudeDeepResearchAdapter.create_interaction().
Internal: never exposed to LLMs, never shown in tool descriptions.

Responsibilities (single responsibility):
  1. Run a single Claude API session with native built-in tools.
  2. Return the result text in AgentResponse — delivery is handled externally
     by AgentWorkerHandler + notification service.

Native tools (GA — no beta headers required):
  web_search_20260209  — web search with dynamic filtering.
  web_fetch_20260209   — URL/PDF fetching with dynamic filtering.
  code_execution_20250825 — auto-injected by the API for dynamic filtering. Do NOT declare.

All tool execution is server-side. The API manages context internally — we do not need to
serialise tool results or maintain history across turns. The only state we manage is the
growing messages[] array for pause_turn continuations.

Thinking: adaptive, effort=high. Temperature must be 1.0 when thinking is active.

Message payload shape (from AgentWorkerHandler):
  message.payload = {"query": <full research brief>, "intent": "execute_deep_research_claude"}
  message.context = {
      "user_id": ..., "account_id": ..., "original_query": ...,
      "system_prompt": ..., "model": ..., "job_id": ...,
  }

Caching strategy:
  System prompt — cached once with cache_control: ephemeral (5 min TTL).
                  All pause_turn continuation requests get a cache HIT on the system prompt.
  messages[]    — grows on pause_turn (user + assistant partial). Not cached; changes each call.

Turn protocol (single loop):
  end_turn   → extract text blocks, return as final result.
  pause_turn → server-side code_execution still running; send back partial response as
               assistant message so the API can resume. Loop again.
  max_tokens → output budget exhausted; return whatever text was produced.
"""
import asyncio
from typing import Any, Optional

from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..infrastructure.agent_config import DEEP_RESEARCH_SECOND_PASS
from ..infrastructure.agent_manifest import Intent
from ..utils.logger import logger
from .base_agent import BaseAgent
from ..domain.retry_policy import NO_RETRY_POLICY


class ClaudeDeepResearchRunnerAgent(BaseAgent):
    """
    Executes the full Claude deep research loop for async deep research tasks.

    Registered as internal specialist (internal=True, ASYNC mode).
    Invoked via agent_execution Cloud Task — never routed to by LLMs.

    Returns the research result text in AgentResponse.result.
    Delivery (HTML report upload, SmartAgent formatting, user notification) is handled
    externally by AgentWorkerHandler + notification service.

    Retry: NO_RETRY_POLICY — a transient retry would re-do 10–25 minutes
    of LLM work and pay for it twice. Cloud Tasks queue retry covers the
    transient case at the right granularity.
    """

    RETRY_POLICY = NO_RETRY_POLICY

    # Safety valve: maximum pause_turn continuations before giving up.
    # Each pause_turn = one server-side tool batch still executing. In practice 5-20 per session.
    _MAX_PAUSE_TURNS = 50

    # Retry on overloaded_error: up to 3 attempts, 30s → 60s → 120s backoff.
    _MAX_OVERLOAD_RETRIES = 3
    _OVERLOAD_RETRY_BASE_DELAY = 30  # seconds; doubles each retry

    # Second-pass critic: run a follow-up session with the first result, asking the model
    # to find what was missed and produce a new, improved final report.
    # Disable via DEEP_RESEARCH_SECOND_PASS=false env var (defaults to True).
    _SECOND_PASS_ENABLED = False  # default off; per-user override via UserBotConfig.deep_research_second_pass

    # Models that support adaptive thinking + output_config effort.
    # Substring tuple — same shape as ClaudeAdapter._THINKING_MODELS (claude_adapter.py:87).
    # Auto-includes future Sonnet/Opus versions (4.7, 4.8, …) without per-release updates.
    # Unified 2026-05-30 to fix divergence where opus-4-7/4-8 ULTRA fell to Haiku-style
    # fallback. See decisions/claude_ultra_tier_to_opus_4_8_plus_dr_gate_unification.md.
    _THINKING_MODELS = ("claude-sonnet", "claude-opus")

    # Dynamic filtering enabled (no allowed_callers restriction).
    # code_execution_20250825 is auto-injected by the API — do NOT declare it explicitly.
    _NATIVE_TOOLS: list = [
        {"type": "web_search_20260209", "name": "web_search"},
        {"type": "web_fetch_20260209",  "name": "web_fetch"},
    ]

    def __init__(
        self,
        config: AgentConfig,
        anthropic_client: Any,
    ) -> None:
        """
        Args:
            config:           Standard agent config.
            anthropic_client: AsyncAnthropic client instance — injected from composition layer.
                              The agent does not create or import the Anthropic SDK.
        """
        super().__init__(config)
        self._client = anthropic_client

    # ------------------------------------------------------------------
    # BaseAgent interface
    # ------------------------------------------------------------------

    async def can_handle(self, message: AgentMessage) -> bool:
        return (
            message.intent == AgentIntent.DELEGATE
            and message.payload.get("intent") == Intent.EXECUTE_DEEP_RESEARCH_CLAUDE
        )

    async def execute(self, message: AgentMessage) -> AgentResponse:
        return await self._run(message)

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    async def _run(self, message: AgentMessage) -> AgentResponse:
        context = message.context or {}
        query = message.payload.get("query", "")
        original_query = context.get("original_query", query)
        system_prompt = context.get("system_prompt", "")
        model = context.get("model", "claude-sonnet-4-6")

        self._on_agent_start(f"[{model}] {query[:60]}")

        # Per-user override from context (set by DeepResearchAgent from UserBotConfig).
        # Falls back to class-level flag AND env var.
        second_pass_enabled = context.get("second_pass", self._SECOND_PASS_ENABLED) and DEEP_RESEARCH_SECOND_PASS

        try:
            (
                result_text,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_write_tokens,
            ) = await self._research_loop(
                query=query,
                system_prompt=system_prompt,
                model=model,
            )
        except Exception as exc:
            logger.error("[DeepResearchRunner] Research loop failed: %s", exc, exc_info=True)
            self._on_agent_error(exc, "research_loop")
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=str(exc),
            )

        round1_text = result_text  # capture before optional second-pass override

        if second_pass_enabled:
            logger.info("[DeepResearchRunner] Starting second-pass critic session")
            critic_query = self._build_critic_query(original_query, result_text)
            try:
                (
                    result_text,
                    extra_in,
                    extra_out,
                    extra_cr,
                    extra_cw,
                ) = await self._research_loop(
                    query=critic_query,
                    system_prompt=system_prompt,
                    model=model,
                )
                input_tokens += extra_in
                output_tokens += extra_out
                cache_read_tokens += extra_cr
                cache_write_tokens += extra_cw
                logger.info(
                    "[DeepResearchRunner] Second-pass complete, extra_tokens=%d",
                    extra_in + extra_out,
                )
            except Exception as exc:
                logger.warning(
                    "[DeepResearchRunner] Second-pass failed (using first-pass result): %s", exc
                )

        total_tokens = input_tokens + output_tokens

        self._on_agent_success(
            char_count=len(result_text),
            token_count=total_tokens,
            output_text=result_text,
        )

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result={
                "text": result_text,
                "round1_text": round1_text,
                "query": original_query,
                "model": model,
                # prompt/completion kept separate so billing prices output at the
                # output rate; total_tokens stays for display/delivery.
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
                "second_pass": second_pass_enabled,
            },
        )

    @staticmethod
    def _build_critic_query(original_query: str, first_pass_result: str) -> str:
        """
        Build the second-pass user message.

        Framed as independent verification: the first-pass result is provided as unverified
        leads — the model must re-investigate using its tools and produce a fresh authoritative
        report. The output frame ("written as if the notes didn't exist") prevents the model
        from switching into critic/reviewer mode in its response body.
        An optional "Research Notes" appendix gives a sanctioned outlet for self-assessment.
        """
        return (
            f"I'm researching the following topic:\n\n"
            f"{original_query}\n\n"
            f"Below are unverified preliminary notes from an earlier investigation. "
            f"Treat them as leads to explore — not as established facts. "
            f"Some claims may be accurate, others incomplete or misleading.\n\n"
            f"--- PRELIMINARY NOTES (unverified) ---\n"
            f"{first_pass_result}\n"
            f"--- END OF PRELIMINARY NOTES ---\n\n"
            f"Using your research tools, independently verify the key claims, "
            f"investigate what may be missing or inaccurate, and conduct your own "
            f"comprehensive research on this topic.\n\n"
            f"Your output must be a complete, authoritative research report — written "
            f"as if the preliminary notes did not exist. Do NOT review or comment on "
            f"the preliminary notes in the body of your report.\n\n"
            f"If your independent research revealed significant discrepancies or important "
            f"additions compared to the preliminary notes, you may include a brief "
            f"\"Research Notes\" appendix at the very end of your report."
        )

    @staticmethod
    def _extract_container_id(response) -> Optional[str]:
        """
        Robustly extract container.id from an Anthropic Message response.

        Handles three SDK variants:
          - Typed attribute:    response.container.id  (newer SDK with Container model)
          - Dict extra field:   response.model_extra["container"]["id"]  (Pydantic extra)
          - Missing attribute:  returns None (older SDK; container not in response)
        """
        # 1. Typed attribute (preferred path)
        container = getattr(response, "container", None)
        # 2. Pydantic v2 extra fields fallback
        if container is None:
            model_extra = getattr(response, "model_extra", None) or {}
            container = model_extra.get("container")
        if container is None:
            return None
        # container may be a dict or a typed object
        if isinstance(container, dict):
            return container.get("id") or None
        return getattr(container, "id", None) or None

    async def _call_with_overload_retry(self, call_kwargs: dict) -> tuple:
        """
        Call the Anthropic streaming API, retrying on overloaded_error with exponential backoff.

        Returns (message, container_id_or_none).

        container_id is captured by scanning EVERY stream event — not just the final message.
        The Anthropic API may include `container` in any SSE event (message_start, message_delta,
        message_stop, or a custom event), and the SDK does not always propagate it to the
        final Message snapshot.

        Raises on the last attempt or on any non-overload error.
        """
        delay = self._OVERLOAD_RETRY_BASE_DELAY
        for attempt in range(self._MAX_OVERLOAD_RETRIES + 1):
            try:
                container_id_found: Optional[str] = None
                async with self._client.messages.stream(**call_kwargs) as stream:
                    # Container arrives in message_delta as event.delta.container.id.
                    # SDK does NOT propagate delta.container into the final Message snapshot —
                    # must be captured here. This is the only reliable source for streaming.
                    async for event in stream:
                        if container_id_found is None and getattr(event, "type", None) == "message_delta":
                            delta = getattr(event, "delta", None)
                            delta_container = getattr(delta, "container", None) if delta else None
                            if delta_container is not None:
                                container_id_found = getattr(delta_container, "id", None) or None
                                if container_id_found:
                                    logger.info(
                                        "[DeepResearchRunner] Container from message_delta: %s",
                                        container_id_found[:16],
                                    )
                    response = await stream.get_final_message()

                # Also check final message (belt-and-suspenders — covers typed SDK field).
                if container_id_found is None:
                    container_id_found = self._extract_container_id(response)
                    if container_id_found:
                        logger.info(
                            "[DeepResearchRunner] Container captured from final message: %s",
                            container_id_found[:16],
                        )

                if container_id_found is None and response.stop_reason == "pause_turn":
                    logger.warning(
                        "[DeepResearchRunner] pause_turn but no container found — "
                        "next continuation will fail; response keys: %s",
                        list(getattr(response, "model_extra", None) or {}),
                    )

                return response, container_id_found

            except Exception as exc:
                is_overload = "overloaded_error" in str(exc)
                if is_overload and attempt < self._MAX_OVERLOAD_RETRIES:
                    logger.warning(
                        "[DeepResearchRunner] overloaded_error (attempt %d/%d) — retrying in %ds",
                        attempt + 1, self._MAX_OVERLOAD_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise

    async def _research_loop(
        self, query: str, system_prompt: str, model: str
    ) -> tuple[str, int, int, int, int]:
        """
        Single Claude session with native built-in tools.

        The API manages all tool execution server-side — we only handle pause_turn
        continuations by sending back the partial response as an assistant message.

        Returns (result_text, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens).
        input/output are kept separate (not pre-summed) so the caller can price output
        at the output rate — collapsing them into one total bills output as cheap input.

        Loop protocol:
          end_turn   → extract text blocks, return as final result.
          pause_turn → server-side code_execution still running; append partial response
                       as assistant message and loop again.
          max_tokens → output budget exhausted; return partial text if any.
        """
        system: list[dict] = (
            [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
            if system_prompt else []
        )

        if any(m in model for m in self._THINKING_MODELS):
            max_tokens = 64_000  # extended output beta supports up to 128K; 64K is safe ceiling
            extra_kwargs: dict = {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": "high"},
                "temperature": 1.0,
            }
        else:
            max_tokens = 32_000
            extra_kwargs = {
                "thinking": {"type": "enabled", "budget_tokens": 24_000},
                "temperature": 1.0,
            }

        messages: list[dict] = [{"role": "user", "content": query}]
        accumulated_content: list = []
        total_input_tokens = 0
        total_output_tokens = 0
        total_tokens = 0
        total_cache_read_tokens = 0
        total_cache_write_tokens = 0
        pause_count = 0
        container_id: Optional[str] = None

        logger.info(
            "[DeepResearchRunner] Starting: model=%s query_len=%d",
            model, len(query),
        )

        while True:
            call_kwargs = dict(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=self._NATIVE_TOOLS,
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31,output-128k-2025-02-19"},
                **extra_kwargs,
            )
            if container_id:
                call_kwargs["container"] = container_id

            response, new_container_id = await self._call_with_overload_retry(call_kwargs)
            container_id = new_container_id or container_id

            if hasattr(response, "usage") and response.usage:
                u = response.usage
                total_input_tokens += u.input_tokens or 0
                total_output_tokens += u.output_tokens or 0
                total_tokens = total_input_tokens + total_output_tokens
                total_cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
                total_cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0

            accumulated_content.extend(response.content)

            block_counts: dict[str, int] = {}
            for b in response.content:
                block_counts[getattr(b, "type", "?")] = block_counts.get(getattr(b, "type", "?"), 0) + 1
            block_summary = " ".join(f"{t}:{n}" for t, n in block_counts.items())
            logger.info(
                "[DeepResearchRunner] stop_reason=%s pause#=%d tokens=%d container=%s blocks=%d (%s)",
                response.stop_reason, pause_count, total_tokens, container_id,
                len(response.content), block_summary,
            )

            if response.stop_reason == "end_turn":
                text = "".join(
                    b.text for b in accumulated_content
                    if getattr(b, "type", None) == "text"
                )
                if not text:
                    logger.warning("[DeepResearchRunner] end_turn with no text blocks")
                response_text_blocks = [
                    b.text for b in accumulated_content if getattr(b, "type", None) == "text"
                ]
                self._debug_raw_turn(
                    system_blocks=system,
                    user_content=query,
                    response_texts=response_text_blocks,
                    tokens=total_tokens,
                    turn=0,
                    model=model,
                )
                return (
                    text,
                    total_input_tokens,
                    total_output_tokens,
                    total_cache_read_tokens,
                    total_cache_write_tokens,
                )

            if response.stop_reason == "pause_turn":
                pause_count += 1
                if pause_count >= self._MAX_PAUSE_TURNS:
                    raise RuntimeError(
                        f"[ClaudeDeepResearchRunner] Exceeded {self._MAX_PAUSE_TURNS} pause_turns"
                    )
                # Resume: send back partial response so the API can continue tool execution.
                messages = [
                    {"role": "user", "content": query},
                    {"role": "assistant", "content": accumulated_content},
                ]
                logger.info(
                    "[DeepResearchRunner] pause_turn #%d — resuming (%d total blocks so far)",
                    pause_count, len(accumulated_content),
                )
                continue

            # max_tokens or unexpected stop reason — return partial text if available.
            logger.warning(
                "[DeepResearchRunner] Unexpected stop_reason=%s — returning partial text",
                response.stop_reason,
            )
            partial = "".join(
                b.text for b in accumulated_content if getattr(b, "type", None) == "text"
            )
            if partial:
                self._debug_raw_turn(
                    system_blocks=system,
                    user_content=query,
                    response_texts=[partial],
                    tokens=total_tokens,
                    turn=0,
                    model=model,
                )
                return (
                    partial,
                    total_input_tokens,
                    total_output_tokens,
                    total_cache_read_tokens,
                    total_cache_write_tokens,
                )
            raise RuntimeError(
                f"[ClaudeDeepResearchRunner] stop_reason={response.stop_reason!r} with no text"
            )
