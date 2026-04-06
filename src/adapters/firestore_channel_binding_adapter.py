"""
FirestoreChannelBindingAdapter — persists channel-to-agent bindings.

Collection: {env_prefix}channel_bindings  (doc ID = channel_id)
"""

from typing import Optional

from ..config.environment import EnvironmentConfig
from ..domain.channel_binding import ChannelBinding
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
        )

    async def save(self, binding: ChannelBinding) -> None:
        await self._collection.document(binding.channel_id).set({
            "channel_id": binding.channel_id,
            "agent_type": binding.agent_type,
            "intent": binding.intent,
            "created_by": binding.created_by,
        })

    async def delete(self, channel_id: str) -> None:
        await self._collection.document(channel_id).delete()
