"""
Unit tests for prompt component domain models.

Session: 23 (Prompt Component Architecture Implementation)
RFC: docs/architecture/rfcs/PROMPT_COMPONENT_ARCHITECTURE_RFC.md
"""

import pytest
from src.domain.prompt import (
    ComponentScope,
    PromptComponent,
    PromptTemplate,
    TEMPLATE_LIGHT,
    TEMPLATE_FULL
)


class TestComponentScope:
    """Tests for ComponentScope enum."""
    
    def test_scope_values_exist(self):
        """Test all required scope values are defined."""
        assert ComponentScope.CLASS_ROOT.value == "class.Alek"
        assert ComponentScope.CLASS_PROPERTIES.value == "class.Alek.properties"
        assert ComponentScope.CLASS_POLICIES.value == "class.Alek.policies"
        assert ComponentScope.CLASS_KNOWLEDGE_BASE.value == "class.Alek.knowledge_base"
        assert ComponentScope.CLASS_RUNTIME_RULES.value == "class.Alek.runtime_rules"
        assert ComponentScope.CLASS_PROTOCOLS.value == "class.Alek.protocols"

    def test_scope_count(self):
        """Test we have exactly 6 scopes (CLASS_TRAINING_DATA removed)."""
        assert len(ComponentScope) == 6


class TestPromptComponent:
    """Tests for PromptComponent dataclass."""
    
    def test_create_valid_component(self):
        """Test creating valid component."""
        comp = PromptComponent(
            id="cognitive_process",
            scope=ComponentScope.CLASS_ROOT,
            content="cognitive_process { ... }",
            order=1
        )
        
        assert comp.id == "cognitive_process"
        assert comp.scope == ComponentScope.CLASS_ROOT
        assert comp.content == "cognitive_process { ... }"
        assert comp.order == 1
        assert comp.is_user_override is False
        assert comp.version == "1.0"
    
    def test_component_with_user_override(self):
        """Test user override flag."""
        comp = PromptComponent(
            id="humor_engine",
            scope=ComponentScope.CLASS_PROPERTIES,
            content='humor_engine { status: "DISABLED" }',
            order=20,
            is_user_override=True
        )
        
        assert comp.is_user_override is True
    
    def test_component_immutable(self):
        """Test component is frozen (immutable)."""
        comp = PromptComponent(
            id="test",
            scope=ComponentScope.CLASS_ROOT,
            content="test {}",
            order=1
        )
        
        with pytest.raises(Exception):  # dataclass frozen raises
            comp.id = "changed"
    
    def test_component_validation_empty_id(self):
        """Test validation fails for empty id."""
        with pytest.raises(ValueError, match="Component id required"):
            PromptComponent(
                id="",
                scope=ComponentScope.CLASS_ROOT,
                content="test",
                order=1
            )
    
    def test_component_allows_empty_content(self):
        """Empty content is allowed — supports fallthrough pattern (SESSION_24)."""
        comp = PromptComponent(
            id="test",
            scope=ComponentScope.CLASS_ROOT,
            content="",
            order=1
        )
        assert comp.content == ""

    def test_component_allows_whitespace_content(self):
        """Whitespace-only content is allowed for the same reason."""
        comp = PromptComponent(
            id="test",
            scope=ComponentScope.CLASS_ROOT,
            content="   ",
            order=1
        )
        assert comp.content == "   "
    
    def test_component_with_custom_version(self):
        """Test component with custom version."""
        comp = PromptComponent(
            id="test",
            scope=ComponentScope.CLASS_ROOT,
            content="test",
            order=1,
            version="2.0"
        )
        
        assert comp.version == "2.0"


class TestPromptTemplate:
    """Tests for PromptTemplate dataclass."""
    
    def test_create_valid_template(self):
        """Test creating valid template."""
        template = PromptTemplate(
            name="TestAgent",
            extends="Agent",
            scopes=[ComponentScope.CLASS_ROOT, ComponentScope.CLASS_PROPERTIES],
            supports_tools=False
        )
        
        assert template.name == "TestAgent"
        assert template.extends == "Agent"
        assert len(template.scopes) == 2
        assert template.supports_tools is False
    
    def test_template_without_extends(self):
        """Test template without parent class."""
        template = PromptTemplate(
            name="BaseAgent",
            extends=None,
            scopes=[ComponentScope.CLASS_ROOT],
            supports_tools=False
        )
        
        assert template.extends is None
    
    def test_template_with_tools(self):
        """Test template with tools support."""
        template = PromptTemplate(
            name="SmartAgent",
            extends="Alek",
            scopes=[ComponentScope.CLASS_PROTOCOLS],
            supports_tools=True
        )
        
        assert template.supports_tools is True


class TestPredefinedTemplates:
    """Tests for predefined template constants."""
    
    def test_template_light_structure(self):
        """Test TEMPLATE_LIGHT structure (quick agent — all 6 scopes, no tools)."""
        assert TEMPLATE_LIGHT.name == "Alek"
        assert TEMPLATE_LIGHT.extends == "Agent"
        assert TEMPLATE_LIGHT.supports_tools is False
        assert len(TEMPLATE_LIGHT.scopes) == 6

        # Verify all scopes present
        for scope in ComponentScope:
            assert scope in TEMPLATE_LIGHT.scopes

    def test_template_full_structure(self):
        """Test TEMPLATE_FULL structure (smart agent — all 6 scopes, with tools)."""
        assert TEMPLATE_FULL.name == "Alek"
        assert TEMPLATE_FULL.extends == "Agent"
        assert TEMPLATE_FULL.supports_tools is True
        assert len(TEMPLATE_FULL.scopes) == 6

        # Verify all scopes present
        for scope in ComponentScope:
            assert scope in TEMPLATE_FULL.scopes

    def test_light_and_full_differ_in_tools(self):
        """LIGHT and FULL share the same scopes; they differ only in supports_tools."""
        assert set(TEMPLATE_LIGHT.scopes) == set(TEMPLATE_FULL.scopes)
        assert TEMPLATE_LIGHT.supports_tools is False
        assert TEMPLATE_FULL.supports_tools is True

    def test_templates_cover_all_scopes(self):
        """Both templates cover all defined ComponentScope values."""
        all_template_scopes = set(TEMPLATE_LIGHT.scopes)
        all_enum_scopes = set(ComponentScope)
        assert all_template_scopes == all_enum_scopes
