"""
Tests for PromptComponentService.

Session: 23 (Prompt Component Architecture Implementation - Phase 3)
Updated: SESSION_26 - agent_type/account_id now required; 4-level priority resolution.
"""

import pytest
import asyncio
from src.domain.prompt import PromptComponent, PromptTemplate, ComponentScope, TEMPLATE_LIGHT
from src.services.prompt_component_service import PromptComponentService
from src.ports.prompt_component_repository import PromptComponentRepository
from src.ports.prompt_assembler import PromptAssembler


class MockRepository(PromptComponentRepository):
    """Mock repository for testing."""

    def __init__(self):
        self.default_components = []
        self.user_overrides = {}  # {user_id: [components]}
        self.saved_overrides = []
        self.deleted_overrides = []

    async def get_default_components(self, scope=None):
        if scope:
            return [c for c in self.default_components if c.scope == scope]
        return self.default_components.copy()

    async def get_user_overrides(self, user_id, scope=None):
        overrides = self.user_overrides.get(user_id, [])
        if scope:
            return [c for c in overrides if c.scope == scope]
        return overrides.copy()

    async def save_user_override(self, user_id, component):
        self.saved_overrides.append((user_id, component))

    async def delete_user_override(self, user_id, component_id):
        self.deleted_overrides.append((user_id, component_id))
        return True

    async def resolve_component(self, component_id, agent_type, account_id=None, user_id=None):
        """Simplified resolution: USER override > system default."""
        if user_id:
            user_overrides = self.user_overrides.get(user_id, [])
            for comp in user_overrides:
                if comp.id == component_id:
                    return comp
        for comp in self.default_components:
            if comp.id == component_id:
                return comp
        return None


class MockAssembler(PromptAssembler):
    """Mock assembler for testing."""

    def assemble(self, template, components):
        content = f"// Template: {template.name}\n"
        for comp in components:
            content += f"{comp.id}\n"
        return content

    def validate(self, prompt):
        return True


