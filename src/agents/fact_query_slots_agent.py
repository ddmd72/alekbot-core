"""
FactQuerySlotsAgent — the query step of fact search (fact profile RFC §5.1).

Turns a request for the user's memory into a `FactQuery`: the slots (subject, kind, people, places, time, domains,
kinds, scope) the search lists run on. The request arrives as Smart's search request, the caller's words (connector,
voice), or the Router's conversation messages. One cheap-model call writes the slots; `parse_slots` decides what
survives. When the call cannot give a usable answer the agent degrades to one semantic list over the raw request: a
memory search always runs, it only loses the narrowing lists.

Internal: not manifest-registered, no tier of its own, and no caller yet. It is built by its caller with the caller's
execution context (the model is the caller's cheap model), like `LelikSummarizerAgent`. The prompt is the P1 head
(`OUTPUT_FORMAT_FACT_QUERY_SLOTS`) plus the LIFE AREAS block rendered from the domain registry, so a new life area
reaches this prompt by code deploy alone.
"""
import json
import time
from datetime import date, datetime, timezone
from typing import Callable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain.agent import AgentConfig, AgentMessage, AgentResponse
from ..domain.fact_domains import render_life_areas_block
from ..domain.fact_query import FactQuery
from ..domain.fact_query_parse import QUERY_DOMAINS, QUERY_INTENTS, ParsedSlots, parse_slots, semantic_only_query
from ..infrastructure.agent_config import FACT_QUERY_SLOTS
from ..ports.llm_port import AgentExecutionContext, LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger
from .base_agent import BaseAgent

AGENT_TYPE = "fact_query_slots"
PATH_REQUEST = "request"
PATH_MESSAGES = "messages"

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# The kinds in the order the prompt lists them.
_KINDS_IN_PROMPT_ORDER = ("preference", "constraint", "intention", "document", "ownership", "incident", "identifier")

