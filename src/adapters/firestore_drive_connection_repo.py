"""FirestoreDriveConnectionRepository — drive connections and the user's drive settings in Firestore (MULTI_DRIVE_RFC §3,
§4.4; port `DriveConnectionRepository`).

Collections (env-prefixed): `{prefix}drive_connections`, doc id `{user_id}:{connection_id}` (`DriveConnection.doc_id`),
and `{prefix}drive_user_settings`, doc id `{user_id}`. Every field is plaintext: a connection holds no secret (the grant
is the credential's). Stored fields: `connection_id, user_id, account_id, credential_id, kind, drive_identity, ref_tag,
mount_name, name_history, indexing_enabled, transcribe_enabled, state, created_at, disconnected_at, updated_at,
searched_by_default, write_folder_id, scope_folder_ids, scope_whole_drive, connector_access` (`name_history`: a list of `{name, at}`,
oldest first; absent on a document stored before it, read as none. The four settings of slice 2b, absent on a document
stored before them, read as the migrated connection's (MULTI_DRIVE_RFC §11.3): searched by default, the write folder at
the root (`ROOT_FOLDER`; a stored null is read-only), the feed's default scope. `connector_access`
(DRIVE_MCP_CONNECTOR_RFC §7.1): `{"mode": "whole"}`, `{"mode": "folders", "folder_ids": [...]}`, or null; absent or
null reads as nothing, and a damaged value reads as nothing with an ERROR naming the connection only, never a raise,
so chat's reads of the connection keep working while the connector fails closed); settings:
`user_id, primary_connection_id, updated_at`.

- Tenancy is structural: every read is addressed by the user, and a document whose stored `user_id` or
  `connection_id` contradicts its address is treated as absent and logged at ERROR (I3).
- `create`, `update`, `update_settings`, `update_connector_access` and `transition` are one transaction each on the one
  document: create never overwrites, `update` writes only the fields given, `update_settings` the four settings and
  `update_connector_access` the connector access, never `state` / `disconnected_at`; `transition` applies `can_transition` to the state it read. `rename` is one transaction over the
  user's documents (a query read inside it), so two renames cannot both take one name.
- Connections are never deleted (a DISCONNECTED document keeps its tag reserved).
- No composite index: `list_for_user` filters on `user_id` alone and `list_by_state` on `state` alone (one `in`); the
  `indexing_enabled` filter and both orders are applied in memory (a handful of connections per user).
"""
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from ..domain.connection_scope import ConnectionScope
from ..domain.drive_connection import ROOT_FOLDER, DriveConnection, connection_doc_id
from ..domain.drive_connection_state import ConnectionState, can_transition
from ..domain.drive_connector_access import ConnectorAccess
from ..domain.drive_mount_name_change import MountNameChange
from ..domain.drive_mount_name_check import MountNameCheck
from ..domain.drive_mounts import renamed_history
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
        "name_history": _history_doc(connection.name_history),
        "indexing_enabled": connection.indexing_enabled,
        "transcribe_enabled": connection.transcribe_enabled,
        "state": connection.state.value,
        "created_at": connection.created_at,
        "disconnected_at": connection.disconnected_at,
        **_account_fields(connection.provider_account),
        **_settings_fields(connection.searched_by_default, connection.write_folder_id, connection.scope_folder_ids,
                           connection.scope_whole_drive),
        "connector_access": _connector_access_doc(connection.connector_access),
        "updated_at": now,
    }


def _history_doc(history: Sequence[MountNameChange]) -> List[Dict[str, Any]]:
    return [{"name": c.name, "at": c.at} for c in history]


def _settings_fields(searched_by_default: bool, write_folder_id: Optional[str], scope_folder_ids: Sequence[str],
                     scope_whole_drive: bool) -> Dict[str, Any]:
    """The stored form of the four Cabinet settings (a None write folder is stored as null: read-only)."""
    return {"searched_by_default": searched_by_default, "write_folder_id": write_folder_id,
            "scope_folder_ids": list(scope_folder_ids), "scope_whole_drive": scope_whole_drive}


def _connector_access_doc(access: Optional[ConnectorAccess]) -> Optional[Dict[str, Any]]:
    """The stored form of the connector access: null for nothing, else a `mode` map."""
    if access is None:
        return None
    if access.whole:
        return {"mode": "whole"}
    return {"mode": "folders", "folder_ids": list(access.folder_ids)}


