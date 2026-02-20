"""
Unit tests for prompt component port interfaces.

Tests verify interface contracts using mock implementations.

Session: 23 (Prompt Component Architecture Implementation)
RFC: docs/architecture/rfcs/PROMPT_COMPONENT_ARCHITECTURE_RFC.md
"""

import pytest
from typing import List, Optional
from src.domain.prompt import PromptComponent, ComponentScope, PromptTemplate, TEMPLATE_LIGHT
from src.ports.prompt_component_repository import PromptComponentRepository
from src.ports.prompt_assembler import PromptAssembler, AssemblyError


# =============================================================================
# Mock Implementations for Testing
# =============================================================================

class MockPromptComponentRepository(PromptComponentRepository):
    """Mock implementation for testing repository interface."""
    
    def __init__(self):
        self.default_components = []
        self.user_overrides = {}
    
    async def get_default_components(self) -> List[PromptComponent]:
        """Return mock default components."""
        return self.default_components
    
    async def get_user_overrides(self, user_id: str) -> List[PromptComponent]:
        """Return mock user overrides."""
        return self.user_overrides.get(user_id, [])
    
    async def save_user_override(self, user_id: str, component: PromptComponent) -> None:
        """Save mock user override."""
        if user_id not in self.user_overrides:
            self.user_overrides[user_id] = []
        self.user_overrides[user_id].append(component)
    
    async def delete_user_override(self, user_id: str, component_id: str) -> None:
        """Delete mock user override."""
        if user_id in self.user_overrides:
            self.user_overrides[user_id] = [
                c for c in self.user_overrides[user_id] if c.id != component_id
            ]

    async def resolve_component(
        self,
        component_id: str,
        agent_type: str,
        user_id: Optional[str] = None
    ) -> Optional[PromptComponent]:
        """Resolve component — mock returns None (no overrides)."""
        return None


class MockPromptAssembler(PromptAssembler):
    """Mock implementation for testing assembler interface."""
    
    def __init__(self, should_validate: bool = True):
        self.should_validate = should_validate
        self.last_assembled = None
    
    def assemble(
        self,
        template: PromptTemplate,
        components: List[PromptComponent],
        runtime_data: dict
    ) -> str:
        """Mock assembly - just concatenate component IDs."""
        result = f"class {template.name} {{\n"
        for comp in components:
            result += f"  {comp.id}\n"
        result += "}"
        self.last_assembled = result
        return result
    
    def validate(self, prompt: str) -> bool:
        """Mock validation."""
        return self.should_validate


# =============================================================================
# Repository Interface Tests
# =============================================================================

