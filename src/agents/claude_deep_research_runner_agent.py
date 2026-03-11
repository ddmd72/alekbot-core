"""
ClaudeDeepResearchRunnerAgent — specialist agent that executes a Claude deep research loop.

Invoked via agent_execution Cloud Task from ClaudeDeepResearchAdapter.create_interaction().
Internal: never exposed to LLMs, never shown in tool descriptions.

Responsibilities (single responsibility):
  1. Run multi-turn Claude API call with native built-in tools.
  2. Return the result text in AgentResponse — delivery is handled externally
     by AgentWorkerHandler + notification service.

Native tools (GA — no beta headers required):
  web_search_20260209  — web search with dynamic filtering (Claude filters results via code)
  web_fetch_20260209   — URL/PDF fetching with dynamic filtering
  code_execution_20250825 is auto-injected by the API when web_search/web_fetch are present;
  do NOT declare it explicitly — the API raises a duplicate-name conflict.

Thinking: adaptive, effort=high. Temperature must be 1.0 when thinking is active.

Message payload shape (from AgentWorkerHandler):
  message.payload = {"query": <full research brief>, "intent": "execute_deep_research_claude"}
  message.context = {
      "user_id": ..., "account_id": ..., "original_query": ...,
      "system_prompt": ..., "model": ..., "job_id": ...,
  }

Caching strategy:
  System block 1 — original system prompt with cache_control: ephemeral.
                   Static, never changes → cache HIT from turn 2 onwards.
  System block 2 — static header (_HISTORY_HEADER + query_prefix). No cache_control;
                   covered by round 1's breakpoint.
  System blocks 3…N — one block per completed research round, each with cache_control.
                   Earlier rounds are stable → cache HIT. Newest round = cache WRITE.
                   Turn 3: round1=HIT, round2=WRITE. Turn 4: round1=HIT, round2=HIT, round3=WRITE.
                   Stays within Anthropic's 4-breakpoint limit (system_prompt + 3 round blocks).
  messages[]     — only the current turn's user message (query on turn 1, continuation prompt
                   on subsequent turns). No growing conversation history.
"""
from typing import Any, Optional

from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..infrastructure.agent_manifest import Intent
from ..utils.logger import logger
from .base_agent import BaseAgent

_HISTORY_HEADER = "=== Previous Research Rounds ==="
_CONTINUATION_PROMPT = (
    "Continue the research based on the previous rounds above. "
    "Do not repeat what was already found — go deeper or cover remaining gaps."
)


