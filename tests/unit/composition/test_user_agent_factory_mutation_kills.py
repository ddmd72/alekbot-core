"""Wiring pins for UserAgentFactory._create_and_cache_agents: the owner's timezone and ids reach the slots and email agents."""
from unittest.mock import AsyncMock, Mock

from src.composition.user_agent_factory import UserAgentFactory
from src.domain.settings import ConsolidationSettings
from src.domain.user import UserBotConfig, UserProfile
from tests.support.agent_services_kit import user_agent_factory_kwargs

from tests.unit.services.test_user_agent_factory import _make_context, mock_dependencies  # noqa: F401


async def _build(mock_dependencies):  # noqa: F811
    profile = UserProfile(user_id="user-1", account_id="acc1", config=UserBotConfig(timezone="Europe/Berlin"))
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
    return await UserAgentFactory(**user_agent_factory_kwargs(**mock_dependencies))._create_and_cache_agents("user-1")


async def test_router_slots_agent_gets_its_id_and_the_user_timezone(mock_dependencies):  # noqa: F811
    cached = await _build(mock_dependencies)
    slots = cached["router_agent"].slots
    assert slots.agent_id == "fact_query_slots_router_user-1"
    assert slots._zone_name == "Europe/Berlin"


async def test_memory_search_slots_agent_is_named_for_the_user(mock_dependencies):  # noqa: F811
    cached = await _build(mock_dependencies)
    assert cached["memory_agent"]._slots.agent_id == "fact_query_slots_user-1"


async def test_email_search_agent_gets_the_user_timezone(mock_dependencies):  # noqa: F811
    cached = await _build(mock_dependencies)
    assert cached["email_search_agent"]._tz_name == "Europe/Berlin"
