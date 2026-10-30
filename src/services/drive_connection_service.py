"""DriveConnectionService — the one owner of drive connections and the primary pointer (MULTI_DRIVE_RFC §3, §4.4, §6,
§8, §9).

Rules (RFC §4.4):
- A connect whose account matches a live (ACTIVE or NEEDS_RECONNECT) connection of the user and kind is that
  connection's reconnect: same id, same tag, ACTIVE again, pointed at the consent's credential. "Same account" is read
  as: the same `credential_id` (the credential store keeps one record per user, kind and provider account, so a consent
  of the same account keeps its credential id), else the same `drive_identity` (the drive a grant of that account
  reads; this also catches the account whose credential was deleted while the connection stayed ACTIVE). Otherwise a
  new connection: a random id, a new tag unique among every connection the user ever had (DISCONNECTED included, never
  reused), the lowest free name of `Drive`, `Drive 2`, … (E11), indexing and transcription off (the `DriveConnection`
  defaults). DISCONNECTED is terminal.
- The primary pointer (`DriveUserSettings.primary_connection_id`) is read fresh on every use and written only here.
  The first live connection becomes the primary; a disconnect of the primary hands it to the oldest other ACTIVE
  connection, or clears it. I2: `set_primary` only names an ACTIVE connection.
- Drives read reconnect state only from the connection; `flag_reconnect` keeps the credential's `needs_reconnect` in
  step (best effort: the connection is the source of truth), and a consent clears both (the connect service saves the
  credential with the flag down, `activate` moves the connection back to ACTIVE).
- Every setting is a Cabinet action (D18); no chat path calls a setter.

Errors: `DriveNotConnectedError` (domain) for a connection that is absent, another user's, or DISCONNECTED where a live
one is needed (ACTIVE for `set_primary`).

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
    status(user_id) -> DriveConnectionStatus
    resolve_ref(user_id, text) -> ResolvedDriveRef | DriveRefRefusal | None
    set_indexing(user_id, connection_id, enabled) -> DriveConnection
    set_transcribe(user_id, connection_id, enabled) -> DriveConnection
    flag_reconnect(handle) -> bool
    disconnect(user_id, connection_id) -> bool
    reconnect_needed(user_id) -> List[DriveConnection]
"""
import asyncio
import secrets
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Union

from ..domain import drive_ref
from ..domain.drive_connection import DEFAULT_MOUNT_NAME, DriveConnection
from ..domain.drive_connection_state import ConnectionState
from ..domain.drive_connection_status import DriveConnectionStatus
from ..domain.drive_handle import DriveHandle
from ..domain.drive_ref_refusal import DriveRefRefusal, disconnected_ref_line, stale_ref_line
from ..domain.drive_resolved_ref import ResolvedDriveRef
from ..domain.drive_user_settings import DriveUserSettings
from ..domain.oauth_credential import OAuthCredential
from ..domain.user_drive import DriveNotConnectedError
from ..ports.drive_connection_repository import DriveConnectionRepository
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..utils.logger import logger

_MAX_CREATE_ATTEMPTS = 32
_LIVE = (ConnectionState.ACTIVE, ConnectionState.NEEDS_RECONNECT)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _random_id() -> str:
    return secrets.token_hex(8)


def _random_tag() -> str:
    """A 2-character tag: a letter, then a letter or digit (936 values; a user has a handful of connections)."""
    return secrets.choice(drive_ref.TAG_FIRST_ALPHABET) + secrets.choice(drive_ref.TAG_ALPHABET)