class ClaudeDeepResearchRunnerAgent(BaseAgent):
    """
    Executes the full Claude deep research loop for async deep research tasks.

    Registered as internal specialist (internal=True, ASYNC mode).
    Invoked via agent_execution Cloud Task — never routed to by LLMs.

    Returns the research result text in AgentResponse.result.
    Delivery (HTML report upload, SmartAgent formatting, user notification) is handled
    externally by AgentWorkerHandler + notification service.
    """

    # Safety valve: maximum tool-use turns before giving up.
    _MAX_TURNS = 15

    # Models that support adaptive thinking + output_config effort.
    _THINKING_MODELS = {"claude-sonnet-4-6", "claude-opus-4-6"}

    # Built-in tools — Anthropic executes these server-side within the API call.
    # No beta headers required — these are GA tools (docs: platform.claude.com/docs/en/agents-and-tools/).
    #
    # web_search_20260209  — dynamic filtering: Claude post-processes results via code execution
    #                        before they reach the context window (reduces tokens, improves quality).
    # web_fetch_20260209   — dynamic filtering: same as above for fetched pages/PDFs.
    # code_execution_20250825 — required for dynamic filtering; also available to Claude directly.
    #                           Free when used alongside web_search or web_fetch.
    # allowed_callers=["direct"] disables dynamic filtering (auto-injected code_execution).
    # Dynamic filtering causes container_id errors in multi-turn loops because the auto-injected
    # code_execution creates a container whose ID is not returned in response.container,
    # making it impossible to pass back on subsequent turns.
    _NATIVE_TOOLS = [
        {"type": "web_search_20260209", "name": "web_search", "allowed_callers": ["direct"]},
        {"type": "web_fetch_20260209",  "name": "web_fetch",  "allowed_callers": ["direct"]},
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

        # Return result text — delivery is handled by AgentWorkerHandler
        # via notification service.
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
        Multi-turn Claude loop with native built-in tools.

        Returns (result_text, total_tokens) where total_tokens is the sum of
        input + output tokens across all turns (for billing tracking).

        Caching strategy:
          - System block 1: original system prompt, cached once (never changes). HIT from turn 2+.
          - System block 2: static header (_HISTORY_HEADER + query_prefix). No cache_control —
            covered by round 1's breakpoint.
          - System blocks 3…N: one block per completed research round, each with cache_control.
            Blocks for earlier rounds are stable → cache HIT. Only the newest round is a WRITE.
            Turn 3: round1=HIT, round2=WRITE. Turn 4: round1=HIT, round2=HIT, round3=WRITE.
            Max 4 cache breakpoints (system_prompt + 3 round blocks) — within Anthropic's limit.
          - messages[]: reset each turn to a single user message — no growing history.

        Built-in tool protocol:
          stop_reason == "end_turn"   → done; extract text blocks.
          stop_reason == "tool_use"   → tools executed server-side; results embedded in
            assistant content. Serialise content into system history; start fresh turn.
          stop_reason == "pause_turn" → same as tool_use (API paused long-running turn).
        """
        # Block 1: original system prompt — cached once, hit every subsequent turn.
        system_block_1: Optional[dict] = (
            {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
            if system_prompt else None
        )

        # Accumulated serialised content from all completed turns.
        # Prefixed with the original research query so it stays explicit in all continuation turns.
        accumulated_rounds: list[str] = []
        query_prefix = f"Research question: {query}"

        total_tokens = 0

        logger.info(
            "[DeepResearchRunner] Starting loop: model=%s max_turns=%d query_len=%d",
            model, self._MAX_TURNS, len(query),
        )

        # Adaptive thinking (+ output_config effort) — Sonnet 4.6 and Opus 4.6 only.
        # Haiku 4.5 supports extended thinking but only via type: "enabled" + budget_tokens.
        # budget_tokens must be < max_tokens, so Haiku needs a higher max_tokens cap.
        if model in self._THINKING_MODELS:
            max_tokens = 16_000
            extra_kwargs: dict = {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": "high"},
                "temperature": 1.0,
            }
        else:
            max_tokens = 48_000  # 32K thinking + 16K output headroom
            extra_kwargs = {
                "thinking": {"type": "enabled", "budget_tokens": 32_000},
                "temperature": 1.0,
            }

        for turn in range(self._MAX_TURNS):
            # Build system for this turn.
            system: list[dict] = []
            if system_block_1:
                system.append(system_block_1)
            if accumulated_rounds:
                # Static header — no cache_control; covered by round 1's breakpoint.
                system.append({
                    "type": "text",
                    "text": _HISTORY_HEADER + "\n\n" + query_prefix,
                })
                # One block per completed round, each with its own cache breakpoint.
                # Earlier rounds are identical to previous turns → cache HIT.
                # Only the newest round is a cache WRITE.
                for round_text in accumulated_rounds:
                    system.append({
                        "type": "text",
                        "text": round_text,
                        "cache_control": {"type": "ephemeral"},
                    })

            # Fresh messages each turn — no growing conversation history.
            user_content = query if turn == 0 else _CONTINUATION_PROMPT
            messages: list[dict] = [{"role": "user", "content": user_content}]

            async with self._client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=self._NATIVE_TOOLS,
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                **extra_kwargs,
            ) as stream:
                response = await stream.get_final_message()

            turn_tokens = 0
            if hasattr(response, "usage") and response.usage:
                turn_tokens = (response.usage.input_tokens or 0) + (response.usage.output_tokens or 0)
                total_tokens += turn_tokens

            response_text_blocks = [
                b.text for b in response.content if getattr(b, "type", None) == "text"
            ]
            self._debug_raw_turn(
                system_blocks=system,
                user_content=user_content,
                response_texts=response_text_blocks,
                tokens=turn_tokens,
                turn=turn,
                model=model,
            )

            logger.info(
                "[DeepResearchRunner] Turn %d/%d: stop_reason=%s blocks=%d tokens=%d",
                turn + 1, self._MAX_TURNS, response.stop_reason, len(response.content), total_tokens,
            )

            if response.stop_reason == "end_turn":
                text = "".join(
                    b.text for b in response.content
                    if getattr(b, "type", None) == "text"
                )
                if not text:
                    logger.warning(
                        "[DeepResearchRunner] end_turn with no text blocks on turn %d", turn + 1
                    )
                return text, total_tokens

            if response.stop_reason in ("tool_use", "pause_turn"):
                # Serialise full turn content into system history for the next turn.
                serialised = self._serialise_turn(turn + 1, response.content)
                accumulated_rounds.append(serialised)
                continue

            # Unexpected stop reason (max_tokens, stop_sequence, etc.)
            logger.warning(
                "[DeepResearchRunner] Unexpected stop_reason=%s on turn %d — stopping",
                response.stop_reason, turn + 1,
            )
            partial = "".join(
                b.text for b in response.content
                if getattr(b, "type", None) == "text"
            )
            if partial:
                return partial, total_tokens
            raise RuntimeError(
                f"[ClaudeDeepResearchRunner] Unexpected stop_reason={response.stop_reason!r} "
                f"with no text on turn {turn + 1}"
            )

        raise RuntimeError(
            f"[ClaudeDeepResearchRunner] Exceeded {self._MAX_TURNS} turns without end_turn"
        )

    @staticmethod
    def _serialise_turn(turn_num: int, content: list) -> str:
        """
        Convert a turn's response content blocks into a text representation
        for inclusion in the accumulated system history.

        Covers all block types that appear in built-in tool responses:
          thinking              — Claude's internal reasoning
          text                  — Claude's visible output / analysis
          tool_use              — search/fetch calls Claude issued
          web_search_tool_result / web_fetch_tool_result — server-side results
        """
        parts = [f"--- Round {turn_num} ---"]

        for block in content:
            block_type = getattr(block, "type", None)

            if block_type == "thinking":
                thinking_text = getattr(block, "thinking", "") or ""
                if thinking_text:
                    parts.append(f"[Thinking]\n{thinking_text}")

            elif block_type == "text":
                text = getattr(block, "text", "") or ""
                if text:
                    parts.append(text)

            elif block_type == "tool_use":
                name = getattr(block, "name", "tool")
                inp = getattr(block, "input", {})
                parts.append(f"[{name}: {inp}]")

            else:
                # web_search_tool_result, web_fetch_tool_result, or unknown built-in result.
                # Use model_dump() to extract content generically.
                try:
                    raw = block.model_dump() if hasattr(block, "model_dump") else {}
                except Exception:
                    raw = {}

                result_content = raw.get("content", [])
                if isinstance(result_content, str) and result_content:
                    parts.append(f"[{block_type}]\n{result_content}")
                elif isinstance(result_content, list):
                    for item in result_content:
                        if isinstance(item, dict):
                            title = item.get("title", "")
                            url = item.get("url", "")
                            text = item.get("text", item.get("content", item.get("snippet", "")))
                            header = " — ".join(filter(None, [title, url]))
                            if header or text:
                                parts.append(f"[{block_type}: {header}]\n{text}" if header else text)

        return "\n\n".join(parts)
