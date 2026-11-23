"""FirestoreDeviceSessionRepository — Cabinet device sessions in Firestore (CABINET_DEVICE_SESSIONS_RFC §7.1, §7.6; port
`DeviceSessionRepository`).

Collection `{prefix}auth_device_sessions_v1` (`EnvironmentConfig.device_sessions_collection`), doc id = the random
`sid`. Stored fields: `sid, user_id, account_id, label, platform_hint, auth_kind, auth_at, created_at, last_seen_at,
expires_at, refresh_hash, prev_refresh_hash, rotated_at, revoked_at, revoke_reason, purge_after` (enums as their
values, timestamps tz-aware UTC). The refresh secret itself is never stored, only its SHA-256. `purge_after` is the
Firestore TTL field (a TTL policy on it is a rollout step, not code).

- `create` never overwrites; `refresh`, `revoke` and `revoke_all` are one transaction each, so the refresh decision is
  taken on the record its write replaces, and two refreshes of one sid cannot both rotate (the loser's transaction
  re-runs on the rotated record).
- A document whose stored `sid` contradicts its id is treated as absent and logged at ERROR.
- No composite index: `list_for_user` and `revoke_all` filter on `user_id` alone; the order is applied in memory (a
  handful of sessions per user).
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from ..domain.device_session import DeviceSession, SessionAuthKind, SessionRevokeReason
from ..domain.device_session_policy import DeviceSessionPolicy, RefreshOutcome
from ..ports.device_session_repository import DeviceSessionRepository
from ..utils.logger import logger

_TIME_FIELDS = ("auth_at", "created_at", "last_seen_at", "expires_at", "rotated_at", "revoked_at", "purge_after")


def _utc(raw: Any) -> Optional[datetime]:
    """A stored timestamp as a plain tz-aware UTC datetime (never a DatetimeWithNanoseconds)."""
    if raw is None:
        return None
    if raw.tzinfo is not None:
        raw = raw.astimezone(timezone.utc)
    return datetime(raw.year, raw.month, raw.day, raw.hour, raw.minute, raw.second, raw.microsecond,
                    tzinfo=timezone.utc)


def _encode(fields: Dict[str, Any]) -> Dict[str, Any]:
    return {name: value.value if isinstance(value, Enum) else value for name, value in fields.items()}


def _to_doc(session: DeviceSession) -> Dict[str, Any]:
    return _encode(dict(session))    # every field, in declaration order


def _from_doc(data: Dict[str, Any]) -> DeviceSession:
    reason = data.get("revoke_reason")
    return DeviceSession(
        sid=data["sid"], user_id=data["user_id"], account_id=data["account_id"], label=data["label"],
        platform_hint=data["platform_hint"], auth_kind=SessionAuthKind(data["auth_kind"]),
        refresh_hash=data["refresh_hash"], prev_refresh_hash=data.get("prev_refresh_hash"),
        revoke_reason=SessionRevokeReason(reason) if reason is not None else None,
        **{name: _utc(data.get(name)) for name in _TIME_FIELDS},
    )


def _changes(outcome: RefreshOutcome, session: DeviceSession, new_hash: str, now: datetime) -> Dict[str, Any]:
    """The fields a refresh outcome writes (none for `DEAD`)."""
    if outcome is RefreshOutcome.ROTATE:
        return {"prev_refresh_hash": session.refresh_hash, "refresh_hash": new_hash, "rotated_at": now,
                "last_seen_at": now, "purge_after": DeviceSessionPolicy.purge_after(now)}
    if outcome is RefreshOutcome.GRACE:
        return {"last_seen_at": now}
    if outcome is RefreshOutcome.REPLAY:
        return {"revoked_at": now, "revoke_reason": SessionRevokeReason.REPLAY}
    return {}


class FirestoreDeviceSessionRepository(DeviceSessionRepository):

    def __init__(self, db_client: Any, collection_name: str) -> None:
        self._db = db_client
        self._col = db_client.collection(collection_name)
        logger.info(f"🔑 Device session store initialized. Collection: {collection_name}")

    @staticmethod
    def _owned(snap: Any) -> Optional[Dict[str, Any]]:
        """The stored document when it exists and its stored `sid` is its id; None otherwise (ERROR on a mismatch)."""
        if not snap.exists:
            return None
        data: Dict[str, Any] = snap.to_dict() or {}
        if data.get("sid") != snap.id:
            logger.error(f"💥 Device session doc {str(snap.id)[:8]} does not match its stored sid; treated as absent")
            return None
        return data

    async def create(self, session: DeviceSession) -> bool:
        ref = self._col.document(session.sid)

        @firestore.async_transactional
        async def _create(transaction: Any) -> bool:
            if (await ref.get(transaction=transaction)).exists:
                return False
            transaction.set(ref, _to_doc(session))
            return True

        created: bool = await _create(self._db.transaction())
        if created:
            logger.info(f"🔑 Device session created: user={session.user_id[:8]} sid={session.sid[:8]} "
                        f"kind={session.auth_kind.value}")
        else:
            logger.warning(f"Device session not created, its sid is taken: sid={session.sid[:8]}")
        return created

    async def get(self, sid: str) -> Optional[DeviceSession]:
        data = self._owned(await self._col.document(sid).get())
        return None if data is None else _from_doc(data)

    async def list_for_user(self, user_id: str) -> List[DeviceSession]:
        snaps = await self._col.where(filter=FieldFilter("user_id", "==", user_id)).get()
        found = [_from_doc(data) for data in (self._owned(s) for s in snaps) if data is not None]
        return sorted(found, key=lambda s: (s.created_at, s.sid))

    async def refresh(self, sid: str, presented_hash: str, new_hash: str,
                      now: datetime) -> Tuple[RefreshOutcome, Optional[DeviceSession]]:
        ref = self._col.document(sid)

        @firestore.async_transactional
        async def _refresh(transaction: Any) -> Tuple[RefreshOutcome, Optional[DeviceSession]]:
            data = self._owned(await ref.get(transaction=transaction))
            if data is None:
                return RefreshOutcome.DEAD, None
            session = _from_doc(data)
            outcome = DeviceSessionPolicy.decide_refresh(session, presented_hash, now)
            changes = _changes(outcome, session, new_hash, now)
            if changes:
                transaction.update(ref, _encode(changes))
            return outcome, session.model_copy(update=changes)

        result: Tuple[RefreshOutcome, Optional[DeviceSession]] = await _refresh(self._db.transaction())
        if result[0] is RefreshOutcome.REPLAY:
            logger.warning(f"⛔ Device session revoked: an old refresh secret came back, sid={sid[:8]}")
        return result

    async def revoke(self, sid: str, reason: SessionRevokeReason, now: datetime) -> bool:
        ref = self._col.document(sid)

        @firestore.async_transactional
        async def _revoke(transaction: Any) -> bool:
            data = self._owned(await ref.get(transaction=transaction))
            if data is None or data.get("revoked_at") is not None:
                return False
            transaction.update(ref, {"revoked_at": now, "revoke_reason": reason.value})
            return True

        revoked: bool = await _revoke(self._db.transaction())
        if revoked:
            logger.info(f"🔒 Device session revoked: sid={sid[:8]} reason={reason.value}")
        return revoked

    async def revoke_all(self, user_id: str, reason: SessionRevokeReason, now: datetime) -> int:
        query = self._col.where(filter=FieldFilter("user_id", "==", user_id))

        @firestore.async_transactional
        async def _revoke_all(transaction: Any) -> int:
            snaps = await query.get(transaction=transaction)
            count = 0
            for snap in snaps:
                data = self._owned(snap)
                if data is None or data.get("user_id") != user_id or data.get("revoked_at") is not None:
                    continue
                transaction.update(snap.reference, {"revoked_at": now, "revoke_reason": reason.value})
                count += 1
            return count

        revoked: int = await _revoke_all(self._db.transaction())
        logger.info(f"🔒 Device sessions revoked: user={user_id[:8]} count={revoked} reason={reason.value}")
        return revoked
