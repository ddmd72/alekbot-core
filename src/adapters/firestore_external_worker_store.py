"""Firestore storage of external workers and their keys (EXTERNAL_TASK_QUEUE_RFC §5.1, §5.6).

Collections (`EnvironmentConfig`): `{prefix}external_workers` (doc id `{user_id}:{worker_id}`; every `ExternalWorker`
field, enums as values) and `{prefix}external_worker_keys` (doc id = the key's keyed digest; every `ExternalWorkerKey`
field but the digest). No raw key is in any field or id.

`create_worker` and `create_key` are one transaction each: a query of the user's workers (or of the worker's keys)
read inside the transaction, the check, then the create. Firestore's serializable transactions make the label check
and the caps hold under concurrent creates. All queries are equality-only (single-field indexes merge).
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from google.api_core.exceptions import NotFound
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from ..config.environment import EnvironmentConfig
from ..domain.external_task_refusal import TaskRefusal
from ..domain.external_worker import ExternalWorker, WorkerStatus, worker_doc_id
from ..domain.external_worker_key import ExternalWorkerKey
from ..ports.external_worker_store import ExternalWorkerStore
from ..utils.logger import logger


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    return value


def _decode(value: Any) -> Any:
    """Plain tz-aware UTC datetimes from Firestore's DatetimeWithNanoseconds."""
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        aware = aware.astimezone(timezone.utc)
        return datetime(aware.year, aware.month, aware.day, aware.hour, aware.minute, aware.second,
                        aware.microsecond, tzinfo=timezone.utc)
    if isinstance(value, dict):
        return {k: _decode(v) for k, v in value.items()}
    return value


def _worker(data: Dict[str, Any]) -> ExternalWorker:
    return ExternalWorker.model_validate(_decode(data))


def _key(key_digest: str, data: Dict[str, Any]) -> ExternalWorkerKey:
    return ExternalWorkerKey.model_validate({**_decode(data), "key_digest": key_digest})


def _key_doc(key: ExternalWorkerKey) -> Dict[str, Any]:
    data: Dict[str, Any] = _encode(key.model_dump())
    del data["key_digest"]
    return data


class FirestoreExternalWorkerStore(ExternalWorkerStore):
    def __init__(self, db_client: Any, env_config: EnvironmentConfig) -> None:
        self._db = db_client
        self._workers = db_client.collection(env_config.external_workers_collection)
        self._keys = db_client.collection(env_config.external_worker_keys_collection)

    # -- workers ----------------------------------------------------------------------------------------------------

    async def create_worker(self, worker: ExternalWorker, max_active: int) -> Optional[TaskRefusal]:
        ref = self._workers.document(worker_doc_id(worker.user_id, worker.worker_id))
        query = self._workers.where(filter=FieldFilter("user_id", "==", worker.user_id))

        @firestore.async_transactional
        async def _run(transaction: Any) -> Optional[TaskRefusal]:
            active = [w for w in (_worker(s.to_dict()) for s in await query.get(transaction=transaction))
                      if w.is_active]
            if any(w.label == worker.label for w in active):
                return TaskRefusal.LABEL_TAKEN
            if len(active) >= max_active:
                return TaskRefusal.WORKERS_CAP
            transaction.create(ref, _encode(worker.model_dump()))
            return None

        return await _run(self._db.transaction())

    async def get_worker(self, user_id: str, worker_id: str) -> Optional[ExternalWorker]:
        if not worker_id:
            return None
        snap = await self._workers.document(worker_doc_id(user_id, worker_id)).get()
        if not snap.exists:
            return None
        worker = _worker(snap.to_dict())
        return worker if worker.user_id == user_id else None

    async def list_workers(self, user_id: str) -> List[ExternalWorker]:
        query = self._workers.where(filter=FieldFilter("user_id", "==", user_id))
        workers = [_worker(s.to_dict()) for s in await query.get()]
        return sorted(workers, key=lambda w: (w.created_at, w.worker_id))

    async def find_active_by_label(self, user_id: str, label: str) -> Optional[ExternalWorker]:
        query = (self._workers.where(filter=FieldFilter("user_id", "==", user_id))
                 .where(filter=FieldFilter("label", "==", label))
                 .where(filter=FieldFilter("status", "==", WorkerStatus.ACTIVE.value)))
        found = [_worker(s.to_dict()) for s in await query.get()]
        return found[0] if found else None

    async def revoke_worker(self, user_id: str, worker_id: str, now: datetime) -> bool:
        if not worker_id:
            return False
        ref = self._workers.document(worker_doc_id(user_id, worker_id))

        @firestore.async_transactional
        async def _run(transaction: Any) -> bool:
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return False
            worker = _worker(snap.to_dict())
            if worker.user_id != user_id or not worker.is_active:
                return False
            transaction.update(ref, {"status": WorkerStatus.REVOKED.value, "updated_at": now})
            return True

        return await _run(self._db.transaction())

    # -- keys -------------------------------------------------------------------------------------------------------

    async def create_key(self, key: ExternalWorkerKey, max_active: int) -> Optional[TaskRefusal]:
        ref = self._keys.document(key.key_digest)
        query = (self._keys.where(filter=FieldFilter("user_id", "==", key.user_id))
                 .where(filter=FieldFilter("worker_id", "==", key.worker_id)))

        @firestore.async_transactional
        async def _run(transaction: Any) -> Optional[TaskRefusal]:
            live = [s for s in await query.get(transaction=transaction) if s.to_dict().get("revoked_at") is None]
            if len(live) >= max_active:
                return TaskRefusal.KEYS_CAP
            transaction.create(ref, _key_doc(key))
            return None

        return await _run(self._db.transaction())

    async def get_key(self, key_digest: str) -> Optional[ExternalWorkerKey]:
        if not key_digest:
            return None
        snap = await self._keys.document(key_digest).get()
        return _key(key_digest, snap.to_dict()) if snap.exists else None

    async def list_keys(self, user_id: str, worker_id: str) -> List[ExternalWorkerKey]:
        query = (self._keys.where(filter=FieldFilter("user_id", "==", user_id))
                 .where(filter=FieldFilter("worker_id", "==", worker_id)))
        keys = [_key(s.id, s.to_dict()) for s in await query.get()]
        return sorted(keys, key=lambda k: (k.created_at, k.key_digest))

    async def revoke_key(self, user_id: str, worker_id: str, key_digest: str, now: datetime) -> bool:
        if not key_digest:
            return False
        ref = self._keys.document(key_digest)

        @firestore.async_transactional
        async def _run(transaction: Any) -> bool:
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return False
            key = _key(key_digest, snap.to_dict())
            if key.user_id != user_id or key.worker_id != worker_id or key.is_revoked:
                return False
            transaction.update(ref, {"revoked_at": now})
            return True

        return await _run(self._db.transaction())

    async def touch_key(self, key_digest: str, now: datetime) -> None:
        if not key_digest:
            return
        try:
            await self._keys.document(key_digest).update({"last_used_at": now})
        except NotFound:
            logger.info("[TaskQueue] touch_key: no key under digest=%s; nothing to stamp", key_digest[:8])
