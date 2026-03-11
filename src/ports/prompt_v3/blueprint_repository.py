"""
BlueprintRepository — port interface for blueprint storage.

Part of Prompt Design System v4 (RFC: docs/10_rfcs/PROMPT_BUILDER_V4_RFC.md).
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from src.domain.prompt_v3.blueprint import Blueprint


class BlueprintRepository(ABC):
    """Port interface for blueprint storage (hexagonal architecture).

    Blueprints define prompt templates with class schemas.
    One blueprint per agent type (e.g., "smart_agent_v1", "quick_agent_v1").

    Implementations:
        - FirestoreBlueprintRepository: Firestore adapter (Phase 2)

    Examples:
        >>> repo = FirestoreBlueprintRepository(db, "development_domain_prompt_blueprints_v3")
        >>> blueprint = await repo.get("universal_agent_v1")
        >>> blueprint.class_order
        ['properties', 'cognitive_process', ...]
    """

    @abstractmethod
    async def get(self, blueprint_id: str) -> Blueprint:
        """Fetch blueprint by ID.

        Args:
            blueprint_id: Blueprint identifier (e.g., "smart_agent_v1")

        Returns:
            Blueprint instance

        Raises:
            KeyError: If blueprint not found

        Examples:
            >>> blueprint = await repo.get("smart_agent_v1")
            >>> assert blueprint.id == "smart_agent_v1"
        """
        pass

    @abstractmethod
    async def list_all(self) -> List[Blueprint]:
        """List all blueprints.

        Returns:
            List of all blueprints

        Examples:
            >>> blueprints = await repo.list_all()
            >>> agent_types = [b.id for b in blueprints]
        """
        pass

    @abstractmethod
    async def save(self, blueprint: Blueprint) -> None:
        """Save blueprint to repository.

        Args:
            blueprint: Blueprint instance to save

        Examples:
            >>> blueprint = Blueprint(...)
            >>> await repo.save(blueprint)
        """
        pass

    @abstractmethod
    async def delete(self, blueprint_id: str) -> None:
        """Delete blueprint from repository.

        Args:
            blueprint_id: Blueprint ID to delete

        Raises:
            KeyError: If blueprint not found

        Examples:
            >>> await repo.delete("old_agent_v1")
        """
        pass

    @abstractmethod
    async def exists(self, blueprint_id: str) -> bool:
        """Check if blueprint exists.

        Args:
            blueprint_id: Blueprint ID to check

        Returns:
            True if blueprint exists, False otherwise

        Examples:
            >>> if await repo.exists("smart_agent_v1"):
            ...     blueprint = await repo.get("smart_agent_v1")
        """
        pass
