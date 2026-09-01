"""
FirestoreChannelBindingAdapter — persists channel-to-agent bindings.

Collection: {env_prefix}channel_bindings  (doc ID = channel_id)
"""

from typing import Optional

from ..config.environment import EnvironmentConfig
from ..domain.channel_binding import ChannelBinding
from ..domain.companion_config import CompanionConfig, CompanionTextMode
from ..domain.entities import FactDomain
from ..ports.channel_binding_port import ChannelBindingPort
from ..utils.logger import logger


class FirestoreChannelBindingAdapter(ChannelBindingPort):

    def __init__(self, db_client, env_config: EnvironmentConfig):
        self._collection = db_client.collection(
            f"{env_config.firestore_collection_prefix}channel_bindings"
        )
        logger.info(
            "🔗 ChannelBinding repository initialized: %schannel_bindings",
            env_config.firestore_collection_prefix,
        )

    async def get(self, channel_id: str) -> Optional[ChannelBinding]:
        doc = await self._collection.document(channel_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        return ChannelBinding(
            channel_id=data["channel_id"],
            agent_type=data["agent_type"],
            intent=data["intent"],
            created_by=data["created_by"],
            companion_config=self._deserialize_companion_config(data.get("companion_config")),
        )

    async def save(self, binding: ChannelBinding) -> None:
        await self._collection.document(binding.channel_id).set({
            "channel_id": binding.channel_id,
            "agent_type": binding.agent_type,
            "intent": binding.intent,
            "created_by": binding.created_by,
            "companion_config": self._serialize_companion_config(binding.companion_config),
        })

    async def delete(self, channel_id: str) -> None:
        await self._collection.document(channel_id).delete()

    @staticmethod
    def _deserialize_companion_config(data: Optional[dict]) -> Optional[CompanionConfig]:
        if data is None:
            return None
        # window_threshold/batch_size have no dataclass default — genuinely required,
        # stay strict. Every other field has a CompanionConfig default, so a partial/
        # hand-written Firestore doc falls back instead of raising KeyError outside
        # handle_message's try/except (Important #2, final whole-branch review 2026-08-31).
        return CompanionConfig(
            window_threshold=data["window_threshold"],
            batch_size=data["batch_size"],
            text_mode=CompanionTextMode(data.get("text_mode", CompanionTextMode.SUMMARY.value)),
            include_biographical=data.get("include_biographical", False),
            session_domains=[FactDomain(d) for d in data.get("session_domains", [])],
            include_standing_directives=data.get("include_standing_directives", False),
            include_own_records=data.get("include_own_records", True),
            history_recent_full_turns=data.get("history_recent_full_turns", 5),
        )

    @staticmethod
    def _serialize_companion_config(config: Optional[CompanionConfig]) -> Optional[dict]:
        if config is None:
            return None
        return {
            "window_threshold": config.window_threshold,
            "batch_size": config.batch_size,
            "text_mode": config.text_mode.value,
            "include_biographical": config.include_biographical,
            "session_domains": [d.value for d in config.session_domains],
            "include_standing_directives": config.include_standing_directives,
            "include_own_records": config.include_own_records,
            "history_recent_full_turns": config.history_recent_full_turns,
        }
