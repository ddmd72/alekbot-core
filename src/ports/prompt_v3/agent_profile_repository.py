"""
AgentProfileRepository - Port interface for agent profile storage.

Agent profiles store slot assignments with 4-level priority:
USER > ACCOUNT > AGENT > SYSTEM

Part of Prompt Design System v3 (RFC).
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional, List

from src.domain.prompt_v3.slot import OwnerType
from src.domain.prompt_v3.token import TokenId
from src.domain.prompt_v3.profile_slot import ProfileSlot


class AgentProfileRepository(ABC):
    """Port interface for agent profile storage (hexagonal architecture).

    Agent profiles map slots to tokens with 4-level priority hierarchy.
    Resolution order: USER > ACCOUNT > AGENT > SYSTEM

    Implementations:
        - FirestoreAgentProfileRepository: Firestore adapter (Phase 2)

    Data Model:
        {
            "owner_type": "USER",  # SYSTEM / AGENT / ACCOUNT / USER
            "owner_value": "user_123",  # Agent type, account ID, or user ID
            "blueprint_id": "smart_agent_v1",
            "slots": [
                {"type": "class", "value": "properties", "non_overridable": False},
                {"type": "token", "value": "HUMOR_PRESET_OFF", "non_overridable": False},
                {"type": "token", "value": "POLICY_OUTPUT_LANGUAGE", "non_overridable": True}
            ]
        }

    Examples:
        >>> repo = FirestoreAgentProfileRepository(
        ...     db,
        ...     profiles_collection="dev_prompt_agent_profiles",
        ...     overrides_collection="dev_prompt_user_token_overrides"
        ... )
    """

    @abstractmethod
    async def get_profile_slots(
        self,
        blueprint_id: str,
        owner_type: OwnerType,
        owner_value: str
    ) -> List[ProfileSlot]:
        """Get unified slot entries for a profile.

        Args:
            blueprint_id: Blueprint identifier
            owner_type: Owner type
            owner_value: Owner-specific value

        Returns:
            List of ProfileSlot entries (empty if none)
        """
        pass

    @abstractmethod
    async def delete_profile(
        self,
        blueprint_id: str,
        owner_type: OwnerType,
        owner_value: str
    ) -> None:
        """Delete agent profile.

        Args:
            blueprint_id: Blueprint identifier
            owner_type: Owner type
            owner_value: Owner-specific value

        Examples:
            >>> await repo.delete_profile(
            ...     "smart_agent_v1",
            ...     OwnerType.USER,
            ...     "user_123"
            ... )
        """
        pass
