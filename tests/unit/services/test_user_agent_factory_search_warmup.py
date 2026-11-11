"""Building a user's agents starts the account's fact-search snapshot load in the background: the build does not wait
for it (it runs inside a request), and a failed load is logged."""
import asyncio
import logging
from unittest.mock import AsyncMock, Mock

from src.composition.user_agent_factory import UserAgentFactory
from src.domain.settings import ConsolidationSettings
from src.domain.user import UserBotConfig, UserProfile

from tests.unit.services.test_user_agent_factory import _make_context, mock_dependencies  # noqa: F401


def _wire(deps, fact_search):
    deps["fact_search"] = fact_search
    deps["config"] = {**deps["config"], "CONSOLIDATION": ConsolidationSettings()}
    deps["user_repo"] = AsyncMock()
    deps["user_repo"].get_user = AsyncMock(
        return_value=UserProfile(user_id="user123", account_id="acc1", config=UserBotConfig()))
    deps["account_repo"] = AsyncMock()
    deps["account_repo"].get_account = AsyncMock(return_value=None)
    deps["repository"] = AsyncMock()
    deps["repository"].get_active_facts = AsyncMock(return_value=[])
    deps["context_builder"] = Mock()
    deps["context_builder"].build = Mock(
        side_effect=lambda agent_type, cfg: _make_context(agent_type, "gemini-2.0-flash"))
    cfg = Mock()
    for name in ("get_semantic_search_limit", "get_biographical_cache_limit",
                 "get_principles_cache_limit", "get_history_recent_full_turns"):
        setattr(cfg, name, Mock(return_value=10))
    deps["config_service"] = cfg
    return UserAgentFactory(**deps)


async def test_building_the_agents_starts_the_warm_up_without_waiting_for_it(mock_dependencies):  # noqa: F811
    started, release = asyncio.Event(), asyncio.Event()

    async def warm(account_id):
        started.set()
        await release.wait()

    fact_search = Mock()
    fact_search.warm = AsyncMock(side_effect=warm)
    factory = _wire(mock_dependencies, fact_search)

    cached = await asyncio.wait_for(factory.ensure_agents_for_user("user123"), timeout=10)  # not held by the load
    await asyncio.wait_for(started.wait(), timeout=5)

    assert cached is factory._cache["user123"]
    fact_search.warm.assert_awaited_once_with("acc1")
    release.set()
    await asyncio.gather(*factory._warmups)


async def test_a_failed_warm_up_is_logged_and_the_build_still_succeeds(mock_dependencies, caplog):  # noqa: F811
    fact_search = Mock()
    fact_search.warm = AsyncMock(side_effect=RuntimeError("snapshot unreadable"))
    factory = _wire(mock_dependencies, fact_search)

    with caplog.at_level(logging.ERROR):
        cached = await factory.ensure_agents_for_user("user123")
        await asyncio.gather(*factory._warmups, return_exceptions=True)
        await asyncio.sleep(0)

    assert cached["memory_agent"] is not None
    assert "search snapshot warm-up failed" in caplog.text and "snapshot unreadable" in caplog.text
