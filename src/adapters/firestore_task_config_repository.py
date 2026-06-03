"""
FirestoreTaskConfigRepository — implements TaskConfigPort.

Collection: {env}_task_config (doc ID = user_id)
Stores per-user tasks integration config: primary_list_id + active subscriptions.

set_primary_list_id_if_absent: Firestore transaction for safe concurrent writes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from google.cloud import firestore

from ..config.environment import EnvironmentConfig
from ..domain.task import TaskSubscriptionConfig, TaskUserConfig
from ..ports.task_config_port import TaskConfigPort
from ..utils.logger import logger


class FirestoreTaskConfigRepository(TaskConfigPort):

    def __init__(self, db_client, env_config: EnvironmentConfig) -> None:
        self.db = db_client
        col = env_config.task_config_collection
        self.collection = self.db.collection(col)
        logger.info(f"📋 FirestoreTaskConfigRepository initialized (collection: {col})")

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_tz(dt) -> Optional[datetime]:
        if dt is None:
            return None
        if isinstance(dt, datetime):
            return datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.microsecond)
        return dt

    @staticmethod
    def _sub_to_dict(sub: TaskSubscriptionConfig) -> Dict[str, Any]:
        return {
            "sub_id": sub.sub_id,
            "list_id": sub.list_id,
            "expires_at": sub.expires_at,
        }

    @staticmethod
    def _sub_from_dict(data: Dict[str, Any]) -> TaskSubscriptionConfig:
        expires_at = data.get("expires_at")
        if isinstance(expires_at, datetime):
            expires_at = datetime(
                expires_at.year, expires_at.month, expires_at.day,
                expires_at.hour, expires_at.minute, expires_at.second,
                expires_at.microsecond,
            )
        return TaskSubscriptionConfig(
            sub_id=data["sub_id"],
            list_id=data["list_id"],
            expires_at=expires_at or datetime.now(timezone.utc),
        )

    def _to_domain(self, data: Dict[str, Any]) -> TaskUserConfig:
        subs = [
            self._sub_from_dict(s)
            for s in data.get("subscriptions", [])
        ]
        return TaskUserConfig(
            primary_list_id=data.get("primary_list_id"),
            subscriptions=subs,
        )

    def _from_domain(self, config: TaskUserConfig) -> Dict[str, Any]:
        return {
            "primary_list_id": config.primary_list_id,
            "subscriptions": [self._sub_to_dict(s) for s in config.subscriptions],
        }

    # ------------------------------------------------------------------
    # TaskConfigPort — get_config
    # ------------------------------------------------------------------

    async def get_config(self, user_id: str) -> TaskUserConfig:
        """Load user's task config. Returns empty TaskUserConfig if not found."""
        doc = await self.collection.document(user_id).get()
        if not doc.exists:
            return TaskUserConfig()
        return self._to_domain(doc.to_dict())

    # ------------------------------------------------------------------
    # TaskConfigPort — save_config
    # ------------------------------------------------------------------

    async def save_config(self, user_id: str, config: TaskUserConfig) -> None:
        """Overwrite user's task config (full document set)."""
        data = self._from_domain(config)
        await self.collection.document(user_id).set(data)
        logger.debug(f"📋 TaskConfig saved for user {user_id[:8]}")

    # ------------------------------------------------------------------
    # TaskConfigPort — set_primary_list_id_if_absent
    # ------------------------------------------------------------------

    async def set_primary_list_id_if_absent(self, user_id: str, list_id: str) -> str:
        """
        Atomic create-if-not-exists for primary_list_id via Firestore transaction.
        If already set: returns existing value unchanged.
        If not set: writes list_id and returns it.
        """
        doc_ref = self.collection.document(user_id)
        transaction = self.db.transaction()

        @firestore.async_transactional
        async def _txn(transaction):
            snapshot = await doc_ref.get(transaction=transaction)
            if snapshot.exists:
                existing = snapshot.to_dict().get("primary_list_id")
                if existing:
                    return existing
            # Set (merge=True preserves subscriptions if doc already exists without primary_list_id)
            transaction.set(doc_ref, {"primary_list_id": list_id}, merge=True)
            return list_id

        result = await _txn(transaction)
        logger.info(f"📋 TaskConfig primary_list_id for user {user_id[:8]}: {result[:8]}")
        return result
