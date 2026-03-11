"""
Unit tests for BiographicalContextService.

Refactored (2026-02-24): Service now calls get_active_facts_ordered (bounded, sorted by
context_priority_rank in Firestore). Python-side sorting and CRITICAL-over-limit logic removed.
Tests focus on: domain-first fill, facts/principles separation, principles limit.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, call

from src.services.biographical_context_service import BiographicalContextService
from src.domain.entities import FactEntity, FactType, FactState, ContextPriority, FactDomain


def make_fact(
    text: str,
    priority: ContextPriority = ContextPriority.MEDIUM,
    tags: list[str] | None = None,
    domain: FactDomain = FactDomain.BIOGRAPHICAL,
    created_at: datetime | None = None,
) -> FactEntity:
    return FactEntity(
        account_id="account-1",
        created_by_user_id="user-1",
        lineage_id=f"lineage-{text[:8]}",
        text=text,
        type=FactType.STATE,
        domain=domain,
        tags=tags or [],
        context_priority=priority,
        created_at=created_at or datetime(2025, 1, 1, tzinfo=timezone.utc),
        state=FactState.CURRENT,
    )


class TestBiographicalContextService:

    @pytest.fixture
    def mock_repo(self):
        return AsyncMock()

    @pytest.fixture
    def service(self, mock_repo):
        return BiographicalContextService(repository=mock_repo, config_service=None, account_repo=None)

    # ========================================================================
    # DOMAIN-FIRST SELECTION
    # ========================================================================

    async def test_biographical_domain_fetched_first(self, service, mock_repo):
        """Service calls get_active_facts_ordered with domain=biographical first."""
        biog_facts = [make_fact("biog fact")]
        mock_repo.get_active_facts_ordered.return_value = biog_facts

        await service.refresh_context("acc-123")

        first_call = mock_repo.get_active_facts_ordered.call_args_list[0]
        assert first_call == call("acc-123", domain="biographical", limit=65)

    async def test_no_fill_when_biographical_reaches_limit(self, service, mock_repo):
        """Second query not issued when biographical facts fill the limit."""
        biog_facts = [make_fact(f"fact {i}") for i in range(65)]  # exactly at default limit
        mock_repo.get_active_facts_ordered.return_value = biog_facts

        await service.refresh_context("acc-123")

        assert mock_repo.get_active_facts_ordered.call_count == 1

    async def test_fill_from_all_domains_when_biographical_insufficient(self, service, mock_repo):
        """When biographical < limit, second query fetches all domains for fill."""
        biog_fact = make_fact("biog fact")
        health_fact = make_fact("health fact", domain=FactDomain.HEALTH)
        biog_facts = [biog_fact]
        all_ordered = [biog_fact, health_fact]  # same object (same ID) + extra

        mock_repo.get_active_facts_ordered.side_effect = [biog_facts, all_ordered]

        result = await service.refresh_context("acc-123")

        assert mock_repo.get_active_facts_ordered.call_count == 2
        second_call = mock_repo.get_active_facts_ordered.call_args_list[1]
        assert second_call == call("acc-123", limit=65)
        # biog_fact + health_fact (deduped biog_fact from all_ordered)
        assert len(result["facts"]) == 2

    async def test_fill_deduplicates_already_fetched_biographical_facts(self, service, mock_repo):
        """Facts from Q1 are not duplicated when Q2 returns them again."""
        biog_fact = make_fact("shared fact")
        extra_fact = make_fact("extra", domain=FactDomain.HEALTH)

        mock_repo.get_active_facts_ordered.side_effect = [
            [biog_fact],
            [biog_fact, extra_fact],  # Q2 includes the same biog fact
        ]

        result = await service.refresh_context("acc-123")
        texts = [f["text"] for f in result["facts"]]
        assert texts.count("shared fact") == 1, "No duplicates after fill"

    # ========================================================================
    # FACTS VS PRINCIPLES SEPARATION
    # ========================================================================

    async def test_mindset_tagged_fact_goes_to_principles(self, service, mock_repo):
        """Facts with 'mindset' tag are separated into principles."""
        facts = [
            make_fact("Regular fact", tags=[]),
            make_fact("A principle", tags=["mindset"]),
        ]
        mock_repo.get_active_facts_ordered.return_value = facts

        result = await service.refresh_context("acc-123")

        assert len(result["facts"]) == 1
        assert result["facts"][0]["text"] == "Regular fact"
        assert len(result["principles"]) == 1
        assert result["principles"][0]["text"] == "A principle"

    async def test_principles_limit_applied(self, service, mock_repo):
        """Principles capped at principles_limit (default 20)."""
        principles = [make_fact(f"principle {i}", tags=["mindset"]) for i in range(25)]
        mock_repo.get_active_facts_ordered.return_value = principles

        result = await service.refresh_context("acc-123")

        assert len(result["principles"]) == 20

    async def test_principles_preserve_db_order(self, service, mock_repo):
        """Principles are returned in the order provided by the repo (already priority-sorted)."""
        principles = [
            make_fact("high principle", priority=ContextPriority.HIGH, tags=["mindset"]),
            make_fact("low principle", priority=ContextPriority.LOW, tags=["mindset"]),
        ]
        mock_repo.get_active_facts_ordered.return_value = principles

        result = await service.refresh_context("acc-123")

        assert result["principles"][0]["text"] == "high principle"
        assert result["principles"][1]["text"] == "low principle"

    # ========================================================================
    # DICT FORMAT
    # ========================================================================

    async def test_fact_dict_contains_required_fields(self, service, mock_repo):
        """Returned fact dicts have expected keys."""
        mock_repo.get_active_facts_ordered.return_value = [make_fact("test")]

        result = await service.refresh_context("acc-123")
        fact = result["facts"][0]

        assert "id" in fact
        assert "text" in fact
        assert "domain" in fact
        assert "tags" in fact
        assert "context_priority" in fact
        assert "created_at" in fact
        assert "_priority_obj" not in fact  # temp field must not leak

    async def test_domain_none_serialized_as_unknown(self, service, mock_repo):
        """Facts with domain=None serialize domain as 'unknown'."""
        fact = FactEntity(
            account_id="account-1",
            created_by_user_id="user-1",
            lineage_id="lineage-x",
            text="no domain fact",
            type=FactType.STATE,
            tags=[],
            state=FactState.CURRENT,
        )
        mock_repo.get_active_facts_ordered.return_value = [fact]

        result = await service.refresh_context("acc-123")
        assert result["facts"][0]["domain"] == "unknown"

    # ========================================================================
    # EMPTY CASES
    # ========================================================================

    async def test_empty_result_when_no_facts(self, service, mock_repo):
        """Empty repo returns empty facts and principles."""
        mock_repo.get_active_facts_ordered.return_value = []

        result = await service.refresh_context("acc-123")

        assert result["facts"] == []
        assert result["principles"] == []

    async def test_only_principles_returns_empty_facts_list(self, service, mock_repo):
        """All mindset-tagged facts → facts list is empty."""
        mock_repo.get_active_facts_ordered.return_value = [
            make_fact("principle", tags=["mindset"])
        ]

        result = await service.refresh_context("acc-123")

        assert result["facts"] == []
        assert len(result["principles"]) == 1
