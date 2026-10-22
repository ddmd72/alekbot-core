"""BiographicalContextService.refresh_context with explicit limits (facts-fix PR): an explicit limit wins over the
service's own resolution; None keeps it."""

import pytest
from unittest.mock import AsyncMock, MagicMock, call

from src.services.biographical_context_service import BiographicalContextService
from tests.unit.services.test_biographical_context_service import make_fact


class TestExplicitLimits:
    """refresh_context(facts_limit=, principles_limit=): explicit limits win over its own resolution."""

    @pytest.fixture
    def mock_repo(self):
        return AsyncMock()

    async def test_explicit_facts_limit_is_the_biographical_query_limit(self, mock_repo):
        service = BiographicalContextService(repository=mock_repo, config_service=None, account_repo=None)
        mock_repo.get_active_facts_ordered.return_value = []

        await service.refresh_context("acc-123", facts_limit=7)

        assert mock_repo.get_active_facts_ordered.call_args_list[0] == call("acc-123", domain="biographical", limit=7)

    async def test_explicit_limits_win_over_the_config_service(self, mock_repo):
        config_service = MagicMock()
        config_service.get_biographical_cache_limit.return_value = 50
        config_service.get_principles_cache_limit.return_value = 9
        service = BiographicalContextService(repository=mock_repo, config_service=config_service, account_repo=None)
        mock_repo.get_active_facts_ordered.return_value = [
            make_fact(f"p{i}", tags=["mindset"]) for i in range(5)
        ]

        result = await service.refresh_context("acc-123", facts_limit=20, principles_limit=2)

        assert mock_repo.get_active_facts_ordered.call_args_list[0] == call("acc-123", domain="biographical", limit=20)
        assert len(result["principles"]) == 2

    async def test_none_keeps_the_config_service_resolution(self, mock_repo):
        config_service = MagicMock()
        config_service.get_biographical_cache_limit.return_value = 50
        config_service.get_principles_cache_limit.return_value = 3
        service = BiographicalContextService(repository=mock_repo, config_service=config_service, account_repo=None)
        mock_repo.get_active_facts_ordered.return_value = [
            make_fact(f"p{i}", tags=["mindset"]) for i in range(5)
        ]

        result = await service.refresh_context("acc-123", facts_limit=None, principles_limit=None)

        assert mock_repo.get_active_facts_ordered.call_args_list[0] == call("acc-123", domain="biographical", limit=50)
        assert len(result["principles"]) == 3

    async def test_one_explicit_limit_leaves_the_other_resolved(self, mock_repo):
        config_service = MagicMock()
        config_service.get_biographical_cache_limit.return_value = 50
        config_service.get_principles_cache_limit.return_value = 3
        service = BiographicalContextService(repository=mock_repo, config_service=config_service, account_repo=None)
        mock_repo.get_active_facts_ordered.return_value = [
            make_fact(f"p{i}", tags=["mindset"]) for i in range(5)
        ]

        result = await service.refresh_context("acc-123", principles_limit=1)

        assert mock_repo.get_active_facts_ordered.call_args_list[0] == call("acc-123", domain="biographical", limit=50)
        assert len(result["principles"]) == 1
