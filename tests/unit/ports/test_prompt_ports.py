"""
Unit tests for prompt-related port contracts.

Covers:
- PromptComponentRepository (5 abstract methods)
- PromptAssembler (2 abstract methods)
- PromptBuilderPort (6 abstract methods: 2 async, 4 sync)
"""

import inspect
import pytest
from abc import ABC
from unittest.mock import AsyncMock, MagicMock

from src.ports.prompt_component_repository import PromptComponentRepository
from src.ports.prompt_assembler import PromptAssembler, AssemblyError
from src.ports.prompt_builder_port import PromptBuilderPort
from src.domain.prompt import PromptComponent, ComponentScope, TEMPLATE_LIGHT


# =============================================================================
# PromptComponentRepository
# =============================================================================

class TestPromptComponentRepositoryContract:
    """Verify PromptComponentRepository port declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(PromptComponentRepository, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            PromptComponentRepository()

    def test_has_get_default_components(self):
        assert hasattr(PromptComponentRepository, "get_default_components")
        assert getattr(PromptComponentRepository.get_default_components, "__isabstractmethod__", False)

    def test_has_get_user_overrides(self):
        assert hasattr(PromptComponentRepository, "get_user_overrides")
        assert getattr(PromptComponentRepository.get_user_overrides, "__isabstractmethod__", False)

    def test_has_save_user_override(self):
        assert hasattr(PromptComponentRepository, "save_user_override")
        assert getattr(PromptComponentRepository.save_user_override, "__isabstractmethod__", False)

    def test_has_delete_user_override(self):
        assert hasattr(PromptComponentRepository, "delete_user_override")
        assert getattr(PromptComponentRepository.delete_user_override, "__isabstractmethod__", False)

    def test_has_resolve_component(self):
        assert hasattr(PromptComponentRepository, "resolve_component")
        assert getattr(PromptComponentRepository.resolve_component, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        """Port should have exactly 5 abstract methods."""
        abstract_methods = {
            name for name, method in inspect.getmembers(PromptComponentRepository)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 5, f"Expected 5 abstract methods, got {abstract_methods}"

    def test_get_default_components_signature(self):
        sig = inspect.signature(PromptComponentRepository.get_default_components)
        params = list(sig.parameters.keys())
        assert params == ["self", "scope"]
        assert sig.parameters["scope"].default is None

    def test_resolve_component_signature(self):
        sig = inspect.signature(PromptComponentRepository.resolve_component)
        params = list(sig.parameters.keys())
        assert params == ["self", "component_id", "agent_type", "user_id"]
        assert sig.parameters["user_id"].default is None


class TestPromptComponentRepositoryMockImplementation:
    """Verify AsyncMock(spec=PromptComponentRepository) satisfies the port contract."""

    @pytest.fixture
    def mock_repo(self):
        return AsyncMock(spec=PromptComponentRepository)

    async def test_get_default_components(self, mock_repo):
        mock_repo.get_default_components.return_value = []
        result = await mock_repo.get_default_components()
        assert isinstance(result, list)

    async def test_get_default_components_with_scope(self, mock_repo):
        mock_repo.get_default_components.return_value = []
        result = await mock_repo.get_default_components(scope=ComponentScope.CLASS_ROOT)
        assert isinstance(result, list)

    async def test_get_user_overrides(self, mock_repo):
        mock_repo.get_user_overrides.return_value = []
        result = await mock_repo.get_user_overrides("user1")
        assert isinstance(result, list)

    async def test_save_user_override(self, mock_repo):
        comp = PromptComponent(id="x", scope=ComponentScope.CLASS_ROOT, content="c", order=1)
        await mock_repo.save_user_override("user1", comp)
        mock_repo.save_user_override.assert_called_once_with("user1", comp)

    async def test_delete_user_override(self, mock_repo):
        await mock_repo.delete_user_override("user1", "comp_id")
        mock_repo.delete_user_override.assert_called_once_with("user1", "comp_id")

    async def test_resolve_component_returns_none(self, mock_repo):
        mock_repo.resolve_component.return_value = None
        result = await mock_repo.resolve_component("comp_id", "quick", user_id="user1")
        assert result is None


# =============================================================================
# PromptAssembler
# =============================================================================

class TestPromptAssemblerContract:
    """Verify PromptAssembler port declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(PromptAssembler, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            PromptAssembler()

    def test_has_assemble(self):
        assert hasattr(PromptAssembler, "assemble")
        assert getattr(PromptAssembler.assemble, "__isabstractmethod__", False)

    def test_has_validate(self):
        assert hasattr(PromptAssembler, "validate")
        assert getattr(PromptAssembler.validate, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        """Port should have exactly 2 abstract methods."""
        abstract_methods = {
            name for name, method in inspect.getmembers(PromptAssembler)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 2, f"Expected 2 abstract methods, got {abstract_methods}"

    def test_assembly_error_is_exception(self):
        assert issubclass(AssemblyError, Exception)

    def test_assemble_signature(self):
        sig = inspect.signature(PromptAssembler.assemble)
        params = list(sig.parameters.keys())
        assert params == ["self", "template", "components", "runtime_data"]
        assert sig.return_annotation == str

    def test_validate_signature(self):
        sig = inspect.signature(PromptAssembler.validate)
        params = list(sig.parameters.keys())
        assert params == ["self", "prompt"]
        assert sig.return_annotation == bool


class TestPromptAssemblerMockImplementation:
    """Verify MagicMock(spec=PromptAssembler) satisfies the port contract."""

    @pytest.fixture
    def mock_assembler(self):
        return MagicMock(spec=PromptAssembler)

    def test_assemble_returns_string(self, mock_assembler):
        mock_assembler.assemble.return_value = "class Alek {}"
        result = mock_assembler.assemble(TEMPLATE_LIGHT, [], {})
        assert isinstance(result, str)

    def test_validate_returns_bool(self, mock_assembler):
        mock_assembler.validate.return_value = True
        result = mock_assembler.validate("some prompt")
        assert isinstance(result, bool)

    def test_assembly_error_is_raiseable(self):
        with pytest.raises(AssemblyError):
            raise AssemblyError("assembly failed")

    def test_assembly_error_preserves_message(self):
        try:
            raise AssemblyError("custom error")
        except AssemblyError as e:
            assert str(e) == "custom error"


# =============================================================================
# PromptBuilderPort
# =============================================================================

class TestPromptBuilderPortContract:
    """Verify PromptBuilderPort port declares all required abstract methods."""

    def test_is_abstract_class(self):
        assert issubclass(PromptBuilderPort, ABC)

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            PromptBuilderPort()

    def test_has_preload_components(self):
        assert hasattr(PromptBuilderPort, "preload_components")
        assert getattr(PromptBuilderPort.preload_components, "__isabstractmethod__", False)

    def test_has_build_for_agent(self):
        assert hasattr(PromptBuilderPort, "build_for_agent")
        assert getattr(PromptBuilderPort.build_for_agent, "__isabstractmethod__", False)

    def test_has_merge_enriched_context_with_biographical(self):
        assert hasattr(PromptBuilderPort, "merge_enriched_context_with_biographical")
        assert getattr(
            PromptBuilderPort.merge_enriched_context_with_biographical,
            "__isabstractmethod__",
            False,
        )

    def test_has_invalidate_cache(self):
        assert hasattr(PromptBuilderPort, "invalidate_cache")
        assert getattr(PromptBuilderPort.invalidate_cache, "__isabstractmethod__", False)

    def test_has_invalidate_biographical_cache(self):
        assert hasattr(PromptBuilderPort, "invalidate_biographical_cache")
        assert getattr(PromptBuilderPort.invalidate_biographical_cache, "__isabstractmethod__", False)

    def test_has_get_cache_stats(self):
        assert hasattr(PromptBuilderPort, "get_cache_stats")
        assert getattr(PromptBuilderPort.get_cache_stats, "__isabstractmethod__", False)

    def test_all_abstract_methods_count(self):
        """Port should have exactly 6 abstract methods."""
        abstract_methods = {
            name for name, method in inspect.getmembers(PromptBuilderPort)
            if getattr(method, "__isabstractmethod__", False)
        }
        assert len(abstract_methods) == 6, f"Expected 6 abstract methods, got {abstract_methods}"

    def test_build_for_agent_signature(self):
        sig = inspect.signature(PromptBuilderPort.build_for_agent)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "agent_type" in params
        assert "user_id" in params
        assert sig.parameters["user_id"].default is None
        assert sig.return_annotation == str

    def test_invalidate_cache_signature(self):
        sig = inspect.signature(PromptBuilderPort.invalidate_cache)
        params = list(sig.parameters.keys())
        assert params == ["self", "component_key"]
        assert sig.parameters["component_key"].default is None

    def test_invalidate_biographical_cache_signature(self):
        sig = inspect.signature(PromptBuilderPort.invalidate_biographical_cache)
        params = list(sig.parameters.keys())
        assert params == ["self", "user_id"]


class TestPromptBuilderPortMockImplementation:
    """Verify AsyncMock(spec=PromptBuilderPort) satisfies the port contract."""

    @pytest.fixture
    def mock_builder(self):
        return AsyncMock(spec=PromptBuilderPort)

    async def test_preload_components(self, mock_builder):
        await mock_builder.preload_components()
        mock_builder.preload_components.assert_called_once()

    async def test_build_for_agent_returns_string(self, mock_builder):
        mock_builder.build_for_agent.return_value = "SYSTEM PROMPT"
        result = await mock_builder.build_for_agent(agent_type="quick", user_id="u1")
        assert isinstance(result, str)
        assert result == "SYSTEM PROMPT"

    def test_invalidate_cache(self, mock_builder):
        mock_builder.invalidate_cache(component_key=None)
        mock_builder.invalidate_cache.assert_called_once_with(component_key=None)

    def test_invalidate_biographical_cache(self, mock_builder):
        mock_builder.invalidate_biographical_cache("user1")
        mock_builder.invalidate_biographical_cache.assert_called_once_with("user1")

    def test_get_cache_stats(self, mock_builder):
        mock_builder.get_cache_stats.return_value = {"hits": 5, "misses": 2}
        result = mock_builder.get_cache_stats()
        assert isinstance(result, dict)
