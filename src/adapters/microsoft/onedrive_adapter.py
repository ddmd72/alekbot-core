"""
OneDriveAdapter — UserDrivePort over Microsoft Graph, scoped to the app's App Folder
(docs/10_rfcs/USER_DRIVE_RFC.md §2, §4.2, §4.3).

Every call takes the `DriveHandle` of the connection it acts on (MULTI_DRIVE_RFC §6, E6): the grant is found by
`handle.credential_id`, and the root cache and the token cache are keyed by the connection / `(user_id, credential_id)`,
never by `user_id` alone (T-3). Item and drive identities are built here (`onedrive_item_identity`,
`onedrive_drive_identity`: `microsoft_onedrive:{driveId}:{itemId}` / `microsoft_onedrive:{driveId}`, §7.2); Graph's raw
facets become typed `DriveMediaFacts` (`media_facts_from_graph`), a file's `quickXorHash` a `ContentIdentity("quickxor", …)`.

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
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple
from urllib.parse import quote, unquote

import aiohttp  # module import on purpose: tests patch aiohttp.ClientSession

from ...domain.connector_spec import ConnectorSpec
from ...domain.drive_capabilities import DriveCapabilities
from ...domain.drive_content_identity import ContentIdentity
from ...domain.drive_handle import DriveHandle
from ...domain.drive_image_mime import MAX_VISION_IMAGE_BYTES
from ...domain.drive_index import DriveChange, DriveChanges, DriveChildrenPage
from ...domain.drive_index_params import DOWNLOAD_TIMEOUT_S, TICK_DEADLINE_S
from ...domain.drive_media_facts import DriveMediaFacts
from ...domain.drive_media_source import MediaSource
from ...domain.user_drive import (
    DriveAccessDenied,
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
# A rendition is asked for as Graph's custom `c{n}x{n}` (scaled to fit, aspect kept); n is at most four digits.
_THUMBNAIL_MAX_EDGE_PX = 9999
# The longest absolute path the App Folder accepts (the user drive service's path check reads it through
# `capabilities`; spike S1, Δ216).
MAX_PATH_CHARS = 400
# The family of the content identity: the hash Graph reports for a file (`file.hashes.quickXorHash`).
CONTENT_FAMILY = "quickxor"
# The App Folder item. Its parentReference.driveId is the drive id; the connect's drive activation
# (`read_drive_id`, below) reads the same field of the same call, so the two compare equal.
_APPROOT = "/me/drive/special/approot"
# Bound on the drive-id read at connect: the OAuth callback waits for it.
DRIVE_ID_READ_TIMEOUT_S = 10.0
_DELTA_START = f"{_APPROOT}/delta?$top=200"   # S0a: $top paginates delta
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


def onedrive_drive_identity(drive_id: str, id_space: str = ONEDRIVE_PROVIDER) -> str:
    """The drive's identity, `{id_space}:{driveId}` (MULTI_DRIVE_RFC §7.2); "" when the drive id is unknown. The
    cut-over script and the runtime build it with this one function (I6)."""
    return f"{id_space}:{drive_id}" if drive_id else ""


def onedrive_item_identity(drive_id: str, item_id: str, id_space: str = ONEDRIVE_PROVIDER) -> str:
    """The item's identity, `{id_space}:{driveId}:{itemId}` (§7.2): byte-identical to the key a note was bound with
    before the rebuild (`i:` + this). "" when the drive id or the item id is unknown."""
    return f"{id_space}:{drive_id}:{item_id}" if drive_id and item_id else ""


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return 0
    return int(value)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """A timestamp Graph printed without an offset is UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def media_facts_from_graph(raw: Mapping[str, Any], *, drive_id: str = "",
                           id_space: str = ONEDRIVE_PROVIDER) -> DriveMediaFacts:
    """Graph's raw facets of one driveItem as typed `DriveMediaFacts` (§7.2): `photo` (taken date, camera), `image`,
    `video` and `audio` (durations are milliseconds already), the `location` facet, `package`, `malware`, and
    `remoteItem` as `alias_of` (the identity of the item it points to, "" when Graph does not say which). A value
    of the wrong type reads as absent. `drive_id` is the item's own drive, the home of an alias whose reference
    carries no drive."""
    photo = raw.get("photo") if isinstance(raw.get("photo"), Mapping) else {}
    image = raw.get("image") if isinstance(raw.get("image"), Mapping) else {}
    video = raw.get("video") if isinstance(raw.get("video"), Mapping) else {}
    audio = raw.get("audio") if isinstance(raw.get("audio"), Mapping) else {}
    location = raw.get("location") if isinstance(raw.get("location"), Mapping) else {}
    lat, lon = location.get("latitude"), location.get("longitude")
    point: Optional[Tuple[float, float]] = None
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) \
            and not isinstance(lat, bool) and not isinstance(lon, bool):
        point = float(lat), float(lon)
    alias_of: Optional[str] = None
    remote = raw.get("remoteItem")
    if remote is not None:
        remote = remote if isinstance(remote, Mapping) else {}
        remote_drive = str((remote.get("parentReference") or {}).get("driveId") or drive_id)
        alias_of = onedrive_item_identity(remote_drive, str(remote.get("id") or ""), id_space)
    return DriveMediaFacts(
        taken_at=_aware(_parse_time(photo.get("takenDateTime"))),
        camera_make=str(photo.get("cameraMake") or ""),
        camera_model=str(photo.get("cameraModel") or ""),
        latitude=point[0] if point else None,
        longitude=point[1] if point else None,
        image_width=_non_negative_int(image.get("width")),
        image_height=_non_negative_int(image.get("height")),
        video_duration_ms=_non_negative_int(video.get("duration")),
        video_width=_non_negative_int(video.get("width")),
        video_height=_non_negative_int(video.get("height")),
        audio_duration_ms=_non_negative_int(audio.get("duration")),
        package="package" in raw,
        malware="malware" in raw,
        alias_of=alias_of,
    )


