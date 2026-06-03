"""
XML Prompt Assembler for Claude-optimized prompts.

Assembles prompt components into XML format without class wrapper.
Used for agents that require XML structured instructions (e.g., consolidation agent).

Part of hexagonal architecture:
- Adapter layer (output port implementation)
- Implements PromptAssembler interface
- XML-specific assembly logic

Session: 26 (Variable Formatting System)
"""

from typing import List, Dict
from ..domain.prompt import PromptComponent, PromptTemplate


class XmlPromptAssembler:
    """
    Assembles prompt components into XML format.

    Unlike GroovyPromptAssembler, this assembler:
    - Does NOT add class wrapper (no "class Alek extends Agent")
    - Simply concatenates XML blocks
    - Preserves XML structure from components
    """

    def assemble(
        self,
        template: PromptTemplate,
        components: List[PromptComponent],
        runtime_data: Dict[str, str] = None
    ) -> str:
        """
        Assemble components into XML format.

        Args:
            template: Template defining structure (name/extends ignored for XML)
            components: List of components to assemble (already filtered and sorted)
            runtime_data: Optional runtime data (not used in XML assembler)

        Returns:
            Assembled XML prompt string
        """
        if not components:
            return "<agent_instructions />"

        # Group components by scope
        components_by_scope = {}
        for component in components:
            if component.scope not in components_by_scope:
                components_by_scope[component.scope] = []
            components_by_scope[component.scope].append(component)

        # Sort components within each scope by order
        for scope_components in components_by_scope.values():
            scope_components.sort(key=lambda c: c.order)

        # Build XML by iterating through template scopes in order
        xml_parts = []

        # Optional: Add root wrapper (can be controlled by template later)
        xml_parts.append("<agent_instructions>")
        xml_parts.append("")

        for scope in template.scopes:
            scope_components = components_by_scope.get(scope, [])
            if not scope_components:
                continue

            # For XML, just concatenate component content
            # Components should already be valid XML blocks
            for component in scope_components:
                if component.content.strip():
                    # Indent component content for readability
                    indented = self._indent(component.content, 1)
                    xml_parts.append(indented)
                    xml_parts.append("")  # Blank line between components

        xml_parts.append("</agent_instructions>")

        return "\n".join(xml_parts)

    def _indent(self, text: str, level: int) -> str:
        """
        Indent text block by specified level.

        Args:
            text: Text to indent
            level: Indentation level (spaces = level * 2)

        Returns:
            Indented text
        """
        indent_str = "  " * level
        lines = text.split('\n')
        return '\n'.join(indent_str + line if line.strip() else line for line in lines)
