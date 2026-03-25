"""
Unit tests for MemorySearchAgent.

Mock boundary: SearchEnrichmentPort, FactRepository, EmbeddingService.

Tests cover:
- can_handle: 3-key format, legacy query, wrong intent, no keys
- _format_fact_rich: text only, all fields, null fields omitted, date truncation, metadata JSON
- execute enriched path: formatted result with --- separator, empty results, exception → failure
- execute legacy path: formatted result, PRINCIPLE facts filtered
- execute no keys: failure response
"""

import json
from dataclasses import dataclass
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.memory_search_agent import FactsMemoryAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.entities import FactType
from src.domain.search import EnrichedContext, EnrichedFact
from src.ports.embedding_service import EmbeddingService
from src.ports.repository import FactRepository
from src.ports.search_enrichment_port import SearchEnrichmentPort


@dataclass
class _FakeFact:
    """Minimal FactEntity stand-in for legacy search tests (no 'content' attribute)."""
    text: str
    type: FactType
    context: Optional[str] = None
    reported_date: Optional[str] = None
    metadata: Optional[dict] = None

_ACCOUNT_ID = "account-abc123"
_USER_ID = "user-abc123"


def _make_agent(with_enrichment: bool = True, with_embedding: bool = False):
    repo = AsyncMock(spec=FactRepository)
    embedding = AsyncMock(spec=EmbeddingService)
    search_enrichment = AsyncMock(spec=SearchEnrichmentPort) if with_enrichment else None

    agent = FactsMemoryAgent(
        config=AgentConfig(
            agent_id=f"memory_search_agent_{_USER_ID}",
            agent_type="memory_search",
        ),
        repository=repo,
        embedding_service=embedding,
        account_id=_ACCOUNT_ID,
        search_enrichment=search_enrichment,
    )
    return agent, repo, embedding, search_enrichment


def _make_enriched_fact(
    content: str,
    context: str = None,
    reported_date: str = None,
    metadata: dict = None,
) -> EnrichedFact:
    return EnrichedFact(
        fact_id="fact-1",
        content=content,
        source="phrase_1",
        context=context,
        reported_date=reported_date,
        metadata=metadata,
    )


def _make_enriched_context(facts: list) -> EnrichedContext:
    return EnrichedContext(
        facts=facts,
        total_sources=3,
        dedup_count=0,
        biographical_dedup_count=0,
    )


def _make_message(payload: dict) -> AgentMessage:
    return AgentMessage(
        intent=AgentIntent.QUERY,
        payload=payload,
        sender="smart_response_agent",
        recipient=f"memory_search_agent_{_USER_ID}",
        task_id="task-1",
        context={"user_id": _USER_ID},
    )


class TestCanHandle:

    async def test_accepts_3key_format(self):
        agent, *_ = _make_agent()
        msg = _make_message({
            "keywords": ["health", "weight"],
            "primary_query": "User weight history",
            "alternative_query": "body weight measurements",
        })
        assert await agent.can_handle(msg) is True

    async def test_accepts_legacy_query(self):
        agent, *_ = _make_agent()
        msg = _make_message({"query": "what is my weight"})
        assert await agent.can_handle(msg) is True

    async def test_rejects_wrong_intent(self):
        agent, *_ = _make_agent()
        msg = AgentMessage(
            intent=AgentIntent.INFORM,
            payload={"query": "test"},
            sender="smart", recipient="memory_search_agent", task_id="t",
            context={"user_id": _USER_ID},
        )
        assert await agent.can_handle(msg) is False

    async def test_rejects_empty_payload(self):
        agent, *_ = _make_agent()
        msg = _make_message({})
        assert await agent.can_handle(msg) is False


class TestFormatFactRich:

    def test_text_only(self):
        agent, *_ = _make_agent()
        fact = _make_enriched_fact("User weighs 82 kg.")
        assert agent._format_fact_rich(fact) == "User weighs 82 kg."

    def test_all_fields_included(self):
        agent, *_ = _make_agent()
        fact = _make_enriched_fact(
            content="User weighs 82 kg.",
            context="weight tracking",
            reported_date="2026-01-21T10:00:00+00:00",
            metadata={"value": 82, "unit": "kg"},
        )
        result = agent._format_fact_rich(fact)
        assert "User weighs 82 kg." in result
        assert "context: weight tracking" in result
        assert "reported: 2026-01-21" in result
        assert '"value": 82' in result

    def test_null_fields_omitted(self):
        agent, *_ = _make_agent()
        fact = _make_enriched_fact("Some fact.", context=None, reported_date=None, metadata=None)
        result = agent._format_fact_rich(fact)
        assert "context:" not in result
        assert "reported:" not in result
        assert "metadata:" not in result

    def test_reported_date_truncated_to_date(self):
        agent, *_ = _make_agent()
        fact = _make_enriched_fact("Fact.", reported_date="2026-03-25T15:09:17.740029+00:00")
        result = agent._format_fact_rich(fact)
        assert "reported: 2026-03-25" in result
        assert "15:09" not in result

    def test_metadata_serialized_as_json(self):
        agent, *_ = _make_agent()
        meta = {"trips": [{"city": "Paris", "month": "Jan"}]}
        fact = _make_enriched_fact("Travel fact.", metadata=meta)
        result = agent._format_fact_rich(fact)
        assert f"metadata: {json.dumps(meta, ensure_ascii=False)}" in result


