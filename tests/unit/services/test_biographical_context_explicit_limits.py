"""BiographicalContextService.refresh_context with explicit limits (facts-fix PR): an explicit limit wins over the
service's own resolution; None keeps it. Asserted on what the refreshed context holds, against a repository fake that
honours `domain` and `limit`."""

from unittest.mock import AsyncMock, MagicMock

from src.services.biographical_context_service import BiographicalContextService
from tests.unit.services.test_biographical_context_service import make_fact


class _LimitHonouringRepo:
    """get_active_facts_ordered over a fixed list: filters by domain when given and returns at most `limit`."""

    def __init__(self, facts):
        self._facts = list(facts)

    async def get_active_facts_ordered(self, account_id, domain=None, limit=100):
        return [f for f in self._facts if domain is None or f.domain.value == domain][:limit]


def _plain(n):
    return [make_fact(f"fact {i}") for i in range(n)]


def _mindset(n):
    return [make_fact(f"principle {i}", tags=["mindset"]) for i in range(n)]


def _config(facts_limit, principles_limit):
    config_service = MagicMock()
    config_service.get_biographical_cache_limit.return_value = facts_limit
    config_service.get_principles_cache_limit.return_value = principles_limit
    return config_service


class TestExplicitLimits:
    """refresh_context(facts_limit=, principles_limit=): explicit limits win over its own resolution."""

    async def test_explicit_facts_limit_caps_the_facts_held(self):
        service = BiographicalContextService(_LimitHonouringRepo(_plain(10)), config_service=None, account_repo=None)

        result = await service.refresh_context("acc-123", facts_limit=7)

        assert [f["text"] for f in result["facts"]] == [f"fact {i}" for i in range(7)]

    async def test_explicit_facts_limit_wins_over_the_config_service(self):
        service = BiographicalContextService(
            _LimitHonouringRepo(_plain(10)), config_service=_config(6, 9), account_repo=None)

        result = await service.refresh_context("acc-123", facts_limit=3)

        assert [f["text"] for f in result["facts"]] == ["fact 0", "fact 1", "fact 2"]

    async def test_explicit_facts_limit_above_the_resolved_one_also_wins(self):
        service = BiographicalContextService(
            _LimitHonouringRepo(_plain(10)), config_service=_config(4, 9), account_repo=None)

        result = await service.refresh_context("acc-123", facts_limit=9)

        assert len(result["facts"]) == 9

    async def test_explicit_principles_limit_wins_over_the_config_service(self):
        service = BiographicalContextService(
            _LimitHonouringRepo(_mindset(5)), config_service=_config(50, 9), account_repo=None)

        result = await service.refresh_context("acc-123", facts_limit=20, principles_limit=2)

        assert len(result["principles"]) == 2

    async def test_none_keeps_the_config_service_resolution(self):
        service = BiographicalContextService(
            _LimitHonouringRepo(_mindset(5) + _plain(10)), config_service=_config(6, 3), account_repo=None)

        result = await service.refresh_context("acc-123", facts_limit=None, principles_limit=None)

        # 6 selected (the resolved facts limit): 5 principles, of which 3 are kept, and 1 plain fact
        assert len(result["principles"]) == 3
        assert [f["text"] for f in result["facts"]] == ["fact 0"]

    async def test_none_principles_limit_keeps_the_resolved_one(self):
        service = BiographicalContextService(
            _LimitHonouringRepo(_mindset(5)), config_service=_config(50, 3), account_repo=None)

        result = await service.refresh_context("acc-123", facts_limit=None, principles_limit=None)

        assert len(result["principles"]) == 3

    async def test_one_explicit_limit_leaves_the_other_resolved(self):
        service = BiographicalContextService(
            _LimitHonouringRepo(_mindset(5) + _plain(10)), config_service=_config(8, 3), account_repo=None)

        result = await service.refresh_context("acc-123", principles_limit=1)

        # 8 selected (the resolved facts limit): 5 principles, of which 1 is kept, and 3 plain facts
        assert len(result["principles"]) == 1
        assert [f["text"] for f in result["facts"]] == ["fact 0", "fact 1", "fact 2"]


class TestAccountLoadFailure:
    async def test_a_failed_account_load_falls_back_to_the_default_limits_and_still_refreshes(self):
        account_repo = AsyncMock()
        account_repo.get_account.side_effect = RuntimeError("account store down")
        service = BiographicalContextService(
            _LimitHonouringRepo(_plain(100)), config_service=None, account_repo=account_repo)

        result = await service.refresh_context("acc-123")

        assert len(result["facts"]) == 65
