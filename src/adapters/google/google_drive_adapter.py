"""
GoogleDriveAdapter — UserDrivePort over Google Drive API v3, one Google account per connection
(docs/10_rfcs/GOOGLE_DRIVE_RFC.md §3; MULTI_DRIVE_RFC §6, §7).

Public interface (the port's methods, in the port's order, then the module's constants):
    GoogleDriveAdapter(oauth_credentials, client_id, client_secret, *, connector: ConnectorSpec)
    get_root, get_item, list_children, search, download, upload, replace_content, move, create_folder, delete,
    child_by_name, area_path, get_changes, get_change, list_children_changes, download_to_file, get_thumbnail,
    media_source, outer_folders, capabilities, forget
    GOOGLE_CALL_TIMEOUT_S, GOOGLE_MAX_PATH_CHARS, MEDIA_TOKEN_MIN_LIFE, GOOGLE_EXPORT_MAX_BYTES

Every call takes the `DriveHandle` of its connection: the grant is found by `handle.credential_id`; the token cache and
the root cache are keyed `(user_id, credential_id)`, the path and outer-folder caches `connection_id` (T-3). The
injected `ConnectorSpec` names the credential family (`kind`) and the identity prefix (`id_space`); no kind literal
lives here. Identities: items `{id_space}:{fileId}` (GE-2), the drive `{id_space}:account:{permissionId}` (GE-1).
The Drive v3 JSON → neutral shapes mapping is `google_drive_mapping`; the cursor is `google_drive_cursor`; tokens come
from `GoogleDriveTokenProvider`.

- Transport: aiohttp, one session per port call (the OneDrive adapter's pattern). Every metadata call is bounded by
  GOOGLE_CALL_TIMEOUT_S, every content transfer (`alt=media`, export, upload) by DOWNLOAD_TIMEOUT_S (G9). Every
  `files`, `changes` and upload call sends `supportsAllDrives=true` (export, revisions, about and drives take no such
  parameter), every list `includeItemsFromAllDrives=true`; lists page on `nextPageToken`. A file read carries the field
  mask F (`google_drive_mapping.FILE_FIELDS`), except the N1 walk's folder reads and `move`'s parent read, which ask
  only for the fields they use.
- Errors (§3.7): classified by the body's `reason` before the status (GE-18). A rate limit (403
  `userRateLimitExceeded`/`rateLimitExceeded`), 429 and 5xx are retried 3 times with truncated exponential backoff (at
  most 32 s), then DriveThrottled; 403 `dailyLimitExceeded` is DriveThrottled(TICK_DEADLINE_S) at once; a 401 gets one
  forced refresh, then DriveAccessDenied; 404 is DriveItemNotFoundError, never a deletion. Any other 4xx is a
  ValueError whose text and log carry the status and reason only. A timeout is an asyncio.TimeoutError, any other
  transport failure an OSError (classified by the tick as for OneDrive).
- Paths (N1): Drive has no path field. A `DriveItem.path` is built by walking `parents` up to My Drive's root through
  a per-connection cache (TTL 5 minutes; a folder change seen in `get_changes` drops its entry), joined with
  `join_drive_path` (G4). A chain that ends at an item with no visible parent prints from that top; a chain is cut at
  MAX_CHAIN_DEPTH; a member shared drive's root is named from `drives.get`.
- Writes (§3.6, GE-15): Drive has no conflict parameter; conflict modes are emulated by listing the parent and
  comparing `name_key` (oldest by `createdTime` first). `delete` trashes, never deletes (GE-19). Natives never take a
  content write (D10).
- Secrets: tokens, thumbnail links and media URLs are never logged or put in an error (GE-17).
- `area_path` is "/": an account drive has no area, its root is the drive root (the account-wide fake's convention;
  the RFC's "" would not be an absolute path, which the port asks for).
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from datetime import timedelta
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode

import aiohttp  # module import on purpose: tests patch aiohttp.ClientSession

from ...domain.connector_spec import ConnectorSpec
from ...domain.drive_capabilities import DriveCapabilities
from ...domain.drive_feed_scope import FeedScope
from ...domain.drive_handle import DriveHandle
from ...domain.drive_image_mime import MAX_VISION_IMAGE_BYTES
from ...domain.drive_index import DriveChange, DriveChanges, DriveChildrenPage
from ...domain.drive_index_params import DOWNLOAD_TIMEOUT_S, TICK_DEADLINE_S
from ...domain.drive_media_source import MediaSource
from ...domain.drive_search import MAX_CHAIN_DEPTH
from ...domain.user_drive import (
    DriveAccessDenied,
    DriveFileTooLarge,
    DriveFileTooLargeError,
    DriveItem,
    DriveItemNotFoundError,
    DriveNameConflictError,
    DriveNotConnectedError,
    DrivePathTooLong,
    DriveResyncRequired,
    DriveStorageFull,
    DriveThrottled,
    DriveThumbnailTooLarge,
    join_drive_path,
    name_key,
)
from ...ports.oauth_credentials_port import OAuthCredentialsPort
from ...ports.user_drive_port import UserDrivePort
from ...utils.logger import logger
from . import google_drive_mapping as gm
from .google_drive_cursor import FEED, FeedCursor, corpora_of, decode, encode, restart
from .google_drive_tokens import GoogleDriveTokenProvider

API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
GOOGLE_CALL_TIMEOUT_S = 60                       # every metadata call (§3.1)
GOOGLE_MAX_PATH_CHARS = 32_768                   # a sentinel that never binds: Drive limits depth, not length (§3.7)
MEDIA_TOKEN_MIN_LIFE = timedelta(minutes=10)     # a media source's token outlives any child run (G2)
GOOGLE_EXPORT_MAX_BYTES = 10 * 1024 * 1024       # Google's export cap [DOC export]
SIMPLE_UPLOAD_MAX = 5 * 1024 * 1024              # ≤ 5 MB multipart, above resumable [DOC uploads]
UPLOAD_CHUNK = 8 * 1024 * 1024                   # a multiple of 256 KiB [DOC uploads]
PAGE_SIZE = 1000                                 # [DOC files.list, changes.list]
REVISION_CALLS_IN_FLIGHT = 4                     # G6: at most 4 revisions.list calls per page
RETRIES = 3
MAX_BACKOFF_S = 32.0                             # truncated exponential backoff [DOC limits]
THROTTLE_HINT_S = 60.0
CACHE_TTL_S = 300.0                              # N1 path cache and the outer folders (5 minutes)
THUMBNAIL_MAX_BYTES = MAX_VISION_IMAGE_BYTES
_THUMBNAIL_MAX_EDGE_PX = 9999                    # OneDrive's bound; the RFC is silent
_DOWNLOAD_CHUNK = 64 * 1024
_ALL = {"supportsAllDrives": "true"}
_LIST = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
_FEED_FIELDS = (f"nextPageToken,newStartPageToken,changes(changeType,removed,driveId,fileId,time,"
                f"file({gm.FILE_FIELDS}),drive(id,name))")
_LIST_FIELDS = f"nextPageToken,files({gm.FILE_FIELDS})"
_WALK_FIELDS = "id,name,parents,driveId,trashed"

Reader = Callable[[Any], Awaitable[Any]]


async def _sleep(seconds: float) -> None:
    """The backoff wait (a seam: tests replace it)."""
    await asyncio.sleep(seconds)


def _clock() -> float:
    """The cache clock (a seam: tests move it)."""
    return time.monotonic()


def _backoff(retry_after: Optional[str], attempt: int) -> float:
    """Retry-After in seconds when given, else 2^attempt; never above MAX_BACKOFF_S."""
    try:
        seconds = float(retry_after) if retry_after is not None else float(2 ** attempt)
    except ValueError:
        seconds = float(2 ** attempt)
    return min(max(seconds, 0.0), MAX_BACKOFF_S)


def _hint(retry_after: Optional[str]) -> float:
    try:
        seconds = float(retry_after) if retry_after is not None else THROTTLE_HINT_S
    except ValueError:
        seconds = THROTTLE_HINT_S
    return min(max(seconds, 0.0), float(TICK_DEADLINE_S))


def _q(file_id: str) -> str:
    return quote(file_id, safe="")


class _BadRequest(ValueError):
    """A 400: a ValueError, with Drive's `reason`, for the callers that decide (a refused page token)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Drive call failed (400 {reason})")
        self.reason = reason


