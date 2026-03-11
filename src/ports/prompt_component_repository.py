"""
Port for prompt component storage.

Part of hexagonal architecture:
- Abstract interface for component persistence
- Storage-agnostic (Firestore, YAML, DB, etc.)
- Enables testing with mock implementations

Session: 23 (Prompt Component Architecture Implementation)
RFC: docs/architecture/rfcs/PROMPT_COMPONENT_ARCHITECTURE_RFC.md
"""

from abc import ABC, abstractmethod
from typing import List, Optional
from ..domain.prompt import PromptComponent, ComponentScope


class PromptComponentRepository(ABC):
    """
    Port for loading/storing prompt components.
    
    Allows different storage backends:
    - Firestore (current implementation)
    - YAML files (future)
    - PostgreSQL (future)
    - In-memory (testing)
    """
    
    @abstractmethod
    async def get_default_components(self, scope: Optional[ComponentScope] = None) -> List[PromptComponent]:
        """
        Load all system default components.
        
        Args:
            scope: Optional filter by component scope
        
        Returns:
            List of default PromptComponent objects, sorted by order
            
        Example:
            components = await repo.get_default_components()
            # Returns: [cognitive_process, humor_engine, behavior_guide, ...]
            
            kb_components = await repo.get_default_components(scope=ComponentScope.CLASS_KNOWLEDGE_BASE)
            # Returns: only knowledge_base components
        """
        pass
    
    @abstractmethod
    async def get_user_overrides(self, user_id: str) -> List[PromptComponent]:
        """
        Load user-specific component overrides.
        
        Args:
            user_id: User identifier
            
        Returns:
            List of user's custom PromptComponent objects
            
        Example:
            overrides = await repo.get_user_overrides("user123")
            # Returns: [custom_humor_engine] (user disabled humor)
        """
        pass
    
    @abstractmethod
    async def save_user_override(
        self, 
        user_id: str, 
        component: PromptComponent
    ) -> None:
        """
        Save user custom component.
        
        Args:
            user_id: User identifier
            component: Custom component to save
            
        Raises:
            ValueError: If component validation fails
            
        Example:
            custom = PromptComponent(
                id="humor_engine",
                scope=ComponentScope.CLASS_PROPERTIES,
                content='humor_engine { status: "DISABLED" }',
                order=20,
                is_user_override=True
            )
            await repo.save_user_override("user123", custom)
        """
        pass
    
    @abstractmethod
    async def delete_user_override(
        self, 
        user_id: str, 
        component_id: str
    ) -> None:
        """
        Delete user override, reverting to default.
        
        Args:
            user_id: User identifier
            component_id: Component to reset (e.g., "humor_engine")
            
        Example:
            await repo.delete_user_override("user123", "humor_engine")
            # User will now use system default humor_engine
        """
        pass
    
    @abstractmethod
    async def resolve_component(
        self,
        component_id: str,
        agent_type: str,
        user_id: Optional[str] = None
    ) -> Optional[PromptComponent]:
        """
        Resolve component using 3-level priority: USER > AGENT > SYSTEM.
        
        Resolution logic:
        1. Try USER level (if user_id provided)
           - If is_enabled=False → return None (EXCLUDED)
           - If text="" → fallthrough to AGENT
           - Else → return component
        2. Try AGENT level (agent_type)
           - Same logic as USER
        3. Try SYSTEM level
           - Same logic
        4. If nothing found → return None
        
        Args:
            component_id: Component identifier (e.g., "cognitive_process")
            agent_type: Agent type (e.g., "quick", "smart", "router")
            user_id: Optional user identifier
            
        Returns:
            Resolved PromptComponent or None if excluded/not found
            
        Example:
            component = await repo.resolve_component(
                component_id="humor_engine",
                agent_type="quick",
                user_id="user123"
            )
            # Returns: USER override if exists, else AGENT, else SYSTEM
        """
        pass
