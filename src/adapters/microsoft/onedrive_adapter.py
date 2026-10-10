"""
OneDriveAdapter — UserDrivePort over Microsoft Graph, scoped to the app's App Folder
(docs/10_rfcs/USER_DRIVE_RFC.md §2, §4.2, §4.3).

Scope Files.ReadWrite.AppFolder: Graph itself refuses anything outside the app folder.
Ids are Graph item ids (stable across move/rename). Paths are display only: Graph returns
them percent-encoded, so they are decoded and made relative to the app folder here. The
provider name stays inside this module and the OAuth edge.

`search` implements the port, but step 1 does not expose it to the model: search does not
work inside the App Folder (empty, then 401 from Substrate Search; RFC §7 A6).
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

import aiohttp  # module import on purpose: tests patch aiohttp.ClientSession

from ...domain.user_drive import (
    DriveItem,
    DriveItemNotFoundError,
    DriveNameConflictError,
    DriveNotConnectedError,
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


class OneDriveAdapter(UserDrivePort):
    def __init__(self, oauth_credentials: OAuthCredentialsPort, client_id: str, client_secret: str) -> None:
        self._oauth = oauth_credentials
        self._tokens = MicrosoftGraphTokenProvider(oauth_credentials, client_id, client_secret,
                                                   ONEDRIVE_PROVIDER, _SCOPE)
        # user_id → (fetched_at, root json); the owner may rename/move the folder, hence a TTL.
        self._root_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        logger.info("✅ OneDriveAdapter initialized")

    # -- connection ---------------------------------------------------------

    @property
    def display_name(self) -> str:
        return "OneDrive"

    async def is_connected(self, user_id: str) -> bool:
        return await self._oauth.is_connected(user_id, ONEDRIVE_PROVIDER)

    async def disconnect(self, user_id: str) -> None:
        await self._oauth.revoke_credentials(user_id, ONEDRIVE_PROVIDER)
        self._tokens.invalidate(user_id)
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

    # -- writes -------------------------------------------------------------

    async def upload(self, user_id: str, parent_id: str, filename: str, data: bytes, content_type: str) -> DriveItem:
        async with aiohttp.ClientSession() as s:
            target = f"/me/drive/items/{_q(parent_id)}:/{_q(filename)}:"
            if len(data) <= _SIMPLE_UPLOAD_MAX:
                raw = await self._call(s, user_id, "PUT",
                                       f"{target}/content?@microsoft.graph.conflictBehavior=rename",
                                       data=data, content_type=content_type)
            else:
                raw = await self._session_upload(s, user_id, f"{target}/createUploadSession",
                                                 {"item": {"@microsoft.graph.conflictBehavior": "rename"}}, data)
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
            raw = await self._call(s, user_id, "POST", f"/me/drive/items/{_q(parent_id)}/children",
                                   json={"name": name, "folder": {},
                                         "@microsoft.graph.conflictBehavior": "fail"})
            return await self._item(s, user_id, raw)

    async def delete(self, user_id: str, item_id: str) -> None:
        async with aiohttp.ClientSession() as s:
            await self._call(s, user_id, "DELETE", f"/me/drive/items/{_q(item_id)}")

    # -- transport ----------------------------------------------------------

    async def _headers(self, user_id: str, *, force_refresh: bool = False) -> Dict[str, str]:
        try:
            headers = await self._tokens.headers(user_id, force_refresh=force_refresh)
        except GraphReauthRequired as exc:
            raise DriveNotConnectedError("Drive access expired; reconnect needed") from exc
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
            kwargs: Dict[str, Any] = {"headers": headers}
            if json is not None:
                kwargs["json"] = json
            if data is not None:
                kwargs["data"] = data
            async with getattr(s, method.lower())(url, **kwargs) as resp:
                status = resp.status
                if status == 401:
                    if refreshed:
                        logger.warning(f"Drive {method} {path}: 401 after a forced token refresh")
                        raise DriveNotConnectedError("Drive access rejected after refresh; reconnect needed")
                    refreshed = True
                    # The token provider dedupes forced refreshes itself; no invalidate here.
                    await self._headers(user_id, force_refresh=True)  # next attempt uses the fresh token
                    continue
                if status in (429, 503) and throttled < _RETRIES:
                    delay = _retry_after(resp.headers.get("Retry-After"), throttled)
                    throttled += 1
                    logger.warning(f"Drive {method} throttled ({status}); retrying in {delay:.0f}s")
                    await asyncio.sleep(delay)
                    continue
                if status == 404:
                    raise DriveItemNotFoundError(f"Drive item not found: {path}")
                if status == 409:
                    raise DriveNameConflictError(f"Drive name taken: {path}")
                if status == 204:
                    return {}
                if not resp.ok:
                    body = await resp.text()
                    logger.error(f"Drive {method} {path} failed ({status}): {body[:300]}")
                    raise ValueError(f"Drive {method} failed ({status})")
                return dict(await resp.json())

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
        raw = await self._call(s, user_id, "GET", "/me/drive/special/approot")
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
        )
