import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from google.api_core.exceptions import AlreadyExists, FailedPrecondition, NotFound
from google.cloud.firestore_v1.async_client import AsyncClient

from ..ports.task_claim_store import TaskClaimStore
from ..utils.logger import logger

_PROCESS_ID = uuid.uuid4().hex  # one id per process, by construction
RUNNING = "running"
DONE = "done"


class FirestoreTaskClaimStore(TaskClaimStore):
    """Claim docs `{holder, attempt, state, created_at, expires_at}`; expires_at is also the Firestore TTL field.

    Collection `{prefix}worker_task_dedup`, shared with the DedupStore marks. A claim is a `create`, else a rewrite under
    a `last_update_time` precondition; mark_done and release are preconditioned on the holder's read. A naive
    `expires_at` is read as UTC (defensive: the client returns aware values).
    """

    def __init__(self, db_client: AsyncClient, collection_name: str, ttl_seconds: int = 3600):
        self.db_client = db_client
        self.collection_name = collection_name
        self.ttl_seconds = ttl_seconds
        self.holder = _PROCESS_ID

    def _doc(self, key: str) -> Any:
        return self.db_client.collection(self.collection_name).document(key)

    def _fresh(self, attempt: str, state: str = RUNNING) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "holder": self.holder,
            "attempt": attempt,
            "state": state,
            "created_at": now,
            "expires_at": now + timedelta(seconds=self.ttl_seconds),
        }

    @staticmethod
    def _expired(data: dict) -> bool:
        expires_at = data.get("expires_at")
        if not isinstance(expires_at, datetime):
            return True
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= expires_at

    async def try_claim(self, key: str, attempt: str) -> bool:
        return await self._claim(key, attempt, retry_create=True)

    async def _claim(self, key: str, attempt: str, retry_create: bool) -> bool:
        ref = self._doc(key)
        try:
            await ref.create(self._fresh(attempt))
            return True
        except AlreadyExists:
            logger.debug("[TaskClaim] claim exists, checking holder: task=%s", key)
        snap = await ref.get()
        if not snap.exists:  # released between our create and get
            return await self._claim(key, attempt, False) if retry_create else True  # fail open: no holder to defer to
        data = snap.to_dict() or {}
        if not self._expired(data):
            if data.get("attempt") == attempt:
                return True  # our own create, retried by the SDK after it landed
            if data.get("state") == DONE:
                return False
        try:
            await ref.update(
                self._fresh(attempt),
                option=self.db_client.write_option(last_update_time=snap.update_time),
            )
        except (FailedPrecondition, NotFound):
            # Our update may have landed and been retried by the SDK: re-read before giving way.
            return await self._after_lost_update(key, attempt, retry_create)
        logger.info("[TaskClaim] took over a dead or expired claim: task=%s", key)
        return True

    async def _after_lost_update(self, key: str, attempt: str, retry_create: bool) -> bool:
        snap = await self._doc(key).get()
        if not snap.exists:
            return await self._claim(key, attempt, False) if retry_create else True  # fail open: no holder to defer to
        return (snap.to_dict() or {}).get("attempt") == attempt

    async def _own(self, key: str, attempt: str) -> Optional[Any]:
        snap = await self._doc(key).get()
        if not snap.exists or (snap.to_dict() or {}).get("attempt") != attempt:
            return None
        return snap

    async def mark_done(self, key: str, attempt: str) -> None:
        snap = await self._own(key, attempt)
        if snap is None:
            return
        try:
            await self._doc(key).update(
                self._fresh(attempt, DONE),
                option=self.db_client.write_option(last_update_time=snap.update_time),
            )
        except (FailedPrecondition, NotFound):
            logger.info("[TaskClaim] claim changed hands before mark_done: task=%s", key)

    async def release(self, key: str, attempt: str) -> None:
        snap = await self._own(key, attempt)
        if snap is None:
            return
        try:
            await self._doc(key).delete(
                option=self.db_client.write_option(last_update_time=snap.update_time)
            )
        except (FailedPrecondition, NotFound):
            logger.info("[TaskClaim] claim changed hands before release: task=%s", key)
