"""get_memory_search on a real factory: it returns the FactsMemoryAgent built for the user (hotfix: it read a
cache attribute that does not exist)."""
from unittest.mock import AsyncMock, Mock

from src.composition.user_agent_factory import UserAgentFactory
from src.domain.settings import ConsolidationSettings
from src.domain.user import UserBotConfig, UserProfile

from tests.unit.services.test_user_agent_factory import _make_context, mock_dependencies  # noqa: F401


async def test_get_memory_search_returns_the_users_facts_memory_agent(mock_dependencies):  # noqa: F811
    from src.agents.memory_search_agent import FactsMemoryAgent
    profile = UserProfile(user_id="user123", account_id="acc1", config=UserBotConfig())
    mock_dependencies["config"] = {**mock_dependencies["config"], "CONSOLIDATION": ConsolidationSettings()}
    mock_dependencies["user_repo"] = AsyncMock()
    mock_dependencies["user_repo"].get_user = AsyncMock(return_value=profile)
    mock_dependencies["account_repo"] = AsyncMock()
    mock_dependencies["account_repo"].get_account = AsyncMock(return_value=None)
    mock_dependencies["repository"] = AsyncMock()
    mock_dependencies["repository"].get_active_facts = AsyncMock(return_value=[])
    mock_dependencies["context_builder"] = Mock()
    mock_dependencies["context_builder"].build = Mock(
        side_effect=lambda agent_type, cfg: _make_context(agent_type, "gemini-2.0-flash"))
    cfg = Mock()
    for name in ("get_semantic_search_limit", "get_biographical_cache_limit",
                 "get_principles_cache_limit", "get_history_recent_full_turns"):
        setattr(cfg, name, Mock(return_value=10))
    mock_dependencies["config_service"] = cfg

    factory = UserAgentFactory(**mock_dependencies)
    agent = await factory.get_memory_search("user123")

    assert isinstance(agent, FactsMemoryAgent)
    assert agent is factory._cache["user123"]["memory_agent"]


async def test_router_gets_the_resolved_semantic_limit(mock_dependencies):  # noqa: F811
    profile = UserProfile(user_id="user123", account_id="acc1", config=UserBotConfig(semantic_search_limit=77))
    mock_dependencies["config"] = {**mock_dependencies["config"], "CONSOLIDATION": ConsolidationSettings()}
    mock_dependencies["user_repo"] = AsyncMock()
    mock_dependencies["user_repo"].get_user = AsyncMock(return_value=profile)
    mock_dependencies["account_repo"] = AsyncMock()
    mock_dependencies["account_repo"].get_account = AsyncMock(return_value=None)
    mock_dependencies["repository"] = AsyncMock()
    mock_dependencies["repository"].get_active_facts = AsyncMock(return_value=[])
    mock_dependencies["context_builder"] = Mock()
    mock_dependencies["context_builder"].build = Mock(
        side_effect=lambda agent_type, cfg: _make_context(agent_type, "gemini-2.0-flash"))
    cfg = Mock()
    for name in ("get_biographical_cache_limit", "get_principles_cache_limit", "get_history_recent_full_turns"):
        setattr(cfg, name, Mock(return_value=10))
    cfg.get_semantic_search_limit = Mock(return_value=77)
    mock_dependencies["config_service"] = cfg

    cached = await UserAgentFactory(**mock_dependencies)._create_and_cache_agents("user123")

    assert cached["router_agent"].prefetch_limit == 77
