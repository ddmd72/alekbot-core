"""
OneDriveAdapter — UserDrivePort over Microsoft Graph, scoped to the app's App Folder
(docs/10_rfcs/USER_DRIVE_RFC.md §2, §4.2, §4.3).

Scope Files.ReadWrite.AppFolder: Graph itself refuses anything outside the app folder.
Ids are Graph item ids (stable across move/rename). Paths are display only: Graph returns
them percent-encoded, so they are decoded and made relative to the app folder here. The
provider name stays inside this module and the OAuth edge.

`search` implements the port, but step 1 does not expose it to the model: search does not
work inside the App Folder (empty, then 401 from Substrate Search; RFC §7 A6).

Change capture for the index (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §4, §7.2): `get_changes`
pages the App Folder's delta feed, which spike S0a confirmed under Files.ReadWrite.AppFolder.
The cursor is this module's own JSON (`{"url", "full", "drive_id", "root_id"}`), opaque to every
caller. A page's cursor names the drive and App Folder that served it: a cursor from another drive (a
reconnect to another account) or from a re-created App Folder is never followed, the enumeration
restarts instead (a cursor without the binding, written before it existed, is followed as before).

Errors (§4): a rejected refresh is DriveReauthRequired (a DriveNotConnectedError); a 401 that
survives a successful forced refresh is DriveAccessDenied for that operation, never a lost grant
(spike A6); 429/503 still failing after the retries here is DriveThrottled; a 410 on the change
feed, or a 404 on a stored change cursor, is DriveResyncRequired. A call that outlives GRAPH_CALL_TIMEOUT_S is a TimeoutError and any other
transport failure (an aiohttp.ClientError) an OSError: the tick classifies them by what the call is about
(§8.2: per-item on a call about one item, systemic on the change feed), Step 1 reports a plain failure.
A failed token refresh is drive-wide, so it stays DriveThrottled.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import replace
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import quote, unquote

import aiohttp  # module import on purpose: tests patch aiohttp.ClientSession

from ...domain.connector_spec import ConnectorSpec
from ...domain.drive_image_mime import MAX_VISION_IMAGE_BYTES
from ...domain.drive_index import DriveChange, DriveChanges, DriveChildrenPage
from ...domain.drive_index_params import DOWNLOAD_TIMEOUT_S, TICK_DEADLINE_S
from ...domain.user_drive import (
    DriveAccessDenied,
    DriveConnection,
    DriveFileTooLarge,
    DriveThumbnailTooLarge,
    DriveItem,
    DriveItemNotFoundError,
    DriveNameConflictError,
    DriveNotConnectedError,
    DrivePathTooLong,
    DriveReauthRequired,
    DriveResyncRequired,
    DriveStorageFull,
    DriveThrottled,
    join_drive_path,
)
from ...domain.oauth_credential import OAuthCredential
from ...ports.oauth_credentials_port import OAuthCredentialsPort
from ...ports.user_drive_port import UserDrivePort
from ...utils.logger import logger
from .graph_auth import GraphReauthRequired, MicrosoftGraphTokenProvider

ONEDRIVE_PROVIDER = "microsoft_onedrive"
_SCOPE = "Files.ReadWrite.AppFolder offline_access"
_GRAPH = "https://graph.microsoft.com/v1.0"
_SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024
_CHUNK = 16 * 320 * 1024  # 5 MiB; Graph requires multiples of 320 KiB
_ROOT_TTL_S = 300.0
_RETRIES = 3
_MAX_RETRY_AFTER_S = 30.0
_UNKNOWN_PARENT = "…"
# Provider-transport default (also used for a failed token refresh): a Retry-After that is missing or
# an HTTP date. The tick continues after it (the tick has its own backoff for embedding errors).
_THROTTLE_HINT_S = 60.0
# Bound on one Graph call (aiohttp's default is 300 s), well under TICK_DEADLINE_S: a hanging call
# ends as a TimeoutError instead of holding the user's tick (RFC Δ9, §8.2).
GRAPH_CALL_TIMEOUT_S = 60.0
# Streamed content reads (§8.1 step 3): chunk size, and the cap on one rendition — the drive models' vision cap,
# base64-safe (domain/drive_image_mime.py); a bigger rendition is no rendition.
_DOWNLOAD_CHUNK = 64 * 1024
THUMBNAIL_MAX_BYTES = MAX_VISION_IMAGE_BYTES
# Graph's named sizes and custom `c{w}x{h}[_crop]`; anything else never reaches the URL.
_THUMBNAIL_SIZE_RE = re.compile(
    r"(?:small|medium|large|smallSquare|mediumSquare|largeSquare|c[1-9]\d{0,3}x[1-9]\d{0,3}(?:_crop)?)")
# The App Folder item. Its parentReference.driveId is the drive id; the OAuth callback
# (src/web/oauth_app.py, _read_drive_id) reads the same field of the same call, so the two compare equal.
_APPROOT = "/me/drive/special/approot"
_DELTA_START = f"{_APPROOT}/delta?$top=200"   # S0a: $top paginates delta
# Facets stored raw on the record (§3.2); `hashes` comes from file.hashes.
_FACETS = ("photo", "image", "video", "audio", "location", "remoteItem", "package", "malware")
# Graph writes the same location two ways: '/drive/root:/…' (GET, children, PUT, PATCH, approot)
# and '/drives/<drive-id>/root:/…' (POST create-folder responses). Strip either prefix (RFC §7).
_ROOT_PREFIX_RE = re.compile(r"^(?:/drive|/drives/[^/]+)/root:")


def _q(segment: str) -> str:
    """One URL path segment: everything percent-encoded, '/' included (quote keeps it by default)."""
    return quote(segment, safe="")


def _normalize_path(path: str) -> str:
    """Decoded path with the '/drive[s/<id>]/root:' prefix removed ('' = the drive root)."""
    return _ROOT_PREFIX_RE.sub("", unquote(path), count=1)


def _retry_after(value: Optional[str], attempt: int) -> float:
    """Seconds to wait: Retry-After in seconds if parseable (it may be an HTTP date), else backoff."""
    try:
        seconds = float(value) if value is not None else float(2 ** attempt)
    except ValueError:
        seconds = float(2 ** attempt)
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER_S)


def _retry_after_hint(value: Optional[str]) -> float:
    """The provider's own Retry-After in seconds for the tick's continuation, capped at TICK_DEADLINE_S:
    the fan-out re-ticks after that anyway, and a far-future delay would break the enqueue."""
    try:
        seconds = float(value) if value is not None else _THROTTLE_HINT_S
    except ValueError:
        seconds = _THROTTLE_HINT_S
    return min(max(seconds, 0.0), float(TICK_DEADLINE_S))


# A path-addressed segment (`items/{id}:/{name}:`): the name is percent-encoded by `_q`, so it holds no '/' or ':'
_NAMED_SEGMENT_RE = re.compile(r":/([^/:]+):")


def _where(path: str) -> str:
    """A call's path for logs and error texts: no query string (delta links carry long opaque tokens), and a
    path-addressed name (an upload's or `child_by_name`'s `:/<name>:`) replaced by its length, `:/<n chars>:` — a file
    name never reaches a log line (USER_DRIVE_INDEXING_RFC §27.12; final review I2)."""
    bare = path.split("?", 1)[0]
    return _NAMED_SEGMENT_RE.sub(lambda m: f":/<{len(unquote(m.group(1)))} chars>:", bare)


def _is_graph_url(url: Optional[str]) -> bool:
    """A cursor or Location target the bearer token may be sent to."""
    return bool(url) and (str(url).startswith("/") or str(url).startswith(_GRAPH + "/"))


def _is_approot_delta(url: Optional[str]) -> bool:
    """A URL of the App Folder's own change feed. A live 410 named the whole drive's legacy feed
    (/drive/root/view.delta) as its Location, which is not this feed (RFC Δ46)."""
    if not _is_graph_url(url):
        return False
    path = _where(str(url))
    return path in (f"{_APPROOT}/delta", f"{_GRAPH}{_APPROOT}/delta")


def _encode_cursor(url: str, full: bool, drive_id: Optional[str] = None, root_id: Optional[str] = None) -> str:
    """A restart cursor carries no binding (its URL is the current grant's own feed); a page's cursor
    carries the drive and App Folder that served the page."""
    data: Dict[str, Any] = {"url": url, "full": full}
    if drive_id is not None:
        data["drive_id"] = drive_id
    if root_id is not None:
        data["root_id"] = root_id
    return json.dumps(data, separators=(",", ":"))


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        logger.warning(f"Drive timestamp not ISO 8601: {str(value)[:40]}")
        return None


def _to_change(raw: Dict[str, Any]) -> DriveChange:
    """A Graph driveItem as an observation (§3.1). A tombstone may carry cTag, size, the folder facet
    and the parent (S0a), but only its id is relied on."""
    item_id = str(raw.get("id", ""))
    if "deleted" in raw:
        # is_folder=False on purpose: the planner decides file vs folder from the existing record.
        return DriveChange(item_id=item_id, deleted=True)
    file = raw.get("file") or {}
    folder = raw.get("folder")
    facets: Dict[str, Any] = {key: raw[key] for key in _FACETS if key in raw}
    if file.get("hashes"):
        facets["hashes"] = dict(file["hashes"])
    return DriveChange(
        item_id=item_id,
        parent_id=str((raw.get("parentReference") or {}).get("id", "")),
        name=str(raw.get("name", "")),
        is_folder=folder is not None,
        size_bytes=int(raw.get("size") or 0),
        mime_type=str(file.get("mimeType", "")),
        content_version="" if folder is not None else str(raw.get("cTag") or ""),
        modified_at=_parse_time(raw.get("lastModifiedDateTime")),
        fs_modified_at=_parse_time((raw.get("fileSystemInfo") or {}).get("lastModifiedDateTime")),
        web_url=str(raw.get("webUrl", "")),
        child_count=int((folder or {}).get("childCount") or 0),
        facets=facets,
        created_at=_parse_time(raw.get("createdDateTime")),
    )


class _BadRequest(ValueError):
    """A 400 that is not a path-length refusal. A ValueError, so every caller keeps its old mapping;
    `code` is Graph's top-level error code (read for the code only, never logged in full)."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _error_codes(body: str) -> Tuple[str, str]:
    """(error code, innerError code) of a Graph error body; '' for what is absent or unparseable."""
    try:
        error = (json.loads(body) or {}).get("error") or {}
        return str(error.get("code") or ""), str((error.get("innerError") or {}).get("code") or "")
    except (ValueError, AttributeError, TypeError):
        return "", ""


class _CursorGone(ValueError):
    """410 Gone: the change cursor expired. A ValueError, so a Step 1 call keeps its old mapping."""

    def __init__(self, location: Optional[str]) -> None:
        super().__init__("Drive change cursor expired (410)")
        self.location = location


class OneDriveAdapter(UserDrivePort):
    def __init__(self, oauth_credentials: OAuthCredentialsPort, client_id: str, client_secret: str, *,
                 connector: ConnectorSpec) -> None:
        """`connector` is the drive kind's spec: the user's credential is the one of `connector.kind` (slice 1b passes
        it by handle)."""
        self._oauth = oauth_credentials
        self._kind = connector.kind
        self._tokens = MicrosoftGraphTokenProvider(oauth_credentials, client_id, client_secret,
                                                   connector.kind, _SCOPE)
        # user_id → (fetched_at, root json); the owner may rename/move the folder, hence a TTL.
        self._root_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        logger.info("✅ OneDriveAdapter initialized")

    # -- connection ---------------------------------------------------------

    @property
    def display_name(self) -> str:
        return "OneDrive"

    async def _credential(self, user_id: str) -> Optional[OAuthCredential]:
        """The user's credential of the drive kind (the oldest when several), or None."""
        found = await self._oauth.list_for_user(user_id, self._kind)
        return found[0] if found else None

    async def is_connected(self, user_id: str) -> bool:
        return bool(await self._oauth.list_for_user(user_id, self._kind))

    async def disconnect(self, user_id: str) -> None:
        for credential in await self._oauth.list_for_user(user_id, self._kind):
            await self._oauth.delete(user_id, credential.credential_id)
        self._tokens.forget(user_id)
        self._root_cache.pop(user_id, None)

    # -- reads --------------------------------------------------------------

    async def get_root(self, user_id: str) -> DriveItem:
        async with aiohttp.ClientSession() as s:
            return self._to_item(await self._root_raw(s, user_id), "", is_root=True)

    async def get_item(self, user_id: str, item_id: str) -> DriveItem:
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, user_id, "GET", f"/me/drive/items/{_q(item_id)}")
            return await self._item(s, user_id, raw)

    async def list_children(self, user_id: str, folder_id: str) -> List[DriveItem]:
        async with aiohttp.ClientSession() as s:
            url: Optional[str] = f"/me/drive/items/{_q(folder_id)}/children?$top=200"
            items: List[DriveItem] = []
            while url:
                page = await self._call(s, user_id, "GET", url)
                for raw in page.get("value", []):
                    items.append(await self._item(s, user_id, raw))
                url = page.get("@odata.nextLink")
            return items

    async def search(self, user_id: str, query: str, limit: int) -> List[DriveItem]:
        async with aiohttp.ClientSession() as s:
            escaped = query.replace("'", "''")
            page = await self._call(s, user_id, "GET",
                                    f"/me/drive/special/approot/search(q='{_q(escaped)}')?$top={limit}")
            return [await self._item(s, user_id, raw) for raw in page.get("value", [])[:limit]]

    async def download(self, user_id: str, item_id: str) -> bytes:
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, user_id, "GET", f"/me/drive/items/{_q(item_id)}")
            url = raw.get("@microsoft.graph.downloadUrl")
            if not url:
                raise DriveItemNotFoundError(f"No downloadable content for item {item_id}")
            # Pre-authenticated short-lived URL on another host: never send the bearer token there.
            async with s.get(url) as resp:
                if resp.status != 200:
                    logger.error(f"Drive download failed ({resp.status}) for item {item_id}")
                    raise ValueError(f"Drive download failed ({resp.status})")
                return bytes(await resp.read())

    async def download_url(self, user_id: str, item_id: str) -> str:
        """The item's pre-authenticated download URL (§27.4), read by the same GET as `download`. It is never
        logged and never put in an error, and it is not fetched here, so no bearer token goes to its host."""
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, user_id, "GET", f"/me/drive/items/{_q(item_id)}")
            url = raw.get("@microsoft.graph.downloadUrl")
            if not url:
                raise DriveItemNotFoundError(f"No downloadable content for item {item_id}")
            return str(url)

    # -- writes -------------------------------------------------------------

    async def upload(self, user_id: str, parent_id: str, filename: str, data: bytes, content_type: str,
                     conflict: Literal["rename", "replace", "fail"] = "rename") -> DriveItem:
        async with aiohttp.ClientSession() as s:
            target = f"/me/drive/items/{_q(parent_id)}:/{_q(filename)}:"
            if len(data) <= _SIMPLE_UPLOAD_MAX:
                raw = await self._call(s, user_id, "PUT",
                                       f"{target}/content?@microsoft.graph.conflictBehavior={conflict}",
                                       data=data, content_type=content_type)
            else:
                raw = await self._session_upload(s, user_id, f"{target}/createUploadSession",
                                                 {"item": {"@microsoft.graph.conflictBehavior": conflict}}, data)
            return await self._item(s, user_id, raw)

    async def replace_content(self, user_id: str, item_id: str, data: bytes, content_type: str) -> DriveItem:
        async with aiohttp.ClientSession() as s:
            base = f"/me/drive/items/{_q(item_id)}"
            if len(data) <= _SIMPLE_UPLOAD_MAX:
                raw = await self._call(s, user_id, "PUT", f"{base}/content", data=data, content_type=content_type)
            else:
                raw = await self._session_upload(s, user_id, f"{base}/createUploadSession",
                                                 {"item": {"@microsoft.graph.conflictBehavior": "replace"}}, data)
            return await self._item(s, user_id, raw)

    async def move(self, user_id: str, item_id: str, new_parent_id: Optional[str] = None,
                   new_name: Optional[str] = None) -> DriveItem:
        body: Dict[str, Any] = {}
        if new_parent_id:
            body["parentReference"] = {"id": new_parent_id}
        if new_name:
            body["name"] = new_name
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, user_id, "PATCH", f"/me/drive/items/{_q(item_id)}", json=body)
            return await self._item(s, user_id, raw)

    async def create_folder(self, user_id: str, parent_id: str, name: str) -> DriveItem:
        async with aiohttp.ClientSession() as s:
            try:
                raw = await self._call(s, user_id, "POST", f"/me/drive/items/{_q(parent_id)}/children",
                                       json={"name": name, "folder": {},
                                             "@microsoft.graph.conflictBehavior": "fail"})
            except _BadRequest as exc:
                if exc.code == "invalidRequest":
                    # S1 (Δ216): a folder created near the path limit answered a plain 400 invalidRequest.
                    raise DrivePathTooLong("Drive folder path is too long") from exc
                raise
            return await self._item(s, user_id, raw)

    async def child_by_name(self, user_id: str, parent_id: str, name: str) -> Optional[DriveChange]:
        """The child with exactly that name: one path-relative GET (S1: works inside the App Folder), no search."""
        async with aiohttp.ClientSession() as s:
            try:
                raw = await self._call(s, user_id, "GET", f"/me/drive/items/{_q(parent_id)}:/{_q(name)}:")
            except DriveItemNotFoundError:
                return None
        return _to_change(raw)

    async def area_path(self, user_id: str) -> str:
        """Decoded absolute path of the area root from the drive root (e.g. '/Apps/Alek-bot'). Log its length only."""
        async with aiohttp.ClientSession() as s:
            return await self._root_abs(s, user_id)

    async def delete(self, user_id: str, item_id: str) -> None:
        async with aiohttp.ClientSession() as s:
            await self._call(s, user_id, "DELETE", f"/me/drive/items/{_q(item_id)}")

    # -- content for the index (USER_DRIVE_INDEXING_RFC §8.1, §24.5) -------------------------

    async def download_to_file(self, user_id: str, item_id: str, dest_path: str, max_bytes: int) -> int:
        """Stream the content to `dest_path`, chunk by chunk, never whole in memory. The stream is cut as soon
        as it passes `max_bytes` (metadata can understate a size, §5.4); the partial file is the caller's. No
        download URL, or one answering 404/410: DriveItemNotFoundError."""
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, user_id, "GET", f"/me/drive/items/{_q(item_id)}")
            url = raw.get("@microsoft.graph.downloadUrl")
            if not url:
                raise DriveItemNotFoundError(f"No downloadable content for item {item_id}")
            written = 0
            try:
                # Pre-authenticated short-lived URL on another host: never send the bearer token there.
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_S)) as resp:
                    if resp.status in (404, 410):   # the content went away between the item read and the fetch
                        logger.info(f"Drive download URL for item {item_id} is gone ({resp.status})")
                        raise DriveItemNotFoundError(f"Content of item {item_id} is gone ({resp.status})")
                    if resp.status != 200:
                        logger.error(f"Drive download failed ({resp.status}) for item {item_id}")
                        raise ValueError(f"Drive download failed ({resp.status})")
                    # Plain writes: 64 KiB chunks to Cloud Run's in-memory /tmp cost microseconds.
                    with open(dest_path, "wb") as fh:
                        async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK):
                            if written + len(chunk) > max_bytes:
                                logger.warning(f"Drive download of item {item_id} passed {max_bytes} bytes; cut")
                                raise DriveFileTooLarge(max_bytes)
                            fh.write(chunk)
                            written += len(chunk)
            except asyncio.TimeoutError as exc:
                logger.warning(f"Drive download of item {item_id} timed out after {written} bytes")
                raise asyncio.TimeoutError("Drive download timed out") from exc
            except aiohttp.ClientError as exc:
                logger.warning(f"Drive download of item {item_id} transport failure: {type(exc).__name__}")
                raise OSError(f"Drive download transport failure ({type(exc).__name__})") from exc
            return written

    async def get_thumbnail(self, user_id: str, item_id: str, size: str) -> Optional[bytes]:
        """One provider rendition: a standard size (`medium` = 176 px, `large` = 800 px) or a custom `c{w}x{h}`
        (Graph scales it, HEIC included; we never decode). The set comes from `/thumbnails?select=<size>`, the
        bytes from its pre-authenticated URL. None: no rendition or its URL gone; DriveThumbnailTooLarge: bigger than
        the vision cap."""
        if not _THUMBNAIL_SIZE_RE.fullmatch(size):
            raise ValueError(f"Unknown thumbnail size {size!r}")
        async with aiohttp.ClientSession() as s:
            page = await self._call(s, user_id, "GET", f"/me/drive/items/{_q(item_id)}/thumbnails?select={size}")
            sets = page.get("value") or []
            rendition = sets[0].get(size) if sets and isinstance(sets[0], dict) else None
            url = str(rendition.get("url") or "") if isinstance(rendition, dict) else ""
            if not url.startswith("https://"):
                if url:
                    logger.warning(f"Drive thumbnail URL for item {item_id} is not https; ignored")
                return None
            try:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=GRAPH_CALL_TIMEOUT_S)) as resp:
                    if resp.status == 404:
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
            except asyncio.TimeoutError as exc:
                logger.warning(f"Drive thumbnail fetch for item {item_id} timed out")
                raise asyncio.TimeoutError("Drive thumbnail fetch timed out") from exc
            except aiohttp.ClientError as exc:
                logger.warning(f"Drive thumbnail fetch for item {item_id} transport failure: {type(exc).__name__}")
                raise OSError(f"Drive thumbnail transport failure ({type(exc).__name__})") from exc

    # -- change capture for the index (USER_DRIVE_INDEXING_RFC §4, §7.2) -------

    async def get_changes(self, user_id: str, cursor: Optional[str]) -> DriveChanges:
        """One delta page. None starts a full enumeration of the App Folder; every page of an
        enumeration (onboarding or after a 410) reports full_enumeration, a deltaLink round does not."""
        url, full, bound_drive, bound_root = self._decode_cursor(cursor)
        async with aiohttp.ClientSession() as s:
            # Read fresh, never from the 5-minute cache, so the root and the page always come from
            # the same token: a cached root with a new grant's page would give a wrong root_item_id.
            # A reconnect itself is noticed by connection() (it drops both caches). One GET per page.
            self._root_cache.pop(user_id, None)
            root = await self._root_raw(s, user_id)
            drive_id = str((root.get("parentReference") or {}).get("driveId", ""))
            root_id = str(root.get("id", ""))
            if (bound_drive and drive_id and bound_drive != drive_id) or \
                    (bound_root and root_id and bound_root != root_id):
                # Never follow it: Graph may refuse it with something other than 410, which would fail
                # every tick before the per-page drive check runs. The restart page carries
                # the fresh drive id, so the tick sees the switch.
                logger.warning(f"Drive change cursor belongs to another drive or App Folder for user "
                               f"{user_id[:8]}; restarting enumeration")
                raise DriveResyncRequired(_encode_cursor(_DELTA_START, True))
            try:
                page = await self._call(s, user_id, "GET", url)
            except _CursorGone as exc:
                if _is_approot_delta(exc.location):
                    restart = str(exc.location)
                else:
                    if exc.location:
                        logger.warning(f"Drive 410 Location is not the App Folder's change feed "
                                       f"({_where(exc.location)}); restarting from a fresh delta")
                    restart = _DELTA_START
                raise DriveResyncRequired(_encode_cursor(restart, True)) from exc
            except DriveItemNotFoundError as exc:
                # A stored link Graph no longer knows is as dead as a 410 one (§7.5). Not on the delta start
                # itself: restarting would ask for the same URL again, every tick.
                if cursor is None or url == _DELTA_START:
                    raise
                logger.warning(f"Drive change cursor not found (404) for user {user_id[:8]}; restarting enumeration")
                raise DriveResyncRequired(_encode_cursor(_DELTA_START, True)) from exc
        next_link, delta_link = page.get("@odata.nextLink"), page.get("@odata.deltaLink")
        if next_link:
            new_cursor, complete = _encode_cursor(str(next_link), full, drive_id, root_id), False
        elif delta_link:
            new_cursor, complete = _encode_cursor(str(delta_link), False, drive_id, root_id), True
        else:
            logger.error("Drive delta page carried neither a nextLink nor a deltaLink")
            raise ValueError("Drive delta page without a continuation link")
        return DriveChanges(
            changes=tuple(_to_change(raw) for raw in page.get("value", [])),
            cursor=new_cursor,
            complete=complete,
            full_enumeration=full,
            drive_id=drive_id,
            root_item_id=root_id,
        )

    async def get_change(self, user_id: str, item_id: str) -> DriveChange:
        """One item by id. A 404 raises DriveItemNotFoundError and is never turned into a deletion
        (USER_DRIVE_INDEXING_RFC §26.2, Δ48): Graph answers 404 both for an item that is gone (deleted, or
        moved out of the App Folder, S0a) and for one whose own permission list lacks the app's entry, which
        `approot/delta` still lists. Existence is decided by delta; the caller decides what its 404 means."""
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, user_id, "GET", f"/me/drive/items/{_q(item_id)}")
        return _to_change(raw)

    async def list_children_changes(self, user_id: str, folder_id: str, limit: int,
                                    cursor: Optional[str] = None) -> DriveChildrenPage:
        """One page of a folder's children: `$top=limit`; `next_cursor` is Graph's nextLink, None on the
        last page. A cursor that does not point at Graph is never followed: the listing restarts."""
        url = f"/me/drive/items/{_q(folder_id)}/children?$top={limit}"
        if cursor is not None:
            if _is_graph_url(cursor):
                url = cursor
            else:
                logger.warning("Drive listing cursor points outside Graph; restarting the listing")
        async with aiohttp.ClientSession() as s:
            page = await self._call(s, user_id, "GET", url)
        next_link = page.get("@odata.nextLink")
        return DriveChildrenPage(changes=tuple(_to_change(raw) for raw in page.get("value", [])),
                                 next_cursor=str(next_link) if next_link else None)

    async def connection(self, user_id: str) -> DriveConnection:
        """Read on every tick (the gate). When the stored access token is not the one cached here, the
        grant changed under this process (a reconnect, maybe to another account, or a refresh by the
        other service): drop the token and root caches, so the next call reads the new grant at once."""
        creds = await self._credential(user_id)
        cached = None if creds is None else self._tokens.cached_token(user_id, creds.credential_id)
        if creds is None or (cached is not None and cached != creds.access_token) \
                or self._tokens.held_credential_id(user_id) not in (None, creds.credential_id):
            self._tokens.forget(user_id)
            self._root_cache.pop(user_id, None)
        if creds is None:
            return DriveConnection(connected=False)
        return DriveConnection(connected=True, needs_reconnect=creds.needs_reconnect, drive_id=creds.drive_id)

    async def flag_reconnect(self, user_id: str) -> bool:
        """Get, then update with the flag set (Gmail precedent, decisions/gmail_grant_revoked_reconnect.md:
        no port change for an atomic set). A reconnect saves fresh credentials, which clears it."""
        creds = await self._credential(user_id)
        if creds is None or creds.needs_reconnect:
            return False
        if not await self._oauth.update(replace(creds, needs_reconnect=True)):
            return False                     # disconnected meanwhile: nothing to flag, and none is brought back
        logger.warning(f"Drive marked needs_reconnect for user {user_id[:8]}")
        return True

    @staticmethod
    def _decode_cursor(cursor: Optional[str]) -> Tuple[str, bool, Optional[str], Optional[str]]:
        """(url, full, drive_id, root_id); a binding absent from the cursor is None (unknown). A cursor
        this module cannot read restarts as a full enumeration."""
        if cursor is None:
            return _DELTA_START, True, None, None
        try:
            data = json.loads(cursor)
            url, full = data["url"], data["full"]
            drive_id, root_id = data.get("drive_id"), data.get("root_id")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            logger.warning(f"Drive change cursor unreadable ({type(exc).__name__}); restarting enumeration")
            raise DriveResyncRequired(_encode_cursor(_DELTA_START, True)) from exc
        if not isinstance(url, str) or not isinstance(full, bool) or not _is_approot_delta(url) \
                or not isinstance(drive_id, (str, type(None))) or not isinstance(root_id, (str, type(None))):
            logger.warning("Drive change cursor malformed or not the App Folder's feed; restarting enumeration")
            raise DriveResyncRequired(_encode_cursor(_DELTA_START, True))
        return url, full, drive_id, root_id

    # -- transport ----------------------------------------------------------

    async def _headers(self, user_id: str, *, force_refresh: bool = False) -> Dict[str, str]:
        try:
            headers = await self._tokens.user_headers(user_id, force_refresh=force_refresh)
        except GraphReauthRequired as exc:
            logger.warning(f"Drive token refresh rejected for user {user_id[:8]}; reconnect needed")
            raise DriveReauthRequired("Drive access expired; reconnect needed") from exc
        except (ValueError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # The token endpoint failed (5xx, network, timeout) but did not reject the grant: systemic,
            # so the tick ends and retries later (§8.2), never charged to the item being read.
            logger.warning(f"Drive token refresh failed for user {user_id[:8]}: {type(exc).__name__} {exc}")
            raise DriveThrottled(_THROTTLE_HINT_S) from exc
        if headers is None:
            raise DriveNotConnectedError(f"No drive credentials for user {user_id[:8]}")
        return headers

    async def _call(self, s: aiohttp.ClientSession, user_id: str, method: str, path: str, *,
                    json: Any = None, data: Optional[bytes] = None,
                    content_type: Optional[str] = None) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{_GRAPH}{path}"
        throttled = 0          # 429/503 retries — separate from the 401 refresh,
        refreshed = False      # so throttling never consumes the forced refresh (§4.2)
        while True:
            headers = await self._headers(user_id)
            if content_type:
                headers = {**headers, "Content-Type": content_type}
            kwargs: Dict[str, Any] = {"headers": headers,
                                      "timeout": aiohttp.ClientTimeout(total=GRAPH_CALL_TIMEOUT_S)}
            if json is not None:
                kwargs["json"] = json
            if data is not None:
                kwargs["data"] = data
            try:
                async with getattr(s, method.lower())(url, **kwargs) as resp:
                    status = resp.status
                    if status == 401:
                        if refreshed:
                            # The refresh succeeded, so the grant is alive: this operation is refused
                            # (spike A6: the App Folder answers 401 to calls it does not support).
                            logger.warning(f"Drive {method} {_where(path)}: 401 after a successful token refresh")
                            raise DriveAccessDenied(f"Drive {method} refused after a token refresh")
                        refreshed = True
                        # The token provider dedupes forced refreshes itself; no invalidate here.
                        await self._headers(user_id, force_refresh=True)  # next attempt uses the fresh token
                        continue
                    if status in (429, 503):
                        if throttled < _RETRIES:
                            delay = _retry_after(resp.headers.get("Retry-After"), throttled)
                            throttled += 1
                            logger.warning(f"Drive {method} throttled ({status}); retrying in {delay:.0f}s")
                            await asyncio.sleep(delay)
                            continue
                        hint = _retry_after_hint(resp.headers.get("Retry-After"))
                        logger.warning(f"Drive {method} {_where(path)} still throttled ({status}) after "
                                       f"{_RETRIES} retries; retry after {hint:.0f}s")
                        raise DriveThrottled(hint)
                    if status == 410:
                        body = await resp.text()
                        logger.warning(f"Drive {method} {_where(path)}: 410 Gone (change cursor expired): "
                                       f"{body[:200]}")
                        raise _CursorGone(resp.headers.get("Location"))
                    if status == 404:
                        raise DriveItemNotFoundError(f"Drive item not found: {_where(path)}")
                    if status == 409:
                        raise DriveNameConflictError(f"Drive name taken: {_where(path)}")
                    if status == 507:
                        logger.warning(f"Drive {method} {_where(path)}: 507 insufficient storage")
                        raise DriveStorageFull("The drive is full")
                    if status == 204:
                        return {}
                    if not resp.ok:
                        body = await resp.text()
                        if status == 400:
                            code, inner = _error_codes(body)
                            if inner == "pathIsTooLong":
                                logger.warning(f"Drive {method} {_where(path)}: 400 path is too long")
                                raise DrivePathTooLong("Drive path is too long")
                            logger.error(f"Drive {method} {_where(path)} failed (400): {code}/{inner}")
                            raise _BadRequest(f"Drive {method} failed (400)", code)
                        code, inner = _error_codes(body)    # the body's free text can quote a file name
                        logger.error(f"Drive {method} {_where(path)} failed ({status}): {code}/{inner}")
                        raise ValueError(f"Drive {method} failed ({status})")
                    return dict(await resp.json())
            except asyncio.TimeoutError as exc:
                # A call past GRAPH_CALL_TIMEOUT_S (aiohttp's socket timeouts subclass it too). §8.2 classifies
                # it by the call's scope: per-item on a call about one item, systemic on the feed.
                logger.warning(f"Drive {method} {_where(path)} timed out: {type(exc).__name__}")
                raise asyncio.TimeoutError(f"Drive {method} timed out") from exc
            except aiohttp.ClientError as exc:
                # Any other transport failure: an OSError, the per-item class on a call about one item (§8.2).
                logger.warning(f"Drive {method} {_where(path)} transport failure: {type(exc).__name__}")
                raise OSError(f"Drive {method} transport failure ({type(exc).__name__})") from exc

    async def _session_upload(self, s: aiohttp.ClientSession, user_id: str, path: str,
                              body: Dict[str, Any], data: bytes) -> Dict[str, Any]:
        upload_url = (await self._call(s, user_id, "POST", path, json=body))["uploadUrl"]
        total = len(data)
        result: Dict[str, Any] = {}
        for start in range(0, total, _CHUNK):
            chunk = data[start:start + _CHUNK]
            end = start + len(chunk) - 1
            # The upload URL is pre-authenticated; a bearer header here is rejected.
            headers = {"Content-Length": str(len(chunk)), "Content-Range": f"bytes {start}-{end}/{total}"}
            async with s.put(upload_url, headers=headers, data=chunk) as resp:
                if resp.status not in (200, 201, 202):
                    text = await resp.text()
                    logger.error(f"Drive chunk upload failed ({resp.status}): {text[:300]}")
                    raise ValueError(f"Drive chunk upload failed ({resp.status})")
                if resp.status in (200, 201):
                    result = dict(await resp.json())
        return result

    # -- paths --------------------------------------------------------------

    async def _root_raw(self, s: aiohttp.ClientSession, user_id: str) -> Dict[str, Any]:
        cached = self._root_cache.get(user_id)
        if cached and time.monotonic() - cached[0] < _ROOT_TTL_S:
            return cached[1]
        raw = await self._call(s, user_id, "GET", _APPROOT)
        self._root_cache[user_id] = (time.monotonic(), raw)
        return raw

    async def _root_abs(self, s: aiohttp.ClientSession, user_id: str) -> str:
        """Normalised absolute path of the app folder, taken from special/approot (it is
        localised and the owner may have moved it), e.g. '/Apps/Alek-bot'."""
        raw = await self._root_raw(s, user_id)
        parent = _normalize_path((raw.get("parentReference") or {}).get("path", "") or "")
        return f"{parent}/{raw.get('name', '')}"

    @staticmethod
    def _rel_parent(raw: Dict[str, Any], root_abs: str) -> Optional[str]:
        """Parent path relative to the area root, decoded; None if absent or not under the root."""
        encoded = (raw.get("parentReference") or {}).get("path")
        if not encoded:
            return None
        parent = _normalize_path(encoded)
        if parent == root_abs:
            return ""
        if parent.startswith(root_abs + "/"):
            return parent[len(root_abs) + 1:]
        return None

    async def _item(self, s: aiohttp.ClientSession, user_id: str, raw: Dict[str, Any]) -> DriveItem:
        root_abs = await self._root_abs(s, user_id)
        rel = self._rel_parent(raw, root_abs)
        if rel is None and raw.get("id"):
            if (raw.get("parentReference") or {}).get("path"):
                logger.warning(f"Drive path outside the app folder for item {raw['id']}; re-reading")
            raw = await self._call(s, user_id, "GET", f"/me/drive/items/{_q(str(raw['id']))}")
            rel = self._rel_parent(raw, root_abs)
            if rel is None:
                logger.warning(f"Drive path still unresolved for item {raw.get('id')}")
                rel = _UNKNOWN_PARENT
        return self._to_item(raw, rel or "")

    @staticmethod
    def _to_item(raw: Dict[str, Any], parent_rel: str, *, is_root: bool = False) -> DriveItem:
        name = str(raw.get("name", ""))
        modified = raw.get("lastModifiedDateTime")
        return DriveItem(
            item_id=str(raw.get("id", "")),
            name=name,
            path="" if is_root else join_drive_path(parent_rel, name),
            is_folder="folder" in raw,
            size_bytes=int(raw.get("size") or 0),
            mime_type=str((raw.get("file") or {}).get("mimeType", "")),
            modified_at=datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else None,
            web_url=str(raw.get("webUrl", "")),
            child_count=int((raw.get("folder") or {}).get("childCount") or 0),
            ctag=str(raw.get("cTag") or ""),
        )
