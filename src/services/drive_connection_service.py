"""DriveConnectionService — the one owner of drive connections and the primary pointer (MULTI_DRIVE_RFC §3, §4.4, §6,
§8, §9).

Rules (RFC §4.4):
- A connect whose account matches a live (ACTIVE or NEEDS_RECONNECT) connection of the user and kind is that
  connection's reconnect: same id, same tag, ACTIVE again, pointed at the consent's credential. "Same account" is read
  as: the same `credential_id` (the credential store keeps one record per user, kind and provider account, so a consent
  of the same account keeps its credential id), else the same `drive_identity` (the drive a grant of that account
  reads; this also catches the account whose credential was deleted while the connection stayed ACTIVE). Otherwise a
  new connection: a random id, a new tag unique among every connection the user ever had (DISCONNECTED included, never
  reused), the lowest free name of `Drive`, `Drive 2`, … (E11; `drive_mounts.default_mount_name`: no live
  connection's current or held former name), recorded as the first `name_history` entry at the creation instant,
  indexing and transcription off (the `DriveConnection` defaults), read-only (no write folder, D11), searched by
  default, the feed's default scope. DISCONNECTED is terminal.
- The primary pointer (`DriveUserSettings.primary_connection_id`) is read fresh on every use and written only here.
  I2: the primary is an ACTIVE connection with a write folder. A connection becomes the primary when the user has none
  and it gets one (an activation of a connection that has a write folder, or the first write folder chosen, E-17); a
  disconnect of the primary hands it to the heir the Cabinet named (RFC §11.7), or clears it.
- The settings (§11.3, §11.8, D4, D11) are written only here, each a Cabinet action: `rename` (validated inside the
  repository's transaction over the user's connections), `set_searched_by_default`, `set_scope` and
  `set_write_folder` (the write folder lies inside the scope; both refusals are `DriveSettingRefused`).
- Drives read reconnect state only from the connection; `flag_reconnect` keeps the credential's `needs_reconnect` in
  step (best effort: the connection is the source of truth), and a consent clears both (the connect service saves the
  credential with the flag down, `activate` moves the connection back to ACTIVE).
- Every setting is a Cabinet action (D18); no chat path calls a setter. `set_connector_access` (DRIVE_MCP_CONNECTOR_RFC
  §7.6) stores what the drive MCP connector reads: nothing, the whole drive, or outermost folders inside the scope; a
  scope change that would leave a connector folder outside the scope is refused (SCOPE_STRANDS_CONNECTOR_LINE).
- Under the connector audience (`CURRENT_DRIVE_AUDIENCE`, §7.4, E12) a drive with no connector access is hidden from
  the live list, the primary, the status, ref resolution, the mount table and the tag lookup; `get` is never filtered
  (the access check refuses it, and the Cabinet and the indexer need it). With no audience every read is unchanged.

Errors: `DriveNotConnectedError` (domain) for a connection that is absent, another user's, or DISCONNECTED where a live
one is needed (ACTIVE for `set_primary`); `DriveSettingRefused` (domain) for a setting a rule refuses, with the line
the Cabinet shows.

Concurrency: `activate` runs under one in-process lock (two consents of one process never create two connections of
one account); every state change is the repository's atomic `transition`.

Public interface:
    DriveConnectionService(connections, credentials, *, clock=None, new_id=None, new_tag=None)
    activate(credential, drive_identity) -> DriveConnection
    get(user_id, connection_id) -> Optional[DriveConnection]
    list_for_user(user_id, *, include_disconnected=False) -> List[DriveConnection]
    handle_for(user_id, connection_id) -> DriveHandle
    primary(user_id) -> Optional[DriveConnection]
    primary_handle(user_id) -> DriveHandle
    set_primary(user_id, connection_id) -> None
    default_name(user_id) -> str
    rename(user_id, connection_id, name, *, primary_top_folders=()) -> MountNameCheck
    set_searched_by_default(user_id, connection_id, enabled) -> DriveConnection
    set_scope(user_id, connection_id, *, folder_ids, whole_drive, capabilities, write_folder_ancestry,
              folder_ancestries, connector_ancestries={}) -> DriveConnection
    set_write_folder(user_id, connection_id, folder_id, *, capabilities, ancestry) -> DriveConnection
    set_connector_access(user_id, connection_id, access, *, capabilities, root_item_id, folder_ancestries)
        -> DriveConnection
    status(user_id) -> DriveConnectionStatus
    resolve_ref(user_id, text) -> ResolvedDriveRef | DriveRefRefusal | None
    mounts(user_id) -> MountTable
    connection_of_tag(user_id, tag) -> Optional[DriveConnection]
    set_indexing(user_id, connection_id, enabled) -> DriveConnection
    set_transcribe(user_id, connection_id, enabled) -> DriveConnection
    flag_reconnect(handle) -> bool
    disconnect(user_id, connection_id, *, heir_id=None) -> bool
    reconnect_needed(user_id) -> List[DriveConnection]
"""
import asyncio
import secrets
from dataclasses import replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from ..domain import drive_ref
from ..domain.drive_access_policy import (
    CONNECTOR_FOLDER_OUTSIDE_SCOPE_LINE, CONNECTOR_ROOT_LINE, SCOPE_BOTH_LINE, SCOPE_LEAVES_WRITE_FOLDER_LINE,
    SCOPE_STRANDS_CONNECTOR_LINE, WRITE_FOLDER_OUTSIDE_SCOPE_LINE, in_scope,
)
from ..domain.drive_ancestry import Ancestry
from ..domain.drive_audience import CURRENT_DRIVE_AUDIENCE, DriveAudience
from ..domain.drive_capabilities import DriveCapabilities
from ..domain.drive_connection import ROOT_FOLDER, DriveConnection
from ..domain.drive_connection_access import ConnectionAccess
from ..domain.drive_connection_state import ConnectionState
from ..domain.drive_connector_access import ConnectorAccess
from ..domain.drive_connection_status import DriveConnectionStatus
from ..domain.drive_feed_scope import FeedScope
from ..domain.drive_handle import DriveHandle
from ..domain.drive_mount_name_change import MountNameChange
from ..domain.drive_mount_name_check import MountNameCheck
from ..domain.drive_mount_table import MountTable
from ..domain.drive_mounts import (
    HEIR_INVALID_LINE, PRIMARY_NEEDS_WRITE_FOLDER_LINE, default_mount_name, mount_table, validate_mount_name,
)
from ..domain.drive_ref_refusal import DriveRefRefusal, disconnected_ref_line, stale_ref_line
from ..domain.drive_resolved_ref import ResolvedDriveRef
from ..domain.drive_setting_refused import DriveSettingRefused
from ..domain.drive_user_settings import DriveUserSettings
from ..domain.oauth_credential import OAuthCredential
from ..domain.provider_account import ProviderAccount
from ..domain.user_drive import DriveNotConnectedError
from ..ports.drive_connection_repository import DriveConnectionRepository
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..utils.logger import logger

