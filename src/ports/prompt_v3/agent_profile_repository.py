"""
AgentProfileRepository — port interface for agent profile and override storage.

Part of Prompt Design System v4 (RFC: docs/10_rfcs/PROMPT_BUILDER_V4_RFC.md).
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional, Set

from src.domain.prompt_v3.slot import OwnerType
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.agent_profile import AgentProfile


class AgentProfileRepository(ABC):
    """Port interface for agent profile and override storage.

    Agent profiles are stored with document ID = agent_id (e.g. "quick").
    The profile document declares which blueprint_id to use — the assembly
    service reads blueprint_id from the profile, not from hardcoded config.

    Override semantics: account/user tokens replace agent tokens matched
    by class + category. Cannot add tokens to classes the agent has not activated.
    non_overridable=True on an agent token blocks replacement.

    Examples:
        >>> profile = await repo.get_agent_profile("quick")
        >>> blueprint_id = profile.blueprint_id  # "universal_agent_v1"
        >>> account_tokens = await repo.get_override_tokens(OwnerType.ACCOUNT, "acc_123")
    """

    @abstractmethod
    async def get_agent_profile(
        self,
        agent_id: str,
    ) -> AgentProfile:
        """Get the agent's profile (blueprint_id + token map).

        Args:
            agent_id: Agent type identifier (e.g. "quick", "router")

        Returns:
            AgentProfile with blueprint_id and tokens dict.
            If no profile found, returns AgentProfile with derived blueprint_id
            and empty tokens.
        """
        pass

    @abstractmethod
    async def get_override_tokens(
        self,
        owner_type: OwnerType,
        owner_id: str,
    ) -> Dict[str, ProfileToken]:
        """Get account or user override tokens.

        Args:
            owner_type: OwnerType.ACCOUNT or OwnerType.USER
            owner_id: Account ID or user ID

        Returns:
            Dict mapping token_id -> ProfileToken. Empty dict if no overrides found.
        """
        pass

    @abstractmethod
    async def set_override_tokens(
        self,
        owner_type: OwnerType,
        owner_id: str,
        tokens: Dict[str, ProfileToken],
        clear_ids: Optional[Set[str]] = None,
    ) -> None:
        """Upsert tokens into an override document, optionally removing others atomically.

        Performs a read-modify-write: preserves existing tokens not in `tokens` or
        `clear_ids`. Safe to call on a non-existent document (creates it).

        Args:
            owner_type: OwnerType.ACCOUNT or OwnerType.USER
            owner_id: Account ID or user ID
            tokens: Token map to upsert {token_id: ProfileToken}
            clear_ids: Token IDs to remove from the override document before upserting.
                       Use to atomically swap one language token for another.
        """
        pass

    @abstractmethod
    async def delete_profile(
        self,
        owner_type: OwnerType,
        owner_value: str,
    ) -> None:
        """Delete a profile or override document.

        Args:
            owner_type: AGENT -> profiles collection; ACCOUNT/USER -> overrides collection
            owner_value: agent_id, account_id, or user_id
        """
        pass
