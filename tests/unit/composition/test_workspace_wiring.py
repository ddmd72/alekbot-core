"""Wiring: adapter → FileConversionService(workspace=port) → WorkspaceService; Smart gets the service, NotesAgent gets
workspace_enabled (AGENT_WORKSPACE_RFC §5.5, §12, §13). No key → no workspace anywhere."""
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from src.adapters.gcs_workspace_adapter import GcsWorkspaceAdapter
from src.composition.user_agent_factory import UserAgentFactory
from src.domain.settings import ConsolidationSettings
from src.domain.user import UserBotConfig, UserProfile
from src.services.workspace_service import WorkspaceService
from tests.unit.composition.test_drive_wiring import _container
from tests.unit.services.test_user_agent_factory import (  # noqa: F401 — fixture + helper
    _make_context,
    mock_dependencies,
)


class TestContainer:
    def test_absent_without_the_bucket(self, monkeypatch):
        c = _container(monkeypatch)
        assert c.workspace_store is None and c.workspace_service is None
        assert c.agent_services()["workspace_service"] is None

    def test_present_with_the_bucket(self, monkeypatch):
        c = _container(monkeypatch, GCS_WORKSPACE_BUCKET="ws-bucket", GCS_MEDIA_BUCKET="media-bucket")
        assert isinstance(c.workspace_store, GcsWorkspaceAdapter)
        assert isinstance(c.workspace_service, WorkspaceService)
        assert c.workspace_service._store is c.workspace_store
        assert c.workspace_service._file_conversion is c.file_conversion_service
        assert c.file_conversion_service._workspace is c.workspace_store
        assert c.agent_services()["workspace_service"] is c.workspace_service

    def test_without_the_media_bucket_copies_from_chat_are_off(self, monkeypatch):
        c = _container(monkeypatch, GCS_WORKSPACE_BUCKET="ws-bucket")
        assert c.workspace_service is not None and c.workspace_service._file_conversion is None


def _factory(deps, **extra):
    deps["config"] = {**deps["config"], "CONSOLIDATION": ConsolidationSettings()}
    profile = UserProfile(user_id="user123", account_id="acc1", config=UserBotConfig())
    deps["user_repo"] = AsyncMock()
    deps["user_repo"].get_user = AsyncMock(return_value=profile)
    deps["account_repo"] = AsyncMock()
    deps["account_repo"].get_account = AsyncMock(return_value=None)
    deps["repository"] = AsyncMock()
    deps["repository"].initialize = AsyncMock(return_value=None)
    deps["repository"].get_active_facts = AsyncMock(return_value=[])
    deps["context_builder"] = Mock()
    deps["context_builder"].build = Mock(side_effect=lambda agent_type, cfg: _make_context(agent_type, "gemini-2.0-flash"))
    cs = Mock()
    cs.get_semantic_search_limit = Mock(return_value=10)
    cs.get_biographical_cache_limit = Mock(return_value=10)
    cs.get_principles_cache_limit = Mock(return_value=10)
    cs.get_history_recent_full_turns = Mock(return_value=2)
    deps["config_service"] = cs
    return UserAgentFactory(**deps, notes_provider=AsyncMock(), recurrence=Mock(), **extra)


@pytest.mark.asyncio
async def test_smart_and_notes_get_their_dependencies(mock_dependencies):  # noqa: F811
    workspace = MagicMock(name="workspace_service")
    cached = await _factory(mock_dependencies, workspace_service=workspace)._create_and_cache_agents("user123")
    assert cached["smart_agent"].workspace_service is workspace
    assert cached["notes_agent"]._workspace_enabled is True
    assert not hasattr(cached["notes_agent"], "_skill_service")  # no skill check (D15)
    assert not hasattr(cached["quick_agent"], "workspace_service")  # Smart only (RFC §11.4)


@pytest.mark.asyncio
async def test_without_the_service_notes_refuses_workspace_files(mock_dependencies):  # noqa: F811
    cached = await _factory(mock_dependencies)._create_and_cache_agents("user123")
    assert cached["smart_agent"].workspace_service is None
    assert cached["notes_agent"]._workspace_enabled is False
