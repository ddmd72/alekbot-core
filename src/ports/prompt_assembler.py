"""
Port for prompt assembly.

Part of hexagonal architecture:
- Abstract interface for prompt assembly logic
- Format-agnostic (Groovy DSL, JSON, XML, etc.)
- Enables testing and alternative implementations

Session: 23 (Prompt Component Architecture Implementation)
RFC: docs/architecture/rfcs/PROMPT_COMPONENT_ARCHITECTURE_RFC.md
"""

from abc import ABC, abstractmethod
from typing import List, Dict
from ..domain.prompt import PromptTemplate, PromptComponent


class PromptAssembler(ABC):
    """
    Port for assembling prompts from components.
    
    Allows different output formats:
    - Groovy DSL (current - for LLM providers)
    - JSON (for API providers that prefer structured)
    - XML (for legacy systems)
    - Plain text (for simple agents)
    """
    
    @abstractmethod
    def assemble(
        self,
        template: PromptTemplate,
        components: List[PromptComponent],
        runtime_data: Dict[str, str]
    ) -> str:
        """
        Assemble final prompt from components and runtime data.
        
        Args:
            template: Template defining structure (TEMPLATE_LIGHT or TEMPLATE_FULL)
            components: List of components to assemble (defaults + user overrides merged)
            runtime_data: Dynamic data for {{PLACEHOLDER}} injection
                         Example: {"BIOGRAPHICAL_CONTEXT": "- User born 1972\n- Lives in Valencia"}
            
        Returns:
            Assembled prompt string (valid Groovy code for current implementation)
            
        Raises:
            AssemblyError: If assembly fails validation
            
        Example:
            prompt = assembler.assemble(
                template=TEMPLATE_LIGHT,
                components=[cognitive_process_comp, humor_engine_comp, ...],
                runtime_data={"BIOGRAPHICAL_CONTEXT": bio_text}
            )
            # Returns: "class Alek extends Agent { ... }"
        """
        pass
    
    @abstractmethod
    def validate(self, prompt: str) -> bool:
        """
        Validate assembled prompt structure.
        
        Args:
            prompt: Assembled prompt string
            
        Returns:
            True if valid, False otherwise
            
        Example:
            is_valid = assembler.validate(assembled_prompt)
            # Checks: balanced braces, class definition, etc.
        """
        pass


class AssemblyError(Exception):
    """
    Raised when prompt assembly fails.
    
    Examples:
        - Unbalanced braces in Groovy code
        - Missing required placeholders
        - Invalid component scope ordering
    """
    pass
