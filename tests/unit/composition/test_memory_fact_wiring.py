"""Wiring for owner-confirmed memory facts (MEMORY_FACT_CONFIRMATION_RFC)."""

from unittest.mock import AsyncMock, Mock, MagicMock

import pytest

from src.adapters.firestore_memory_fact_draft_repository import (
    FirestoreMemoryFactDraftRepository,
)
from src.composition.user_agent_factory import UserAgentFactory
from src.domain.settings import ConsolidationSettings
from src.domain.user import UserBotConfig, UserProfile
from src.services.memory_fact_service import MemoryFactService
from tests.unit.composition.test_service_container_wiring import (  # noqa: F401 — fixtures
    container,
    fake_config,
)
from tests.unit.services.test_user_agent_factory import (  # noqa: F401 — fixture + helper
    _make_context,
    mock_dependencies,
)


@pytest.mark.datafile
def test_container_exposes_memory_fact_service(container):  # noqa: F811
    assert isinstance(container.memory_fact_service, MemoryFactService)
    assert isinstance(container.memory_fact_service._repo, FirestoreMemoryFactDraftRepository)
    assert container.memory_fact_service._sessions is container.session_store


@pytest.mark.datafile
def test_agent_services_includes_memory_fact_service(container):  # noqa: F811
    assert container.agent_services()["memory_fact_service"] is container.memory_fact_service


@pytest.mark.asyncio
async def test_smart_agent_built_with_injected_memory_fact_service(mock_dependencies):  # noqa: F811
    sentinel = MagicMock(name="memory_fact_service_sentinel")
    mock_dependencies["config"] = {
        **mock_dependencies["config"],
        "CONSOLIDATION": ConsolidationSettings(),
    }
    profile = UserProfile(user_id="user123", account_id="acc1", config=UserBotConfig())
    mock_dependencies["user_repo"] = AsyncMock()
    mock_dependencies["user_repo"].get_user = AsyncMock(return_value=profile)
    mock_dependencies["account_repo"] = AsyncMock()
    mock_dependencies["account_repo"].get_account = AsyncMock(return_value=None)
    mock_dependencies["repository"] = AsyncMock()
    mock_dependencies["repository"].initialize = AsyncMock(return_value=None)
    mock_dependencies["repository"].get_active_facts = AsyncMock(return_value=[])
    mock_dependencies["context_builder"] = Mock()
    mock_dependencies["context_builder"].build = Mock(
        side_effect=lambda agent_type, cfg: _make_context(agent_type, "gemini-2.0-flash")
    )
    cs = Mock()
    cs.get_semantic_search_limit = Mock(return_value=10)
    cs.get_biographical_cache_limit = Mock(return_value=10)
    cs.get_principles_cache_limit = Mock(return_value=10)
    cs.get_history_recent_full_turns = Mock(return_value=2)
    mock_dependencies["config_service"] = cs

    factory = UserAgentFactory(**mock_dependencies, memory_fact_service=sentinel)
    cached = await factory._create_and_cache_agents("user123")

    assert cached["smart_agent"].memory_fact_service is sentinel
    assert not hasattr(cached["quick_agent"], "memory_fact_service")
