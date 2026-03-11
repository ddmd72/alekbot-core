"""
Tests for GroovyPromptAssembler.

Session: 23 (Prompt Component Architecture Implementation)
"""

import pytest
from src.domain.prompt import PromptComponent, PromptTemplate, ComponentScope, TEMPLATE_LIGHT
from src.adapters.groovy_prompt_assembler import GroovyPromptAssembler
from src.ports.prompt_assembler import AssemblyError


class TestGroovyPromptAssembler:
    """Test Groovy DSL assembly logic."""
    
    @pytest.fixture
    def assembler(self):
        return GroovyPromptAssembler()
    
    @pytest.fixture
    def sample_components(self):
        """Sample components for testing."""
        return [
            PromptComponent(
                id="cognitive_process",
                scope=ComponentScope.CLASS_ROOT,
                content='cognitive_process {\n    description: "Think step by step"\n}',
                order=1
            ),
            PromptComponent(
                id="archetype",
                scope=ComponentScope.CLASS_PROPERTIES,
                content='archetype: "Friendly AI"',
                order=10
            ),
            PromptComponent(
                id="vibe",
                scope=ComponentScope.CLASS_PROPERTIES,
                content='vibe: "casual"',
                order=11
            ),
            PromptComponent(
                id="Output_Language_Protocol",
                scope=ComponentScope.CLASS_POLICIES,
                content='rule Output_Language_Protocol(language: String) {\n    "Always respond in ${language}"\n}',
                order=100
            ),
        ]
    
    def test_assemble_basic(self, assembler, sample_components):
        """Test basic assembly with multiple components."""
        result = assembler.assemble(TEMPLATE_LIGHT, sample_components)
        
        # Check structure
        assert "class Alek {" in result
        assert "cognitive_process {" in result
        assert "properties {" in result
        assert 'archetype: "Friendly AI"' in result
        assert "policies {" in result
        assert "Output_Language_Protocol" in result
        
        # Check closing brace
        assert result.strip().endswith("}")
    
    def test_assemble_empty_components(self, assembler):
        """Test assembly with no components."""
        with pytest.raises(AssemblyError, match="empty"):
            assembler.assemble(TEMPLATE_LIGHT, [])
    
    def test_assemble_only_root_components(self, assembler):
        """Test assembly with only CLASS_ROOT components."""
        components = [
            PromptComponent(
                id="cognitive_process",
                scope=ComponentScope.CLASS_ROOT,
                content='cognitive_process {\n    description: "Test"\n}',
                order=1
            )
        ]
        
        result = assembler.assemble(TEMPLATE_LIGHT, components)
        
        assert "class Alek {" in result
        assert "cognitive_process {" in result
        # Should not have properties section
        assert "properties {" not in result
    
    def test_assemble_respects_order(self, assembler):
        """Test that components are assembled in correct order."""
        components = [
            PromptComponent(
                id="second",
                scope=ComponentScope.CLASS_PROPERTIES,
                content='second: "B"',
                order=20
            ),
            PromptComponent(
                id="first",
                scope=ComponentScope.CLASS_PROPERTIES,
                content='first: "A"',
                order=10
            ),
        ]
        
        result = assembler.assemble(TEMPLATE_LIGHT, components)
        
        # "first" should appear before "second"
        first_pos = result.index('first: "A"')
        second_pos = result.index('second: "B"')
        assert first_pos < second_pos
    
    def test_assemble_all_scopes(self, assembler):
        """Test assembly with components in all scopes."""
        components = [
            PromptComponent(
                id="root",
                scope=ComponentScope.CLASS_ROOT,
                content="root_block { }",
                order=1
            ),
            PromptComponent(
                id="prop",
                scope=ComponentScope.CLASS_PROPERTIES,
                content='prop: "value"',
                order=10
            ),
            PromptComponent(
                id="policy",
                scope=ComponentScope.CLASS_POLICIES,
                content="rule Policy() { }",
                order=100
            ),
            PromptComponent(
                id="kb",
                scope=ComponentScope.CLASS_KNOWLEDGE_BASE,
                content="kb_item { }",
                order=200
            ),
            PromptComponent(
                id="protocol",
                scope=ComponentScope.CLASS_PROTOCOLS,
                content="protocol_block { }",
                order=300
            ),
            PromptComponent(
                id="runtime",
                scope=ComponentScope.CLASS_RUNTIME_RULES,
                content="runtime_rule { }",
                order=400
            ),
        ]
        
        result = assembler.assemble(TEMPLATE_LIGHT, components)
        
        # Check all sections present
        assert "root_block" in result
        assert "properties {" in result
        assert 'prop: "value"' in result
        assert "policies {" in result
        assert "rule Policy()" in result
        assert "knowledge_base {" in result
        assert "kb_item" in result
        assert "protocols {" in result
        assert "protocol_block" in result
        assert "runtime_rules {" in result
        assert "runtime_rule" in result
    
    def test_validate_balanced_braces(self, assembler):
        """Test validation catches unbalanced braces."""
        with pytest.raises(AssemblyError, match="Unbalanced braces"):
            assembler.validate("class Alek { properties { }")  # Missing close brace
    
    def test_validate_missing_class_declaration(self, assembler):
        """Test validation catches missing class declaration."""
        with pytest.raises(AssemblyError, match="Missing 'class Alek'"):
            assembler.validate("properties { }")
    
    def test_validate_valid_prompt(self, assembler):
        """Test validation passes for valid prompt."""
        valid_prompt = """
        class Alek {
            properties {
                archetype: "Test"
            }
        }
        """
        assert assembler.validate(valid_prompt) is True
    
    def test_indentation(self, assembler):
        """Test proper indentation of nested blocks."""
        components = [
            PromptComponent(
                id="prop",
                scope=ComponentScope.CLASS_PROPERTIES,
                content='test: "value"',
                order=10
            )
        ]
        
        result = assembler.assemble(TEMPLATE_LIGHT, components)
        
        # Check indentation (properties block should be indented)
        lines = result.split("\n")
        properties_line = [l for l in lines if "properties {" in l][0]
        assert properties_line.startswith("    ")  # 1 level indent
        
        test_line = [l for l in lines if 'test: "value"' in l][0]
        assert test_line.startswith("        ")  # 2 level indent
    
    def test_assemble_with_multiline_content(self, assembler):
        """Test assembly with multiline component content."""
        components = [
            PromptComponent(
                id="cognitive_process",
                scope=ComponentScope.CLASS_ROOT,
                content='''cognitive_process {
    step1: "analyze"
    step2: "respond"
}''',
                order=1
            )
        ]
        
        result = assembler.assemble(TEMPLATE_LIGHT, components)
        
        assert "cognitive_process {" in result
        assert "step1" in result
        assert "step2" in result