def _connector_access(raw: Any, connection_id: str = "") -> Optional[ConnectorAccess]:
    """The stored connector access: absent or null is nothing; a damaged value is nothing too, logged at ERROR with
    the connection id only (never a raise: chat's reads of the connection must keep working; the connector fails
    closed)."""
    if raw is None:
        return None
    try:
        if not isinstance(raw, dict):
            raise ValueError("not a map")
        mode = raw.get("mode")
        if mode == "whole" and set(raw) == {"mode"}:
            return ConnectorAccess.whole_drive()
        ids = raw.get("folder_ids")
        if mode == "folders" and set(raw) == {"mode", "folder_ids"} and isinstance(ids, list) and ids:
            return ConnectorAccess.folders(ids)
        raise ValueError("unknown form")
    except ValueError:
        logger.error(f"💥 Drive connection {connection_id[:8]} has a damaged connector_access; read as nothing")
        return None


def _account_fields(account: Optional[ProviderAccount]) -> Dict[str, Any]:
    """The stored form of the provider account: realm and subject only (the label is ciphertext in the credential)."""
    return {"provider_realm": account.realm if account else "", "provider_subject": account.subject if account else ""}


def _name_history(raw: Any) -> Tuple[MountNameChange, ...]:
    """The stored `name_history` (absent or empty on a document stored before the field: no former names)."""
    changes = []
    for entry in raw or []:
        at = _utc(entry.get("at"))
        if at is None:
            raise ValueError("a stored drive connection name change has no time")
        changes.append(MountNameChange(name=entry["name"], at=at))
    return tuple(changes)


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
        name_history=_name_history(data.get("name_history")),
        searched_by_default=bool(data.get("searched_by_default", True)),
        # absent: a document stored before 2b, the migrated connection, whose write folder is the root (§11.3)
        write_folder_id=data["write_folder_id"] if "write_folder_id" in data else ROOT_FOLDER,
        scope_folder_ids=tuple(data.get("scope_folder_ids") or ()),
        scope_whole_drive=bool(data.get("scope_whole_drive", False)),
        connector_access=_connector_access(data.get("connector_access"), str(data.get("connection_id", ""))),
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

    async def update_settings(self, user_id: str, connection_id: str, *, searched_by_default: bool,
                              write_folder_id: Optional[str], scope_folder_ids: Sequence[str],
                              scope_whole_drive: bool) -> Optional[DriveConnection]:
        fields = _settings_fields(searched_by_default, write_folder_id, scope_folder_ids, scope_whole_drive)
        ref = self._ref(user_id, connection_id)

        @firestore.async_transactional
        async def _update(transaction: Any) -> Optional[DriveConnection]:
            data = self._owned(await ref.get(transaction=transaction), user_id, connection_id)
            if data is None:
                return None
            stored = _from_doc({**data, **fields})      # the domain's rules first: nothing invalid is written
            transaction.update(ref, {**fields, "updated_at": datetime.now(timezone.utc)})
            return stored

        updated: Optional[DriveConnection] = await _update(self._db.transaction())
        return updated

    async def update_connector_access(self, user_id: str, connection_id: str,
                                      access: Optional[ConnectorAccess]) -> Optional[DriveConnection]:
        fields = {"connector_access": _connector_access_doc(access)}
        ref = self._ref(user_id, connection_id)

        @firestore.async_transactional
        async def _update(transaction: Any) -> Optional[DriveConnection]:
            data = self._owned(await ref.get(transaction=transaction), user_id, connection_id)
            if data is None:
                return None
            stored = _from_doc({**data, **fields})
            transaction.update(ref, {**fields, "updated_at": datetime.now(timezone.utc)})
            return stored

        updated: Optional[DriveConnection] = await _update(self._db.transaction())
        return updated

    async def rename(self, user_id: str, connection_id: str, at: datetime,
                     check: Callable[[Sequence[DriveConnection]], MountNameCheck]) -> Optional[MountNameCheck]:
        ref = self._ref(user_id, connection_id)
        query = self._col.where(filter=FieldFilter("user_id", "==", user_id))

        @firestore.async_transactional
        async def _rename(transaction: Any) -> Optional[MountNameCheck]:
            snaps = await query.get(transaction=transaction)
            every = sorted((_from_doc(data) for data in (self._owned(s, user_id) for s in snaps) if data is not None),
                           key=lambda c: (c.created_at, c.connection_id))
            current = next((c for c in every if c.connection_id == connection_id), None)
            if current is None or current.state is ConnectionState.DISCONNECTED:
                return None
            result = check(every)
            if result.ok and result.name != current.mount_name:
                transaction.update(ref, {"mount_name": result.name,
                                         "name_history": _history_doc(renamed_history(current, result.name, at)),
                                         "updated_at": datetime.now(timezone.utc)})
            return result

        checked: Optional[MountNameCheck] = await _rename(self._db.transaction())
        return checked

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
