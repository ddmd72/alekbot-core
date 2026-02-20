"""
Groovy DSL assembler for prompt components.

Session: 23 (Prompt Component Architecture Implementation)
RFC: docs/architecture/rfcs/PROMPT_COMPONENT_ARCHITECTURE_RFC.md
"""

from typing import List, Dict
from src.domain.prompt import PromptComponent, PromptTemplate, ComponentScope
from src.ports.prompt_assembler import PromptAssembler, AssemblyError
from src.utils.logger import logger


class GroovyPromptAssembler(PromptAssembler):
    """
    Assembles prompt components into Groovy DSL format.
    
    Follows the structure defined in GROOVY_PROMPT_PATTERN.md:
    - class Alek { ... }
    - properties { ... }
    - policies { ... }
    - knowledge_base { ... }
    - protocols { ... }
    - runtime_rules { ... }
    """
    
    def assemble(
        self, 
        template: PromptTemplate, 
        components: List[PromptComponent],
        runtime_data: Dict[str, str] = None
    ) -> str:
        """
        Assemble components into final Groovy prompt.
        
        Args:
            template: Template defining structure
            components: List of components to include
            runtime_data: Optional runtime data for placeholder injection (not used in Groovy assembler)
            
        Returns:
            Assembled Groovy DSL string
            
        Raises:
            AssemblyError: If assembly fails validation
            
        Note:
            runtime_data is reserved for future use. Currently, runtime injection
            is handled by PromptBuilder after assembly.
        """
        # Validate input
        if not components:
            raise AssemblyError("Cannot assemble prompt: component list is empty")
        
        # Group components by scope
        components_by_scope = self._group_by_scope(components)

        # Assemble final prompt following template.scopes order
        prompt_parts = [
            f"// Generated prompt using template: {template.name}",
            f"// Extends: {template.extends}",
            "",
            "class Alek {",
        ]

        # Iterate through template.scopes in order
        for scope in template.scopes:
            scope_components = components_by_scope.get(scope, [])
            if not scope_components:
                continue

            # Handle each scope type
            if scope == ComponentScope.CLASS_ROOT:
                # Root blocks without wrapper
                for component in scope_components:
                    prompt_parts.append(self._indent(component.content, 1))
                    prompt_parts.append("")

            elif scope == ComponentScope.CLASS_PROPERTIES:
                # properties { }
                section_content = self._build_properties_section(scope_components)
                prompt_parts.append(self._indent("properties {", 1))
                prompt_parts.append(self._indent(section_content, 2))
                prompt_parts.append(self._indent("}", 1))
                prompt_parts.append("")

            elif scope == ComponentScope.CLASS_POLICIES:
                # policies { }
                section_content = self._build_policies_section(scope_components)
                prompt_parts.append(self._indent("policies {", 1))
                prompt_parts.append(self._indent(section_content, 2))
                prompt_parts.append(self._indent("}", 1))
                prompt_parts.append("")

            elif scope == ComponentScope.CLASS_KNOWLEDGE_BASE:
                # knowledge_base { }
                section_content = self._build_knowledge_base_section(scope_components)
                prompt_parts.append(self._indent("knowledge_base {", 1))
                prompt_parts.append(self._indent(section_content, 2))
                prompt_parts.append(self._indent("}", 1))
                prompt_parts.append("")

            elif scope == ComponentScope.CLASS_PROTOCOLS:
                # protocols { }
                section_content = self._build_protocols_section(scope_components)
                prompt_parts.append(self._indent("protocols {", 1))
                prompt_parts.append(self._indent(section_content, 2))
                prompt_parts.append(self._indent("}", 1))
                prompt_parts.append("")

            elif scope == ComponentScope.CLASS_RUNTIME_RULES:
                # runtime_rules { }
                section_content = self._build_runtime_rules_section(scope_components)
                prompt_parts.append(self._indent("runtime_rules {", 1))
                prompt_parts.append(self._indent(section_content, 2))
                prompt_parts.append(self._indent("}", 1))
                prompt_parts.append("")

        prompt_parts.append("}")
        
        assembled = "\n".join(prompt_parts)
        
        # Validate result
        self.validate(assembled)
        
        logger.info(f"✅ Assembled prompt from {len(components)} components ({len(assembled)} chars)")
        
        return assembled
    
    def validate(self, prompt: str) -> bool:
        """
        Validate assembled prompt structure.
        
        Args:
            prompt: Assembled prompt string
            
        Returns:
            True if valid
            
        Raises:
            AssemblyError: If validation fails
        """
        # Basic validation checks
        if not prompt.strip():
            raise AssemblyError("Assembled prompt is empty")
        
        if "class Alek {" not in prompt:
            raise AssemblyError("Missing 'class Alek' declaration")
        
        # Check balanced braces
        open_braces = prompt.count("{")
        close_braces = prompt.count("}")
        
        if open_braces != close_braces:
            raise AssemblyError(
                f"Unbalanced braces: {open_braces} open, {close_braces} close"
            )
        
        return True
    
    def _group_by_scope(self, components: List[PromptComponent]) -> dict:
        """Group components by their scope."""
        grouped = {}
        for component in components:
            if component.scope not in grouped:
                grouped[component.scope] = []
            grouped[component.scope].append(component)
        
        # Sort each group by order
        for scope in grouped:
            grouped[scope].sort(key=lambda c: c.order)
        
        return grouped
    
    def _build_properties_section(self, components: List[PromptComponent]) -> str:
        """Build properties { } section content."""
        parts = []
        for comp in components:
            # Components in properties are typically simple assignments or blocks
            # Content already includes the component structure (e.g., "archetype: ...")
            parts.append(comp.content)
        return "\n\n".join(parts)
    
    def _build_policies_section(self, components: List[PromptComponent]) -> str:
        """Build policies { } section content."""
        parts = []
        for comp in components:
            # Policies are rule blocks
            parts.append(comp.content)
        return "\n\n".join(parts)
    
    def _build_knowledge_base_section(self, components: List[PromptComponent]) -> str:
        """Build knowledge_base { } section content."""
        parts = []
        for comp in components:
            parts.append(comp.content)
        return "\n\n".join(parts)
    
    def _build_protocols_section(self, components: List[PromptComponent]) -> str:
        """Build protocols { } section content."""
        parts = []
        for comp in components:
            parts.append(comp.content)
        return "\n\n".join(parts)
    
    def _build_runtime_rules_section(self, components: List[PromptComponent]) -> str:
        """Build runtime_rules { } section content."""
        parts = []
        for comp in components:
            parts.append(comp.content)
        return "\n\n".join(parts)
    
    def _indent(self, text: str, level: int) -> str:
        """Indent text by specified number of levels (4 spaces per level)."""
        if not text:
            return ""
        
        indent = "    " * level
        lines = text.split("\n")
        return "\n".join(indent + line if line.strip() else "" for line in lines)