class TestExecuteEnrichedPath:

    async def test_two_facts_joined_by_separator(self):
        agent, _, _, enrichment = _make_agent()
        fact1 = _make_enriched_fact("Fact one.", context="ctx1", reported_date="2026-01-01")
        fact2 = _make_enriched_fact("Fact two.", context="ctx2")
        enrichment.enrich_context.return_value = _make_enriched_context([fact1, fact2])

        msg = _make_message({
            "keywords": ["health"],
            "primary_query": "user health",
            "alternative_query": "",
        })
        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert "---" in response.result
        assert "Fact one." in response.result
        assert "Fact two." in response.result
        assert "context: ctx1" in response.result

    async def test_empty_results_returns_empty_string(self):
        agent, _, _, enrichment = _make_agent()
        enrichment.enrich_context.return_value = _make_enriched_context([])

        msg = _make_message({
            "keywords": ["health"],
            "primary_query": "user health",
            "alternative_query": "",
        })
        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert response.result == ""

    async def test_enrichment_exception_returns_failure(self):
        agent, _, _, enrichment = _make_agent()
        enrichment.enrich_context.side_effect = RuntimeError("Firestore unavailable")

        msg = _make_message({
            "keywords": ["health"],
            "primary_query": "user health",
            "alternative_query": "",
        })
        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILED
        assert "Firestore unavailable" in response.error

    async def test_metadata_in_agent_response(self):
        agent, _, _, enrichment = _make_agent()
        enrichment.enrich_context.return_value = _make_enriched_context(
            [_make_enriched_fact("Fact.")]
        )

        msg = _make_message({
            "keywords": ["x"],
            "primary_query": "query",
            "alternative_query": "",
        })
        response = await agent.execute(msg)

        assert response.metadata["search_strategy"] == "multi_vector_rrf"
        assert response.metadata["result_count"] == 1


class TestExecuteLegacyPath:

    async def test_returns_formatted_string(self):
        agent, repo, embedding, _ = _make_agent(with_enrichment=False)
        embedding.get_embedding.return_value = [0.1] * 768
        repo.search_facts.return_value = [
            _FakeFact(text="Legacy fact.", type=FactType.STATE, context="legacy ctx"),
        ]

        msg = _make_message({"query": "some query"})
        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert "Legacy fact." in response.result
        assert "context: legacy ctx" in response.result

    async def test_principle_facts_filtered(self):
        agent, repo, embedding, _ = _make_agent(with_enrichment=False)
        embedding.get_embedding.return_value = [0.1] * 768
        repo.search_facts.return_value = [
            _FakeFact(text="Normal fact.", type=FactType.STATE),
            _FakeFact(text="Should be filtered.", type=FactType.PRINCIPLE),
        ]

        msg = _make_message({"query": "some query"})
        response = await agent.execute(msg)

        assert "Normal fact." in response.result
        assert "Should be filtered." not in response.result


class TestExecuteNoKeys:

    async def test_empty_payload_returns_failure(self):
        agent, *_ = _make_agent()
        msg = _make_message({"query": ""})
        response = await agent.execute(msg)
        assert response.status == AgentStatus.FAILED
        assert "No search keys" in response.error


class TestSaveToMemory:
    """Tests for the save_to_memory intent path."""

    # --- can_handle routing ---

    async def test_can_handle_accepts_payload_text(self):
        """payload['text'] present (params-spread from context={"text":"..."}) → accepted."""
        agent, *_ = _make_agent()
        msg = _make_message({"text": "User weighs 80 kg. Mentioned in diet discussion."})
        assert await agent.can_handle(msg) is True

    async def test_can_handle_accepts_intent_flag(self):
        """payload['intent']=='save_to_memory' without text → accepted (fallback path)."""
        agent, *_ = _make_agent()
        msg = _make_message({"intent": "save_to_memory", "query": "Save user weight fact"})
        assert await agent.can_handle(msg) is True

    # --- execute routing and result ---

    async def test_execute_routes_to_save_when_text_present(self):
        """execute() routes to _handle_save() when payload['text'] is set."""
        agent, *_ = _make_agent()
        passage = "User weighs 80 kg. Mentioned in diet discussion."
        msg = _make_message({"text": passage})
        response = await agent.execute(msg)
        assert response.status == AgentStatus.SUCCESS
        assert response.result == {"saved": True}

    async def test_execute_uses_payload_text_as_consolidation_text(self):
        """history_context contains the full passage from payload['text']."""
        agent, *_ = _make_agent()
        passage = "User weighs 80 kg. Mentioned in diet discussion."
        msg = _make_message({"text": passage})
        response = await agent.execute(msg)
        assert response.history_context == {"consolidation_text": passage}

    async def test_execute_fallback_to_query_when_no_text(self):
        """When text absent but intent flag set, query is used as consolidation_text."""
        agent, *_ = _make_agent()
        task_desc = "Save user weight fact"
        msg = _make_message({"intent": "save_to_memory", "query": task_desc})
        response = await agent.execute(msg)
        assert response.status == AgentStatus.SUCCESS
        assert response.history_context == {"consolidation_text": task_desc}

    async def test_execute_empty_text_returns_failure(self):
        """Both text and query absent → failure, no exception."""
        agent, *_ = _make_agent()
        msg = _make_message({"intent": "save_to_memory", "query": ""})
        response = await agent.execute(msg)
        assert response.status == AgentStatus.FAILED
        assert "empty text" in response.error