class _ExportTooLarge(ValueError):
    """403 `exportSizeLimitExceeded` on an export call (G7)."""


class GoogleDriveAdapter(UserDrivePort):
    def __init__(self, oauth_credentials: OAuthCredentialsPort, client_id: str, client_secret: str, *,
                 connector: ConnectorSpec) -> None:
        """`connector` is the drive kind's spec: its `kind` names the credential family the token provider refreshes
        and its `id_space` prefixes every identity this adapter builds."""
        if not connector.id_space:
            raise ValueError("a drive connector needs an id_space")
        self._id_space = connector.id_space
        self._tokens = GoogleDriveTokenProvider(oauth_credentials, client_id, client_secret, connector.kind)
        # (user_id, credential_id) → (My Drive root resource, drive identity): cached until `forget` (§3.2)
        self._roots: Dict[Tuple[str, str], Tuple[Dict[str, Any], str]] = {}
        # connection_id → {folder id → (fetched_at, name, parent id)}: the N1 walk's cache
        self._paths: Dict[str, Dict[str, Tuple[float, str, str]]] = {}
        # connection_id → (fetched_at, outer folders)
        self._outer: Dict[str, Tuple[float, List[DriveItem]]] = {}
        logger.info("✅ GoogleDriveAdapter initialized")

    # -- per connection -------------------------------------------------------------------------------------------

    async def capabilities(self, handle: DriveHandle) -> DriveCapabilities:
        """Writable, one cursor over the whole account, a path limit that never binds (§3.7)."""
        return DriveCapabilities(max_path_chars=GOOGLE_MAX_PATH_CHARS, writable=True, feed_scope=FeedScope.ACCOUNT)

    async def forget(self, handle: DriveHandle) -> None:
        """Drop the connection's root, path and outer-folder caches and its credential's cached token."""
        self._roots.pop((handle.user_id, handle.credential_id), None)
        self._paths.pop(handle.connection_id, None)
        self._outer.pop(handle.connection_id, None)
        self._tokens.invalidate(handle.user_id, handle.credential_id)

    async def outer_folders(self, handle: DriveHandle) -> List[DriveItem]:
        """The parentless folders the account sees (tops of shared chains) and each member shared drive's root, named
        from `drives.list` (G8, GE-12), by name; cached per connection for CACHE_TTL_S, dropped by `forget`."""
        cached = self._outer.get(handle.connection_id)
        if cached and _clock() - cached[0] < CACHE_TTL_S:
            return list(cached[1])
        async with aiohttp.ClientSession() as s:
            _, identity = await self._root_info(s, handle)
            q = f"mimeType='{gm.FOLDER_MIME}' and trashed=false"
            raws = await self._list_all(s, handle, {"q": q, "corpora": "user"}, "files.list")
            tops = [gm.to_item(raw, join_drive_path("", str(raw.get("name") or "")), self._id_space, identity)
                    for raw in raws if not gm.parent_of(raw)]
            tops += [gm.drive_root_item(drive_id, name, self._id_space, identity)
                     for drive_id, name in await self._member_drives(s, handle)]
        tops.sort(key=lambda item: (item.name.casefold(), item.item_id))
        self._outer[handle.connection_id] = (_clock(), tops)
        return list(tops)

    # -- reads ----------------------------------------------------------------------------------------------------

    async def get_root(self, handle: DriveHandle) -> DriveItem:
        """My Drive's root (`files.get('root')`) with the drive identity from `about.get` (GE-1)."""
        async with aiohttp.ClientSession() as s:
            root, identity = await self._root_info(s, handle)
            return gm.to_item(root, "", self._id_space, identity)

    async def get_item(self, handle: DriveHandle, item_id: str) -> DriveItem:
        """One item with its path (N1); a trashed item is not found."""
        async with aiohttp.ClientSession() as s:
            raw = await self._get(s, handle, item_id)
            if raw.get("trashed") is True:
                raise DriveItemNotFoundError(f"Drive item {item_id} is in the trash")
            return (await self._items(s, handle, [raw]))[0]

    async def list_children(self, handle: DriveHandle, folder_id: str) -> List[DriveItem]:
        """Every child of a folder that is not trashed (all pages)."""
        async with aiohttp.ClientSession() as s:
            folder = await self._get(s, handle, folder_id)
            if folder.get("trashed") is True:
                raise DriveItemNotFoundError(f"Drive folder {folder_id} is in the trash")
            base = await self._path(s, handle, folder)
            raws = await self._children(s, handle, folder_id)
            return await self._items(s, handle, raws, base=base)

    async def search(self, handle: DriveHandle, query: str, limit: int) -> List[DriveItem]:
        """Name or full-text matches, at most `limit`; the query is escaped (`'`, `\\`)."""
        value = gm.escape_query(query)
        q = f"(name contains '{value}' or fullText contains '{value}') and trashed=false"
        async with aiohttp.ClientSession() as s:
            page = await self._call(s, handle, "GET", f"{API}/files", "files.list",
                                    params={**_LIST, "q": q, "pageSize": str(limit), "fields": _LIST_FIELDS})
            return await self._items(s, handle, list(page.get("files") or [])[:limit])

    async def download(self, handle: DriveHandle, item_id: str) -> bytes:
        """A binary's bytes (`alt=media`) or a native's export (§3.5). An over-cap export is DriveFileTooLargeError
        (G7); a folder, a shortcut or a native with no export has no downloadable content (DriveItemNotFoundError)."""
        async with aiohttp.ClientSession() as s:
            raw = await self._get(s, handle, item_id)
            url, params = self._content_request(raw)

            async def read(resp: Any) -> bytes:
                return bytes(await resp.read())

            try:
                return await self._call(s, handle, "GET", url, "content", params=params, timeout=DOWNLOAD_TIMEOUT_S,
                                        reader=read)
            except _ExportTooLarge as exc:
                _, identity = await self._root_info(s, handle)
                item = gm.to_item(raw, await self._path(s, handle, raw), self._id_space, identity)
                raise DriveFileTooLargeError(item, GOOGLE_EXPORT_MAX_BYTES) from exc

    async def media_source(self, handle: DriveHandle, item_id: str) -> MediaSource:
        """The `alt=media` URL (an export URL for a native) with the bearer as a header: no header-free URL exists
        [PROBE P-g], so the media relay streams it (G2). The token lives at least MEDIA_TOKEN_MIN_LIFE, refreshed first
        otherwise. The url and headers are secrets: never logged, never in an error."""
        async with aiohttp.ClientSession() as s:
            raw = await self._get(s, handle, item_id)
            url, params = self._content_request(raw)
        hit = await self._tokens.token(handle.user_id, handle.credential_id, min_life=MEDIA_TOKEN_MIN_LIFE)
        if hit is None:
            raise DriveNotConnectedError(f"No drive credentials for connection {handle.connection_id[:8]}")
        token, expiry = hit
        return MediaSource(url=f"{url}?{urlencode(params)}", headers={"Authorization": f"Bearer {token}"},
                           expires_at=expiry)

    # -- writes ---------------------------------------------------------------------------------------------------

    async def upload(self, handle: DriveHandle, parent_id: str, filename: str, data: bytes, content_type: str,
                     conflict: Literal["rename", "replace", "fail"] = "rename") -> DriveItem:
        """A new file in `parent_id` (§3.6): `fail` refuses a taken name (by `name_key`), `rename` takes the first free
        `name (n).ext`, `replace` overwrites the oldest match. Never a `mimeType` in the metadata (it would convert the
        file [DOC uploads]); `parents=[parent_id]`."""
        async with aiohttp.ClientSession() as s:
            siblings = await self._children(s, handle, parent_id)
            matches = self._matches(siblings, filename)
            if matches and conflict == "fail":
                raise DriveNameConflictError("Drive name taken")
            if matches and conflict == "replace":
                target = matches[0]
                if gm.is_folder(target):
                    raise DriveNameConflictError("Drive name taken by a folder")
                return await self._replace(s, handle, target, data, content_type)
            if matches:
                filename = self._free_name(siblings, filename)
            meta = {"name": filename, "parents": [parent_id]}
            if len(data) <= SIMPLE_UPLOAD_MAX:
                raw = await self._multipart(s, handle, meta, data, content_type)
            else:
                raw = await self._resumable(s, handle, "POST", f"{UPLOAD_API}/files", meta, data, content_type)
            return (await self._items(s, handle, [raw]))[0]

    async def replace_content(self, handle: DriveHandle, item_id: str, data: bytes, content_type: str) -> DriveItem:
        """New content for a binary, same id and name. A native or a folder is refused (DriveAccessDenied) before any
        content call: the policy refuses it first (`can_write_content=False`), this is the second line (D10)."""
        async with aiohttp.ClientSession() as s:
            raw = await self._get(s, handle, item_id)
            return await self._replace(s, handle, raw, data, content_type)

    async def move(self, handle: DriveHandle, item_id: str, new_parent_id: Optional[str] = None,
                   new_name: Optional[str] = None) -> DriveItem:
        """`files.update` with `addParents`/`removeParents` and the new name; a taken target name (by `name_key`) is
        DriveNameConflictError (§3.6)."""
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, handle, "GET", f"{API}/files/{_q(item_id)}", "files.get",
                                   params={**_ALL, "fields": "id,name,parents"})
            current = list(raw.get("parents") or [])
            target_parent = new_parent_id or gm.parent_of(raw)
            target_name = new_name or str(raw.get("name") or "")
            if target_parent:
                clash = [m for m in self._matches(await self._children(s, handle, target_parent), target_name)
                         if m.get("id") != item_id]
                if clash:
                    raise DriveNameConflictError("Drive name taken")
            params: Dict[str, str] = {**_ALL, "fields": gm.FILE_FIELDS}
            if new_parent_id and new_parent_id not in current:
                params["addParents"] = new_parent_id
                if current:
                    params["removeParents"] = ",".join(current)
            body: Dict[str, Any] = {"name": new_name} if new_name else {}
            moved = await self._call(s, handle, "PATCH", f"{API}/files/{_q(item_id)}", "files.update",
                                     params=params, json_body=body)
            self._drop_path(handle, item_id)
            return (await self._items(s, handle, [moved]))[0]

    async def create_folder(self, handle: DriveHandle, parent_id: str, name: str) -> DriveItem:
        """One folder; a taken name (by `name_key`) is DriveNameConflictError."""
        async with aiohttp.ClientSession() as s:
            if self._matches(await self._children(s, handle, parent_id), name):
                raise DriveNameConflictError("Drive name taken")
            raw = await self._call(s, handle, "POST", f"{API}/files", "files.create",
                                   params={**_ALL, "fields": gm.FILE_FIELDS},
                                   json_body={"name": name, "mimeType": gm.FOLDER_MIME, "parents": [parent_id]})
            return (await self._items(s, handle, [raw]))[0]

    async def delete(self, handle: DriveHandle, item_id: str) -> None:
        """Into the trash (`{trashed: true}`), recoverable for 30 days; never `files.delete` (GE-19)."""
        async with aiohttp.ClientSession() as s:
            await self._call(s, handle, "PATCH", f"{API}/files/{_q(item_id)}", "files.update",
                             params={**_ALL, "fields": "id,trashed"}, json_body={"trashed": True})
        self._drop_path(handle, item_id)

    async def child_by_name(self, handle: DriveHandle, parent_id: str, name: str) -> Optional[DriveChange]:
        """The oldest child (by `createdTime`) whose `name_key` equals the name's; the parent's children are listed
        (Drive keeps case twins, so an exact `name=` query would miss one, GE-15)."""
        async with aiohttp.ClientSession() as s:
            matches = self._matches(await self._children(s, handle, parent_id), name)
            if not matches:
                return None
            return (await self._changes(s, handle, matches[:1]))[0]

    async def area_path(self, handle: DriveHandle) -> str:
        """"/": an account drive has no area; its root is the drive root."""
        return "/"

    # -- change capture for the index (§3.3) ----------------------------------------------------------------------

    async def get_changes(self, handle: DriveHandle, cursor: Optional[str]) -> DriveChanges:
        """One page: of the emulated enumeration (every page `full_enumeration`, the last `complete`, then the feed
        starts at the start token) or of the account-wide feed (`complete` only on `newStartPageToken`). A cursor of
        another drive identity, unreadable or of another version is DriveResyncRequired(<fresh cursor>)."""
        async with aiohttp.ClientSession() as s:
            root, identity = await self._root_info(s, handle)
            fresh = encode(restart(identity))
            if cursor is None:
                cur = restart(identity)
            else:
                try:
                    cur = decode(cursor)
                except ValueError as exc:
                    logger.warning(f"Drive change cursor unreadable ({exc}) for connection "
                                   f"{handle.connection_id[:8]}; restarting enumeration")
                    raise DriveResyncRequired(fresh) from exc
                if cur.drive != identity:
                    logger.warning(f"Drive change cursor belongs to another drive for connection "
                                   f"{handle.connection_id[:8]}; restarting enumeration")
                    raise DriveResyncRequired(fresh)
            root_id = str(root.get("id") or "")
            if cur.phase == FEED:
                return await self._feed_page(s, handle, cur, root_id, identity)
            return await self._enumeration_page(s, handle, cur, root_id, identity)

    async def get_change(self, handle: DriveHandle, item_id: str) -> DriveChange:
        """One item by id; trashed is a deletion; 404 is DriveItemNotFoundError, never a deletion."""
        async with aiohttp.ClientSession() as s:
            raw = await self._get(s, handle, item_id)
            return (await self._changes(s, handle, [raw]))[0]

    async def list_children_changes(self, handle: DriveHandle, folder_id: str, limit: int,
                                    cursor: Optional[str] = None) -> DriveChildrenPage:
        """One page of a folder's children; the cursor is `{folder_id, page_token}`, and one of another folder (or one
        this module cannot read) restarts the listing. DriveItemNotFoundError when the folder is gone."""
        token = ""
        if cursor is not None:
            try:
                data = json.loads(cursor)
                if not isinstance(data, dict) or data.get("folder_id") != folder_id \
                        or not isinstance(data.get("page_token"), str):
                    raise ValueError("another folder's cursor")
                token = data["page_token"]
            except (TypeError, ValueError):
                logger.warning("Drive listing cursor of another folder or unreadable; restarting the listing")
        async with aiohttp.ClientSession() as s:
            folder = await self._get(s, handle, folder_id)
            if folder.get("trashed") is True:
                raise DriveItemNotFoundError(f"Drive folder {folder_id} is in the trash")
            params = {**_LIST, "q": f"'{gm.escape_query(folder_id)}' in parents and trashed=false",
                      "pageSize": str(limit), "fields": _LIST_FIELDS}
            if token:
                params["pageToken"] = token
            page = await self._call(s, handle, "GET", f"{API}/files", "files.list", params=params)
            changes = await self._changes(s, handle, list(page.get("files") or []))
        nxt = page.get("nextPageToken")
        next_cursor = json.dumps({"folder_id": folder_id, "page_token": str(nxt)}) if nxt else None
        return DriveChildrenPage(changes=tuple(changes), next_cursor=next_cursor)

    # -- content for the index ------------------------------------------------------------------------------------

    async def download_to_file(self, handle: DriveHandle, item_id: str, dest_path: str, max_bytes: int) -> int:
        """Stream the bytes (or the export) to `dest_path` in 64 KiB chunks, cut as soon as they pass `max_bytes`
        (DriveFileTooLarge); an over-cap export is DriveFileTooLarge too (G7)."""
        async with aiohttp.ClientSession() as s:
            raw = await self._get(s, handle, item_id)
            url, params = self._content_request(raw)

            async def stream(resp: Any) -> int:
                written = 0
                with open(dest_path, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK):
                        if written + len(chunk) > max_bytes:
                            logger.warning(f"Drive download of item {item_id} passed {max_bytes} bytes; cut")
                            raise DriveFileTooLarge(max_bytes)
                        fh.write(chunk)
                        written += len(chunk)
                return written

            try:
                return await self._call(s, handle, "GET", url, "content", params=params, timeout=DOWNLOAD_TIMEOUT_S,
                                        reader=stream)
            except _ExportTooLarge as exc:
                raise DriveFileTooLarge(GOOGLE_EXPORT_MAX_BYTES) from exc

    async def get_thumbnail(self, handle: DriveHandle, item_id: str, max_edge_px: int) -> Optional[bytes]:
        """The item's `thumbnailLink` with its `=s<n>` suffix set to `max_edge_px`, fetched without the bearer (another
        host; answered without a token [PROBE P-f]), https only. The link is a secret (GE-17): never logged, never in
        an error. None: no rendition or its link gone; DriveThumbnailTooLarge over the vision cap."""
        if isinstance(max_edge_px, bool) or not isinstance(max_edge_px, int) \
                or not 1 <= max_edge_px <= _THUMBNAIL_MAX_EDGE_PX:
            raise ValueError(f"A thumbnail edge is 1..{_THUMBNAIL_MAX_EDGE_PX} px: {max_edge_px!r}")
        size = f"s{max_edge_px}"
        async with aiohttp.ClientSession() as s:
            raw = await self._get(s, handle, item_id)
            if raw.get("trashed") is True:
                raise DriveItemNotFoundError(f"Drive item {item_id} is in the trash")
            link = str(raw.get("thumbnailLink") or "")
            if not link.startswith("https://"):
                if link:
                    logger.warning(f"Drive thumbnail link of item {item_id} is not https; ignored")
                return None
            url = gm.sized_thumbnail(link, max_edge_px)
            try:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=GOOGLE_CALL_TIMEOUT_S),
                                 allow_redirects=False) as resp:
                    if resp.status == 404 or 300 <= resp.status < 400:   # a redirect is no rendition (GE-17)
                        return None
                    if resp.status != 200:
                        logger.error(f"Drive thumbnail fetch failed ({resp.status}) for item {item_id}")
                        raise ValueError(f"Drive thumbnail fetch failed ({resp.status})")
                    data = bytearray()
                    async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK):
                        data.extend(chunk)
                        if len(data) > THUMBNAIL_MAX_BYTES:
                            logger.warning(f"Drive thumbnail {size} of item {item_id} exceeds "
                                           f"{THUMBNAIL_MAX_BYTES} bytes; not used")
                            raise DriveThumbnailTooLarge(size, THUMBNAIL_MAX_BYTES)
                    return bytes(data)
            except asyncio.TimeoutError:
                logger.warning(f"Drive thumbnail fetch for item {item_id} timed out")
                raise asyncio.TimeoutError("Drive thumbnail fetch timed out") from None
            except aiohttp.ClientError as exc:
                logger.warning(f"Drive thumbnail fetch for item {item_id} transport failure: {type(exc).__name__}")
                raise OSError(f"Drive thumbnail transport failure ({type(exc).__name__})") from None

    # -- the feed -------------------------------------------------------------------------------------------------

    async def _enumeration_page(self, s: aiohttp.ClientSession, handle: DriveHandle, cur: FeedCursor, root_id: str,
                                identity: str) -> DriveChanges:
        """One `files.list` page of the pass in progress (GE-14): `corpora=user`, then per member shared drive its
        root as a folder change and `corpora=drive`; `orderBy=folder` throughout. A refused stored page token restarts
        the pass (GE-35); a refusal of a pass's first page is a plain failure."""
        if cur.fresh:
            start = await self._call(s, handle, "GET", f"{API}/changes/startPageToken", "changes.getStartPageToken",
                                     params=dict(_ALL))
            cur = FeedCursor(drive=identity, start=str(start.get("startPageToken") or ""),
                             corpora=corpora_of(await self._member_drives(s, handle)))
        corpus = (cur.corpora or ({"c": "user"},))[0]
        params = {**_LIST, "q": "trashed=false", "orderBy": "folder", "pageSize": str(PAGE_SIZE),
                  "fields": _LIST_FIELDS}
        if corpus["c"] == "drive":
            params.update({"corpora": "drive", "driveId": corpus["id"]})
        else:
            params["corpora"] = "user"
        try:
            page = await self._call(s, handle, "GET", f"{API}/files", "files.list",
                                    params={**params, "pageToken": cur.list_token} if cur.list_token else params)
        except _BadRequest:
            if not cur.list_token:
                raise
            logger.warning(f"Drive list page token refused for connection {handle.connection_id[:8]}; "
                           f"restarting the pass")
            cur = cur.with_list("")
            page = await self._call(s, handle, "GET", f"{API}/files", "files.list", params=params)
        changes: List[DriveChange] = []
        if corpus["c"] == "drive" and not cur.list_token:
            changes.append(gm.drive_root_change(corpus["id"], corpus["name"], self._id_space))
        changes += await self._changes(s, handle, list(page.get("files") or []))
        self._drop_paths(handle, changes)
        nxt = page.get("nextPageToken")
        complete = False
        if nxt:
            new = cur.with_list(str(nxt))
        else:
            new = cur.next_pass()
            if not new.corpora:
                new, complete = cur.to_feed(), True
        return DriveChanges(changes=tuple(changes), cursor=encode(new), complete=complete, full_enumeration=True,
                            root_item_id=root_id, drive_identity=identity)

    async def _feed_page(self, s: aiohttp.ClientSession, handle: DriveHandle, cur: FeedCursor, root_id: str,
                         identity: str) -> DriveChanges:
        """One `changes.list` page. `removed` or `file.trashed` is a deletion; `changeType=drive` removed (membership
        lost) is DriveResyncRequired, otherwise the drive root's change with its new name (GE-34); a 400 `invalid`
        token is DriveResyncRequired."""
        params = {**_LIST, "pageToken": cur.page, "pageSize": str(PAGE_SIZE), "includeRemoved": "true",
                  "restrictToMyDrive": "false", "fields": _FEED_FIELDS}
        try:
            page = await self._call(s, handle, "GET", f"{API}/changes", "changes.list", params=params)
        except _BadRequest as exc:
            if exc.reason != "invalid":
                raise
            logger.warning(f"Drive change token refused (400 invalid) for connection {handle.connection_id[:8]}; "
                           f"restarting enumeration")
            raise DriveResyncRequired(encode(restart(identity))) from exc
        rows = [row for row in (page.get("changes") or []) if isinstance(row, dict)]
        files = [row["file"] for row in rows
                 if row.get("changeType") != "drive" and not row.get("removed") and isinstance(row.get("file"), dict)]
        built = iter(await self._changes(s, handle, files))
        changes: List[DriveChange] = []
        for row in rows:
            if row.get("changeType") == "drive":
                drive_id = str(row.get("driveId") or "")
                if row.get("removed"):
                    logger.warning(f"Drive membership of a shared drive lost for connection "
                                   f"{handle.connection_id[:8]}; restarting enumeration")
                    raise DriveResyncRequired(encode(restart(identity)))
                drive = row.get("drive")
                name = str(drive.get("name") or "") if isinstance(drive, dict) else ""
                changes.append(gm.drive_root_change(drive_id, name, self._id_space))
                self._outer.pop(handle.connection_id, None)
            elif row.get("removed") or not isinstance(row.get("file"), dict):
                file_id = str(row.get("fileId") or "")
                changes.append(DriveChange(item_id=file_id, deleted=True,
                                           identity=gm.item_identity(self._id_space, file_id)))
            else:
                changes.append(next(built))
        self._drop_paths(handle, changes)
        if page.get("nextPageToken"):
            new, complete = cur.with_page(str(page["nextPageToken"])), False
        elif page.get("newStartPageToken"):
            new, complete = cur.with_page(str(page["newStartPageToken"])), True
        else:
            logger.error("Drive change page carried neither nextPageToken nor newStartPageToken")
            raise ValueError("Drive change page without a continuation token")
        return DriveChanges(changes=tuple(changes), cursor=encode(new), complete=complete, full_enumeration=False,
                            root_item_id=root_id, drive_identity=identity)

    async def _member_drives(self, s: aiohttp.ClientSession, handle: DriveHandle) -> List[Tuple[str, str]]:
        """(id, name) of every shared drive the account is a member of (`drives.list`, all pages)."""
        out: List[Tuple[str, str]] = []
        token = ""
        while True:
            params = {"pageSize": "100", "fields": "nextPageToken,drives(id,name)"}
            if token:
                params["pageToken"] = token
            page = await self._call(s, handle, "GET", f"{API}/drives", "drives.list", params=params)
            for drive in page.get("drives") or []:
                if isinstance(drive, dict) and drive.get("id"):
                    out.append((str(drive["id"]), str(drive.get("name") or "")))
            token = str(page.get("nextPageToken") or "")
            if not token:
                return out

    # -- items, versions, paths -----------------------------------------------------------------------------------

    async def _root_info(self, s: aiohttp.ClientSession, handle: DriveHandle) -> Tuple[Dict[str, Any], str]:
        key = (handle.user_id, handle.credential_id)
        cached = self._roots.get(key)
        if cached:
            return cached
        root = await self._call(s, handle, "GET", f"{API}/files/root", "files.get",
                                params={**_ALL, "fields": gm.FILE_FIELDS})
        about = await self._call(s, handle, "GET", f"{API}/about", "about.get",
                                 params={"fields": "user(permissionId)"})
        permission_id = str(((about.get("user") or {}) if isinstance(about.get("user"), dict) else {})
                            .get("permissionId") or "")
        if not permission_id:
            logger.error(f"Drive about.get answered without a permission id for connection {handle.connection_id[:8]}")
            raise ValueError("Drive about.get answered without a permission id")
        info = (root, gm.drive_identity(self._id_space, permission_id))
        self._roots[key] = info
        return info

    async def _get(self, s: aiohttp.ClientSession, handle: DriveHandle, item_id: str) -> Dict[str, Any]:
        return await self._call(s, handle, "GET", f"{API}/files/{_q(item_id)}", "files.get",
                                params={**_ALL, "fields": gm.FILE_FIELDS})

    async def _children(self, s: aiohttp.ClientSession, handle: DriveHandle, parent_id: str) -> List[Dict[str, Any]]:
        q = f"'{gm.escape_query(parent_id)}' in parents and trashed=false"
        return await self._list_all(s, handle, {"q": q}, "files.list")

    async def _list_all(self, s: aiohttp.ClientSession, handle: DriveHandle, params: Dict[str, str],
                        what: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        token = ""
        while True:
            call = {**_LIST, **params, "pageSize": str(PAGE_SIZE), "fields": _LIST_FIELDS}
            if token:
                call["pageToken"] = token
            page = await self._call(s, handle, "GET", f"{API}/files", what, params=call)
            out.extend(raw for raw in page.get("files") or [] if isinstance(raw, dict))
            token = str(page.get("nextPageToken") or "")
            if not token:
                return out

    @staticmethod
    def _matches(siblings: Sequence[Dict[str, Any]], name: str) -> List[Dict[str, Any]]:
        """The siblings whose name equals `name` by `name_key`, oldest (`createdTime`) first."""
        wanted = name_key(name)
        found = [raw for raw in siblings if name_key(str(raw.get("name") or "")) == wanted]
        return sorted(found, key=lambda raw: (str(raw.get("createdTime") or ""), str(raw.get("id") or "")))

    @staticmethod
    def _free_name(siblings: Sequence[Dict[str, Any]], name: str) -> str:
        taken = {name_key(str(raw.get("name") or "")) for raw in siblings}
        stem, ext = gm.split_name(name)
        n = 1
        while name_key(f"{stem} ({n}){ext}") in taken:
            n += 1
        return f"{stem} ({n}){ext}"

    async def _head_revisions(self, s: aiohttp.ClientSession, handle: DriveHandle,
                              raws: Sequence[Dict[str, Any]]) -> Dict[str, Optional[str]]:
        """The head revision id of each exportable native whose `canReadRevisions` is true (G6, GE-4), at most
        REVISION_CALLS_IN_FLIGHT calls at once. A refused or empty list is None (the item falls back to `m:`)."""
        wanted = [raw for raw in raws if gm.is_native(raw) and gm.export_mime(raw) and gm.can_read_revisions(raw)
                  and raw.get("trashed") is not True]
        if not wanted:
            return {}
        gate = asyncio.Semaphore(REVISION_CALLS_IN_FLIGHT)

        async def head(raw: Dict[str, Any]) -> Optional[str]:
            async with gate:
                return await self._head_revision(s, handle, str(raw.get("id") or ""))

        heads = await asyncio.gather(*(head(raw) for raw in wanted))
        return {str(raw.get("id") or ""): rev for raw, rev in zip(wanted, heads)}

    async def _head_revision(self, s: aiohttp.ClientSession, handle: DriveHandle, file_id: str) -> Optional[str]:
        last: Optional[str] = None
        token = ""
        try:
            while True:
                params = {"pageSize": str(PAGE_SIZE), "fields": "nextPageToken,revisions(id)"}
                if token:
                    params["pageToken"] = token
                page = await self._call(s, handle, "GET", f"{API}/files/{_q(file_id)}/revisions", "revisions.list",
                                        params=params)
                revisions = [r for r in page.get("revisions") or [] if isinstance(r, dict) and r.get("id")]
                if revisions:
                    last = str(revisions[-1]["id"])
                token = str(page.get("nextPageToken") or "")
                if not token:
                    return last
        except (DriveItemNotFoundError, DriveAccessDenied, ValueError) as exc:
            logger.info(f"Drive revisions of item {file_id} not read ({type(exc).__name__}); modified time used")
            return None

    async def _changes(self, s: aiohttp.ClientSession, handle: DriveHandle,
                       raws: Sequence[Dict[str, Any]]) -> List[DriveChange]:
        heads = await self._head_revisions(s, handle, raws)
        return [gm.to_change(raw, self._id_space, heads.get(str(raw.get("id") or ""))) for raw in raws]

    async def _items(self, s: aiohttp.ClientSession, handle: DriveHandle, raws: Sequence[Dict[str, Any]], *,
                     base: Optional[str] = None) -> List[DriveItem]:
        """Items with paths: `base` is their common parent's path (a listing), else each is walked (N1)."""
        _, identity = await self._root_info(s, handle)
        heads = await self._head_revisions(s, handle, raws)
        out: List[DriveItem] = []
        for raw in raws:
            name = str(raw.get("name") or "")
            path = join_drive_path(base, name) if base is not None else await self._path(s, handle, raw)
            out.append(gm.to_item(raw, path, self._id_space, identity, heads.get(str(raw.get("id") or ""))))
        return out

    async def _path(self, s: aiohttp.ClientSession, handle: DriveHandle, raw: Dict[str, Any]) -> str:
        """N1: the names from the root (excluded) or from the chain's top down to `raw`, joined with
        `join_drive_path`; "" for the root itself. The walk stops at the root, at an item with no visible parent, at a
        member shared drive's root (named from `drives.get`) or after MAX_CHAIN_DEPTH names."""
        root, _ = await self._root_info(s, handle)
        root_id = str(root.get("id") or "")
        if raw.get("id") == root_id:
            return ""
        names = [str(raw.get("name") or "")]
        parent = gm.parent_of(raw)
        shared_drive = str(raw.get("driveId") or "")
        while parent and parent != root_id and len(names) < MAX_CHAIN_DEPTH:
            entry = await self._parent_entry(s, handle, parent, shared_drive)
            if entry is None:
                break
            name, parent = entry
            names.append(name)
        path = ""
        for name in reversed(names):
            path = join_drive_path(path, name)
        return path

    async def _parent_entry(self, s: aiohttp.ClientSession, handle: DriveHandle, folder_id: str,
                            shared_drive: str) -> Optional[Tuple[str, str]]:
        """(name, parent id) of one folder of a chain, from the connection's cache (TTL CACHE_TTL_S) or one read;
        None when the folder cannot be read (the chain's top is its child)."""
        cache = self._paths.setdefault(handle.connection_id, {})
        hit = cache.get(folder_id)
        if hit and _clock() - hit[0] < CACHE_TTL_S:
            return hit[1], hit[2]
        try:
            if folder_id == shared_drive:
                drive = await self._call(s, handle, "GET", f"{API}/drives/{_q(folder_id)}", "drives.get",
                                         params={"fields": "id,name"})
                entry = (str(drive.get("name") or ""), "")
            else:
                raw = await self._call(s, handle, "GET", f"{API}/files/{_q(folder_id)}", "files.get",
                                       params={**_ALL, "fields": _WALK_FIELDS})
                entry = (str(raw.get("name") or ""), gm.parent_of(raw))
        except (DriveItemNotFoundError, DriveAccessDenied):
            return None
        cache[folder_id] = (_clock(), entry[0], entry[1])
        return entry

    def _drop_path(self, handle: DriveHandle, item_id: str) -> None:
        cache = self._paths.get(handle.connection_id)
        if cache is not None:
            cache.pop(item_id, None)

    def _drop_paths(self, handle: DriveHandle, changes: Sequence[DriveChange]) -> None:
        """A folder renamed, moved or trashed (or any deletion) drops its N1 cache entry."""
        for change in changes:
            if change.is_folder or change.deleted:
                self._drop_path(handle, change.item_id)

    # -- content and uploads --------------------------------------------------------------------------------------

    @staticmethod
    def _content_request(raw: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
        """(url, params) of the item's content: `alt=media` for a binary, the export of an exportable native.
        DriveItemNotFoundError for a trashed item, a folder, a shortcut or a native with no export."""
        file_id = str(raw.get("id") or "")
        if raw.get("trashed") is True:
            raise DriveItemNotFoundError(f"Drive item {file_id} is in the trash")
        if gm.is_folder(raw) or gm.is_shortcut(raw):
            raise DriveItemNotFoundError(f"No downloadable content for item {file_id}")
        if gm.is_native(raw):
            export = gm.export_mime(raw)
            if export is None:
                raise DriveItemNotFoundError(f"No downloadable content for item {file_id}")
            return f"{API}/files/{_q(file_id)}/export", {"mimeType": export}
        return f"{API}/files/{_q(file_id)}", {"alt": "media", **_ALL}

    async def _replace(self, s: aiohttp.ClientSession, handle: DriveHandle, raw: Dict[str, Any], data: bytes,
                       content_type: str) -> DriveItem:
        file_id = str(raw.get("id") or "")
        if gm.is_native(raw) or gm.is_folder(raw) or gm.is_shortcut(raw):
            logger.warning(f"Drive content write refused for item {file_id}: not an ordinary file")
            raise DriveAccessDenied("Drive refuses a content write to this item")
        url = f"{UPLOAD_API}/files/{_q(file_id)}"
        if len(data) <= SIMPLE_UPLOAD_MAX:
            updated = await self._call(s, handle, "PATCH", url, "files.update(media)",
                                       params={**_ALL, "uploadType": "media", "fields": gm.FILE_FIELDS},
                                       data=data, headers={"Content-Type": content_type}, timeout=DOWNLOAD_TIMEOUT_S)
        else:
            updated = await self._resumable(s, handle, "PATCH", url, {}, data, content_type)
        return (await self._items(s, handle, [updated]))[0]

    async def _multipart(self, s: aiohttp.ClientSession, handle: DriveHandle, meta: Dict[str, Any], data: bytes,
                         content_type: str) -> Dict[str, Any]:
        boundary = f"alek-{secrets.token_hex(12)}"
        head = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(meta, ensure_ascii=False)}\r\n--{boundary}\r\nContent-Type: {content_type}\r\n\r\n")
        body = head.encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("ascii")
        return await self._call(s, handle, "POST", f"{UPLOAD_API}/files", "files.create(multipart)",
                                params={**_ALL, "uploadType": "multipart", "fields": gm.FILE_FIELDS}, data=body,
                                headers={"Content-Type": f"multipart/related; boundary={boundary}"},
                                timeout=DOWNLOAD_TIMEOUT_S)

    async def _resumable(self, s: aiohttp.ClientSession, handle: DriveHandle, method: str, url: str,
                         meta: Dict[str, Any], data: bytes, content_type: str) -> Dict[str, Any]:
        """A resumable session, then UPLOAD_CHUNK-sized PUTs to its URI (no bearer: the URI is the session's own
        credential [DOC uploads])."""
        async def location(resp: Any) -> str:
            return str(resp.headers.get("Location") or "")

        session_url = await self._call(s, handle, method, url, "upload session",
                                       params={**_ALL, "uploadType": "resumable", "fields": gm.FILE_FIELDS},
                                       json_body=meta,
                                       headers={"X-Upload-Content-Type": content_type,
                                                "X-Upload-Content-Length": str(len(data))},
                                       reader=location)
        if not session_url.startswith(UPLOAD_API + "/"):
            logger.error("Drive upload session URI is not Drive's upload endpoint")
            raise ValueError("Drive upload session without a valid URI")
        total = len(data)
        try:
            for start in range(0, total, UPLOAD_CHUNK):
                chunk = data[start:start + UPLOAD_CHUNK]
                headers = {"Content-Length": str(len(chunk)),
                           "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{total}"}
                async with s.put(session_url, headers=headers, data=chunk,
                                 timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_S)) as resp:
                    if resp.status == 308:
                        continue
                    if resp.status in (200, 201):
                        return dict(await resp.json(content_type=None))
                    reason = gm.error_reason(await resp.text())
                    logger.error(f"Drive chunk upload failed ({resp.status}): {reason}")
                    if gm.classify(resp.status, reason) == gm.STORAGE_FULL:
                        raise DriveStorageFull("The drive is full")
                    raise ValueError(f"Drive chunk upload failed ({resp.status} {reason})")
        except asyncio.TimeoutError as exc:
            logger.warning("Drive chunk upload timed out")
            raise asyncio.TimeoutError("Drive upload timed out") from exc
        except aiohttp.ClientError as exc:
            logger.warning(f"Drive chunk upload transport failure: {type(exc).__name__}")
            raise OSError(f"Drive upload transport failure ({type(exc).__name__})") from exc
        logger.error("Drive resumable upload ended without the file")
        raise ValueError("Drive resumable upload ended without the file")

    # -- transport ------------------------------------------------------------------------------------------------

    async def _token(self, handle: DriveHandle, *, force_refresh: bool = False) -> str:
        """The handle's own credential's token, never another connection's. DriveReauthRequired and DriveThrottled
        come from the provider."""
        hit = await self._tokens.token(handle.user_id, handle.credential_id, force_refresh=force_refresh)
        if hit is None:
            raise DriveNotConnectedError(f"No drive credentials for connection {handle.connection_id[:8]}")
        return hit[0]

    async def _call(self, s: aiohttp.ClientSession, handle: DriveHandle, method: str, url: str, what: str, *,
                    params: Optional[Dict[str, str]] = None, json_body: Any = None, data: Optional[bytes] = None,
                    headers: Optional[Dict[str, str]] = None, timeout: float = GOOGLE_CALL_TIMEOUT_S,
                    reader: Optional[Reader] = None) -> Any:
        """One Drive call with the bearer: the §3.7 mapping, the retries and the forced refresh. `what` names the call
        in logs (never the URL or its query, which may hold a name). `reader` consumes a 2xx response (bytes, a
        stream, a header); without it the JSON body is returned."""
        throttled = 0
        refreshed = False
        while True:
            token = await self._token(handle)
            kwargs: Dict[str, Any] = {"headers": {"Authorization": f"Bearer {token}", **(headers or {})},
                                      "params": params or {}, "timeout": aiohttp.ClientTimeout(total=timeout)}
            if json_body is not None:
                kwargs["json"] = json_body
            if data is not None:
                kwargs["data"] = data
            try:
                async with getattr(s, method.lower())(url, **kwargs) as resp:
                    status = resp.status
                    if 200 <= status < 300:
                        if reader is not None:
                            return await reader(resp)
                        if status == 204:
                            return {}
                        return dict(await resp.json(content_type=None) or {})
                    reason = gm.error_reason(await resp.text())
                    outcome = gm.classify(status, reason)
                    if outcome == gm.UNAUTHORIZED:
                        if refreshed:
                            logger.warning(f"Drive {method} {what}: 401 after a successful token refresh")
                            raise DriveAccessDenied(f"Drive {what} refused after a token refresh")
                        refreshed = True
                        await self._token(handle, force_refresh=True)
                        continue
                    if outcome == gm.RETRY:
                        if throttled < RETRIES:
                            delay = _backoff(resp.headers.get("Retry-After"), throttled)
                            throttled += 1
                            logger.warning(f"Drive {method} {what} throttled ({status} {reason}); "
                                           f"retrying in {delay:.0f}s")
                            await _sleep(delay)
                            continue
                        hint = _hint(resp.headers.get("Retry-After"))
                        logger.warning(f"Drive {method} {what} still throttled ({status} {reason}) after {RETRIES} "
                                       f"retries; retry after {hint:.0f}s")
                        raise DriveThrottled(hint)
                    if outcome == gm.DAILY_LIMIT:
                        logger.warning(f"Drive {method} {what}: daily limit reached ({status} {reason})")
                        raise DriveThrottled(float(TICK_DEADLINE_S))
                    if outcome == gm.NOT_FOUND:
                        raise DriveItemNotFoundError(f"Drive item not found ({what})")
                    logger.error(f"Drive {method} {what} failed ({status} {reason})")
                    if outcome == gm.ACCESS_DENIED:
                        raise DriveAccessDenied(f"Drive refused the call ({status} {reason})")
                    if outcome == gm.STORAGE_FULL:
                        raise DriveStorageFull("The drive is full")
                    if outcome == gm.PATH_TOO_LONG:
                        raise DrivePathTooLong("Drive folder hierarchy is too deep")
                    if outcome == gm.EXPORT_TOO_LARGE:
                        raise _ExportTooLarge(f"Drive export over the cap ({status} {reason})")
                    if outcome == gm.BAD_REQUEST:
                        raise _BadRequest(reason)
                    raise ValueError(f"Drive call failed ({status} {reason})")
            except asyncio.TimeoutError as exc:
                logger.warning(f"Drive {method} {what} timed out")
                raise asyncio.TimeoutError(f"Drive {what} timed out") from exc
            except aiohttp.ClientError as exc:
                logger.warning(f"Drive {method} {what} transport failure: {type(exc).__name__}")
                raise OSError(f"Drive {what} transport failure ({type(exc).__name__})") from exc
