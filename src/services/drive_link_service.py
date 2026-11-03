"""DriveLinkService — the drive connector's one-time download link (DRIVE_MCP_CONNECTOR_RFC §8, E13, E18).

Public interface:
    DriveLinkService(store, drive, media, *, grant_allowed, digest_key, base_url, clock, new_id, max_reads)
        .issue(user_id, account_id, grant_id, file_ref) -> IssuedLink     (the MCP tool; under the handler's audience)
        .head(raw_id) -> Optional[LinkFile]                               (no consume, no bytes)
        .open(raw_id) -> Optional[OpenedLink]                             (consume, then checks, then the stream)

Issue: a grant id is required (a token minted before it existed → DriveLinkRefused); the ref is resolved and the file
checked under the audience the MCP handler set; a folder → DriveLinkFolder. The link keeps user, account, connection,
ref tag, item and grant id, `expires_at` = now + LINK_TTL_S; the raw id is returned in the URL only, the store keeps
its keyed digest.

Fetch (HEAD and GET): the store (`peek` / `consume`), then the grant (`grant_allowed`: an active refresh token with the
grant id and the whitelist), then under the connector audience a fresh ref resolution (a hidden or disconnected drive
is refused; the connection must be the one the link was issued on), then the access check (`get_item` /
`UserDriveService.media_source`: the folder is checked at fetch, not at issue), then, for a GET, the first chunk of the
stream. Every refusal answers None (the route's one 404); an unexpected exception is logged by type only and answers
None (fail closed). The consume comes first: a GET that is then refused has spent its read.

Logs: `[DriveLink] issued user=<8> key=<8>`, `[DriveLink] get|head ok|refused reason=… user=<8> key=<8>`; never the
raw id, the URL, the path or the name (§9).
"""
from __future__ import annotations

import mimetypes
import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, AsyncIterator, Awaitable, Callable, Optional, TypeVar

from ..domain import drive_ref
from ..domain.drive_audience import connector_audience
from ..domain.drive_link import (
    LINK_DIGEST_PURPOSE,
    LINK_MAX_READS,
    LINK_PATH,
    LINK_PURGE_AFTER_S,
    LINK_TTL_S,
    DriveLink,
)
from ..domain.drive_link_outcome import IssuedLink, LinkFile, OpenedLink
from ..domain.drive_mounts import canonical_path
from ..domain.drive_resolved_ref import ResolvedDriveRef
from ..domain.exceptions import DriveLinkFolder, DriveLinkRefused, MediaStreamFailed
from ..domain.request_context import RequestContext
from ..domain.secret_digest import keyed_digest
from ..domain.user_drive import (
    DriveAccessRefused,
    DriveItem,
    DriveItemNotFoundError,
    DriveNotConnectedError,
    DrivePathError,
    DriveRefRefused,
    drive_label,
)
from ..ports.drive_link_store import DriveLinkStore
from ..ports.media_stream_port import MediaStreamPort
from ..utils.logger import logger

if TYPE_CHECKING:
    from .user_drive_service import UserDriveService

GrantCheck = Callable[[str, Optional[str]], Awaitable[bool]]
_T = TypeVar("_T")
OCTET_STREAM = "application/octet-stream"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_raw_id() -> str:
    return secrets.token_urlsafe(16)


