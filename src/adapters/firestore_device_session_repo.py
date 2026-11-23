"""FirestoreDeviceSessionRepository — Cabinet device sessions and passkeys in Firestore (CABINET_DEVICE_SESSIONS_RFC
§7.1, §7.3, §7.6; port `DeviceSessionRepository`).

Collection `{prefix}auth_device_sessions_v1` (`EnvironmentConfig.device_sessions_collection`), doc id = the random
`sid`. Stored fields: `sid, user_id, account_id, label, platform_hint, auth_kind, auth_at, created_at, last_seen_at,
expires_at, refresh_hash, prev_refresh_hash, rotated_at, revoked_at, revoke_reason, purge_after, passkey_at` (enums as their
values, timestamps tz-aware UTC). The refresh secret itself is never stored, only its SHA-256. `purge_after` is the
Firestore TTL field (a TTL policy on it is a rollout step, not code).

Passkeys: collection `{prefix}auth_passkeys_v1` (`EnvironmentConfig.passkeys_collection`), doc id = `cred_hash` (the
SHA-256 hex of the credential id). Stored fields: `cred_hash, credential_id, user_id, account_id, kind, public_key,
algorithm, sign_count, label, platform_hint, backup_eligible, created_at, last_used_at, revoked_at` (the id and the key
as bytes). No TTL: a revoked passkey stays as a record.

- `create` and `add_passkey` never overwrite; `refresh`, `revoke`, `record_passkey_assertion`, `touch_passkey`,
  `revoke_passkey` and `revoke_all` are one transaction each, so the refresh decision is taken on the record its write replaces, two refreshes of one
  sid cannot both rotate (the loser's transaction re-runs on the rotated record), and two assertions with one counter
  cannot both advance it. `revoke_all` reads the user's sessions and passkeys first, then writes both.
- A document whose stored `sid` (or `cred_hash`) contradicts its id is treated as absent and logged at ERROR.
- No composite index: `list_for_user`, `list_passkeys` and `revoke_all` filter on `user_id` alone; the order is applied
  in memory (a handful of records per user).
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from ..domain.device_session import DeviceSession, SessionAuthKind, SessionRevokeReason
from ..domain.device_session_policy import DeviceSessionPolicy, RefreshOutcome
from ..domain.passkey import Passkey, PasskeyKind, sign_count_advances
from ..ports.device_session_repository import DeviceSessionRepository
from ..utils.logger import logger

_TIME_FIELDS = ("auth_at", "created_at", "last_seen_at", "expires_at", "rotated_at", "revoked_at", "purge_after",
                "passkey_at")
_PASSKEY_TIME_FIELDS = ("created_at", "last_used_at", "revoked_at")


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


def _passkey_to_doc(passkey: Passkey) -> Dict[str, Any]:
    return _encode(dict(passkey))    # every field, in declaration order


def _passkey_from_doc(data: Dict[str, Any]) -> Passkey:
    return Passkey(
        cred_hash=data["cred_hash"], credential_id=bytes(data["credential_id"]), user_id=data["user_id"],
        account_id=data["account_id"], kind=PasskeyKind(data["kind"]), public_key=bytes(data["public_key"]),
        algorithm=data["algorithm"], sign_count=data["sign_count"], label=data["label"],
        platform_hint=data["platform_hint"], backup_eligible=data["backup_eligible"],
        **{name: _utc(data.get(name)) for name in _PASSKEY_TIME_FIELDS},
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

    def __init__(self, db_client: Any, collection_name: str, passkeys_collection: str) -> None:
        self._db = db_client
        self._col = db_client.collection(collection_name)
        self._passkeys = db_client.collection(passkeys_collection)
        logger.info(f"🔑 Device session store initialized. Collections: {collection_name}, {passkeys_collection}")

    @staticmethod
    def _owned(snap: Any, id_field: str = "sid") -> Optional[Dict[str, Any]]:
        """The stored document when it exists and its stored `id_field` is its id; None otherwise (ERROR on a
        mismatch)."""
        if not snap.exists:
            return None
        data: Dict[str, Any] = snap.to_dict() or {}
        if data.get(id_field) != snap.id:
            logger.error(f"💥 Device session store doc {str(snap.id)[:8]} does not match its stored {id_field}; "
                         f"treated as absent")
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

    async def record_passkey_assertion(self, sid: str, user_id: str, at: datetime) -> bool:
        ref = self._col.document(sid)

        @firestore.async_transactional
        async def _record(transaction: Any) -> bool:
            data = self._owned(await ref.get(transaction=transaction))
            if data is None or data.get("user_id") != user_id or not DeviceSessionPolicy.is_live(_from_doc(data), at):
                return False
            transaction.update(ref, {"passkey_at": at})
            return True

        recorded: bool = await _record(self._db.transaction())
        if not recorded:
            logger.warning(f"⛔ Passkey assertion not recorded (session absent, not the user's, or ended): "
                           f"sid={sid[:8]}")
        return recorded

    async def revoke_all(self, user_id: str, reason: SessionRevokeReason, now: datetime) -> int:
        sessions = self._col.where(filter=FieldFilter("user_id", "==", user_id))
        passkeys = self._passkeys.where(filter=FieldFilter("user_id", "==", user_id))

        def _live(snaps: Any, id_field: str) -> List[Any]:
            found = []
            for snap in snaps:
                data = self._owned(snap, id_field)
                if data is not None and data.get("user_id") == user_id and data.get("revoked_at") is None:
                    found.append(snap.reference)
            return found

        @firestore.async_transactional
        async def _revoke_all(transaction: Any) -> Tuple[int, int]:
            live_sessions = _live(await sessions.get(transaction=transaction), "sid")      # every read before a write
            live_passkeys = _live(await passkeys.get(transaction=transaction), "cred_hash")
            for ref in live_sessions:
                transaction.update(ref, {"revoked_at": now, "revoke_reason": reason.value})
            for ref in live_passkeys:
                transaction.update(ref, {"revoked_at": now})
            return len(live_sessions), len(live_passkeys)

        revoked, keys = await _revoke_all(self._db.transaction())
        logger.info(f"🔒 Device sessions revoked: user={user_id[:8]} count={revoked} passkeys={keys} "
                    f"reason={reason.value}")
        return revoked

    # ------------------------------------------------------------------ passkeys

    async def add_passkey(self, passkey: Passkey) -> bool:
        ref = self._passkeys.document(passkey.cred_hash)

        @firestore.async_transactional
        async def _add(transaction: Any) -> bool:
            if (await ref.get(transaction=transaction)).exists:
                return False
            transaction.set(ref, _passkey_to_doc(passkey))
            return True

        added: bool = await _add(self._db.transaction())
        if added:
            logger.info(f"🔑 Passkey added: user={passkey.user_id[:8]} key={passkey.cred_hash[:8]}")
        else:
            logger.warning(f"Passkey not added, its cred_hash is taken: key={passkey.cred_hash[:8]}")
        return added

    async def get_passkey(self, cred_hash: str) -> Optional[Passkey]:
        data = self._owned(await self._passkeys.document(cred_hash).get(), "cred_hash")
        return None if data is None else _passkey_from_doc(data)

    async def list_passkeys(self, user_id: str) -> List[Passkey]:
        snaps = await self._passkeys.where(filter=FieldFilter("user_id", "==", user_id)).get()
        found = [_passkey_from_doc(data) for data in (self._owned(s, "cred_hash") for s in snaps) if data is not None]
        return sorted(found, key=lambda p: (p.created_at, p.cred_hash))

    async def touch_passkey(self, cred_hash: str, sign_count: int, last_used_at: datetime) -> bool:
        ref = self._passkeys.document(cred_hash)

        @firestore.async_transactional
        async def _touch(transaction: Any) -> bool:
            data = self._owned(await ref.get(transaction=transaction), "cred_hash")
            if data is None or data.get("revoked_at") is not None:
                return False
            if not sign_count_advances(data["sign_count"], sign_count):
                return False
            transaction.update(ref, {"sign_count": sign_count, "last_used_at": last_used_at})
            return True

        touched: bool = await _touch(self._db.transaction())
        if not touched:
            logger.warning(f"⛔ Passkey use not recorded (absent, revoked or counter did not advance): "
                           f"key={cred_hash[:8]}")
        return touched

    async def revoke_passkey(self, cred_hash: str, now: datetime) -> bool:
        ref = self._passkeys.document(cred_hash)

        @firestore.async_transactional
        async def _revoke(transaction: Any) -> bool:
            data = self._owned(await ref.get(transaction=transaction), "cred_hash")
            if data is None or data.get("revoked_at") is not None:
                return False
            transaction.update(ref, {"revoked_at": now})
            return True

        revoked: bool = await _revoke(self._db.transaction())
        if revoked:
            logger.info(f"🔒 Passkey revoked: key={cred_hash[:8]}")
        return revoked