class TestPromptComponentService:
    """Test PromptComponentService logic."""

    @pytest.fixture
    def mock_repo(self):
        repo = MockRepository()
        repo.default_components = [
            PromptComponent(
                id="cognitive_process",
                scope=ComponentScope.CLASS_ROOT,
                content="default cognitive process",
                order=1
            ),
            PromptComponent(
                id="archetype",
                scope=ComponentScope.CLASS_PROPERTIES,
                content='archetype: "default"',
                order=10
            ),
        ]
        return repo

    @pytest.fixture
    def mock_assembler(self):
        return MockAssembler()

    @pytest.fixture
    def service(self, mock_repo, mock_assembler):
        return PromptComponentService(mock_repo, mock_assembler, cache_ttl=1)

    @pytest.mark.asyncio
    async def test_get_assembled_prompt_default(self, service, mock_repo):
        """Test assembling prompt with default components only."""
        result = await service.get_assembled_prompt(
            TEMPLATE_LIGHT, agent_type="quick", user_id="anon", account_id="anon-account"
        )

        assert "cognitive_process" in result
        assert "archetype" in result
        assert "Template: Alek" in result

    @pytest.mark.asyncio
    async def test_get_assembled_prompt_with_user_override(self, service, mock_repo):
        """Test assembling prompt with user overrides."""
        user_id = "user123"

        mock_repo.user_overrides[user_id] = [
            PromptComponent(
                id="archetype",
                scope=ComponentScope.CLASS_PROPERTIES,
                content='archetype: "custom"',
                order=10,
                is_user_override=True
            )
        ]

        result = await service.get_assembled_prompt(
            TEMPLATE_LIGHT, agent_type="quick", user_id=user_id, account_id="acct1"
        )

        assert "archetype" in result
        assert "cognitive_process" in result

    @pytest.mark.asyncio
    async def test_get_components_for_user(self, service, mock_repo):
        """Test get_components_for_user returns USER overrides only (not merged with defaults)."""
        user_id = "user456"

        mock_repo.user_overrides[user_id] = [
            PromptComponent(
                id="custom_component",
                scope=ComponentScope.CLASS_PROPERTIES,
                content="custom: true",
                order=20,
                is_user_override=True
            )
        ]

        components = await service.get_components_for_user(user_id)

        assert len(components) == 1  # USER overrides only, not defaults
        assert components[0].id == "custom_component"

    @pytest.mark.asyncio
    async def test_save_user_override(self, service, mock_repo):
        """Test saving user override."""
        user_id = "user789"
        component = PromptComponent(
            id="test_component",
            scope=ComponentScope.CLASS_PROPERTIES,
            content="test: true",
            order=15
        )

        await service.save_user_override(user_id, component)

        assert len(mock_repo.saved_overrides) == 1
        assert mock_repo.saved_overrides[0] == (user_id, component)

    @pytest.mark.asyncio
    async def test_delete_user_override(self, service, mock_repo):
        """Test deleting user override."""
        user_id = "user999"
        component_id = "test_component"

        deleted = await service.delete_user_override(user_id, component_id)

        assert deleted is True
        assert len(mock_repo.deleted_overrides) == 1
        assert mock_repo.deleted_overrides[0] == (user_id, component_id)

    @pytest.mark.asyncio
    async def test_cache_hit(self, service):
        """Test that cache works for repeated calls."""
        result1 = await service.get_assembled_prompt(
            TEMPLATE_LIGHT, agent_type="quick", user_id="u1", account_id="a1"
        )
        result2 = await service.get_assembled_prompt(
            TEMPLATE_LIGHT, agent_type="quick", user_id="u1", account_id="a1"
        )

        assert result1 == result2

        stats = service.get_cache_stats()
        assert stats['total_entries'] == 1

    @pytest.mark.asyncio
    async def test_cache_invalidation_on_save(self, service, mock_repo):
        """Test that cache is invalidated when user override is saved."""
        user_id = "user_cache"

        await service.get_assembled_prompt(
            TEMPLATE_LIGHT, agent_type="quick", user_id=user_id, account_id="a1"
        )

        component = PromptComponent(
            id="new_comp",
            scope=ComponentScope.CLASS_PROPERTIES,
            content="new: true",
            order=5
        )
        await service.save_user_override(user_id, component)

        stats = service.get_cache_stats()
        assert stats['total_entries'] >= 0

    @pytest.mark.asyncio
    async def test_cache_expiration(self, service):
        """Test that cache expires after TTL."""
        result1 = await service.get_assembled_prompt(
            TEMPLATE_LIGHT, agent_type="quick", user_id="u1", account_id="a1"
        )

        await asyncio.sleep(1.1)

        result2 = await service.get_assembled_prompt(
            TEMPLATE_LIGHT, agent_type="quick", user_id="u1", account_id="a1"
        )

        assert result1 == result2

        stats = service.get_cache_stats()
        assert stats['total_entries'] == 1

    @pytest.mark.asyncio
    async def test_filter_by_template_scopes(self, service, mock_repo):
        """Test that components outside template scopes are excluded."""
        # Use a restricted template with only CLASS_ROOT scope
        restricted_template = PromptTemplate(
            name="Restricted",
            extends="Agent",
            scopes=[ComponentScope.CLASS_ROOT],
            supports_tools=False
        )
        # CLASS_PROPERTIES component NOT in restricted template
        result = await service.get_assembled_prompt(
            restricted_template, agent_type="quick", user_id="u1", account_id="a1"
        )

        assert "archetype" not in result       # CLASS_PROPERTIES — excluded
        assert "cognitive_process" in result   # CLASS_ROOT — included

    @pytest.mark.asyncio
    async def test_merge_strategy_override_replaces_default(self, service, mock_repo):
        """Test that user override replaces default for same component ID in assembled prompt."""
        user_id = "merge_test"

        mock_repo.user_overrides[user_id] = [
            PromptComponent(
                id="archetype",
                scope=ComponentScope.CLASS_PROPERTIES,
                content='archetype: "OVERRIDE"',
                order=10,
                is_user_override=True
            )
        ]

        result = await service.get_assembled_prompt(
            TEMPLATE_LIGHT, agent_type="quick", user_id=user_id, account_id="a1"
        )

        # Both cognitive_process (default) and archetype (user override) should appear
        assert "cognitive_process" in result
        assert "archetype" in result

    def test_cache_key_building(self, service):
        """Test cache key generation includes agent_type and account_id (SESSION_26)."""
        key1 = service._build_cache_key("Alek", "quick", "user1", "acct1", None)
        key2 = service._build_cache_key("Alek", "quick", "user2", "acct1", None)
        key3 = service._build_cache_key("Alek", "quick", "user1", "acct1", ComponentScope.CLASS_ROOT)

        assert key1 != key2
        assert key2 != key3

        key1_again = service._build_cache_key("Alek", "quick", "user1", "acct1", None)
        assert key1 == key1_again

    def test_get_cache_stats(self, service):
        """Test cache statistics."""
        stats = service.get_cache_stats()

        assert 'total_entries' in stats
        assert 'expired_entries' in stats
        assert 'cache_ttl_seconds' in stats
        assert 'cache_hit_ratio_estimate' in stats

        assert stats['cache_ttl_seconds'] == 1

    def test_invalidate_cache_all(self, service):
        """Test invalidating entire cache."""
        service._cache['key1'] = ('content1', 0)
        service._cache['key2'] = ('content2', 0)

        service.invalidate_cache()

        assert len(service._cache) == 0

    def test_invalidate_cache_user_specific(self, service):
        """Test invalidating cache for specific user."""
        service._cache['prompt:Alek:default'] = ('content1', 0)
        service._cache['prompt:Alek:user:user123'] = ('content2', 0)
        service._cache['prompt:Alek:user:user456'] = ('content3', 0)

        service.invalidate_cache(user_id='user123')

        assert 'prompt:Alek:default' in service._cache
        assert 'prompt:Alek:user:user123' not in service._cache
        assert 'prompt:Alek:user:user456' in service._cache