class TestPromptComponentRepositoryInterface:
    """Tests for PromptComponentRepository interface contract."""
    
    @pytest.fixture
    def repo(self):
        """Create mock repository."""
        return MockPromptComponentRepository()
    
    @pytest.mark.asyncio
    async def test_get_default_components_returns_list(self, repo):
        """Test get_default_components returns list."""
        components = await repo.get_default_components()
        assert isinstance(components, list)
    
    @pytest.mark.asyncio
    async def test_get_default_components_empty_initially(self, repo):
        """Test repository starts empty."""
        components = await repo.get_default_components()
        assert len(components) == 0
    
    @pytest.mark.asyncio
    async def test_get_default_components_can_populate(self, repo):
        """Test can populate default components."""
        test_comp = PromptComponent(
            id="test",
            scope=ComponentScope.CLASS_ROOT,
            content="test",
            order=1
        )
        repo.default_components = [test_comp]
        
        components = await repo.get_default_components()
        assert len(components) == 1
        assert components[0].id == "test"
    
    @pytest.mark.asyncio
    async def test_get_user_overrides_returns_list(self, repo):
        """Test get_user_overrides returns list."""
        overrides = await repo.get_user_overrides("user123")
        assert isinstance(overrides, list)
    
    @pytest.mark.asyncio
    async def test_get_user_overrides_empty_for_new_user(self, repo):
        """Test new user has no overrides."""
        overrides = await repo.get_user_overrides("new_user")
        assert len(overrides) == 0
    
    @pytest.mark.asyncio
    async def test_save_user_override(self, repo):
        """Test saving user override."""
        override = PromptComponent(
            id="custom",
            scope=ComponentScope.CLASS_PROPERTIES,
            content="custom content",
            order=10,
            is_user_override=True
        )
        
        await repo.save_user_override("user123", override)
        
        overrides = await repo.get_user_overrides("user123")
        assert len(overrides) == 1
        assert overrides[0].id == "custom"
    
    @pytest.mark.asyncio
    async def test_save_multiple_user_overrides(self, repo):
        """Test saving multiple overrides."""
        override1 = PromptComponent(
            id="override1",
            scope=ComponentScope.CLASS_PROPERTIES,
            content="content1",
            order=10,
            is_user_override=True
        )
        override2 = PromptComponent(
            id="override2",
            scope=ComponentScope.CLASS_POLICIES,
            content="content2",
            order=20,
            is_user_override=True
        )
        
        await repo.save_user_override("user123", override1)
        await repo.save_user_override("user123", override2)
        
        overrides = await repo.get_user_overrides("user123")
        assert len(overrides) == 2
    
    @pytest.mark.asyncio
    async def test_delete_user_override(self, repo):
        """Test deleting user override."""
        override = PromptComponent(
            id="to_delete",
            scope=ComponentScope.CLASS_PROPERTIES,
            content="content",
            order=10,
            is_user_override=True
        )
        
        await repo.save_user_override("user123", override)
        assert len(await repo.get_user_overrides("user123")) == 1
        
        await repo.delete_user_override("user123", "to_delete")
        assert len(await repo.get_user_overrides("user123")) == 0
    
    @pytest.mark.asyncio
    async def test_delete_specific_override_only(self, repo):
        """Test deleting only specific override."""
        override1 = PromptComponent(
            id="keep",
            scope=ComponentScope.CLASS_PROPERTIES,
            content="content1",
            order=10,
            is_user_override=True
        )
        override2 = PromptComponent(
            id="delete",
            scope=ComponentScope.CLASS_POLICIES,
            content="content2",
            order=20,
            is_user_override=True
        )
        
        await repo.save_user_override("user123", override1)
        await repo.save_user_override("user123", override2)
        
        await repo.delete_user_override("user123", "delete")
        
        overrides = await repo.get_user_overrides("user123")
        assert len(overrides) == 1
        assert overrides[0].id == "keep"


# =============================================================================
# Assembler Interface Tests
# =============================================================================

class TestPromptAssemblerInterface:
    """Tests for PromptAssembler interface contract."""
    
    @pytest.fixture
    def assembler(self):
        """Create mock assembler."""
        return MockPromptAssembler()
    
    def test_assemble_returns_string(self, assembler):
        """Test assemble returns string."""
        components = [
            PromptComponent(
                id="test",
                scope=ComponentScope.CLASS_ROOT,
                content="test content",
                order=1
            )
        ]
        
        result = assembler.assemble(
            template=TEMPLATE_LIGHT,
            components=components,
            runtime_data={}
        )
        
        assert isinstance(result, str)
        assert len(result) > 0
    
    def test_assemble_uses_template_name(self, assembler):
        """Test assembled prompt includes template name."""
        components = []
        
        result = assembler.assemble(
            template=TEMPLATE_LIGHT,
            components=components,
            runtime_data={}
        )
        
        assert "Alek" in result
    
    def test_assemble_includes_components(self, assembler):
        """Test assembled prompt includes components."""
        components = [
            PromptComponent(
                id="comp1",
                scope=ComponentScope.CLASS_ROOT,
                content="content1",
                order=1
            ),
            PromptComponent(
                id="comp2",
                scope=ComponentScope.CLASS_PROPERTIES,
                content="content2",
                order=2
            )
        ]
        
        result = assembler.assemble(
            template=TEMPLATE_LIGHT,
            components=components,
            runtime_data={}
        )
        
        assert "comp1" in result
        assert "comp2" in result
    
    def test_validate_returns_bool(self, assembler):
        """Test validate returns boolean."""
        result = assembler.validate("test prompt")
        assert isinstance(result, bool)
    
    def test_validate_success(self, assembler):
        """Test validation passes for valid prompt."""
        assembler.should_validate = True
        assert assembler.validate("valid prompt") is True
    
    def test_validate_failure(self, assembler):
        """Test validation fails for invalid prompt."""
        assembler.should_validate = False
        assert assembler.validate("invalid prompt") is False
    
    def test_assembly_error_exception(self):
        """Test AssemblyError can be raised."""
        with pytest.raises(AssemblyError):
            raise AssemblyError("Test error")
    
    def test_assembly_error_with_message(self):
        """Test AssemblyError preserves message."""
        try:
            raise AssemblyError("Custom error message")
        except AssemblyError as e:
            assert str(e) == "Custom error message"
