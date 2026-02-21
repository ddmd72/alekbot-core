"""
Unit tests for Prompt Design System v3 port contracts.

Covers:
- AgentProfileRepository (2 abstract methods)
- BlueprintRepository (5 abstract methods)
- TokenRepository (8 abstract methods)
"""

import inspect
import pytest
from abc import ABC
from unittest.mock import AsyncMock, MagicMock

from src.ports.prompt_v3.agent_profile_repository import AgentProfileRepository
from src.ports.prompt_v3.blueprint_repository import BlueprintRepository
from src.ports.prompt_v3.token_repository import TokenRepository
from src.domain.prompt_v3.slot import OwnerType
from src.domain.prompt_v3.token import TokenId, TokenCategory, TokenClass


# =============================================================================
# AgentProfileRepository
# =============================================================================

class TestAgentProfileRepositoryContract:
    """Verify AgentProfileRepository port declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(AgentProfileRepository, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            AgentProfileRepository()

    def test_has_get_profile_slots(self):
        assert getattr(AgentProfileRepository.get_profile_slots, "__isabstractmethod__", False)

    def test_has_delete_profile(self):
        assert getattr(AgentProfileRepository.delete_profile, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        """Port should have exactly 2 abstract methods."""
        abstract_methods = {
            name for name, method in inspect.getmembers(AgentProfileRepository)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 2, f"Expected 2 abstract methods, got {abstract_methods}"

    def test_get_profile_slots_signature(self):
        sig = inspect.signature(AgentProfileRepository.get_profile_slots)
        params = list(sig.parameters.keys())
        assert params == ["self", "blueprint_id", "owner_type", "owner_value"]

    def test_delete_profile_signature(self):
        sig = inspect.signature(AgentProfileRepository.delete_profile)
        params = list(sig.parameters.keys())
        assert params == ["self", "blueprint_id", "owner_type", "owner_value"]


class TestAgentProfileRepositoryMockImplementation:
    """Verify AsyncMock(spec=AgentProfileRepository) satisfies the port contract."""

    @pytest.fixture
    def mock_repo(self):
        return AsyncMock(spec=AgentProfileRepository)

    async def test_get_profile_slots_returns_list(self, mock_repo):
        mock_repo.get_profile_slots.return_value = []
        result = await mock_repo.get_profile_slots("smart_v1", OwnerType.USER, "user1")
        assert isinstance(result, list)

    async def test_delete_profile(self, mock_repo):
        await mock_repo.delete_profile("smart_v1", OwnerType.USER, "user1")
        mock_repo.delete_profile.assert_called_once_with("smart_v1", OwnerType.USER, "user1")


# =============================================================================
# BlueprintRepository
# =============================================================================

class TestBlueprintRepositoryContract:
    """Verify BlueprintRepository port declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(BlueprintRepository, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            BlueprintRepository()

    def test_has_get(self):
        assert getattr(BlueprintRepository.get, "__isabstractmethod__", False)

    def test_has_list_all(self):
        assert getattr(BlueprintRepository.list_all, "__isabstractmethod__", False)

    def test_has_save(self):
        assert getattr(BlueprintRepository.save, "__isabstractmethod__", False)

    def test_has_delete(self):
        assert getattr(BlueprintRepository.delete, "__isabstractmethod__", False)

    def test_has_exists(self):
        assert getattr(BlueprintRepository.exists, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        """Port should have exactly 5 abstract methods."""
        abstract_methods = {
            name for name, method in inspect.getmembers(BlueprintRepository)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 5, f"Expected 5 abstract methods, got {abstract_methods}"

    def test_get_signature(self):
        sig = inspect.signature(BlueprintRepository.get)
        params = list(sig.parameters.keys())
        assert params == ["self", "blueprint_id"]

    def test_exists_signature(self):
        sig = inspect.signature(BlueprintRepository.exists)
        params = list(sig.parameters.keys())
        assert params == ["self", "blueprint_id"]
        assert sig.return_annotation == bool


class TestBlueprintRepositoryMockImplementation:
    """Verify AsyncMock(spec=BlueprintRepository) satisfies the port contract."""

    @pytest.fixture
    def mock_repo(self):
        return AsyncMock(spec=BlueprintRepository)

    async def test_get_returns_blueprint(self, mock_repo):
        bp = MagicMock()
        mock_repo.get.return_value = bp
        result = await mock_repo.get("smart_agent_v1")
        assert result is bp

    async def test_list_all_returns_list(self, mock_repo):
        mock_repo.list_all.return_value = []
        result = await mock_repo.list_all()
        assert isinstance(result, list)

    async def test_save_blueprint(self, mock_repo):
        bp = MagicMock()
        await mock_repo.save(bp)
        mock_repo.save.assert_called_once_with(bp)

    async def test_delete_blueprint(self, mock_repo):
        await mock_repo.delete("old_agent_v1")
        mock_repo.delete.assert_called_once_with("old_agent_v1")

    async def test_exists_returns_bool(self, mock_repo):
        mock_repo.exists.return_value = True
        result = await mock_repo.exists("smart_agent_v1")
        assert result is True


# =============================================================================
# TokenRepository
# =============================================================================

class TestTokenRepositoryContract:
    """Verify TokenRepository port declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(TokenRepository, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            TokenRepository()

    def test_has_get(self):
        assert getattr(TokenRepository.get, "__isabstractmethod__", False)

    def test_has_list_by_category(self):
        assert getattr(TokenRepository.list_by_category, "__isabstractmethod__", False)

    def test_has_list_by_class(self):
        assert getattr(TokenRepository.list_by_class, "__isabstractmethod__", False)

    def test_has_list_all(self):
        assert getattr(TokenRepository.list_all, "__isabstractmethod__", False)

    def test_has_save(self):
        assert getattr(TokenRepository.save, "__isabstractmethod__", False)

    def test_has_delete(self):
        assert getattr(TokenRepository.delete, "__isabstractmethod__", False)

    def test_has_exists(self):
        assert getattr(TokenRepository.exists, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        """Port should have exactly 7 abstract methods."""
        abstract_methods = {
            name for name, method in inspect.getmembers(TokenRepository)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 7, f"Expected 7 abstract methods, got {abstract_methods}"

    def test_get_signature(self):
        sig = inspect.signature(TokenRepository.get)
        params = list(sig.parameters.keys())
        assert params == ["self", "token_id"]

    def test_list_by_category_signature(self):
        sig = inspect.signature(TokenRepository.list_by_category)
        params = list(sig.parameters.keys())
        assert params == ["self", "category"]

    def test_exists_signature(self):
        sig = inspect.signature(TokenRepository.exists)
        params = list(sig.parameters.keys())
        assert params == ["self", "token_id"]
        assert sig.return_annotation == bool


class TestTokenRepositoryMockImplementation:
    """Verify AsyncMock(spec=TokenRepository) satisfies the port contract."""

    @pytest.fixture
    def mock_repo(self):
        return AsyncMock(spec=TokenRepository)

    async def test_get_returns_token(self, mock_repo):
        token = MagicMock()
        mock_repo.get.return_value = token
        result = await mock_repo.get(TokenId("HUMOR_PRESET_OFF"))
        assert result is token

    async def test_list_by_category_returns_list(self, mock_repo):
        mock_repo.list_by_category.return_value = []
        result = await mock_repo.list_by_category(TokenCategory("humor_engine"))
        assert isinstance(result, list)

    async def test_list_by_class_returns_list(self, mock_repo):
        mock_repo.list_by_class.return_value = []
        result = await mock_repo.list_by_class(TokenClass("properties"))
        assert isinstance(result, list)

    async def test_list_all_returns_list(self, mock_repo):
        mock_repo.list_all.return_value = []
        result = await mock_repo.list_all()
        assert isinstance(result, list)

    async def test_save_token(self, mock_repo):
        token = MagicMock()
        await mock_repo.save(token)
        mock_repo.save.assert_called_once_with(token)

    async def test_delete_token(self, mock_repo):
        await mock_repo.delete(TokenId("OLD_TOKEN"))
        mock_repo.delete.assert_called_once()

    async def test_exists_returns_bool(self, mock_repo):
        mock_repo.exists.return_value = False
        result = await mock_repo.exists(TokenId("MISSING"))
        assert result is False
