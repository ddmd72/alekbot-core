"""refresh_context for an account without account_defaults resolves the system limits and builds the cache.

Real ConfigurationService (not a mock): the former fallback handed it a SearchConfig, which has no
biographical_cache_limit, so the refresh raised AttributeError (swallowed by the caller) and the baseline never refreshed."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.settings import SearchConfig
from src.services.biographical_context_service import BiographicalContextService
from src.services.configuration_service import ConfigurationService
from tests.unit.services.test_biographical_context_explicit_limits import _LimitHonouringRepo, _plain
from tests.unit.services.test_biographical_context_service import make_fact


def _account_repo(account):
    repo = MagicMock()
    repo.get_account = AsyncMock(return_value=account)
    return repo


@pytest.mark.parametrize("account", [MagicMock(account_defaults=None), None], ids=["no-defaults", "no-account"])
async def test_refresh_without_account_defaults_builds_the_cache_at_system_limits(account):
    n = SearchConfig().DEFAULT_BIOGRAPHICAL_CACHE_LIMIT + 5
    service = BiographicalContextService(
        _LimitHonouringRepo(_plain(n)), config_service=ConfigurationService(), account_repo=_account_repo(account))

    result = await service.refresh_context("acc-123")

    assert len(result["facts"]) == SearchConfig().DEFAULT_BIOGRAPHICAL_CACHE_LIMIT


async def test_system_principles_limit_applies_without_account_defaults():
    limit = SearchConfig().DEFAULT_PRINCIPLES_CACHE_LIMIT
    facts = [make_fact(f"principle {i}", tags=["mindset"]) for i in range(limit + 4)]
    service = BiographicalContextService(
        _LimitHonouringRepo(facts), config_service=ConfigurationService(),
        account_repo=_account_repo(MagicMock(account_defaults=None)))

    result = await service.refresh_context("acc-123")

    assert len(result["principles"]) == limit
