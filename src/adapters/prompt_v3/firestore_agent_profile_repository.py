"""
FirestoreAgentProfileRepository — Firestore adapter for agent profile storage.

Part of Prompt Design System v4 (RFC: docs/10_rfcs/PROMPT_BUILDER_V4_RFC.md).
"""

import logging
from typing import Dict, Optional, Set

from google.cloud import firestore

from src.ports.prompt_v3.agent_profile_repository import AgentProfileRepository
from src.domain.prompt_v3.slot import OwnerType
from src.domain.prompt_v3.profile_slot import ProfileToken
from src.domain.prompt_v3.agent_profile import AgentProfile

logger = logging.getLogger(__name__)


class FirestoreAgentProfileRepository(AgentProfileRepository):
    """Firestore adapter for agent profile and override storage.

    Collections:
        profiles_collection  — agent base profiles (AGENT level)
            Document ID: {agent_id}  (e.g. "quick", "router")
        overrides_collection — account/user overrides (ACCOUNT/USER levels)
            Document ID: {OWNER_TYPE}_{owner_id}
                         (e.g. "ACCOUNT_acc_123", "USER_user_456")

    Data Model (profiles):
        {
            "blueprint_id": "universal_agent_v1",
            "agent_id": "quick",
            "tokens": {
                "COGNITIVE_PROCESS_QUICK":        {"order": 10, "non_overridable": true},
                "HUMOR_PRESET_LIGHT":             {"order": 40},
                "VOICE_CONVERSATIONAL":           {"order": 60}
            }
        }

    Data Model (overrides):
        {
            "owner_type": "ACCOUNT",
            "owner_id": "acc_123",
            "tokens": {
                "VOICE_CONVERSATIONAL": {"order": 60}
            }
        }
    """

    def __init__(
        self,
        db: firestore.Client,
        profiles_collection: str,
        overrides_collection: str,
    ):
        self.db = db
        self.profiles_collection = profiles_collection
        self.overrides_collection = overrides_collection

    async def get_agent_profile(
        self,
        agent_id: str,
    ) -> AgentProfile:
        """Get agent profile (blueprint_id + token map) from profiles collection."""
        doc_ref = self.db.collection(self.profiles_collection).document(agent_id)
        doc = await doc_ref.get()

        if not doc.exists:
            logger.debug(f"Agent profile not found: {agent_id}, returning empty profile")
            return AgentProfile(
                blueprint_id=f"{agent_id}_agent_v1",
                tokens={},
            )

        data = doc.to_dict()
        blueprint_id = data.get("blueprint_id", f"{agent_id}_agent_v1")
        tokens_raw = data.get("tokens", {})

        tokens: Dict[str, ProfileToken] = {}
        for token_id, slot_data in tokens_raw.items():
            if not isinstance(slot_data, dict):
                logger.warning(f"Unexpected token entry for {token_id}: {slot_data}, skipping")
                continue
            tokens[token_id] = ProfileToken.from_dict(token_id, slot_data)

        logger.debug(f"Loaded profile for {agent_id}: blueprint={blueprint_id}, tokens={len(tokens)}")
        return AgentProfile(blueprint_id=blueprint_id, tokens=tokens)

    async def get_override_tokens(
        self,
        owner_type: OwnerType,
        owner_id: str,
    ) -> Dict[str, ProfileToken]:
        """Get account or user override tokens from overrides collection."""
        override_id = f"{owner_type.value.upper()}_{owner_id}"
        doc_ref = self.db.collection(self.overrides_collection).document(override_id)
        doc = await doc_ref.get()

        if not doc.exists:
            logger.debug(f"No overrides found: {override_id}")
            return {}

        data = doc.to_dict()
        tokens_raw = data.get("tokens", {})

        result = {}
        for token_id, slot_data in tokens_raw.items():
            if not isinstance(slot_data, dict):
                logger.warning(f"Unexpected override entry for {token_id}: {slot_data}, skipping")
                continue
            result[token_id] = ProfileToken.from_dict(token_id, slot_data)

        logger.debug(f"Loaded {len(result)} override tokens from {override_id}")
        return result

    async def set_override_tokens(
        self,
        owner_type: OwnerType,
        owner_id: str,
        tokens: Dict[str, ProfileToken],
        clear_ids: Optional[Set[str]] = None,
    ) -> None:
        """Upsert tokens into override document, optionally clearing others atomically."""
        override_id = f"{owner_type.value.upper()}_{owner_id}"
        doc_ref = self.db.collection(self.overrides_collection).document(override_id)

        doc = await doc_ref.get()
        existing_tokens = doc.to_dict().get("tokens", {}) if doc.exists else {}

        if clear_ids:
            for tid in clear_ids:
                existing_tokens.pop(tid, None)

        for tid, pt in tokens.items():
            existing_tokens[tid] = {"order": pt.order, "non_overridable": pt.non_overridable}

        await doc_ref.set({
            "owner_type": owner_type.value.upper(),
            "owner_id": owner_id,
            "tokens": existing_tokens,
        })
        logger.debug(
            f"set_override_tokens: {override_id} upserted={list(tokens)} "
            f"cleared={list(clear_ids or [])}"
        )

    async def delete_profile(
        self,
        owner_type: OwnerType,
        owner_value: str,
    ) -> None:
        """Delete a profile or override document."""
        if owner_type == OwnerType.AGENT:
            profile_id = owner_value
            collection = self.profiles_collection
        else:
            profile_id = f"{owner_type.value.upper()}_{owner_value}"
            collection = self.overrides_collection

        doc_ref = self.db.collection(collection).document(profile_id)
        doc = await doc_ref.get()

        if not doc.exists:
            logger.warning(f"Profile not found for deletion: {profile_id} in {collection}")
            return

        await doc_ref.delete()
        logger.info(f"Deleted profile: {profile_id} from {collection}")