class _Refused(Exception):
    """One fetch refusal; `reason` is the log's reason word."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DriveLinkService:
    def __init__(self, store: DriveLinkStore, drive: "UserDriveService", media: MediaStreamPort, *,
                 grant_allowed: GrantCheck, digest_key: bytes, base_url: str,
                 clock: Callable[[], datetime] = _utcnow, new_id: Callable[[], str] = _new_raw_id,
                 max_reads: int = LINK_MAX_READS) -> None:
        if not digest_key:
            raise ValueError("the link store needs a digest key")
        if not 1 <= max_reads <= 3:
            raise ValueError("a link allows 1 to 3 reads")
        self._store = store
        self._drive = drive
        self._media = media
        self._grant_allowed = grant_allowed
        self._digest_key = digest_key
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        self._new_id = new_id
        self._max_reads = max_reads

    # -- issue ------------------------------------------------------------------------------------------------------

    async def issue(self, user_id: str, account_id: str, grant_id: Optional[str], file_ref: str) -> IssuedLink:
        """One link for the file at `file_ref`, checked under the caller's audience. Raises DriveLinkRefused (no grant
        id), DriveLinkFolder, and the drive's own refusals (DriveRefRefused, DriveAccessRefused,
        DriveItemNotFoundError, DriveNotConnectedError, DrivePathError)."""
        if not grant_id:
            logger.info(f"[DriveLink] issue refused reason=no_grant user={user_id[:8]}")
            raise DriveLinkRefused("the access token carries no grant id")
        target = await self._drive.resolve_ref(user_id, file_ref)
        connection = target.connection
        item = await self._drive.get_item(target.handle, target.item_id)
        shown = replace(item, path=canonical_path(connection.mount_name, item.path))
        if item.is_folder:
            raise DriveLinkFolder(shown.path or "/")
        raw = self._new_id()
        key = self._key(raw)
        now = self._clock()
        link = DriveLink(
            link_key=key, user_id=user_id, account_id=account_id, connection_id=connection.connection_id,
            ref_tag=connection.ref_tag, item_id=target.item_id, grant_id=grant_id, created_at=now,
            expires_at=now + timedelta(seconds=LINK_TTL_S), purge_after=now + timedelta(seconds=LINK_PURGE_AFTER_S))
        await self._store.create(link)
        logger.info(f"[DriveLink] issued user={user_id[:8]} key={key[:8]}")
        return IssuedLink(url=f"{self._base_url}{LINK_PATH}{raw}", label=drive_label(shown, connection.ref_tag),
                          expires_at=link.expires_at)

    # -- fetch ------------------------------------------------------------------------------------------------------

    async def head(self, raw_id: str) -> Optional[LinkFile]:
        """The link's file when every check passes; consumes nothing and reads no bytes."""
        key = self._key(raw_id)
        user = ""
        try:
            link = await self._store.peek(key, self._clock(), self._max_reads)
            if link is None:
                raise _Refused("unknown_or_spent")
            user = link.user_id
            target = await self._checked(link)
            with RequestContext(user_id=link.user_id, account_id=link.account_id), connector_audience():
                item = await self._access(lambda: self._drive.get_item(target.handle, link.item_id))
            found = LinkFile(name=item.name, mime=_mime_of(item))
        except _Refused as refused:
            self._log("head", "refused", refused.reason, user, key)
            return None
        except Exception as exc:
            logger.error(f"[DriveLink] head failed: {type(exc).__name__} user={user[:8]} key={key[:8]}")
            return None
        self._log("head", "ok", "", user, key)
        return found

    async def open(self, raw_id: str) -> Optional[OpenedLink]:
        """Consume one read, re-check everything, then open the stream (its first chunk fetched here)."""
        key = self._key(raw_id)
        user = ""
        try:
            link = await self._store.consume(key, self._clock(), self._max_reads)
            if link is None:
                raise _Refused("unknown_or_spent")
            user = link.user_id
            target = await self._checked(link)
            with RequestContext(user_id=link.user_id, account_id=link.account_id), connector_audience():
                item, source = await self._access(lambda: self._drive.media_source(target.handle, link.item_id))
            chunks = await _primed(self._media.chunks(source))
            opened = OpenedLink(name=item.name, mime=_mime_of(item), chunks=chunks)
        except _Refused as refused:
            self._log("get", "refused", refused.reason, user, key)
            return None
        except Exception as exc:
            logger.error(f"[DriveLink] get failed: {type(exc).__name__} user={user[:8]} key={key[:8]}")
            return None
        self._log("get", "ok", "", user, key)
        return opened

    async def _checked(self, link: DriveLink) -> ResolvedDriveRef:
        """The grant, then the connection read fresh under the connector audience: the link's own connection, live and
        visible to the connector."""
        if not await self._grant_allowed(link.user_id, link.grant_id):
            raise _Refused("grant")
        with RequestContext(user_id=link.user_id, account_id=link.account_id), connector_audience():
            try:
                target = await self._drive.resolve_ref(link.user_id, drive_ref.format(link.ref_tag, link.item_id))
            except (DriveRefRefused, DriveNotConnectedError, DrivePathError):
                raise _Refused("connection") from None
        if target.connection.connection_id != link.connection_id:
            raise _Refused("connection")
        return target

    @staticmethod
    async def _access(read: Callable[[], Awaitable[_T]]) -> _T:
        try:
            return await read()
        except DriveAccessRefused:
            raise _Refused("access") from None
        except (DriveItemNotFoundError, DrivePathError, DriveNotConnectedError, DriveRefRefused):
            raise _Refused("gone") from None

    def _key(self, raw_id: str) -> str:
        return keyed_digest(self._digest_key, LINK_DIGEST_PURPOSE, raw_id)

    @staticmethod
    def _log(op: str, status: str, reason: str, user: str, key: str) -> None:
        why = f" reason={reason}" if reason else ""
        logger.info(f"[DriveLink] {op} {status}{why} user={user[:8]} key={key[:8]}")


def _mime_of(item: DriveItem) -> str:
    return item.mime_type or mimetypes.guess_type(item.name)[0] or OCTET_STREAM


async def _primed(stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """`stream` with its first chunk already fetched: a source that fails before any byte raises here (a refusal),
    not inside the response. The returned generator closes `stream` when it is closed."""
    try:
        first = await stream.__anext__()
    except StopAsyncIteration:
        first = b""
    except DriveItemNotFoundError:
        await _close(stream)
        raise _Refused("gone") from None
    except MediaStreamFailed:
        await _close(stream)
        raise _Refused("stream") from None

    async def rest() -> AsyncIterator[bytes]:
        try:
            if first:
                yield first
            async for chunk in stream:
                yield chunk
        finally:
            await _close(stream)

    return rest()


async def _close(stream: AsyncIterator[bytes]) -> None:
    aclose = getattr(stream, "aclose", None)
    if aclose is not None:
        await aclose()