_MAX_CREATE_ATTEMPTS = 32
_LIVE = (ConnectionState.ACTIVE, ConnectionState.NEEDS_RECONNECT)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stored_account(credential: OAuthCredential) -> ProviderAccount:
    """The credential's provider account as a connection keeps it: realm and subject, the label stays in the ciphertext."""
    return ProviderAccount(realm=credential.account.realm, subject=credential.account.subject, label="")


def _random_id() -> str:
    return secrets.token_hex(8)


def _random_tag() -> str:
    """A 2-character tag: a letter, then a letter or digit (936 values; a user has a handful of connections)."""
    return secrets.choice(drive_ref.TAG_FIRST_ALPHABET) + secrets.choice(drive_ref.TAG_ALPHABET)


def _connector_reads() -> bool:
    """The current read runs under the connector audience (DRIVE_MCP_CONNECTOR_RFC §7.2)."""
    return CURRENT_DRIVE_AUDIENCE.get() is DriveAudience.CONNECTOR


def _visible(connection: DriveConnection) -> bool:
    """The connector sees the drive: it has some connector access (§7.4)."""
    return connection.connector_access is not None


def _outermost(ids: Sequence[str], chains: Mapping[str, Ancestry]) -> Tuple[str, ...]:
    """`ids` without the folders that lie under another selected one (their chain holds it)."""
    for folder in ids:
        chain = chains.get(folder)
        if chain is None or chain.item_id != folder:
            raise ValueError("set_scope needs each selected folder's own ancestry")
    chosen = set(ids)
    return tuple(f for f in ids if not any(a in chosen for a in chains[f].ids[1:]))


