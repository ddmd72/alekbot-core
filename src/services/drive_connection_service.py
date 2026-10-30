"""DriveConnectionService — the one owner of drive connections and the primary pointer (MULTI_DRIVE_RFC §3, §4.4, §6,
§8, §9).

SKELETON (slice 1b, C1b-3): the public interface is frozen here; slice 1b part P1b-1 implements every body (they raise
NotImplementedError until then). Callers (P1b-3 services, P1b-4 indexer, P1b-5 notes) code against these signatures.

Rules the implementation keeps (RFC §4.4):
- A connect whose `(kind, account)` matches an ACTIVE or NEEDS_RECONNECT connection of the user is that connection's
  reconnect (same id, same tag, ACTIVE again); otherwise a new connection with a new random id and a new tag, unique per
  user and never reused, named `Drive`, `Drive 2`, … (E11). DISCONNECTED is terminal.
- The primary pointer (`DriveUserSettings.primary_connection_id`) is read fresh on every use and written only here;
  I2: the primary is ACTIVE (stage 1: its write folder is the area root).
- Drives read reconnect state only from the connection; this service keeps the credential's `needs_reconnect` in step.
- Every setting is a Cabinet action (D18); no chat path calls a setter.

Errors: `DriveNotConnectedError` (domain) for a connection that is absent, another user's, or DISCONNECTED where a live
one is needed.

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
from datetime import datetime
from typing import Callable, List, Optional, Union

from ..domain.drive_connection import DriveConnection
from ..domain.drive_connection_status import DriveConnectionStatus
from ..domain.drive_handle import DriveHandle
from ..domain.drive_ref_refusal import DriveRefRefusal
from ..domain.drive_resolved_ref import ResolvedDriveRef
from ..domain.oauth_credential import OAuthCredential
from ..ports.drive_connection_repository import DriveConnectionRepository
from ..ports.oauth_credentials_port import OAuthCredentialsPort


class DriveConnectionService:

    def __init__(self, connections: DriveConnectionRepository, credentials: OAuthCredentialsPort, *,
                 clock: Optional[Callable[[], datetime]] = None, new_id: Optional[Callable[[], str]] = None,
                 new_tag: Optional[Callable[[], str]] = None) -> None:
        """`clock` (default: UTC now), `new_id` (a random connection id with no `:` or `/`) and `new_tag` (a random
        candidate tag, `drive_ref.is_valid_tag`) are injectable for tests."""
        self._connections = connections
        self._credentials = credentials
        self._clock = clock
        self._new_id = new_id
        self._new_tag = new_tag

    async def activate(self, credential: OAuthCredential, drive_identity: str) -> DriveConnection:
        """The drive activation of a completed consent (RFC §5.2): create the connection of `credential` (a saved drive
        credential) or, when `(kind, account)` matches a live connection of the user, reconnect it (point it at this
        credential, state ACTIVE). Sets the primary when the user has none. `drive_identity` is the adapter-built
        identity of the drive the grant reads (`get_root` after the save). Returns the stored connection. ValueError for
        a credential without `credential_id` or `account_id`."""
        raise NotImplementedError

    async def get(self, user_id: str, connection_id: str) -> Optional[DriveConnection]:
        """One connection of the user, in any state; None when absent."""
        raise NotImplementedError

    async def list_for_user(self, user_id: str, *, include_disconnected: bool = False) -> List[DriveConnection]:
        """The user's connections, oldest first; DISCONNECTED ones only with `include_disconnected`."""
        raise NotImplementedError

    async def handle_for(self, user_id: str, connection_id: str) -> DriveHandle:
        """The handle of a live (ACTIVE or NEEDS_RECONNECT) connection; DriveNotConnectedError otherwise."""
        raise NotImplementedError

    async def primary(self, user_id: str) -> Optional[DriveConnection]:
        """The user's primary connection, read fresh; None when there is none or it is not ACTIVE."""
        raise NotImplementedError

    async def primary_handle(self, user_id: str) -> DriveHandle:
        """The primary's handle: the stage-1 way to address a path. DriveNotConnectedError when there is no primary."""
        raise NotImplementedError

    async def set_primary(self, user_id: str, connection_id: str) -> None:
        """Point the primary at an ACTIVE connection of the user (I2); DriveNotConnectedError otherwise."""
        raise NotImplementedError

    async def status(self, user_id: str) -> DriveConnectionStatus:
        """`DriveConnectionStatus.of(primary connection)` (the Cabinet's `/api/drive/status` until stage 2)."""
        raise NotImplementedError

    async def resolve_ref(self, user_id: str, text: str) -> Optional[Union[ResolvedDriveRef, DriveRefRefusal]]:
        """The connection and item id of the first drive ref in `text` (`drive_ref.parse`):
        - a tag of a live connection of the user -> ResolvedDriveRef;
        - an untagged ref, or a tag the user never had -> DriveRefRefusal(`stale_ref_line(ref)`);
        - the tag of a DISCONNECTED connection -> DriveRefRefusal(`disconnected_ref_line(ref)`);
        - no drive ref in `text` -> None."""
        raise NotImplementedError

    async def set_indexing(self, user_id: str, connection_id: str, enabled: bool) -> DriveConnection:
        """Set the connection's indexing switch; returns the stored connection. DriveNotConnectedError for an absent or
        DISCONNECTED connection."""
        raise NotImplementedError

    async def set_transcribe(self, user_id: str, connection_id: str, enabled: bool) -> DriveConnection:
        """Set the connection's transcription switch; as `set_indexing`."""
        raise NotImplementedError

    async def flag_reconnect(self, handle: DriveHandle) -> bool:
        """The adapter raised OAuthGrantRevokedError for `handle`: move the connection ACTIVE -> NEEDS_RECONNECT and set
        the credential's `needs_reconnect`. True only on the transition (the one notice); False when already flagged,
        absent or DISCONNECTED."""
        raise NotImplementedError

    async def disconnect(self, user_id: str, connection_id: str) -> bool:
        """End the connection (DISCONNECTED, `disconnected_at` now; the document and tag stay reserved) and, when it was
        the primary, hand the primary to the oldest other ACTIVE connection (or clear it). True only on the transition.
        The purge, transcript retention (I8), note archive, `forget` and the revoke rule are run by the disconnect flow
        that calls this (stage 2), not here."""
        raise NotImplementedError

    async def reconnect_needed(self, user_id: str) -> List[DriveConnection]:
        """R1 (`decisions/reconnect_notice_r1.md`) reading connections: the user's connections owed the reconnect notice:
        NEEDS_RECONNECT ones, and ACTIVE ones whose credential is gone (checked with `list_keys`, never a decrypt, E28).
        DISCONNECTED never is."""
        raise NotImplementedError
