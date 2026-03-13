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
from typing import Any, Optional

from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..infrastructure.agent_manifest import Intent
from ..utils.logger import logger
from .base_agent import BaseAgent


class ClaudeDeepResearchRunnerAgent(BaseAgent):
    """
    Executes the full Claude deep research loop for async deep research tasks.

    Registered as internal specialist (internal=True, ASYNC mode).
    Invoked via agent_execution Cloud Task — never routed to by LLMs.

    Returns the research result text in AgentResponse.result.
    Delivery (HTML report upload, SmartAgent formatting, user notification) is handled
    externally by AgentWorkerHandler + notification service.
    """

    # Safety valve: maximum pause_turn continuations before giving up.
    # Each pause_turn = one server-side tool batch still executing. In practice 5-20 per session.
    _MAX_PAUSE_TURNS = 50

    # Models that support adaptive thinking + output_config effort.
    _THINKING_MODELS = {"claude-sonnet-4-6", "claude-opus-4-6"}

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

    def _get_alternative_agents(self) -> list:
        return []

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

        try:
            result_text, total_tokens = await self._research_loop(
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

        self._on_agent_success(
            char_count=len(result_text),
            token_count=total_tokens,
            output_text=result_text[:500],
        )

        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result={
                "text": result_text,
                "query": original_query,
            },
        )

    async def _research_loop(self, query: str, system_prompt: str, model: str) -> tuple[str, int]:
        """
        Single Claude session with native built-in tools.

        The API manages all tool execution server-side — we only handle pause_turn
        continuations by sending back the partial response as an assistant message.

        Returns (result_text, total_tokens).

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

        if model in self._THINKING_MODELS:
            max_tokens = 32_000
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
        total_tokens = 0
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
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                **extra_kwargs,
            )
            if container_id:
                call_kwargs["container"] = container_id

            async with self._client.messages.stream(**call_kwargs) as stream:
                response = await stream.get_final_message()

            # Extract container_id for code_execution continuations.
            raw_container = getattr(response, "container", None)
            if raw_container is not None:
                container_id = getattr(raw_container, "id", None) or str(raw_container) or None

            if hasattr(response, "usage") and response.usage:
                turn_tokens = (response.usage.input_tokens or 0) + (response.usage.output_tokens or 0)
                total_tokens += turn_tokens

            accumulated_content.extend(response.content)

            block_summary = [
                f"{getattr(b, 'type', '?')}:{getattr(b, 'name', '') or ''}"
                for b in response.content
            ]
            logger.info(
                "[DeepResearchRunner] stop_reason=%s pause#=%d tokens=%d container=%s new_blocks=%s",
                response.stop_reason, pause_count, total_tokens, container_id, block_summary,
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
                return text, total_tokens

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
                return partial, total_tokens
            raise RuntimeError(
                f"[ClaudeDeepResearchRunner] stop_reason={response.stop_reason!r} with no text"
            )
