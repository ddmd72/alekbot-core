"""
Unit tests for BiographicalContextService

Session: 2026-02-16 Deliberate Fact Management - Priority-Based Refresh
Tests priority-based selection with sorting and principles logic.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from src.services.biographical_context_service import BiographicalContextService
from src.domain.entities import FactEntity, FactType, FactState, ContextPriority


class TestBiographicalContextService:
    """Test suite for priority-based biographical context refresh."""
    
    @pytest.fixture
    def mock_repository(self):
        """Create mock fact repository."""
        repo = AsyncMock()
        return repo
    
    @pytest.fixture
    def service(self, mock_repository):
        """Create biographical context service with mocked dependencies."""
        return BiographicalContextService(
            repository=mock_repository,
            config_service=None,  # Will use defaults
            account_repo=None
        )
    
    # ========================================================================
    # PRIORITY-BASED SELECTION TESTS
    # ========================================================================
    
    @pytest.mark.asyncio
    async def test_critical_facts_always_included_over_limit(
        self,
        service,
        mock_repository
    ):
        """CRITICAL facts always included even if exceeding limit."""
        # Create 5 CRITICAL facts + 5 HIGH facts
        facts = []
        for i in range(5):
            facts.append(FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-21",
                text=f"Critical fact {i}",
                type=FactType.STATE,
                tags=["critical"],
                context_priority=ContextPriority.CRITICAL,
                created_at=datetime(2025, 1, i+1, tzinfo=timezone.utc),
                state=FactState.CURRENT
            ))
        
        for i in range(5):
            facts.append(FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-22",
                text=f"High fact {i}",
                type=FactType.STATE,
                tags=["high"],
                context_priority=ContextPriority.HIGH,
                created_at=datetime(2025, 1, i+1, tzinfo=timezone.utc),
                state=FactState.CURRENT
            ))
        
        mock_repository.get_active_facts.return_value = facts
        
        # Set limit to 3 (but 5 CRITICAL should still be included)
        service._repo = mock_repository
        
        result = await service.refresh_context("account123")
        
        # All 5 CRITICAL + some HIGH (up to system default limit)
        returned_facts = result["facts"]
        critical_count = len([f for f in returned_facts if f["context_priority"] == "critical"])
        
        assert critical_count == 5, "All CRITICAL facts should be included"
        assert len(returned_facts) >= 5, "Should include at least CRITICAL facts"
    
    @pytest.mark.asyncio
    async def test_priority_ordering_critical_high_medium_low(
        self,
        service,
        mock_repository
    ):
        """Facts returned in priority order: CRITICAL → HIGH → MEDIUM → LOW."""
        facts = [
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-1",
                text="Low fact",
                type=FactType.STATE,
                tags=["low"],
                context_priority=ContextPriority.LOW,
                created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                state=FactState.CURRENT
            ),
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-2",
                text="Critical fact",
                type=FactType.STATE,
                tags=["critical"],
                context_priority=ContextPriority.CRITICAL,
                created_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
                state=FactState.CURRENT
            ),
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-3",
                text="Medium fact",
                type=FactType.STATE,
                tags=["medium"],
                context_priority=ContextPriority.MEDIUM,
                created_at=datetime(2025, 1, 3, tzinfo=timezone.utc),
                state=FactState.CURRENT
            ),
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-4",
                text="High fact",
                type=FactType.STATE,
                tags=["high"],
                context_priority=ContextPriority.HIGH,
                created_at=datetime(2025, 1, 4, tzinfo=timezone.utc),
                state=FactState.CURRENT
            )
        ]
        
        mock_repository.get_active_facts.return_value = facts
        
        result = await service.refresh_context("account123")
        returned_facts = result["facts"]
        
        # Check priority order
        priorities = [f["context_priority"] for f in returned_facts]
        
        assert priorities[0] == "critical", "CRITICAL should be first"
        assert priorities[1] == "high", "HIGH should be second"
        assert priorities[2] == "medium", "MEDIUM should be third"
        assert priorities[3] == "low", "LOW should be last"
    
    # ========================================================================
    # SORTING WITHIN PRIORITY GROUPS
    # ========================================================================
    
    @pytest.mark.asyncio
    async def test_sorting_within_priority_newest_first(
        self,
        service,
        mock_repository
    ):
        """Within each priority group, facts sorted by created_at DESC (newest first)."""
        facts = [
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-5",
                text="High fact old",
                type=FactType.STATE,
                tags=["high"],
                context_priority=ContextPriority.HIGH,
                created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                state=FactState.CURRENT
            ),
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-6",
                text="High fact middle",
                type=FactType.STATE,
                tags=["high"],
                context_priority=ContextPriority.HIGH,
                created_at=datetime(2025, 1, 15, tzinfo=timezone.utc),
                state=FactState.CURRENT
            ),
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-7",
                text="High fact newest",
                type=FactType.STATE,
                tags=["high"],
                context_priority=ContextPriority.HIGH,
                created_at=datetime(2025, 1, 30, tzinfo=timezone.utc),
                state=FactState.CURRENT
            )
        ]
        
        mock_repository.get_active_facts.return_value = facts
        
        result = await service.refresh_context("account123")
        returned_facts = result["facts"]
        
        # All should be HIGH priority, sorted by date DESC
        assert returned_facts[0]["text"] == "High fact newest"
        assert returned_facts[1]["text"] == "High fact middle"
        assert returned_facts[2]["text"] == "High fact old"
    
    # ========================================================================
    # PRINCIPLES PRIORITY LOGIC
    # ========================================================================
    
    @pytest.mark.asyncio
    async def test_principles_apply_same_priority_logic(
        self,
        service,
        mock_repository
    ):
        """Principles should use same priority logic as facts."""
        facts = [
            # 3 CRITICAL principles
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-8",
                text="Critical principle 1",
                type=FactType.PRINCIPLE,
                tags=["mindset"],
                context_priority=ContextPriority.CRITICAL,
                created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                state=FactState.CURRENT
            ),
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-9",
                text="Critical principle 2",
                type=FactType.PRINCIPLE,
                tags=["mindset"],
                context_priority=ContextPriority.CRITICAL,
                created_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
                state=FactState.CURRENT
            ),
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-10",
                text="Critical principle 3",
                type=FactType.PRINCIPLE,
                tags=["mindset"],
                context_priority=ContextPriority.CRITICAL,
                created_at=datetime(2025, 1, 3, tzinfo=timezone.utc),
                state=FactState.CURRENT
            ),
            # 5 LOW principles
            *[
                FactEntity(
                    account_id="account-1",
                    created_by_user_id="user-1",
                    lineage_id="lineage-11",
                    text=f"Low principle {i}",
                    type=FactType.PRINCIPLE,
                    tags=["mindset"],
                    context_priority=ContextPriority.LOW,
                    created_at=datetime(2025, 1, i+10, tzinfo=timezone.utc),
                    state=FactState.CURRENT
                )
                for i in range(5)
            ]
        ]
        
        mock_repository.get_active_facts.return_value = facts
        
        result = await service.refresh_context("account123")
        principles = result["principles"]
        
        # All 3 CRITICAL principles should be included
        critical_count = len([p for p in principles if p["context_priority"] == "critical"])
        assert critical_count == 3, "All CRITICAL principles should be included"
        
        # Should include CRITICAL + some LOW (up to default limit)
        assert len(principles) >= 3
    
    @pytest.mark.asyncio
    async def test_critical_principles_over_limit(
        self,
        service,
        mock_repository
    ):
        """CRITICAL principles included even if exceeding principles_limit."""
        # Create 10 CRITICAL principles (exceeds typical limit of 3)
        facts = [
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-12",
                text=f"Critical principle {i}",
                type=FactType.PRINCIPLE,
                tags=["mindset"],
                context_priority=ContextPriority.CRITICAL,
                created_at=datetime(2025, 1, i+1, tzinfo=timezone.utc),
                state=FactState.CURRENT
            )
            for i in range(10)
        ]
        
        mock_repository.get_active_facts.return_value = facts
        
        result = await service.refresh_context("account123")
        principles = result["principles"]
        
        # All 10 CRITICAL principles should be included (over limit)
        assert len(principles) == 10, "All CRITICAL principles should be included even over limit"
    
    # ========================================================================
    # MIXED FACTS AND PRINCIPLES
    # ========================================================================
    
    @pytest.mark.asyncio
    async def test_separate_facts_and_principles(
        self,
        service,
        mock_repository
    ):
        """Facts and principles separated correctly."""
        facts = [
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-13",
                text="Biographical fact",
                type=FactType.STATE,
                tags=["bio"],
                context_priority=ContextPriority.HIGH,
                created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                state=FactState.CURRENT
            ),
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-14",
                text="Principle fact",
                type=FactType.PRINCIPLE,
                tags=["mindset"],
                context_priority=ContextPriority.HIGH,
                created_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
                state=FactState.CURRENT
            ),
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-15",
                text="Medical fact",
                type=FactType.STATE,
                tags=["medical"],
                context_priority=ContextPriority.MEDIUM,
                created_at=datetime(2025, 1, 3, tzinfo=timezone.utc),
                state=FactState.CURRENT
            )
        ]
        
        mock_repository.get_active_facts.return_value = facts
        
        result = await service.refresh_context("account123")
        
        assert len(result["facts"]) == 2, "Should have 2 non-principle facts"
        assert len(result["principles"]) == 1, "Should have 1 principle"
        
        assert result["principles"][0]["text"] == "Principle fact"
        assert result["facts"][0]["text"] == "Biographical fact"
        assert result["facts"][1]["text"] == "Medical fact"
    
    # ========================================================================
    # DEFAULT PRIORITY HANDLING
    # ========================================================================
    
    @pytest.mark.asyncio
    async def test_none_priority_defaults_to_medium(
        self,
        service,
        mock_repository
    ):
        """Facts with None priority default to MEDIUM."""
        facts = [
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-16",
                text="Fact without priority",
                type=FactType.STATE,
                tags=["test"],
                created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                state=FactState.CURRENT
            ),
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-17",
                text="High priority fact",
                type=FactType.STATE,
                tags=["test"],
                context_priority=ContextPriority.HIGH,
                created_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
                state=FactState.CURRENT
            )
        ]
        
        mock_repository.get_active_facts.return_value = facts
        
        result = await service.refresh_context("account123")
        returned_facts = result["facts"]
        
        # HIGH should come first, then MEDIUM (None)
        assert returned_facts[0]["text"] == "High priority fact"
        assert returned_facts[1]["text"] == "Fact without priority"
        assert returned_facts[1]["context_priority"] == "medium"
    
    # ========================================================================
    # ARCHIVAL PRIORITY EXCLUSION
    # ========================================================================
    
    @pytest.mark.asyncio
    async def test_archival_priority_excluded(
        self,
        service,
        mock_repository
    ):
        """Facts with ARCHIVAL priority are excluded from cache."""
        facts = [
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-18",
                text="Active fact",
                type=FactType.STATE,
                tags=["active"],
                context_priority=ContextPriority.HIGH,
                created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                state=FactState.CURRENT
            ),
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-19",
                text="Archived fact",
                type=FactType.STATE,
                tags=["archived"],
                context_priority=ContextPriority.ARCHIVAL,
                created_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
                state=FactState.CURRENT
            )
        ]
        
        mock_repository.get_active_facts.return_value = facts
        
        result = await service.refresh_context("account123")
        returned_facts = result["facts"]
        
        assert len(returned_facts) == 1, "ARCHIVAL facts should be excluded"
        assert returned_facts[0]["text"] == "Active fact"
    
    # ========================================================================
    # EMPTY CASES
    # ========================================================================
    
    @pytest.mark.asyncio
    async def test_no_facts_returns_empty_lists(
        self,
        service,
        mock_repository
    ):
        """No facts returns empty facts and principles lists."""
        mock_repository.get_active_facts.return_value = []
        
        result = await service.refresh_context("account123")
        
        assert result["facts"] == []
        assert result["principles"] == []
    
    @pytest.mark.asyncio
    async def test_only_principles_no_facts(
        self,
        service,
        mock_repository
    ):
        """Only principles, no biographical facts."""
        facts = [
            FactEntity(
                account_id="account-1",
                created_by_user_id="user-1",
                lineage_id="lineage-20",
                text="Principle 1",
                type=FactType.PRINCIPLE,
                tags=["mindset"],
                context_priority=ContextPriority.HIGH,
                created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                state=FactState.CURRENT
            )
        ]
        
        mock_repository.get_active_facts.return_value = facts
        
        result = await service.refresh_context("account123")
        
        assert len(result["facts"]) == 0
        assert len(result["principles"]) == 1
