"""FirestoreDriveConnectionRepository — drive connections and the user's drive settings in Firestore (MULTI_DRIVE_RFC §3,
§4.4; port `DriveConnectionRepository`).

Collections (env-prefixed): `{prefix}drive_connections`, doc id `{user_id}:{connection_id}` (`DriveConnection.doc_id`),
and `{prefix}drive_user_settings`, doc id `{user_id}`. Every field is plaintext: a connection holds no secret (the grant
is the credential's). Stored fields: `connection_id, user_id, account_id, credential_id, kind, drive_identity, ref_tag,
mount_name, indexing_enabled, transcribe_enabled, state, created_at, disconnected_at, updated_at`; settings:
`user_id, primary_connection_id, updated_at`.

- Tenancy is structural: every read is addressed by the user, and a document whose stored `user_id` or
  `connection_id` contradicts its address is treated as absent and logged at ERROR (I3).
- `create`, `update` and `transition` are one transaction each on the one document: create never overwrites,
  `update` writes only the fields given and never `state` / `disconnected_at`, `transition` applies `can_transition`
  to the state it read.
- Connections are never deleted (a DISCONNECTED document keeps its tag reserved).
- No composite index: `list_for_user` filters on `user_id` alone and `list_by_state` on `state` alone (one `in`); the
  `indexing_enabled` filter and both orders are applied in memory (a handful of connections per user).
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from ..domain.connection_scope import ConnectionScope
from ..domain.drive_connection import DriveConnection, connection_doc_id
from ..domain.drive_connection_state import ConnectionState, can_transition
from ..domain.drive_user_settings import DriveUserSettings
from ..domain.provider_account import ProviderAccount
from ..ports.drive_connection_repository import DriveConnectionRepository
from ..utils.logger import logger

_TEXT_FIELDS = ("credential_id", "drive_identity", "mount_name")


def _utc(raw: Any) -> Optional[datetime]:
    """A stored timestamp as a plain tz-aware UTC datetime (never a DatetimeWithNanoseconds, which breaks a write-back)."""
    if raw is None:
        return None
    if raw.tzinfo is not None:
        raw = raw.astimezone(timezone.utc)
    return datetime(raw.year, raw.month, raw.day, raw.hour, raw.minute, raw.second, raw.microsecond,
                    tzinfo=timezone.utc)


def _to_doc(connection: DriveConnection, now: datetime) -> Dict[str, Any]:
    return {
        "connection_id": connection.connection_id,
        "user_id": connection.user_id,
        "account_id": connection.account_id,
        "credential_id": connection.credential_id,
        "kind": connection.kind,
        "drive_identity": connection.drive_identity,
        "ref_tag": connection.ref_tag,
        "mount_name": connection.mount_name,
        "indexing_enabled": connection.indexing_enabled,
        "transcribe_enabled": connection.transcribe_enabled,
        "state": connection.state.value,
        "created_at": connection.created_at,
        "disconnected_at": connection.disconnected_at,
        **_account_fields(connection.provider_account),
        "updated_at": now,
    }


def _account_fields(account: Optional[ProviderAccount]) -> Dict[str, Any]:
    """The stored form of the provider account: realm and subject only (the label is ciphertext in the credential)."""
    return {"provider_realm": account.realm if account else "", "provider_subject": account.subject if account else ""}


def _from_doc(data: Dict[str, Any]) -> DriveConnection:
    created_at = _utc(data["created_at"])
    if created_at is None:
        raise ValueError("a stored drive connection has no created_at")
    return DriveConnection(
        connection_id=data["connection_id"],
        user_id=data["user_id"],
        account_id=data["account_id"],
        credential_id=data["credential_id"],
        kind=data["kind"],
        drive_identity=data["drive_identity"],
        ref_tag=data["ref_tag"],
        mount_name=data["mount_name"],
        indexing_enabled=bool(data.get("indexing_enabled", False)),
        transcribe_enabled=bool(data.get("transcribe_enabled", False)),
        state=ConnectionState(data["state"]),
        created_at=created_at,
        disconnected_at=_utc(data.get("disconnected_at")),
        provider_account=ProviderAccount(realm=data["provider_realm"], subject=data["provider_subject"], label="")
        if data.get("provider_realm") and data.get("provider_subject") else None,
    )


class FirestoreDriveConnectionRepository(DriveConnectionRepository):

    def __init__(self, db_client: Any, connections_collection: str, settings_collection: str) -> None:
        self._db = db_client
        self._col = db_client.collection(connections_collection)
        self._settings = db_client.collection(settings_collection)
        logger.info(f"📂 Drive connection store initialized. Collections: {connections_collection}, "
                    f"{settings_collection}")

    # ------------------------------------------------------------------ helpers

    def _ref(self, user_id: str, connection_id: str) -> Any:
        return self._col.document(connection_doc_id(ConnectionScope(user_id=user_id, connection_id=connection_id)))

    @staticmethod
    def _owned(snap: Any, user_id: str, connection_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The stored document when it exists and matches its address (`{user_id}:{connection_id}` and the two stored
        fields); None otherwise, logged at ERROR when the stored identity contradicts the address (I3)."""
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        expected = connection_id if connection_id is not None else str(data.get("connection_id") or "")
        if not user_id or data.get("user_id") != user_id or data.get("connection_id") != expected \
                or snap.id != f"{user_id}:{expected}":
            logger.error(f"💥 Drive connection doc {str(snap.id)[:12]} does not match its address "
                         f"(user={user_id[:8]}); treated as absent")
            return None
        return data

    # ------------------------------------------------------------------ connections

    async def create(self, connection: DriveConnection) -> bool:
        ref = self._ref(connection.user_id, connection.connection_id)

        @firestore.async_transactional
        async def _create(transaction: Any) -> bool:
            snap = await ref.get(transaction=transaction)
            if snap.exists:
                return False
            transaction.set(ref, _to_doc(connection, datetime.now(timezone.utc)))
            return True

        created: bool = await _create(self._db.transaction())
        if created:
            logger.info(f"🔗 Drive connection created: user={connection.user_id[:8]} "
                        f"connection={connection.connection_id[:8]} kind={connection.kind}")
        else:
            logger.warning(f"Drive connection not created, its id is taken: user={connection.user_id[:8]} "
                           f"connection={connection.connection_id[:8]}")
        return created

    async def get(self, user_id: str, connection_id: str) -> Optional[DriveConnection]:
        data = self._owned(await self._ref(user_id, connection_id).get(), user_id, connection_id)
        return None if data is None else _from_doc(data)

    async def list_for_user(self, user_id: str) -> List[DriveConnection]:
        snaps = await self._col.where(filter=FieldFilter("user_id", "==", user_id)).get()
        found = [_from_doc(data) for data in (self._owned(s, user_id) for s in snaps) if data is not None]
        return sorted(found, key=lambda c: (c.created_at, c.connection_id))

    async def list_by_state(self, states: Sequence[ConnectionState], *,
                            indexing_enabled: Optional[bool] = None) -> List[DriveConnection]:
        wanted = [state.value for state in dict.fromkeys(states)]
        if not wanted:
            return []
        snaps = await self._col.where(filter=FieldFilter("state", "in", wanted)).get()
        found: List[DriveConnection] = []
        for snap in snaps:
            data = self._owned(snap, str((snap.to_dict() or {}).get("user_id") or ""))
            if data is None:
                continue
            connection = _from_doc(data)
            if indexing_enabled is None or connection.indexing_enabled is indexing_enabled:
                found.append(connection)
        return sorted(found, key=lambda c: (c.user_id, c.created_at, c.connection_id))

    async def update(self, user_id: str, connection_id: str, *, credential_id: Optional[str] = None,
                     drive_identity: Optional[str] = None, mount_name: Optional[str] = None,
                     indexing_enabled: Optional[bool] = None,
                     transcribe_enabled: Optional[bool] = None,
                     provider_account: Optional[ProviderAccount] = None) -> Optional[DriveConnection]:
        given = {"credential_id": credential_id, "drive_identity": drive_identity, "mount_name": mount_name,
                 "indexing_enabled": indexing_enabled, "transcribe_enabled": transcribe_enabled}
        fields = {name: value for name, value in given.items() if value is not None}
        if provider_account is not None:
            fields.update(_account_fields(provider_account))
        for name in _TEXT_FIELDS:
            if name in fields and not fields[name]:
                raise ValueError(f"a drive connection's {name} cannot be emptied")
        ref = self._ref(user_id, connection_id)

        @firestore.async_transactional
        async def _update(transaction: Any) -> Optional[DriveConnection]:
            data = self._owned(await ref.get(transaction=transaction), user_id, connection_id)
            if data is None:
                return None
            transaction.update(ref, {**fields, "updated_at": datetime.now(timezone.utc)})
            return _from_doc({**data, **fields})

        updated: Optional[DriveConnection] = await _update(self._db.transaction())
        return updated

    async def transition(self, user_id: str, connection_id: str, to: ConnectionState, now: datetime) -> bool:
        ref = self._ref(user_id, connection_id)

        @firestore.async_transactional
        async def _transition(transaction: Any) -> Optional[ConnectionState]:
            data = self._owned(await ref.get(transaction=transaction), user_id, connection_id)
            if data is None:
                return None
            current = ConnectionState(data["state"])
            if not can_transition(current, to):
                return None
            transaction.update(ref, {
                "state": to.value,
                "disconnected_at": now if to is ConnectionState.DISCONNECTED else None,
                "updated_at": datetime.now(timezone.utc),
            })
            return current

        before: Optional[ConnectionState] = await _transition(self._db.transaction())
        if before is None:
            return False
        logger.info(f"🔗 Drive connection {connection_id[:8]} of user={user_id[:8]}: {before.value} -> {to.value}")
        return True

    # ------------------------------------------------------------------ settings

    async def get_settings(self, user_id: str) -> Optional[DriveUserSettings]:
        snap = await self._settings.document(user_id).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        if data.get("user_id") != user_id:
            logger.error(f"💥 Drive user settings stored under another user (user={user_id[:8]}); treated as absent")
            return None
        return DriveUserSettings(user_id=user_id, primary_connection_id=data.get("primary_connection_id") or None)

    async def save_settings(self, settings: DriveUserSettings) -> None:
        await self._settings.document(settings.user_id).set({
            "user_id": settings.user_id,
            "primary_connection_id": settings.primary_connection_id,
            "updated_at": datetime.now(timezone.utc),
        })