def _free_mount_name(connections: List[DriveConnection]) -> str:
    """The lowest of `Drive`, `Drive 2`, `Drive 3`, … that no live connection of the user is named (casefold)."""
    taken = {c.mount_name.casefold() for c in connections if c.state in _LIVE}
    if DEFAULT_MOUNT_NAME.casefold() not in taken:
        return DEFAULT_MOUNT_NAME
    n = 2
    while f"{DEFAULT_MOUNT_NAME} {n}".casefold() in taken:
        n += 1
    return f"{DEFAULT_MOUNT_NAME} {n}"


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
        this credential, state ACTIVE). Sets the primary when the user has none. `drive_identity` is the adapter-built
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
        if current.credential_id != credential.credential_id or current.drive_identity != drive_identity:
            await self._connections.update(user_id, connection_id, credential_id=credential.credential_id,
                                           drive_identity=drive_identity)
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
        name = _free_mount_name(every)
        for _ in range(_MAX_CREATE_ATTEMPTS):
            tag = self._new_tag()
            if not drive_ref.is_valid_tag(tag) or tag in taken_tags:
                continue
            connection = DriveConnection(
                connection_id=self._new_id(), user_id=credential.user_id, account_id=credential.account_id or "",
                credential_id=credential.credential_id, kind=credential.kind, drive_identity=drive_identity,
                ref_tag=tag, mount_name=name, created_at=self._clock())
            if await self._connections.create(connection):
                return connection
            taken_tags.add(tag)        # the id was taken: try another id and tag
        logger.error(f"💥 No free connection id and ref tag for user={credential.user_id[:8]} after "
                     f"{_MAX_CREATE_ATTEMPTS} attempts")
        raise RuntimeError("no free drive connection id and ref tag")

    async def _ensure_primary(self, connection: DriveConnection) -> None:
        """Point the primary at `connection` when the user has none (no pointer, or one at an absent or DISCONNECTED
        connection) and `connection` is ACTIVE."""
        current = await self._pointed(connection.user_id)
        if current is not None and current.state in _LIVE:
            return
        if connection.state is ConnectionState.ACTIVE:
            await self._connections.save_settings(
                DriveUserSettings(user_id=connection.user_id, primary_connection_id=connection.connection_id))
            logger.info(f"🔗 Drive primary of user={connection.user_id[:8]} -> {connection.connection_id[:8]}")

    # ------------------------------------------------------------------ reads

    async def get(self, user_id: str, connection_id: str) -> Optional[DriveConnection]:
        """One connection of the user, in any state; None when absent."""
        return await self._connections.get(user_id, connection_id)

    async def list_for_user(self, user_id: str, *, include_disconnected: bool = False) -> List[DriveConnection]:
        """The user's connections, oldest first; DISCONNECTED ones only with `include_disconnected`."""
        every = await self._connections.list_for_user(user_id)
        return every if include_disconnected else [c for c in every if c.state in _LIVE]

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
        """The user's primary connection, read fresh; None when there is none or it is not ACTIVE."""
        pointed = await self._pointed(user_id)
        return pointed if pointed is not None and pointed.state is ConnectionState.ACTIVE else None

    async def primary_handle(self, user_id: str) -> DriveHandle:
        """The primary's handle: the stage-1 way to address a path. DriveNotConnectedError when there is no primary."""
        primary = await self.primary(user_id)
        if primary is None:
            raise DriveNotConnectedError("No drive is connected")
        return primary.handle

    async def set_primary(self, user_id: str, connection_id: str) -> None:
        """Point the primary at an ACTIVE connection of the user (I2); DriveNotConnectedError otherwise."""
        connection = await self._connections.get(user_id, connection_id)
        if connection is None or connection.state is not ConnectionState.ACTIVE:
            raise DriveNotConnectedError("Only a connected drive can be the primary")
        await self._connections.save_settings(DriveUserSettings(user_id=user_id, primary_connection_id=connection_id))
        logger.info(f"🔗 Drive primary of user={user_id[:8]} set to {connection_id[:8]}")

    async def status(self, user_id: str) -> DriveConnectionStatus:
        """`DriveConnectionStatus.of(primary connection)` (the Cabinet's `/api/drive/status` until stage 2). The
        pointed connection is read in any state (a primary that needs a reconnect reads "connected, needs reconnect");
        without a live pointed one, the oldest live connection."""
        pointed = await self._pointed(user_id)
        if pointed is None or pointed.state not in _LIVE:
            live = await self.list_for_user(user_id)
            pointed = live[0] if live else None
        return DriveConnectionStatus.of(pointed)

    async def resolve_ref(self, user_id: str, text: str) -> Optional[Union[ResolvedDriveRef, DriveRefRefusal]]:
        """The connection and item id of the first drive ref in `text` (`drive_ref.parse`):
        - a tag of a live connection of the user -> ResolvedDriveRef;
        - an untagged ref, or a tag the user never had -> DriveRefRefusal(`stale_ref_line(ref)`);
        - the tag of a DISCONNECTED connection -> DriveRefRefusal(`disconnected_ref_line(ref)`);
        - no drive ref in `text` -> None."""
        parsed = drive_ref.parse(text)
        if parsed is None:
            return None
        if not isinstance(parsed, drive_ref.DriveRef):
            return DriveRefRefusal(line=stale_ref_line(str(parsed)))
        owner = next((c for c in await self._connections.list_for_user(user_id) if c.ref_tag == parsed.tag), None)
        if owner is None:
            return DriveRefRefusal(line=stale_ref_line(str(parsed)))
        if owner.state is ConnectionState.DISCONNECTED:
            return DriveRefRefusal(line=disconnected_ref_line(str(parsed)))
        return ResolvedDriveRef(connection=owner, item_id=parsed.item_id)

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

    async def disconnect(self, user_id: str, connection_id: str) -> bool:
        """End the connection (DISCONNECTED, `disconnected_at` now; the document and tag stay reserved) and, when it was
        the primary, hand the primary to the oldest other ACTIVE connection (or clear it). True only on the transition.
        The purge, transcript retention (I8), note archive, `forget` and the revoke rule are run by the disconnect flow
        that calls this, not here."""
        if not await self._connections.transition(user_id, connection_id, ConnectionState.DISCONNECTED,
                                                  self._clock()):
            return False
        settings = await self._connections.get_settings(user_id)
        if settings is not None and settings.primary_connection_id == connection_id:
            heir = next((c for c in await self._connections.list_for_user(user_id)
                         if c.state is ConnectionState.ACTIVE and c.connection_id != connection_id), None)
            await self._connections.save_settings(DriveUserSettings(
                user_id=user_id, primary_connection_id=None if heir is None else heir.connection_id))
            logger.info(f"🔗 Drive primary of user={user_id[:8]} -> "
                        f"{'none' if heir is None else heir.connection_id[:8]} after a disconnect")
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