_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["subject", "kind", "people", "places", "time", "domains", "scope", "kinds", "domain_link"],
    "properties": {
        "subject": {"type": "string"},
        "kind": {"type": "string"},
        "people": {"type": "array", "items": {"type": "string"}},
        "places": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "country"],
                "properties": {"name": {"type": "string"}, "country": {"type": "string"}},
            },
        },
        "time": {
            "type": "object",
            "required": ["intent", "from", "to"],
            "properties": {
                "intent": {"type": "string", "enum": list(QUERY_INTENTS)},
                "from": {"type": "string"},
                "to": {"type": "string"},
            },
        },
        "domains": {"type": "array", "items": {"type": "string", "enum": list(QUERY_DOMAINS)}},
        "kinds": {"type": "array", "items": {"type": "string", "enum": list(_KINDS_IN_PROMPT_ORDER)}},
        "domain_link": {"type": "string", "enum": ["one_matter", "separate_topics"]},
        "scope": {"type": "string", "enum": ["top", "all"]},
    },
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _InvalidOutput(ValueError):
    """The model's reply is not usable slots; `reason` is the log word for it."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class FactQuerySlotsAgent(BaseAgent):
    """Public interface: `query_for_request(...)` and `query_for_messages(...)`, both returning a `FactQuery` and
    never raising for a model failure."""

    MAX_TOKENS = FACT_QUERY_SLOTS.max_tokens

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        prompt_builder: PromptBuilderPort,
        user_timezone: Optional[str] = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        super().__init__(config)
        self._set_execution_context(execution_context)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self.prompt_builder = prompt_builder
        self._zone_name, self._zone = _resolve_zone(user_timezone)
        self._clock = clock

    # ── Delegation surface (internal use only) ────────────────────────────────────────────────────────────────

    async def can_handle(self, message: AgentMessage) -> bool:
        return isinstance(message.payload.get("request"), str)

    async def execute(self, message: AgentMessage) -> AgentResponse:
        request = message.payload.get("request")
        if not isinstance(request, str):
            return AgentResponse.failure(
                task_id=message.task_id, agent_id=self.agent_id, error="No request provided in payload",
            )
        query = await self.query_for_request(
            request, path=str(message.payload.get("path") or PATH_REQUEST),
            account_id=message.context.get("account_id"), user_id=message.context.get("user_id"),
        )
        return AgentResponse.success(task_id=message.task_id, agent_id=self.agent_id, result={"query": query})

    # ── Public API ────────────────────────────────────────────────────────────────────────────────────────────

    async def query_for_request(
        self, request: str, *, path: str, account_id: Optional[str], user_id: Optional[str],
    ) -> FactQuery:
        """The query for one request written as text: Smart's search request or the caller's own words."""
        messages = [Message(role="user", parts=[MessagePart(text=f'SEARCH_REQUEST "{request}"')])]
        return await self._query(messages, request, path=path, account_id=account_id, user_id=user_id)

    async def query_for_messages(
        self, messages: Sequence[Message], *, path: str, account_id: Optional[str], user_id: Optional[str],
    ) -> FactQuery:
        """The query for a conversation: the last user message is the question, the earlier ones say what a short
        follow-up refers to. Messages without text are dropped and trailing non-user turns popped."""
        kept = [m for m in messages if _text_of(m)]
        while kept and kept[-1].role != "user":
            kept.pop()
        raw = _text_of(kept[-1]) if kept else ""
        return await self._query(kept, raw, path=path, account_id=account_id, user_id=user_id)

    # ── One call ──────────────────────────────────────────────────────────────────────────────────────────────

    async def _query(
        self, messages: List[Message], raw: str, *, path: str, account_id: Optional[str], user_id: Optional[str],
    ) -> FactQuery:
        started = time.perf_counter()
        local_now = self._clock().astimezone(self._zone)
        today = local_now.date()

        def degraded(reason: str) -> FactQuery:
            query = semantic_only_query(raw, today=today, path=path)
            self._log(path, f"degraded:{reason}", query, (), (), started)
            return query

        if not messages:
            return degraded("no_messages")
        try:
            system = await self.prompt_builder.build_for_agent(
                AGENT_TYPE, user_id=user_id, account_id=account_id,
                include_biographical=False, include_directives=False, include_location=False,
                extra_static_blocks=[render_life_areas_block()],
            )
        except Exception as e:
            logger.error(f"[FactQuery] prompt build failed: {type(e).__name__}: {e}")
            return degraded("prompt_build_failed")
        system += self._today_line(local_now)

        history = list(messages)
        reason = "invalid_output"
        for attempt in range(2):
            try:
                response = await self._call_llm(self._request(system, history), turn=attempt)
            except Exception as e:
                logger.warning(f"[FactQuery] model call failed: {type(e).__name__}: {e}")
                return degraded("call_failed")
            text = response.text or ""
            try:
                parsed = self._parse(text, today=today, path=path)
            except _InvalidOutput as e:
                reason = e.reason
                logger.warning(f"[FactQuery] invalid output on attempt {attempt + 1}/2 ({reason}): {e}")
                history.append(Message(role="model", parts=[MessagePart(text=text)]))
                history.append(Message(role="user", parts=[MessagePart(text=(
                    f"Your previous output was not valid query slots ({e}). Output only the JSON object described "
                    "by the output format: no markdown, no explanation."
                ))]))
                continue
            self._log(path, "slots", parsed.query, parsed.refused, parsed.dropped, started)
            return parsed.query
        return degraded(reason)

    def _request(self, system: str, history: List[Message]) -> LLMRequest:
        return LLMRequest(
            model_name=self.model_name, system_instruction=system, messages=list(history), tools=[],
            max_tokens=self.MAX_TOKENS, disable_safety=True, thinking=None,
            response_mime_type="application/json", response_schema=_RESPONSE_SCHEMA,
        )

    @staticmethod
    def _parse(text: str, *, today: date, path: str) -> ParsedSlots:
        if not text.strip():
            raise _InvalidOutput("empty reply", "empty_output")
        try:
            payload = json.loads(text)
        except ValueError as e:
            raise _InvalidOutput(f"not JSON: {e}", "invalid_output") from e
        try:
            return parse_slots(payload, today=today, path=path)
        except ValueError as e:
            raise _InvalidOutput(str(e), "invalid_output") from e

    def _today_line(self, local_now: datetime) -> str:
        return (f"\nToday: {local_now.date().isoformat()} ({_WEEKDAYS[local_now.weekday()]}), "
                f"timezone {self._zone_name}.\n")

    @staticmethod
    def _log(path: str, outcome: str, query: FactQuery, refused: Tuple[str, ...], dropped: Tuple[str, ...],
             started: float) -> None:
        logger.info(
            "[FactQuery] path=%s outcome=%s intent=%s domains=%s kinds=%s people=%d places=%d scope=%s "
            "refused=%s dropped=%d ms=%d",
            path, outcome, query.time.intent, ",".join(query.domains) or "-", ",".join(query.kinds) or "-",
            len(query.people), len(query.places), query.scope, ",".join(refused) or "-", len(dropped),
            round((time.perf_counter() - started) * 1000),
        )


def _text_of(message: Message) -> str:
    return "".join(p.text or "" for p in message.parts).strip()


def _resolve_zone(name: Optional[str]) -> Tuple[str, ZoneInfo]:
    """The user's zone; an unknown or malformed name falls back to UTC (as the prompt assembler does)."""
    try:
        return (name or "UTC"), ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return "UTC", ZoneInfo("UTC")