def content_identity_from_graph(raw: Mapping[str, Any]) -> Optional[ContentIdentity]:
    """`ContentIdentity("quickxor", quickXorHash, size)` of a file driveItem; None for a folder or a file Graph gave no
    hash for (no family is implied by absence, D12)."""
    file = raw.get("file") if isinstance(raw.get("file"), Mapping) else {}
    hashes = file.get("hashes") if isinstance(file.get("hashes"), Mapping) else {}
    digest = hashes.get("quickXorHash")
    if not digest or "folder" in raw:
        return None
    return ContentIdentity(CONTENT_FAMILY, str(digest), _non_negative_int(raw.get("size")))


def _item_drive_id(raw: Mapping[str, Any], fallback: str = "") -> str:
    return str((raw.get("parentReference") or {}).get("driveId") or fallback)


def _to_change(raw: Dict[str, Any], drive_id: str = "", id_space: str = ONEDRIVE_PROVIDER) -> DriveChange:
    """A Graph driveItem as an observation (§3.1). `drive_id` is the drive the item lives in when the item does not
    name it. A tombstone may carry cTag, size, the folder facet and the parent (S0a), but only its id is relied on."""
    item_id = str(raw.get("id", ""))
    own_drive = _item_drive_id(raw, drive_id)
    identity = onedrive_item_identity(own_drive, item_id, id_space)
    if "deleted" in raw:
        # is_folder=False on purpose: the planner decides file vs folder from the existing record.
        return DriveChange(item_id=item_id, deleted=True, identity=identity)
    file = raw.get("file") or {}
    folder = raw.get("folder")
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
        created_at=_parse_time(raw.get("createdDateTime")),
        identity=identity,
        content_identity=content_identity_from_graph(raw),
        media=media_facts_from_graph(raw, drive_id=own_drive, id_space=id_space),
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
        """`connector` is the drive kind's spec: its `kind` names the credential family the token provider refreshes
        and its `id_space` prefixes every identity this adapter builds. The grant of a call is the handle's own."""
        self._id_space = connector.id_space or ONEDRIVE_PROVIDER
        self._tokens = MicrosoftGraphTokenProvider(oauth_credentials, client_id, client_secret,
                                                   connector.kind, _SCOPE)
        # connection_id → (fetched_at, root json); the owner may rename/move the folder, hence a TTL.
        self._root_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        logger.info("✅ OneDriveAdapter initialized")

    # -- per connection -----------------------------------------------------

    async def capabilities(self, handle: DriveHandle) -> DriveCapabilities:
        """The App Folder's path limit (§7.1)."""
        return DriveCapabilities(max_path_chars=MAX_PATH_CHARS)

    async def forget(self, handle: DriveHandle) -> None:
        """Drop the connection's root cache and its credential's cached token (after a disconnect)."""
        self._root_cache.pop(handle.connection_id, None)
        self._tokens.invalidate(handle.user_id, handle.credential_id)

    async def read_drive_id(self, access_token: str) -> str:
        """The drive id of a just-granted token, for the index's drive-switch check (USER_DRIVE_INDEXING_RFC §10.1
        step 0), read by the drive connect's activation before the credential carries it. Read from the App Folder
        item: it answers under Files.ReadWrite.AppFolder (spike A6 saw other calls refused with 401), and
        `parentReference.driveId` is the field `get_changes` binds its cursor to, so both sides compare the same
        string. Best effort: "" (any failure, a non-2xx, a body without the field, past DRIVE_ID_READ_TIMEOUT_S)
        only skips the check, so it never fails the connect. Not on UserDrivePort: it runs before a connection
        exists, so there is no handle yet (MULTI_DRIVE_RFC §5.1)."""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{_GRAPH}{_APPROOT}", headers={"Authorization": f"Bearer {access_token}"},
                                 timeout=aiohttp.ClientTimeout(total=DRIVE_ID_READ_TIMEOUT_S)) as resp:
                    if not resp.ok:
                        logger.warning(f"⚠️ Drive id not read at connect ({resp.status})")
                        return ""
                    body = await resp.json()
            drive_id = (body.get("parentReference") or {}).get("driveId") if isinstance(body, dict) else None
            return drive_id if isinstance(drive_id, str) else ""
        except Exception as exc:
            logger.warning(f"⚠️ Drive id not read at connect: {type(exc).__name__}")
            return ""

    # -- reads --------------------------------------------------------------

    async def get_root(self, handle: DriveHandle) -> DriveItem:
        async with aiohttp.ClientSession() as s:
            root = await self._root_raw(s, handle)
            return self._to_item(root, "", is_root=True, drive_id=_item_drive_id(root))

    async def get_item(self, handle: DriveHandle, item_id: str) -> DriveItem:
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, handle, "GET", f"/me/drive/items/{_q(item_id)}")
            return await self._item(s, handle, raw)

    async def list_children(self, handle: DriveHandle, folder_id: str) -> List[DriveItem]:
        async with aiohttp.ClientSession() as s:
            url: Optional[str] = f"/me/drive/items/{_q(folder_id)}/children?$top=200"
            items: List[DriveItem] = []
            while url:
                page = await self._call(s, handle, "GET", url)
                for raw in page.get("value", []):
                    items.append(await self._item(s, handle, raw))
                url = page.get("@odata.nextLink")
            return items

    async def search(self, handle: DriveHandle, query: str, limit: int) -> List[DriveItem]:
        async with aiohttp.ClientSession() as s:
            escaped = query.replace("'", "''")
            page = await self._call(s, handle, "GET",
                                    f"/me/drive/special/approot/search(q='{_q(escaped)}')?$top={limit}")
            return [await self._item(s, handle, raw) for raw in page.get("value", [])[:limit]]

    async def download(self, handle: DriveHandle, item_id: str) -> bytes:
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, handle, "GET", f"/me/drive/items/{_q(item_id)}")
            url = raw.get("@microsoft.graph.downloadUrl")
            if not url:
                raise DriveItemNotFoundError(f"No downloadable content for item {item_id}")
            # Pre-authenticated short-lived URL on another host: never send the bearer token there.
            async with s.get(url) as resp:
                if resp.status != 200:
                    logger.error(f"Drive download failed ({resp.status}) for item {item_id}")
                    raise ValueError(f"Drive download failed ({resp.status})")
                return bytes(await resp.read())

    async def media_source(self, handle: DriveHandle, item_id: str) -> MediaSource:
        """Where the item's bytes can be streamed from: Graph's pre-authenticated download URL (§27.4), read by the
        same GET as `download`. It carries no headers, so the parser sandbox may receive it (E24). The URL is a secret:
        never logged and never put in an error; it is not fetched here, so no bearer token goes to its host."""
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, handle, "GET", f"/me/drive/items/{_q(item_id)}")
            url = raw.get("@microsoft.graph.downloadUrl")
            if not url:
                raise DriveItemNotFoundError(f"No downloadable content for item {item_id}")
            return MediaSource(url=str(url), headers={})

    # -- writes -------------------------------------------------------------

    async def upload(self, handle: DriveHandle, parent_id: str, filename: str, data: bytes, content_type: str,
                     conflict: Literal["rename", "replace", "fail"] = "rename") -> DriveItem:
        async with aiohttp.ClientSession() as s:
            target = f"/me/drive/items/{_q(parent_id)}:/{_q(filename)}:"
            if len(data) <= _SIMPLE_UPLOAD_MAX:
                raw = await self._call(s, handle, "PUT",
                                       f"{target}/content?@microsoft.graph.conflictBehavior={conflict}",
                                       data=data, content_type=content_type)
            else:
                raw = await self._session_upload(s, handle, f"{target}/createUploadSession",
                                                 {"item": {"@microsoft.graph.conflictBehavior": conflict}}, data)
            return await self._item(s, handle, raw)

    async def replace_content(self, handle: DriveHandle, item_id: str, data: bytes, content_type: str) -> DriveItem:
        async with aiohttp.ClientSession() as s:
            base = f"/me/drive/items/{_q(item_id)}"
            if len(data) <= _SIMPLE_UPLOAD_MAX:
                raw = await self._call(s, handle, "PUT", f"{base}/content", data=data, content_type=content_type)
            else:
                raw = await self._session_upload(s, handle, f"{base}/createUploadSession",
                                                 {"item": {"@microsoft.graph.conflictBehavior": "replace"}}, data)
            return await self._item(s, handle, raw)

    async def move(self, handle: DriveHandle, item_id: str, new_parent_id: Optional[str] = None,
                   new_name: Optional[str] = None) -> DriveItem:
        body: Dict[str, Any] = {}
        if new_parent_id:
            body["parentReference"] = {"id": new_parent_id}
        if new_name:
            body["name"] = new_name
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, handle, "PATCH", f"/me/drive/items/{_q(item_id)}", json=body)
            return await self._item(s, handle, raw)

    async def create_folder(self, handle: DriveHandle, parent_id: str, name: str) -> DriveItem:
        async with aiohttp.ClientSession() as s:
            try:
                raw = await self._call(s, handle, "POST", f"/me/drive/items/{_q(parent_id)}/children",
                                       json={"name": name, "folder": {},
                                             "@microsoft.graph.conflictBehavior": "fail"})
            except _BadRequest as exc:
                if exc.code == "invalidRequest":
                    # S1 (Δ216): a folder created near the path limit answered a plain 400 invalidRequest.
                    raise DrivePathTooLong("Drive folder path is too long") from exc
                raise
            return await self._item(s, handle, raw)

    async def child_by_name(self, handle: DriveHandle, parent_id: str, name: str) -> Optional[DriveChange]:
        """The child with exactly that name: one path-relative GET (S1: works inside the App Folder), no search."""
        async with aiohttp.ClientSession() as s:
            try:
                raw = await self._call(s, handle, "GET", f"/me/drive/items/{_q(parent_id)}:/{_q(name)}:")
            except DriveItemNotFoundError:
                return None
            return self._change(handle, raw)

    async def area_path(self, handle: DriveHandle) -> str:
        """Decoded absolute path of the area root from the drive root (e.g. '/Apps/Alek-bot'). Log its length only."""
        async with aiohttp.ClientSession() as s:
            return await self._root_abs(s, handle)

    async def delete(self, handle: DriveHandle, item_id: str) -> None:
        async with aiohttp.ClientSession() as s:
            await self._call(s, handle, "DELETE", f"/me/drive/items/{_q(item_id)}")

    # -- content for the index (USER_DRIVE_INDEXING_RFC §8.1, §24.5) -------------------------

    async def download_to_file(self, handle: DriveHandle, item_id: str, dest_path: str, max_bytes: int) -> int:
        """Stream the content to `dest_path`, chunk by chunk, never whole in memory. The stream is cut as soon
        as it passes `max_bytes` (metadata can understate a size, §5.4); the partial file is the caller's. No
        download URL, or one answering 404/410: DriveItemNotFoundError."""
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, handle, "GET", f"/me/drive/items/{_q(item_id)}")
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

    async def get_thumbnail(self, handle: DriveHandle, item_id: str, max_edge_px: int) -> Optional[bytes]:
        """One provider rendition whose longer edge is at most `max_edge_px`: Graph's custom `c{n}x{n}` (scaled to fit,
        HEIC included; we never decode). The set comes from `/thumbnails?select=<size>`, the bytes from its
        pre-authenticated URL. None: no rendition or its URL gone; DriveThumbnailTooLarge: bigger than the vision
        cap; ValueError: an edge outside 1..9999."""
        if isinstance(max_edge_px, bool) or not isinstance(max_edge_px, int) \
                or not 1 <= max_edge_px <= _THUMBNAIL_MAX_EDGE_PX:
            raise ValueError(f"A thumbnail edge is 1..{_THUMBNAIL_MAX_EDGE_PX} px: {max_edge_px!r}")
        size = f"c{max_edge_px}x{max_edge_px}"
        async with aiohttp.ClientSession() as s:
            page = await self._call(s, handle, "GET", f"/me/drive/items/{_q(item_id)}/thumbnails?select={size}")
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

    async def get_changes(self, handle: DriveHandle, cursor: Optional[str]) -> DriveChanges:
        """One delta page. None starts a full enumeration of the App Folder; every page of an
        enumeration (onboarding or after a 410) reports full_enumeration, a deltaLink round does not."""
        url, full, bound_drive, bound_root = self._decode_cursor(cursor)
        async with aiohttp.ClientSession() as s:
            # Read fresh, never from the 5-minute cache, so the root and the page always come from
            # the same token: a cached root with a new grant's page would give a wrong root_item_id.
            # A reconnect is a new credential, hence a new token cache key. One GET per page.
            self._root_cache.pop(handle.connection_id, None)
            root = await self._root_raw(s, handle)
            drive_id = str((root.get("parentReference") or {}).get("driveId", ""))
            root_id = str(root.get("id", ""))
            if (bound_drive and drive_id and bound_drive != drive_id) or \
                    (bound_root and root_id and bound_root != root_id):
                # Never follow it: Graph may refuse it with something other than 410, which would fail
                # every tick before the per-page drive check runs. The restart page carries
                # the fresh drive id, so the tick sees the switch.
                logger.warning(f"Drive change cursor belongs to another drive or App Folder for connection "
                               f"{handle.connection_id[:8]}; restarting enumeration")
                raise DriveResyncRequired(_encode_cursor(_DELTA_START, True))
            try:
                page = await self._call(s, handle, "GET", url)
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
                logger.warning(f"Drive change cursor not found (404) for connection {handle.connection_id[:8]}; "
                               f"restarting enumeration")
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
            changes=tuple(_to_change(raw, drive_id, self._id_space) for raw in page.get("value", [])),
            cursor=new_cursor,
            complete=complete,
            full_enumeration=full,
            drive_id=drive_id,
            root_item_id=root_id,
            drive_identity=onedrive_drive_identity(drive_id, self._id_space),
        )

    async def get_change(self, handle: DriveHandle, item_id: str) -> DriveChange:
        """One item by id. A 404 raises DriveItemNotFoundError and is never turned into a deletion
        (USER_DRIVE_INDEXING_RFC §26.2, Δ48): Graph answers 404 both for an item that is gone (deleted, or
        moved out of the App Folder, S0a) and for one whose own permission list lacks the app's entry, which
        `approot/delta` still lists. Existence is decided by delta; the caller decides what its 404 means."""
        async with aiohttp.ClientSession() as s:
            raw = await self._call(s, handle, "GET", f"/me/drive/items/{_q(item_id)}")
            return self._change(handle, raw)

    async def list_children_changes(self, handle: DriveHandle, folder_id: str, limit: int,
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
            page = await self._call(s, handle, "GET", url)
            changes = tuple(self._change(handle, raw) for raw in page.get("value", []))
        next_link = page.get("@odata.nextLink")
        return DriveChildrenPage(changes=changes, next_cursor=str(next_link) if next_link else None)

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

    async def _headers(self, handle: DriveHandle, *, force_refresh: bool = False) -> Dict[str, str]:
        """The bearer header of the handle's own credential, never another connection's."""
        try:
            headers = await self._tokens.headers(handle.user_id, handle.credential_id, force_refresh=force_refresh)
        except GraphReauthRequired as exc:
            logger.warning(f"Drive token refresh rejected for connection {handle.connection_id[:8]}; reconnect needed")
            raise DriveReauthRequired("Drive access expired; reconnect needed") from exc
        except (ValueError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # The token endpoint failed (5xx, network, timeout) but did not reject the grant: systemic,
            # so the tick ends and retries later (§8.2), never charged to the item being read.
            logger.warning(f"Drive token refresh failed for connection {handle.connection_id[:8]}: "
                           f"{type(exc).__name__} {exc}")
            raise DriveThrottled(_THROTTLE_HINT_S) from exc
        if headers is None:
            raise DriveNotConnectedError(f"No drive credentials for connection {handle.connection_id[:8]}")
        return headers

    async def _call(self, s: aiohttp.ClientSession, handle: DriveHandle, method: str, path: str, *,
                    json: Any = None, data: Optional[bytes] = None,
                    content_type: Optional[str] = None) -> Dict[str, Any]:
        url = path if path.startswith("http") else f"{_GRAPH}{path}"
        throttled = 0          # 429/503 retries — separate from the 401 refresh,
        refreshed = False      # so throttling never consumes the forced refresh (§4.2)
        while True:
            headers = await self._headers(handle)
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
                        await self._headers(handle, force_refresh=True)  # next attempt uses the fresh token
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

    async def _session_upload(self, s: aiohttp.ClientSession, handle: DriveHandle, path: str,
                              body: Dict[str, Any], data: bytes) -> Dict[str, Any]:
        upload_url = (await self._call(s, handle, "POST", path, json=body))["uploadUrl"]
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

    async def _root_raw(self, s: aiohttp.ClientSession, handle: DriveHandle) -> Dict[str, Any]:
        cached = self._root_cache.get(handle.connection_id)
        if cached and time.monotonic() - cached[0] < _ROOT_TTL_S:
            return cached[1]
        raw = await self._call(s, handle, "GET", _APPROOT)
        self._root_cache[handle.connection_id] = (time.monotonic(), raw)
        return raw

    async def _root_abs(self, s: aiohttp.ClientSession, handle: DriveHandle) -> str:
        """Normalised absolute path of the app folder, taken from special/approot (it is
        localised and the owner may have moved it), e.g. '/Apps/Alek-bot'."""
        raw = await self._root_raw(s, handle)
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

    def _known_drive_id(self, handle: DriveHandle) -> str:
        """The connection's drive id from the cached App Folder item (stale or not: a drive id never changes under a
        grant), "" when the root was not read yet. An item names its own drive, so this is only the fallback."""
        cached = self._root_cache.get(handle.connection_id)
        return _item_drive_id(cached[1]) if cached else ""

    def _change(self, handle: DriveHandle, raw: Dict[str, Any]) -> DriveChange:
        """An observation; an item that does not name its drive takes the connection's."""
        return _to_change(raw, self._known_drive_id(handle), self._id_space)

    async def _item(self, s: aiohttp.ClientSession, handle: DriveHandle, raw: Dict[str, Any]) -> DriveItem:
        root_abs = await self._root_abs(s, handle)
        rel = self._rel_parent(raw, root_abs)
        if rel is None and raw.get("id"):
            if (raw.get("parentReference") or {}).get("path"):
                logger.warning(f"Drive path outside the app folder for item {raw['id']}; re-reading")
            raw = await self._call(s, handle, "GET", f"/me/drive/items/{_q(str(raw['id']))}")
            rel = self._rel_parent(raw, root_abs)
            if rel is None:
                logger.warning(f"Drive path still unresolved for item {raw.get('id')}")
                rel = _UNKNOWN_PARENT
        return self._to_item(raw, rel or "", drive_id=self._known_drive_id(handle))

    def _to_item(self, raw: Dict[str, Any], parent_rel: str, *, is_root: bool = False, drive_id: str = "") -> DriveItem:
        name = str(raw.get("name", ""))
        modified = raw.get("lastModifiedDateTime")
        own_drive = _item_drive_id(raw, drive_id)
        is_folder = "folder" in raw
        return DriveItem(
            item_id=str(raw.get("id", "")),
            name=name,
            path="" if is_root else join_drive_path(parent_rel, name),
            is_folder=is_folder,
            size_bytes=int(raw.get("size") or 0),
            mime_type=str((raw.get("file") or {}).get("mimeType", "")),
            modified_at=datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else None,
            web_url=str(raw.get("webUrl", "")),
            child_count=int((raw.get("folder") or {}).get("childCount") or 0),
            identity=onedrive_item_identity(own_drive, str(raw.get("id", "")), self._id_space),
            drive_identity=onedrive_drive_identity(own_drive, self._id_space),
            content_version="" if is_folder else str(raw.get("cTag") or ""),
            content_identity=content_identity_from_graph(raw),
            media=media_facts_from_graph(raw, drive_id=own_drive, id_space=self._id_space),
        )
