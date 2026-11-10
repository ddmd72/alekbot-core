"""
Memory Search Agent
===================

The one memory entry point (fact profile RFC §5.4). A caller sends one to four English phrases in `queries`, most
important first. For each phrase the query-slots agent reads the structured query (people, places, time, domains); the
fact search scores every phrase on its own candidates and fuses them in the caller's priority order.

There is no other request shape and no fallback: a call without phrases comes back as a tool note.
"""

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, List, Sequence

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..domain.fact_query import FactQuery
from ..domain.fact_query_parse import semantic_only_query
from ..domain.mcp import QUERIES_MAX, normalize_queries, queries_dropped
from ..domain.fact_search_result import PhraseSearchResult
from ..domain.memory_search_format import format_search_result
from ..domain.memory_search_texts import NO_QUERIES_NOTE
from ..ports.fact_search_port import FactSearchPort
from ..utils.logger import logger

if TYPE_CHECKING:
    from .fact_query_slots_agent import FactQuerySlotsAgent

PATH = "memory_agent"


class FactsMemoryAgent(BaseAgent):
    """
    Agent responsible for searching the user's personal memory (facts).

    Capabilities:
    - search_memory: `payload["queries"]` (1-4 phrases) -> slots per phrase -> scored multi-phrase search
    """

    def __init__(
        self,
        config: AgentConfig,
        fact_search: FactSearchPort,
        slots: "FactQuerySlotsAgent",
        account_id: str,
        user_id: str,
        user_location: str,
    ):
        super().__init__(config)
        self._fact_search = fact_search
        self._slots = slots
        self._account_id = account_id
        self._user_id = user_id
        self._user_location = user_location
        logger.info(f"🧠 FactsMemoryAgent initialized for account {account_id[:20]}...")

    @property
    def account_id(self) -> str:
        """The tenant this agent serves (read by the connector's tenant check)."""
        return self._account_id

    async def can_handle(self, message: AgentMessage) -> bool:
        """The coordinator routes `search_memory` here as a QUERY; a call without `queries` must reach `execute` and
        come back as a tool note rather than fall through to another agent."""
        return message.intent == AgentIntent.QUERY

    async def execute(self, message: AgentMessage) -> AgentResponse:
        """Read `queries` from the payload (the coordinator spreads declared context fields there, never into the
        message context) and search. `query` is the need in a few words: logged, never searched."""
        need = str(message.payload.get("query", ""))
        self._on_agent_start(need)

        raw = message.payload.get("queries")
        queries = normalize_queries(raw)
        if not queries:
            logger.warning(f"🧠 [FactsMemoryAgent] search_memory without phrases (need={need[:80]!r})")
            return AgentResponse.success(
                task_id=message.task_id, agent_id=self.agent_id, result=NO_QUERIES_NOTE, confidence=0.0,
            )
        dropped = queries_dropped(raw)
        if dropped:
            logger.warning(f"🧠 [FactsMemoryAgent] {dropped} phrase(s) beyond the first {QUERIES_MAX} dropped")
        logger.info(f"🧠 [FactsMemoryAgent] need={need[:80]!r}, {len(queries)} phrase(s)")

        started = time.perf_counter()
        try:
            found = await self.search_facts(queries, path=PATH, message=_latest_user_text(message))
        except Exception as e:
            self._on_agent_error(e)
            return AgentResponse.failure(
                task_id=message.task_id, agent_id=self.agent_id, error=f"Memory search failed: {e}",
            )

        facts = found.facts
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        result = format_search_result(queries, found)
        logger.info(f"✅ [FactsMemoryAgent] {len(facts)} fact(s) for {len(queries)} phrase(s) in {elapsed_ms} ms")
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=result,
            confidence=min(1.0, len(facts) / 5.0) if facts else 0.0,
            metadata={
                "path": PATH,
                "phrase_count": len(queries),
                "result_count": len(facts),
                "total_duration_ms": elapsed_ms,
                "account_id": self._account_id,
            },
        )

    async def search_facts(
        self, queries: Sequence[str], *, path: str, message: str = "",
    ) -> PhraseSearchResult:
        """The fused facts, with the phrases that found each, for `queries` (1-4 non-empty phrases, priority order). `message`: the user's latest
        message when the calling turn has one; it is what the message similarity reads, else the phrase itself."""
        built = await asyncio.gather(*(self._query_for(phrase, path) for phrase in queries))
        fact_queries = [
            replace(q, home=self._user_location, message=message or q.message or phrase)
            for q, phrase in zip(built, queries)
        ]
        return await self._fact_search.search_phrases_matched(self._account_id, fact_queries)

    async def _query_for(self, phrase: str, path: str) -> FactQuery:
        """One phrase's query. The slots agent degrades on its own failures; an exception that still escapes costs
        this phrase its slots, not the search."""
        try:
            return await self._slots.query_for_request(
                phrase, path=path, account_id=self._account_id, user_id=self._user_id,
            )
        except Exception as e:
            logger.warning(f"⚠️ [FactsMemoryAgent] slots failed for one phrase ({type(e).__name__}): {e}")
            return replace(
                semantic_only_query(phrase, today=datetime.now(timezone.utc).date(), path=path), message=phrase,
            )

    def _get_alternative_agents(self) -> List[str]:
        return ["web_search_agent", "reasoning_agent"]


def _latest_user_text(message: AgentMessage) -> str:
    """The user's latest message of the calling turn, or "". Smart and Quick pass their `current_message_parts`
    through the delegation context; Lelik's call context ends with the pending request."""
    parts = message.context.get("current_message_parts") or []
    text = " ".join(t for t in (_part_text(p) for p in parts) if t).strip()
    if text:
        return text
    for entry in reversed(message.payload.get("call_context") or []):
        if isinstance(entry, dict) and entry.get("role") == "user" and str(entry.get("text") or "").strip():
            return str(entry["text"]).strip()
    return ""


def _part_text(part: Any) -> str:
    text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
    return text.strip() if isinstance(text, str) else ""
