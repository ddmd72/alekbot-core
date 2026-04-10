"""
FirestoreNotificationStateAdapter — persists the last active messaging channel per user.
Collection: {env}_user_notification_state  (doc ID = user_id)
"""
from datetime import datetime, timezone
from typing import Optional

from ..config.environment import EnvironmentConfig
from ..domain.notification import NotificationChannel
from ..ports.notification_state_port import NotificationStatePort
from ..utils.logger import logger


class FirestoreNotificationStateAdapter(NotificationStatePort):

    def __init__(self, db_client, env_config: EnvironmentConfig):
        self.db = db_client
        self.collection = self.db.collection(env_config.user_notification_state_collection)
        self._primary_collection = self.db.collection(
            f"{env_config.firestore_collection_prefix}user_primary_channel"
        )
        logger.info(
            f"📬 NotificationState repository initialized: {env_config.user_notification_state_collection}"
        )

    async def save(self, user_id: str, platform: str, channel_id: str) -> None:
        await self.collection.document(user_id).set({
            "user_id": user_id,
            "platform": platform,
            "channel_id": channel_id,
            "updated_at": datetime.now(timezone.utc),
        })

    async def get(self, user_id: str) -> Optional[NotificationChannel]:
        doc = await self.collection.document(user_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        updated_at = data.get("updated_at")
        if updated_at and hasattr(updated_at, "replace"):
            updated_at = datetime(
                updated_at.year, updated_at.month, updated_at.day,
                updated_at.hour, updated_at.minute, updated_at.second,
                updated_at.microsecond,
            )
        return NotificationChannel(
            user_id=data["user_id"],
            platform=data["platform"],
            channel_id=data["channel_id"],
            updated_at=updated_at or datetime.now(timezone.utc),
        )

    async def save_primary(self, user_id: str, platform: str, channel_id: str) -> None:
        await self._primary_collection.document(user_id).set({
            "user_id": user_id,
            "platform": platform,
            "channel_id": channel_id,
            "updated_at": datetime.now(timezone.utc),
        })

    async def get_primary(self, user_id: str) -> Optional[NotificationChannel]:
        doc = await self._primary_collection.document(user_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        updated_at = data.get("updated_at")
        if updated_at and hasattr(updated_at, "replace"):
            updated_at = datetime(
                updated_at.year, updated_at.month, updated_at.day,
                updated_at.hour, updated_at.minute, updated_at.second,
                updated_at.microsecond,
            )
        return NotificationChannel(
            user_id=data["user_id"],
            platform=data["platform"],
            channel_id=data["channel_id"],
            updated_at=updated_at or datetime.now(timezone.utc),
        )