class DriveConnectionService:

    def __init__(self, connections: DriveConnectionRepository, credentials: OAuthCredentialsPort, *,
                 clock: Optional[Callable[[], datetime]] = None, new_id: Optional[Callable[[], str]] = None,
                 new_tag: Optional[Callable[[], str]] = None) -> None:
        """`clock` (default: UTC now), `new_id` (a random connection id with no `:` or `/`) and `new_tag` (a random
        candidate tag, `drive_ref.is_valid_tag`) are injectable for tests."""
        self._connections = connections
        self._credentials = credentials
        self._clock: Callable[[], datetime] = clock or _utcnow
        self._new_id: Callable[[], str] = new_id or _random_id
        self._new_tag: Callable[[], str] = new_tag or _random_tag
        self._activate_lock = asyncio.Lock()

    # ------------------------------------------------------------------ connect

    async def activate(self, credential: OAuthCredential, drive_identity: str) -> DriveConnection:
        """The drive activation of a completed consent (RFC §5.2): create the connection of `credential` (a saved drive
        credential) or, when it matches a live connection of the user (module docstring), reconnect it (point it at
        this credential, state ACTIVE). Sets the primary when the user has none and the connection has a write folder
        (a new connection has none, D11: it becomes the primary with its first write folder). `drive_identity` is the
        adapter-built
        identity of the drive the grant reads (`get_root` after the save). Returns the stored connection. ValueError for
        a credential without `credential_id` or `account_id`, or an empty `drive_identity`."""
        if not credential.credential_id or not credential.account_id:
            raise ValueError("a drive connection needs a saved credential with its credential_id and account_id")
        if not drive_identity:
            raise ValueError("a drive connection needs the drive's identity")
        user_id = credential.user_id
        async with self._activate_lock:
            every = await self._connections.list_for_user(user_id)
            live = [c for c in every if c.state in _LIVE and c.kind == credential.kind]
            match = next((c for c in live if c.credential_id == credential.credential_id), None) \
                or next((c for c in live if c.drive_identity == drive_identity), None)
            if match is not None:
                connection = await self._reconnect(match, credential, drive_identity)
            else:
                connection = await self._create(credential, drive_identity, every)
            await self._ensure_primary(connection)
            return connection

    async def _reconnect(self, current: DriveConnection, credential: OAuthCredential,
                         drive_identity: str) -> DriveConnection:
        user_id, connection_id = current.user_id, current.connection_id
        if current.drive_identity != drive_identity:
            logger.warning(f"🔗 Drive connection {connection_id[:8]} of user={user_id[:8]} now reads another drive "
                           f"identity on reconnect; stored the new one")
        account = _stored_account(credential)
        if (current.credential_id != credential.credential_id or current.drive_identity != drive_identity
                or current.provider_account != account):
            await self._connections.update(user_id, connection_id, credential_id=credential.credential_id,
                                           drive_identity=drive_identity, provider_account=account)
        if current.state is ConnectionState.NEEDS_RECONNECT:
            await self._connections.transition(user_id, connection_id, ConnectionState.ACTIVE, self._clock())
        stored = await self._connections.get(user_id, connection_id)
        if stored is None:      # connections are never deleted; a vanished document is a broken store
            raise RuntimeError(f"drive connection {connection_id[:8]} vanished during its reconnect")
        logger.info(f"🔗 Drive reconnected: user={user_id[:8]} connection={connection_id[:8]} "
                    f"state={stored.state.value}")
        return stored

    async def _create(self, credential: OAuthCredential, drive_identity: str,
                      every: List[DriveConnection]) -> DriveConnection:
        taken_tags: Set[str] = {c.ref_tag for c in every}
        now = self._clock()
        name = default_mount_name(every, now)
        for _ in range(_MAX_CREATE_ATTEMPTS):
            tag = self._new_tag()
            if not drive_ref.is_valid_tag(tag) or tag in taken_tags:
                continue
            connection = DriveConnection(
                connection_id=self._new_id(), user_id=credential.user_id, account_id=credential.account_id or "",
                credential_id=credential.credential_id, kind=credential.kind, drive_identity=drive_identity,
                ref_tag=tag, mount_name=name, created_at=now, name_history=(MountNameChange(name=name, at=now),),
                provider_account=_stored_account(credential), write_folder_id=None)       # D11: read-only at first
            if await self._connections.create(connection):
                return connection
            taken_tags.add(tag)        # the id was taken: try another id and tag
        logger.error(f"💥 No free connection id and ref tag for user={credential.user_id[:8]} after "
                     f"{_MAX_CREATE_ATTEMPTS} attempts")
        raise RuntimeError("no free drive connection id and ref tag")

    async def _ensure_primary(self, connection: DriveConnection) -> None:
        """Point the primary at `connection` when the user has none (no pointer, or one at an absent or DISCONNECTED
        connection) and `connection` is ACTIVE with a write folder (I2)."""
        current = await self._pointed(connection.user_id)
        if current is not None and current.state in _LIVE:
            return
        if connection.state is ConnectionState.ACTIVE and connection.write_folder_id is not None:
            await self._connections.save_settings(
                DriveUserSettings(user_id=connection.user_id, primary_connection_id=connection.connection_id))
            logger.info(f"🔗 Drive primary of user={connection.user_id[:8]} -> {connection.connection_id[:8]}")

    # ------------------------------------------------------------------ reads

    async def get(self, user_id: str, connection_id: str) -> Optional[DriveConnection]:
        """One connection of the user, in any state; None when absent."""
        return await self._connections.get(user_id, connection_id)

    async def list_for_user(self, user_id: str, *, include_disconnected: bool = False) -> List[DriveConnection]:
        """The user's connections, oldest first; DISCONNECTED ones only with `include_disconnected`. Under the
        connector audience only the drives it sees."""
        every = await self._visible_of(user_id)
        return every if include_disconnected else [c for c in every if c.state in _LIVE]

    async def _visible_of(self, user_id: str) -> List[DriveConnection]:
        """Every connection of the user, oldest first; under the connector audience only the drives it sees."""
        every = await self._connections.list_for_user(user_id)
        return [c for c in every if _visible(c)] if _connector_reads() else every

    async def handle_for(self, user_id: str, connection_id: str) -> DriveHandle:
        """The handle of a live (ACTIVE or NEEDS_RECONNECT) connection; DriveNotConnectedError otherwise."""
        return (await self._live(user_id, connection_id)).handle

    async def _live(self, user_id: str, connection_id: str) -> DriveConnection:
        connection = await self._connections.get(user_id, connection_id)
        if connection is None or connection.state not in _LIVE:
            raise DriveNotConnectedError("The drive connection is absent or no longer connected")
        return connection

    async def _pointed(self, user_id: str) -> Optional[DriveConnection]:
        """The connection the primary pointer names, in any state; None without a pointer or for an absent one."""
        settings = await self._connections.get_settings(user_id)
        if settings is None or settings.primary_connection_id is None:
            return None
        return await self._connections.get(user_id, settings.primary_connection_id)

    async def primary(self, user_id: str) -> Optional[DriveConnection]:
        """The user's primary connection, read fresh; None when there is none or it is not ACTIVE. Under the connector
        audience: the pointed one when ACTIVE and visible, else the oldest ACTIVE visible one, else None (§7.4)."""
        pointed = await self._pointed(user_id)
        if not _connector_reads():
            return pointed if pointed is not None and pointed.state is ConnectionState.ACTIVE else None
        if pointed is not None and pointed.state is ConnectionState.ACTIVE and _visible(pointed):
            return pointed
        return next((c for c in await self._visible_of(user_id) if c.state is ConnectionState.ACTIVE), None)

    async def primary_handle(self, user_id: str) -> DriveHandle:
        """The primary's handle: the stage-1 way to address a path. DriveNotConnectedError when there is no primary."""
        primary = await self.primary(user_id)
        if primary is None:
            raise DriveNotConnectedError("No drive is connected")
        return primary.handle

    async def set_primary(self, user_id: str, connection_id: str) -> None:
        """Point the primary at an ACTIVE connection of the user with a write folder (I2). DriveNotConnectedError for
        one that is not ACTIVE; DriveSettingRefused(PRIMARY_NEEDS_WRITE_FOLDER_LINE) for a read-only one."""
        connection = await self._connections.get(user_id, connection_id)
        if connection is None or connection.state is not ConnectionState.ACTIVE:
            raise DriveNotConnectedError("Only a connected drive can be the primary")
        if connection.write_folder_id is None:
            raise DriveSettingRefused(PRIMARY_NEEDS_WRITE_FOLDER_LINE)
        await self._connections.save_settings(DriveUserSettings(user_id=user_id, primary_connection_id=connection_id))
        logger.info(f"🔗 Drive primary of user={user_id[:8]} set to {connection_id[:8]}")

    async def status(self, user_id: str) -> DriveConnectionStatus:
        """`DriveConnectionStatus.of(primary connection)` (the drive gate's and search's read). The
        pointed connection is read in any state (a primary that needs a reconnect reads "connected, needs reconnect");
        without a live pointed one, the oldest live connection. Under the connector audience over the visible drives
        only."""
        pointed = await self._pointed(user_id)
        if pointed is not None and _connector_reads() and not _visible(pointed):
            pointed = None
        if pointed is None or pointed.state not in _LIVE:
            live = await self.list_for_user(user_id)
            pointed = live[0] if live else None
        return DriveConnectionStatus.of(pointed)

    async def resolve_ref(self, user_id: str, text: str) -> Optional[Union[ResolvedDriveRef, DriveRefRefusal]]:
        """The connection and item id of the first drive ref in `text` (`drive_ref.parse`):
        - a tag of a live connection of the user -> ResolvedDriveRef;
        - an untagged ref, or a tag the user never had -> DriveRefRefusal(`stale_ref_line(ref)`);
        - the tag of a DISCONNECTED connection -> DriveRefRefusal(`disconnected_ref_line(ref)`);
        - no drive ref in `text` -> None.
        Under the connector audience a hidden drive's tag answers as a tag the user never had."""
        parsed = drive_ref.parse(text)
        if parsed is None:
            return None
        if not isinstance(parsed, drive_ref.DriveRef):
            return DriveRefRefusal(line=stale_ref_line(str(parsed)))
        owner = next((c for c in await self._visible_of(user_id) if c.ref_tag == parsed.tag), None)
        if owner is None:
            return DriveRefRefusal(line=stale_ref_line(str(parsed)))
        if owner.state is ConnectionState.DISCONNECTED:
            return DriveRefRefusal(line=disconnected_ref_line(str(parsed)))
        return ResolvedDriveRef(connection=owner, item_id=parsed.item_id)

    async def mounts(self, user_id: str) -> MountTable:
        """The user's mount tree, read fresh (RFC §11.1): the live connections, oldest first, with their held former
        names, and the primary the pointer names (ACTIVE or NEEDS_RECONNECT). Under the connector audience only the
        visible drives, the pointer moved to the first visible live one when it names a hidden drive (§7.4)."""
        every = await self._visible_of(user_id)
        settings = await self._connections.get_settings(user_id)
        pointer = None if settings is None else settings.primary_connection_id
        if _connector_reads():
            live = [c.connection_id for c in every if c.state in _LIVE]
            if pointer not in live:
                pointer = live[0] if live else None
        return mount_table(every, pointer, self._clock())

    async def connection_of_tag(self, user_id: str, tag: str) -> Optional[DriveConnection]:
        """The user's connection whose ref tag is `tag`, in any state (DISCONNECTED included); None when the user never
        had it. Under the connector audience None for a hidden drive."""
        return next((c for c in await self._visible_of(user_id) if c.ref_tag == tag), None)

    # ------------------------------------------------------------------ settings (Cabinet actions, D18)

    async def set_indexing(self, user_id: str, connection_id: str, enabled: bool) -> DriveConnection:
        """Set the connection's indexing switch; returns the stored connection. DriveNotConnectedError for an absent or
        DISCONNECTED connection."""
        await self._live(user_id, connection_id)
        stored = await self._connections.update(user_id, connection_id, indexing_enabled=enabled)
        if stored is None:
            raise DriveNotConnectedError("The drive connection is absent")
        logger.info(f"🗂️ Drive indexing {'on' if enabled else 'off'}: user={user_id[:8]} "
                    f"connection={connection_id[:8]}")
        return stored

    async def set_transcribe(self, user_id: str, connection_id: str, enabled: bool) -> DriveConnection:
        """Set the connection's transcription switch; as `set_indexing`."""
        await self._live(user_id, connection_id)
        stored = await self._connections.update(user_id, connection_id, transcribe_enabled=enabled)
        if stored is None:
            raise DriveNotConnectedError("The drive connection is absent")
        logger.info(f"🎙️ Drive transcription {'on' if enabled else 'off'}: user={user_id[:8]} "
                    f"connection={connection_id[:8]}")
        return stored

    async def default_name(self, user_id: str) -> str:
        """The name "Add a drive" prefills (`drive_mounts.default_mount_name` over every connection of the user)."""
        return default_mount_name(await self._connections.list_for_user(user_id), self._clock())

    async def rename(self, user_id: str, connection_id: str, name: str, *,
                     primary_top_folders: Sequence[str] = ()) -> MountNameCheck:
        """RFC §11.8: validate `name` (`validate_mount_name`, run inside the repository's transaction over the user's
        connections) and, when valid and different, store it with its history entry. `primary_top_folders` (the
        primary's top-level folder names, read by the caller) give the shadowing warning. Returns the check: an error
        wrote nothing. DriveNotConnectedError for an absent or DISCONNECTED connection."""
        settings = await self._connections.get_settings(user_id)
        pointer = None if settings is None else settings.primary_connection_id
        now = self._clock()

        def check(every: Sequence[DriveConnection]) -> MountNameCheck:
            return validate_mount_name(name, connection_id=connection_id, connections=every,
                                       primary_connection_id=pointer, primary_top_folders=primary_top_folders,
                                       now=now)

        result = await self._connections.rename(user_id, connection_id, now, check)
        if result is None:
            raise DriveNotConnectedError("The drive connection is absent or no longer connected")
        logger.info(f"🏷️ Drive rename: user={user_id[:8]} connection={connection_id[:8]} "
                    f"{'stored' if result.ok else 'refused'}{' with a warning' if result.warning else ''}")
        return result

    async def set_searched_by_default(self, user_id: str, connection_id: str, enabled: bool) -> DriveConnection:
        """Set whether an empty `drives` value searches the connection (D4). DriveNotConnectedError for an absent or
        DISCONNECTED connection."""
        connection = await self._live(user_id, connection_id)
        return await self._write_settings(connection, searched_by_default=enabled)

    async def set_scope(self, user_id: str, connection_id: str, *, folder_ids: Sequence[str], whole_drive: bool,
                        capabilities: DriveCapabilities, write_folder_ancestry: Optional[Ancestry],
                        folder_ancestries: Mapping[str, Ancestry],
                        connector_ancestries: Mapping[str, Ancestry] = MappingProxyType({})) -> DriveConnection:
        """Set the folders to index or the whole drive (§11.3). The selection is normalised: de-duplicated, order kept,
        and a folder whose ancestor is also selected is dropped (nested scope folders: a delete inside the write folder
        could otherwise take a protected scope folder with its parent). `folder_ancestries` holds each selected
        folder's chain and `write_folder_ancestry` the write folder's, both read by the caller through
        `UserDriveService.ancestry` (this service makes no provider call); a selected folder without its chain is a
        ValueError. DriveSettingRefused: SCOPE_BOTH_LINE for both; SCOPE_LEAVES_WRITE_FOLDER_LINE when the current
        write folder would lie outside the new scope (the root as the write folder is inside only a whole-drive scope
        or an AREA feed's default scope); SCOPE_STRANDS_CONNECTOR_LINE when a connector folder would lie outside the new
        scope (`connector_ancestries` holds each connector folder's chain, read by the caller; a missing one is a
        ValueError; whole-drive connector access never strands). DriveNotConnectedError for an absent or DISCONNECTED
        connection."""
        connection = await self._live(user_id, connection_id)
        ids = tuple(dict.fromkeys(folder_ids))
        if whole_drive and ids:
            raise DriveSettingRefused(SCOPE_BOTH_LINE)
        ids = _outermost(ids, folder_ancestries)
        folder = connection.write_folder_id
        if folder is not None:
            scope = ConnectionAccess(drive_name=connection.mount_name, root_item_id="", scope_folder_ids=frozenset(ids),
                                     scope_whole_drive=whole_drive, write_folder_id=folder)
            if not self._admits(scope, capabilities, folder, write_folder_ancestry):
                raise DriveSettingRefused(SCOPE_LEAVES_WRITE_FOLDER_LINE.format(drive=connection.mount_name))
        connector = connection.connector_access
        if connector is not None and connector.folder_ids:
            scope = ConnectionAccess(drive_name=connection.mount_name, root_item_id="", scope_folder_ids=frozenset(ids),
                                     scope_whole_drive=whole_drive, write_folder_id=None)
            for connector_folder in connector.folder_ids:
                chain = connector_ancestries.get(connector_folder)
                if chain is None or chain.item_id != connector_folder:
                    raise ValueError("set_scope needs each connector folder's own ancestry")
                if not in_scope(scope, capabilities, chain):
                    raise DriveSettingRefused(SCOPE_STRANDS_CONNECTOR_LINE)
        return await self._write_settings(connection, scope_folder_ids=ids, scope_whole_drive=whole_drive)

    async def set_write_folder(self, user_id: str, connection_id: str, folder_id: Optional[str], *,
                               capabilities: DriveCapabilities, ancestry: Optional[Ancestry]) -> DriveConnection:
        """Set the write folder (§11.3, D11): None makes the drive read-only (refused while it is the primary,
        PRIMARY_NEEDS_WRITE_FOLDER_LINE); ROOT_FOLDER or a folder id must lie inside the scope (`ancestry` is the
        folder's chain, read by the caller) else WRITE_FOLDER_OUTSIDE_SCOPE_LINE; both as DriveSettingRefused. A user
        with no live primary gets this connection as the primary when it is ACTIVE (E-17). DriveNotConnectedError for
        an absent or DISCONNECTED connection."""
        connection = await self._live(user_id, connection_id)
        if folder_id is None:
            settings = await self._connections.get_settings(user_id)
            if settings is not None and settings.primary_connection_id == connection_id:
                raise DriveSettingRefused(PRIMARY_NEEDS_WRITE_FOLDER_LINE)
        else:
            if folder_id != ROOT_FOLDER and ancestry is not None and ancestry.item_id != folder_id:
                raise ValueError("the ancestry given is not the write folder's")
            if not self._admits(ConnectionAccess.of(connection, ""), capabilities, folder_id, ancestry):
                raise DriveSettingRefused(WRITE_FOLDER_OUTSIDE_SCOPE_LINE.format(drive=connection.mount_name))
        stored = await self._write_settings(connection, write_folder_id=folder_id)
        await self._ensure_primary(stored)
        return stored

    async def set_connector_access(self, user_id: str, connection_id: str, access: Optional[ConnectorAccess], *,
                                   capabilities: DriveCapabilities, root_item_id: str,
                                   folder_ancestries: Mapping[str, Ancestry]) -> DriveConnection:
        """Set what the drive MCP connector reads on this drive (DRIVE_MCP_CONNECTOR_RFC §7.1, §7.6): None (nothing),
        the whole drive, or folders. Folders are normalised to the outermost ones (`folder_ancestries` holds each
        chain, read by the caller through `UserDriveService.ancestry`; a missing one is a ValueError) and each must lie
        inside the scope; the drive's root (`root_item_id`) is refused. DriveSettingRefused: CONNECTOR_ROOT_LINE,
        CONNECTOR_FOLDER_OUTSIDE_SCOPE_LINE. DriveNotConnectedError for an absent or DISCONNECTED connection."""
        connection = await self._live(user_id, connection_id)
        if access is not None and access.folder_ids:
            ids = _outermost(access.folder_ids, folder_ancestries)
            if root_item_id in ids:
                raise DriveSettingRefused(CONNECTOR_ROOT_LINE)
            scope = ConnectionAccess.of(connection, root_item_id)
            for folder in ids:
                if not in_scope(scope, capabilities, folder_ancestries[folder]):
                    raise DriveSettingRefused(CONNECTOR_FOLDER_OUTSIDE_SCOPE_LINE.format(drive=connection.mount_name))
            access = ConnectorAccess.folders(ids)
        stored = await self._connections.update_connector_access(user_id, connection_id, access)
        if stored is None:
            raise DriveNotConnectedError("The drive connection is absent")
        shown = "nothing" if access is None else "whole" if access.whole else f"{len(access.folder_ids)} folders"
        logger.info(f"🔌 Drive connector access of connection {connection_id[:8]} (user={user_id[:8]}) → {shown}")
        return stored

    @staticmethod
    def _admits(scope: ConnectionAccess, capabilities: DriveCapabilities, folder: str,
                ancestry: Optional[Ancestry]) -> bool:
        """The scope admits the write folder `folder`: the root only as the whole drive or an AREA default scope."""
        if folder == ROOT_FOLDER:
            return scope.scope_whole_drive or (not scope.scope_folder_ids
                                               and capabilities.feed_scope is FeedScope.AREA)
        return in_scope(scope, capabilities, ancestry)

    async def _write_settings(self, connection: DriveConnection, **changes: Any) -> DriveConnection:
        """Replace the changed settings (the domain's rules run on the result) and write the four whole."""
        wanted = replace(connection, **changes)
        stored = await self._connections.update_settings(
            connection.user_id, connection.connection_id, searched_by_default=wanted.searched_by_default,
            write_folder_id=wanted.write_folder_id, scope_folder_ids=wanted.scope_folder_ids,
            scope_whole_drive=wanted.scope_whole_drive)
        if stored is None:
            raise DriveNotConnectedError("The drive connection is absent")
        logger.info(f"⚙️ Drive settings of connection {connection.connection_id[:8]} (user={connection.user_id[:8]}) "
                    f"changed: {', '.join(sorted(changes))}")
        return stored

    # ------------------------------------------------------------------ state

    async def flag_reconnect(self, handle: DriveHandle) -> bool:
        """The adapter raised OAuthGrantRevokedError for `handle`: move the connection ACTIVE -> NEEDS_RECONNECT and set
        the credential's `needs_reconnect`. True only on the transition (the one notice); False when already flagged,
        absent, DISCONNECTED, or when the connection no longer reads the handle's credential (a reconnect replaced the
        grant the error came from)."""
        connection = await self._connections.get(handle.user_id, handle.connection_id)
        if connection is None:
            return False
        if connection.credential_id != handle.credential_id:
            logger.info(f"Drive reconnect flag skipped: connection {handle.connection_id[:8]} reads another grant now")
            return False
        if not await self._connections.transition(handle.user_id, handle.connection_id,
                                                  ConnectionState.NEEDS_RECONNECT, self._clock()):
            return False
        logger.warning(f"Drive connection {handle.connection_id[:8]} of user={handle.user_id[:8]} needs a reconnect")
        await self._flag_credential(handle)
        return True

    async def _flag_credential(self, handle: DriveHandle) -> None:
        """Keep the credential's `needs_reconnect` in step. Best effort: drives read the connection's state."""
        try:
            credential = await self._credentials.get(handle.user_id, handle.credential_id)
            if credential is not None and not credential.needs_reconnect:
                await self._credentials.update(replace(credential, needs_reconnect=True))
        except Exception as exc:
            logger.error(f"💥 Setting needs_reconnect on credential {handle.credential_id[:8]} failed: "
                         f"{type(exc).__name__}", exc_info=True)

    async def disconnect(self, user_id: str, connection_id: str, *, heir_id: Optional[str] = None) -> bool:
        """End the connection (DISCONNECTED, `disconnected_at` now; the document and tag stay reserved) and, when it was
        the primary, hand the primary to `heir_id` (the Cabinet asks the owner first, RFC §11.7) or clear it; no heir is
        chosen automatically. `heir_id` must be another ACTIVE connection with a write folder, checked before anything
        is written: DriveSettingRefused(HEIR_INVALID_LINE) otherwise. True only on the transition. The purge,
        transcript retention (I8), note archive, `forget` and the revoke rule are run by the disconnect flow that calls
        this, not here."""
        if heir_id is not None:
            heir = await self._connections.get(user_id, heir_id)
            if (heir_id == connection_id or heir is None or heir.state is not ConnectionState.ACTIVE
                    or heir.write_folder_id is None):
                raise DriveSettingRefused(HEIR_INVALID_LINE)
        if not await self._connections.transition(user_id, connection_id, ConnectionState.DISCONNECTED,
                                                  self._clock()):
            return False
        settings = await self._connections.get_settings(user_id)
        if settings is not None and settings.primary_connection_id == connection_id:
            await self._connections.save_settings(DriveUserSettings(user_id=user_id, primary_connection_id=heir_id))
            logger.info(f"🔗 Drive primary of user={user_id[:8]} -> "
                        f"{'none' if heir_id is None else heir_id[:8]} after a disconnect")
        logger.info(f"🔌 Drive connection {connection_id[:8]} of user={user_id[:8]} disconnected")
        return True

    async def reconnect_needed(self, user_id: str) -> List[DriveConnection]:
        """R1 (`decisions/reconnect_notice_r1.md`) reading connections: the user's connections owed the reconnect notice:
        NEEDS_RECONNECT ones, and ACTIVE ones whose credential is gone (checked with `list_keys`, never a decrypt, E28).
        DISCONNECTED never is."""
        live = await self.list_for_user(user_id)
        held: Dict[str, Set[str]] = {}
        owed: List[DriveConnection] = []
        for connection in live:
            if connection.state is ConnectionState.NEEDS_RECONNECT:
                owed.append(connection)
                continue
            if connection.kind not in held:
                held[connection.kind] = {k.credential_id for k in await self._credentials.list_keys(user_id,
                                                                                                    connection.kind)}
            if connection.credential_id not in held[connection.kind]:
                owed.append(connection)
        return owed
