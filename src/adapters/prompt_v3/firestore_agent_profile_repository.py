"""
FirestoreAgentProfileRepository - Firestore adapter for agent profile storage.

Implements 4-level priority resolution: USER > ACCOUNT > AGENT > SYSTEM

Part of Prompt Design System v3 (RFC).
"""

import logging
from typing import Dict, Optional, List, Union

from google.cloud import firestore

from src.ports.prompt_v3.agent_profile_repository import AgentProfileRepository
from src.domain.prompt_v3.slot import OwnerType
from src.domain.prompt_v3.token import TokenId
from src.domain.prompt_v3.profile_slot import ProfileSlot

logger = logging.getLogger(__name__)


class FirestoreAgentProfileRepository(AgentProfileRepository):
    """Firestore adapter for agent profile storage with dual-collection architecture.

    Collections (Phase 5-1):
        1. agent_profiles: SYSTEM/AGENT level profiles (admin-controlled)
        2. user_token_overrides: USER/ACCOUNT level overrides (user-modifiable)

    Data Model (agent_profiles):
        {
            "profile_id": "system_smart",  # Simplified: {owner_type}_{owner_value}
            "blueprint_id": "universal_agent_v1",
            "owner_type": "SYSTEM",
            "owner_value": "smart",  # Agent type
            "slots": [...]
        }

    Data Model (user_token_overrides):
        {
            "override_id": "user_user_123",  # {owner_type}_{owner_value}
            "owner_type": "USER",
            "owner_value": "user_123",
            "slots": [...]
        }

    Resolution Priority: USER > ACCOUNT > AGENT > SYSTEM

    Examples:
        >>> from google.cloud import firestore
        >>> db = firestore.Client()
        >>> repo = FirestoreAgentProfileRepository(
        ...     db,
        ...     profiles_collection="dev_agent_profiles",
        ...     overrides_collection="dev_user_token_overrides"
        ... )
        >>>
        >>> # Resolve with 4-level priority
        >>> slots = await repo.get_profile_slots(
        ...     blueprint_id="universal_agent_v1",
        ...     owner_type=OwnerType.SYSTEM,
        ...     owner_value="smart"
        ... )
    """

    def __init__(
        self,
        db: firestore.Client,
        profiles_collection: str,
        overrides_collection: str
    ):
        """Initialize Firestore agent profile repository with dual-collection support.

        Args:
            db: Firestore client instance
            profiles_collection: Agent profiles collection (SYSTEM/AGENT levels)
            overrides_collection: User overrides collection (USER/ACCOUNT levels)
        """
        self.db = db
        self.profiles_collection = profiles_collection
        self.overrides_collection = overrides_collection

    def _make_profile_id(
        self,
        blueprint_id: str,
        owner_type: OwnerType,
        owner_value: str
    ) -> str:
        """Generate profile/override ID.

        Format: {blueprint_id}_{owner_type}_{owner_value}
        Matches format used by create_default_profiles.py

        Examples:
            >>> repo._make_profile_id("universal_agent_v1", OwnerType.SYSTEM, "smart")
            "universal_agent_v1_SYSTEM_smart"
            >>> repo._make_profile_id("universal_agent_v1", OwnerType.USER, "user_123")
            "universal_agent_v1_USER_user_123"
        """
        return f"{blueprint_id}_{owner_type.value.upper()}_{owner_value}"

    def _get_collection(self, owner_type: OwnerType) -> str:
        """Route to correct collection based on owner_type.

        Args:
            owner_type: SYSTEM/AGENT → profiles, USER/ACCOUNT → overrides

        Returns:
            Collection name
        """
        if owner_type in [OwnerType.SYSTEM, OwnerType.AGENT]:
            return self.profiles_collection
        else:  # USER, ACCOUNT
            return self.overrides_collection

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
        profile_id = self._make_profile_id(blueprint_id, owner_type, owner_value)
        collection_name = self._get_collection(owner_type)
        doc_ref = self.db.collection(collection_name).document(profile_id)
        doc = await doc_ref.get()

        if not doc.exists:
            logger.debug(f"Profile not found: {profile_id} in {collection_name}")
            return []

        data = doc.to_dict()
        raw_slots = data.get("slots", [])

        slots: List[ProfileSlot] = []
        for item in raw_slots:
            if isinstance(item, dict):
                slots.append(ProfileSlot.from_dict(item))
            else:
                logger.warning(f"Unknown slot entry format: {item}, skipping")

        logger.info(f"Loaded {len(slots)} slots from {profile_id}")
        return slots

    async def delete_profile(
        self,
        blueprint_id: str,
        owner_type: OwnerType,
        owner_value: str
    ) -> None:
        """Delete agent profile or user override from appropriate collection.

        Args:
            blueprint_id: Blueprint identifier
            owner_type: Owner type (determines which collection)
            owner_value: Owner-specific value
        """
        profile_id = self._make_profile_id(blueprint_id, owner_type, owner_value)
        collection_name = self._get_collection(owner_type)
        doc_ref = self.db.collection(collection_name).document(profile_id)
        doc = await doc_ref.get()

        if not doc.exists:
            logger.warning(f"Profile not found for deletion in {collection_name}: {profile_id}")
            return

        doc_ref.delete()
        logger.info(f"Deleted from {collection_name}: {profile_id}")
