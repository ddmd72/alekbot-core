"""A stateful Google Drive v3 at the aiohttp boundary, for the Google drive adapter's contract and wire tests
(GOOGLE_DRIVE_RFC §6 row "Boundary fake"; modelled on `graph_drive_fake.py`).

`GoogleDriveFake.install(monkeypatch)` replaces `aiohttp.ClientSession`. Each bearer token is one account
(`add_account(token, permission_id)`), so a call that carries the wrong grant lands in the wrong account and a test sees
it. File ids are unique across accounts (`fid-<n>`), as Drive's are. It serves what the adapter calls:
- `about.get` (`user.permissionId`), `files.get` (`root` included; a `closed` item answers 404 while lists still show
  it), `files.list` (the four query shapes the adapter sends, `corpora`, `orderBy=folder`, page tokens that
  `expire_list_tokens()` makes refused with 400), `files.create`, `files.update` (rename, `addParents`/`removeParents`,
  `{trashed: true}` with every descendant logged), `files.delete` (recorded in `permanent_deletes`), `alt=media`,
  `files.export` (per export mime; `too_large` answers 403 `exportSizeLimitExceeded`), `revisions.list` (paged;
  `can_read_revisions=False` answers 403), `drives.list`/`drives.get` (member shared drives),
  `changes.getStartPageToken` and `changes.list` (numeric tokens; a change log of file and drive changes, coalesced
  per item within a page; `expired` answers 400 `invalid`), multipart / media / resumable uploads (sessions answer 308 until complete), the
  thumbnail host (`thumbnailLink` with a `=s<n>` suffix; a request carrying `Authorization` is refused) and the token
  endpoint (`refresh_tokens`; scripted failures).
- `fail(route, status, reason)` makes the next call(s) of a route answer an error body
  `{"error": {"code", "message", "errors": [{"reason"}]}}`; `status` may also be "timeout" or "transport".
- `requests` records every call (method, url, params, headers, json, data, route), `upload_metas` every upload's
  metadata as sent; `max_in_flight[route]` the most calls of a route open at once (every request yields to the loop
  before it is answered).

`google_setup(monkeypatch)` builds the adapter over this fake with two accounts (`tok-a`/`perm-a`, `tok-b`/`perm-b`),
their credentials in the real credential store and one handle each, and replaces the backoff sleep with a recorder.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union
from urllib.parse import parse_qsl, urlsplit

import aiohttp

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
TOKEN = "https://oauth2.googleapis.com/token"
THUMB = "https://thumb.example.invalid"
WEB = "https://drive.example.invalid"
FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
SLIDES = "application/vnd.google-apps.presentation"
DRAWING = "application/vnd.google-apps.drawing"
FORM = "application/vnd.google-apps.form"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_T0 = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
_ALL_CAPS = ("canDownload", "canModifyContent", "canRename", "canMoveItemWithinDrive", "canTrash", "canAddChildren")

Failure = Tuple[Union[int, str], str, Dict[str, str]]


def _stamp(n: int) -> str:
    return (_T0 + timedelta(seconds=n)).isoformat().replace("+00:00", ".000Z")


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def error_body(status: int, reason: str, message: str = "a message that quotes Garden plan 2026/27") -> Dict[str, Any]:
    """Drive's error shape; the message deliberately quotes a file name, which must never reach a log."""
    return {"error": {"code": status, "message": message,
                      "errors": [{"reason": reason, "domain": "global", "message": message}] if reason else []}}


@dataclass
class FileNode:
    id: str
    name: str
    mime: str
    parents: List[str]
    created: str
    modified: str
    data: bytes = b""
    trashed: bool = False
    md5: Optional[str] = None
    head: Optional[str] = None
    revisions: List[str] = field(default_factory=list)
    can_read_revisions: bool = True
    caps: Dict[str, bool] = field(default_factory=dict)
    image: Optional[Dict[str, Any]] = None
    video: Optional[Dict[str, Any]] = None
    thumbnail: Optional[bytes] = None
    thumbnail_link: Optional[str] = None               # overrides the fake host's https link
    target: Optional[str] = None
    drive_id: Optional[str] = None
    exports: Dict[str, bytes] = field(default_factory=dict)
    too_large: Set[str] = field(default_factory=set)
    size: Optional[int] = None

    @property
    def is_folder(self) -> bool:
        return self.mime == FOLDER


class GoogleAccount:
    """One Google account: My Drive, items shared to it, member shared drives and its change log."""

    def __init__(self, fake: "GoogleDriveFake", permission_id: str) -> None:
        self.fake = fake
        self.permission_id = permission_id
        self.root = self._node(f"root-{permission_id}", "My Drive", FOLDER, [])
        self.files: Dict[str, FileNode] = {self.root.id: self.root}
        self.closed: Set[str] = set()                      # id reads answer 404, lists still show it
        self.drives: Dict[str, str] = {}                   # member shared drives: id -> name
        self.log: List[Tuple[str, str, bool]] = []         # (kind "file"|"drive", id, removed)
        self.expired = False                               # changes.list answers 400 invalid
        self.list_epoch = 0                                # bumped by expire_list_tokens

    # -- planting -------------------------------------------------------------------------------------------------

    def _node(self, file_id: str, name: str, mime: str, parents: List[str], **kw: Any) -> FileNode:
        n = next(self.fake.clock)
        return FileNode(id=file_id, name=name, mime=mime, parents=parents, created=_stamp(n), modified=_stamp(n), **kw)

    def _add(self, node: FileNode) -> FileNode:
        self.files[node.id] = node
        self.log.append(("file", node.id, False))
        return node

    def add_file(self, parent: Optional[str], name: str, data: bytes = b"", *, mime: str = "application/octet-stream",
                 hashed: bool = True, drive_id: Optional[str] = None, **kw: Any) -> FileNode:
        node = self._node(self.fake.new_id(), name, mime, [parent] if parent else [], data=data, drive_id=drive_id,
                          md5=_md5(data) if hashed else None, head=self.fake.new_rev(), **kw)
        return self._add(node)

    def add_folder(self, parent: Optional[str], name: str, *, drive_id: Optional[str] = None,
                   **kw: Any) -> FileNode:
        return self._add(self._node(self.fake.new_id(), name, FOLDER, [parent] if parent else [], drive_id=drive_id,
                                    **kw))

    def add_native(self, parent: Optional[str], name: str, mime: str = DOC, *,
                   exports: Optional[Dict[str, bytes]] = None, revisions: Sequence[str] = ("rev-n1",),
                   can_read_revisions: bool = True, size: int = 0, **kw: Any) -> FileNode:
        return self._add(self._node(self.fake.new_id(), name, mime, [parent] if parent else [],
                                    exports=dict(exports or {}), revisions=list(revisions),
                                    can_read_revisions=can_read_revisions, size=size, **kw))

    def add_shortcut(self, parent: Optional[str], name: str, target: str) -> FileNode:
        return self._add(self._node(self.fake.new_id(), name, SHORTCUT, [parent] if parent else [], target=target))

    def add_shared_drive(self, name: str) -> str:
        drive_id = f"drive-{next(self.fake.ids)}"
        self.drives[drive_id] = name
        return drive_id

    def rename_drive(self, drive_id: str, name: str) -> None:
        self.drives[drive_id] = name
        self.log.append(("drive", drive_id, False))

    def leave_drive(self, drive_id: str) -> None:
        self.drives.pop(drive_id, None)
        self.log.append(("drive", drive_id, True))

    def touch(self, node: FileNode) -> None:
        node.modified = _stamp(next(self.fake.clock))
        self.log.append(("file", node.id, False))

    def set_content(self, node: FileNode, data: bytes, mime: Optional[str] = None) -> None:
        node.data, node.md5, node.head = data, _md5(data), self.fake.new_rev()
        if mime:
            node.mime = mime
        self.touch(node)

    def trash(self, node: FileNode) -> None:
        node.trashed = True
        self.touch(node)
        for child in [n for n in self.files.values() if node.id in n.parents]:
            self.trash(child)

    def lose(self, node: FileNode) -> None:
        """Access lost: the item vanishes and the feed reports it removed."""
        self.files.pop(node.id, None)
        self.log.append(("file", node.id, True))

    def expire_list_tokens(self) -> None:
        self.list_epoch += 1

    def children(self, parent: str) -> List[FileNode]:
        return [n for n in self.files.values() if parent in n.parents and not n.trashed]

    # -- rendering ------------------------------------------------------------------------------------------------

    def raw(self, node: FileNode) -> Dict[str, Any]:
        caps = {name: True for name in _ALL_CAPS}
        caps["canReadRevisions"] = node.can_read_revisions
        caps.update(node.caps)
        out: Dict[str, Any] = {"kind": "drive#file", "id": node.id, "name": node.name, "mimeType": node.mime,
                               "createdTime": node.created, "modifiedTime": node.modified, "trashed": node.trashed,
                               "webViewLink": f"{WEB}/{node.id}", "capabilities": caps}
        if node.parents:
            out["parents"] = list(node.parents)
        if node.mime not in (FOLDER, SHORTCUT):
            out["size"] = str(node.size if node.size is not None else len(node.data))
        if not node.mime.startswith("application/vnd.google-apps."):
            if node.md5:
                out["md5Checksum"] = node.md5
            if node.head:
                out["headRevisionId"] = node.head
        if node.image is not None:
            out["imageMediaMetadata"] = dict(node.image)
        if node.video is not None:
            out["videoMediaMetadata"] = dict(node.video)
        if node.thumbnail_link is not None:
            out["thumbnailLink"] = node.thumbnail_link
        elif node.thumbnail is not None:
            out["thumbnailLink"] = f"{THUMB}/{node.id}=s220"
        if node.target is not None:
            out["shortcutDetails"] = {"targetId": node.target, "targetMimeType": "application/pdf"}
        if node.drive_id:
            out["driveId"] = node.drive_id
        return out


class _Content:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def iter_chunked(self, n: int):
        for i in range(0, len(self._data), n):
            yield self._data[i:i + n]


class _Response:
    def __init__(self, status: int, body: Any = None, headers: Optional[Dict[str, str]] = None) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = body
        self.ok = status < 400

    async def json(self, *a: Any, **k: Any) -> Any:
        return json.loads(self._body) if isinstance(self._body, bytes) else self._body

    async def text(self) -> str:
        return self._body.decode() if isinstance(self._body, bytes) else json.dumps(self._body)

    async def read(self) -> bytes:
        return self._body if isinstance(self._body, bytes) else (await self.text()).encode()

    @property
    def content(self) -> _Content:
        return _Content(self._body if isinstance(self._body, bytes) else b"")


@dataclass(frozen=True)
class Seen:
    method: str
    url: str
    route: str
    params: Dict[str, str]
    headers: Dict[str, str]
    json: Any
    data: Any

    @property
    def bearer(self) -> str:
        return str(self.headers.get("Authorization", "")).removeprefix("Bearer ")


class _Request:
    def __init__(self, fake: "GoogleDriveFake", method: str, url: str, kw: Dict[str, Any]) -> None:
        self._fake, self._method, self._url, self._kw = fake, method, url, kw
        self._route = ""

    async def __aenter__(self) -> _Response:
        params = {**dict(parse_qsl(urlsplit(self._url).query)), **{k: str(v) for k, v in
                                                                    (self._kw.get("params") or {}).items()}}
        self._route = self._fake.route_of(self._method, self._url, params)
        fake = self._fake
        fake.in_flight[self._route] = fake.in_flight.get(self._route, 0) + 1
        fake.max_in_flight[self._route] = max(fake.max_in_flight.get(self._route, 0), fake.in_flight[self._route])
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return fake.serve(self._method, self._url, self._route, params, self._kw)

    async def __aexit__(self, *a: Any) -> bool:
        self._fake.in_flight[self._route] -= 1
        return False


class _Session:
    def __init__(self, fake: "GoogleDriveFake") -> None:
        self._fake = fake

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    def get(self, url: str, **kw: Any) -> _Request:
        return _Request(self._fake, "GET", url, kw)

    def post(self, url: str, **kw: Any) -> _Request:
        return _Request(self._fake, "POST", url, kw)

    def put(self, url: str, **kw: Any) -> _Request:
        return _Request(self._fake, "PUT", url, kw)

    def patch(self, url: str, **kw: Any) -> _Request:
        return _Request(self._fake, "PATCH", url, kw)

    def delete(self, url: str, **kw: Any) -> _Request:
        return _Request(self._fake, "DELETE", url, kw)


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


_QUOTED = r"'((?:[^'\\]|\\.)*)'"


class GoogleDriveFake:
    def __init__(self) -> None:
        self.ids = itertools.count(1)
        self.revs = itertools.count(1)
        self.clock = itertools.count(1)
        self.accounts: Dict[str, GoogleAccount] = {}           # bearer token -> account
        self.refresh_tokens: Dict[str, GoogleAccount] = {}     # refresh token -> account
        self.requests: List[Seen] = []
        self.unrouted: List[str] = []
        self.failures: Dict[str, List[Failure]] = {}
        self.permanent_deletes: List[str] = []
        self.upload_metas: List[Dict[str, Any]] = []           # the metadata of every upload, as sent
        self.in_flight: Dict[str, int] = {}
        self.max_in_flight: Dict[str, int] = {}
        self.token_lifetime_s = 3599
        self._issued = itertools.count(1)
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def new_id(self) -> str:
        return f"fid-{next(self.ids)}"

    def new_rev(self) -> str:
        return f"rev-{next(self.revs)}"

    def add_account(self, token: str, permission_id: str, refresh_token: str = "") -> GoogleAccount:
        account = GoogleAccount(self, permission_id)
        self.accounts[token] = account
        if refresh_token:
            self.refresh_tokens[refresh_token] = account
        return account

    def install(self, monkeypatch: Any) -> "GoogleDriveFake":
        monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **k: _Session(self))
        return self

    def fail(self, route: str, status: Union[int, str], reason: str = "", *, times: int = 1,
             headers: Optional[Dict[str, str]] = None) -> None:
        """The next `times` calls of `route` answer `status` with `reason` ("timeout"/"transport": raise)."""
        self.failures.setdefault(route, []).extend([(status, reason, dict(headers or {}))] * times)

    def calls(self, route: str) -> List[Seen]:
        return [r for r in self.requests if r.route == route]

    # -- routing --------------------------------------------------------------------------------------------------

    def route_of(self, method: str, url: str, params: Dict[str, str]) -> str:
        base = url.split("?", 1)[0]
        if base == TOKEN:
            return "token"
        if url.startswith(THUMB + "/"):
            return "thumbnail"
        if base.startswith(UPLOAD):
            if method == "PUT":
                return "upload.chunk"
            return f"upload.{params.get('uploadType', '')}"
        if not base.startswith(API):
            return "unrouted"
        path = base[len(API):]
        if path == "/about":
            return "about.get"
        if path == "/changes/startPageToken":
            return "changes.getStartPageToken"
        if path == "/changes":
            return "changes.list"
        if path == "/drives":
            return "drives.list"
        if path.startswith("/drives/"):
            return "drives.get"
        if path == "/files":
            return "files.list" if method == "GET" else "files.create"
        if path.endswith("/export"):
            return "export"
        if path.endswith("/revisions"):
            return "revisions.list"
        if method == "GET":
            return "media" if params.get("alt") == "media" else "files.get"
        return {"PATCH": "files.update", "DELETE": "files.delete"}.get(method, "unrouted")

    def serve(self, method: str, url: str, route: str, params: Dict[str, str], kw: Dict[str, Any]) -> _Response:
        headers = {str(k): str(v) for k, v in (kw.get("headers") or {}).items()}
        self.requests.append(Seen(method, url, route, params, headers, kw.get("json"), kw.get("data")))
        queued = self.failures.get(route)
        if queued:
            status, reason, extra = queued.pop(0)
            if status == "timeout":
                raise asyncio.TimeoutError()
            if status == "transport":
                raise aiohttp.ClientConnectionError("fake transport failure")
            return _Response(int(status), error_body(int(status), reason), extra)
        if route == "token":
            return self._token(kw.get("data") or {})
        if route == "thumbnail":
            return self._thumbnail(url, headers)
        if route == "upload.chunk":
            return self._chunk(params, headers, bytes(kw.get("data") or b""))
        if route == "unrouted":
            self.unrouted.append(f"{method} {url}")
            raise aiohttp.ClientConnectionError(f"no route in the fake: {method} {url}")
        account = self.accounts.get(headers.get("Authorization", "").removeprefix("Bearer "))
        if account is None:
            return _Response(401, error_body(401, "authError"))
        base = url.split("?", 1)[0]
        path = base[len(UPLOAD):] if base.startswith(UPLOAD) else base[len(API):]
        return getattr(self, "_" + route.replace(".", "_"))(account, method, path, params, kw, headers)

    # -- token and thumbnails -------------------------------------------------------------------------------------

    def _token(self, data: Dict[str, Any]) -> _Response:
        account = self.refresh_tokens.get(str(data.get("refresh_token")))
        if account is None or data.get("grant_type") != "refresh_token":
            return _Response(400, {"error": "invalid_grant", "error_description": "Token has been revoked."})
        token = f"tok-{account.permission_id}-{next(self._issued)}"
        self.accounts[token] = account
        return _Response(200, {"access_token": token, "expires_in": self.token_lifetime_s, "token_type": "Bearer",
                               "scope": "https://www.googleapis.com/auth/drive"})

    def _thumbnail(self, url: str, headers: Dict[str, str]) -> _Response:
        if "Authorization" in headers:
            return _Response(403, b"a bearer was sent to the thumbnail host")
        m = re.fullmatch(re.escape(THUMB) + r"/([^=]+)=s(\d+)", url)
        if not m:
            return _Response(404, b"")
        node = next((a.files.get(m.group(1)) for a in self.accounts.values() if m.group(1) in a.files), None)
        if node is None or node.thumbnail is None:
            return _Response(404, b"")
        return _Response(200, node.thumbnail)

    # -- metadata -------------------------------------------------------------------------------------------------

    def _visible(self, account: GoogleAccount, file_id: str) -> Optional[FileNode]:
        node = account.root if file_id == "root" else account.files.get(file_id)
        return None if node is None or file_id in account.closed else node

    @staticmethod
    def _not_found() -> _Response:
        return _Response(404, error_body(404, "notFound"))

    def _about_get(self, account: GoogleAccount, *a: Any) -> _Response:
        return _Response(200, {"user": {"permissionId": account.permission_id}})

    def _files_get(self, account: GoogleAccount, method: str, path: str, params: Dict[str, str], *a: Any) -> _Response:
        node = self._visible(account, path.removeprefix("/files/"))
        return _Response(200, account.raw(node)) if node else self._not_found()

    def _files_list(self, account: GoogleAccount, method: str, path: str, params: Dict[str, str],
                    *a: Any) -> _Response:
        q = params.get("q", "")
        nodes = [n for n in account.files.values() if n is not account.root and not n.trashed]
        m_parent = re.fullmatch(_QUOTED + r" in parents and trashed=false", q)
        m_mime = re.fullmatch(r"mimeType=" + _QUOTED + r" and trashed=false", q)
        m_search = re.fullmatch(r"\(name contains " + _QUOTED + r" or fullText contains " + _QUOTED
                                + r"\) and trashed=false", q)
        if m_parent:
            parent = _unescape(m_parent.group(1))
            nodes = [n for n in nodes if parent in n.parents]
        elif m_mime:
            nodes = [n for n in nodes if n.mime == _unescape(m_mime.group(1))]
        elif m_search:
            term = _unescape(m_search.group(1)).lower()
            nodes = [n for n in nodes if term in n.name.lower() or term.encode() in n.data.lower()]
        elif q != "trashed=false":
            self.unrouted.append(f"files.list q={q}")
            raise aiohttp.ClientConnectionError(f"the fake does not know the query {q!r}")
        corpora = params.get("corpora")
        if corpora == "drive":
            nodes = [n for n in nodes if n.drive_id == params.get("driveId")]
        elif corpora == "user":
            nodes = [n for n in nodes if not n.drive_id or n.drive_id not in account.drives]
        nodes.sort(key=lambda n: (n.created, n.id))
        if params.get("orderBy") == "folder":
            nodes.sort(key=lambda n: not n.is_folder)
        offset = 0
        token = params.get("pageToken")
        if token:
            m = re.fullmatch(r"lt\.(\d+)\.(\d+)", token)
            if not m or int(m.group(1)) != account.list_epoch:
                return _Response(400, error_body(400, "invalid", "Invalid Value"))
            offset = int(m.group(2))
        size = int(params.get("pageSize") or 100)
        body: Dict[str, Any] = {"files": [account.raw(n) for n in nodes[offset:offset + size]]}
        if offset + size < len(nodes):
            body["nextPageToken"] = f"lt.{account.list_epoch}.{offset + size}"
        return _Response(200, body)

    def _files_create(self, account: GoogleAccount, method: str, path: str, params: Dict[str, str],
                      kw: Dict[str, Any], *a: Any) -> _Response:
        meta = dict(kw.get("json") or {})
        parents = list(meta.get("parents") or [])
        if parents and self._visible(account, parents[0]) is None:
            return self._not_found()
        parent_drive = account.files[parents[0]].drive_id if parents else None
        if meta.get("mimeType") == FOLDER:
            node = account.add_folder(parents[0] if parents else None, str(meta["name"]), drive_id=parent_drive)
        else:
            node = account.add_file(parents[0] if parents else None, str(meta["name"]), drive_id=parent_drive)
        return _Response(200, account.raw(node))

    def _files_update(self, account: GoogleAccount, method: str, path: str, params: Dict[str, str],
                      kw: Dict[str, Any], *a: Any) -> _Response:
        node = self._visible(account, path.removeprefix("/files/"))
        if node is None:
            return self._not_found()
        body = dict(kw.get("json") or {})
        if body.get("trashed") is True:
            account.trash(node)
            return _Response(200, account.raw(node))
        if params.get("addParents"):
            removed = set((params.get("removeParents") or "").split(","))
            node.parents = [p for p in node.parents if p not in removed] + [params["addParents"]]
        if body.get("name"):
            node.name = str(body["name"])
        account.touch(node)
        return _Response(200, account.raw(node))

    def _files_delete(self, account: GoogleAccount, method: str, path: str, *a: Any) -> _Response:
        file_id = path.removeprefix("/files/")
        self.permanent_deletes.append(file_id)
        account.files.pop(file_id, None)
        return _Response(204, b"")

    # -- content --------------------------------------------------------------------------------------------------

    def _media(self, account: GoogleAccount, method: str, path: str, *a: Any) -> _Response:
        node = self._visible(account, path.removeprefix("/files/"))
        if node is None:
            return self._not_found()
        if node.mime.startswith("application/vnd.google-apps."):
            return _Response(403, error_body(403, "fileNotDownloadable"))
        return _Response(200, node.data)

    def _export(self, account: GoogleAccount, method: str, path: str, params: Dict[str, str], *a: Any) -> _Response:
        node = self._visible(account, path.removeprefix("/files/").removesuffix("/export"))
        if node is None:
            return self._not_found()
        mime = params.get("mimeType", "")
        if mime in node.too_large:
            return _Response(403, error_body(403, "exportSizeLimitExceeded"))
        if mime not in node.exports:
            return _Response(400, error_body(400, "badRequest"))
        return _Response(200, node.exports[mime])

    def _revisions_list(self, account: GoogleAccount, method: str, path: str, params: Dict[str, str],
                        *a: Any) -> _Response:
        node = self._visible(account, path.removeprefix("/files/").removesuffix("/revisions"))
        if node is None:
            return self._not_found()
        if not node.can_read_revisions:
            return _Response(403, error_body(403, "insufficientFilePermissions"))
        offset = int((params.get("pageToken") or "rv.0").split(".")[1])
        size = int(params.get("pageSize") or 200)
        body: Dict[str, Any] = {"revisions": [{"id": r} for r in node.revisions[offset:offset + size]]}
        if offset + size < len(node.revisions):
            body["nextPageToken"] = f"rv.{offset + size}"
        return _Response(200, body)

    # -- shared drives and the change log -------------------------------------------------------------------------

    def _drives_list(self, account: GoogleAccount, method: str, path: str, params: Dict[str, str],
                     *a: Any) -> _Response:
        rows = [{"id": i, "name": n} for i, n in account.drives.items()]
        offset = int(params.get("pageToken") or 0)
        size = int(params.get("pageSize") or 10)
        body: Dict[str, Any] = {"drives": rows[offset:offset + size]}
        if offset + size < len(rows):
            body["nextPageToken"] = str(offset + size)
        return _Response(200, body)

    def _drives_get(self, account: GoogleAccount, method: str, path: str, *a: Any) -> _Response:
        drive_id = path.removeprefix("/drives/")
        name = account.drives.get(drive_id)
        return _Response(200, {"id": drive_id, "name": name}) if name is not None else self._not_found()

    def _changes_getStartPageToken(self, account: GoogleAccount, *a: Any) -> _Response:
        return _Response(200, {"startPageToken": str(len(account.log) + 1)})

    def _changes_list(self, account: GoogleAccount, method: str, path: str, params: Dict[str, str],
                      *a: Any) -> _Response:
        token = params.get("pageToken", "")
        if account.expired or not token.isdigit():
            return _Response(400, error_body(400, "invalid", "Invalid Value"))
        start = int(token)
        size = int(params.get("pageSize") or 100)
        entries = account.log[start - 1:start - 1 + size]
        latest: Dict[Tuple[str, str], Tuple[int, bool]] = {}
        for pos, (kind, item_id, removed) in enumerate(entries):
            latest.pop((kind, item_id), None)
            latest[(kind, item_id)] = (pos, removed)
        rows: List[Dict[str, Any]] = []
        for (kind, item_id), (_, removed) in latest.items():
            if kind == "drive":
                row: Dict[str, Any] = {"kind": "drive#change", "changeType": "drive", "driveId": item_id,
                                       "removed": removed, "time": _stamp(0)}
                if not removed:
                    row["drive"] = {"id": item_id, "name": account.drives.get(item_id, "")}
                rows.append(row)
                continue
            node = account.files.get(item_id)
            row = {"kind": "drive#change", "changeType": "file", "fileId": item_id, "time": _stamp(0),
                   "removed": removed or node is None}
            if node is not None and not removed:
                row["file"] = account.raw(node)
            rows.append(row)
        body: Dict[str, Any] = {"changes": rows}
        if start - 1 + size < len(account.log):
            body["nextPageToken"] = str(start + size)
        else:
            body["newStartPageToken"] = str(len(account.log) + 1)
        return _Response(200, body)

    # -- uploads --------------------------------------------------------------------------------------------------

    def _upload_multipart(self, account: GoogleAccount, method: str, path: str, params: Dict[str, str],
                          kw: Dict[str, Any], headers: Dict[str, str]) -> _Response:
        boundary = headers.get("Content-Type", "").split("boundary=", 1)[1]
        parts = bytes(kw.get("data") or b"").split(f"--{boundary}".encode())
        meta_part, media_part = parts[1], parts[2]
        meta = json.loads(meta_part.split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n"))
        media_head, media = media_part.split(b"\r\n\r\n", 1)
        media = media[:-2] if media.endswith(b"\r\n") else media
        mime = media_head.decode().split("Content-Type: ", 1)[1].strip()
        return self._create_upload(account, meta, media, mime)

    def _create_upload(self, account: GoogleAccount, meta: Dict[str, Any], data: bytes, mime: str) -> _Response:
        self.upload_metas.append(dict(meta))
        parents = list(meta.get("parents") or [])
        if parents and self._visible(account, parents[0]) is None:
            return self._not_found()
        parent = parents[0] if parents else account.root.id
        node = account.add_file(parent, str(meta.get("name") or "Untitled"), data, mime=mime,
                                drive_id=account.files[parent].drive_id)
        return _Response(200, account.raw(node))

    def _upload_media(self, account: GoogleAccount, method: str, path: str, params: Dict[str, str],
                      kw: Dict[str, Any], headers: Dict[str, str]) -> _Response:
        node = self._visible(account, path.removeprefix("/files/"))
        if node is None:
            return self._not_found()
        account.set_content(node, bytes(kw.get("data") or b""), headers.get("Content-Type"))
        return _Response(200, account.raw(node))

    def _upload_resumable(self, account: GoogleAccount, method: str, path: str, params: Dict[str, str],
                          kw: Dict[str, Any], headers: Dict[str, str]) -> _Response:
        upload_id = f"up-{len(self._sessions) + 1}"
        target = path.removeprefix("/files/") if method == "PATCH" else None
        self._sessions[upload_id] = {"account": account, "target": target, "meta": dict(kw.get("json") or {}),
                                     "total": int(headers.get("X-Upload-Content-Length", "0")),
                                     "mime": headers.get("X-Upload-Content-Type", ""), "buf": bytearray()}
        return _Response(200, {}, {"Location": f"{UPLOAD}/files?uploadType=resumable&upload_id={upload_id}"})

    def _chunk(self, params: Dict[str, str], headers: Dict[str, str], data: bytes) -> _Response:
        session = self._sessions[params["upload_id"]]
        session["buf"].extend(data)
        if len(session["buf"]) < session["total"]:
            return _Response(308, b"", {"Range": f"bytes=0-{len(session['buf']) - 1}"})
        account: GoogleAccount = session["account"]
        if session["target"] is None:
            return self._create_upload(account, session["meta"], bytes(session["buf"]), session["mime"])
        node = account.files[session["target"]]
        account.set_content(node, bytes(session["buf"]), session["mime"])
        return _Response(200, account.raw(node))


# -- the adapter over the fake --------------------------------------------------------------------------------------


@dataclass
class GoogleSetup:
    fake: GoogleDriveFake
    adapter: Any
    store: Any
    handles: List[Any]
    accounts: List[GoogleAccount]
    sleeps: List[float]

    @property
    def handle(self) -> Any:
        return self.handles[0]

    @property
    def other(self) -> Any:
        return self.handles[1]

    @property
    def a(self) -> GoogleAccount:
        return self.accounts[0]

    @property
    def b(self) -> GoogleAccount:
        return self.accounts[1]


async def google_setup(monkeypatch: Any, *, token_expiry: Optional[datetime] = None) -> GoogleSetup:
    """The adapter over a fresh fake: accounts `perm-a` (token `tok-a`, refresh `rt-a`) and `perm-b` (`tok-b`, `rt-b`)
    of Alex Doe (`usr-1`), connections `conn-1` / `conn-2`; tokens live an hour unless `token_expiry` says otherwise."""
    from src.adapters.google import google_drive_adapter
    from src.adapters.google.google_drive_adapter import GoogleDriveAdapter
    from src.composition.connector_registry import ConnectorRegistry
    from src.domain.provider_account import ProviderAccount
    from tests.support.drive_handles import drive_handle
    from tests.support.oauth_credentials_store import CredentialsStore, creds

    fake = GoogleDriveFake().install(monkeypatch)
    sleeps: List[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(google_drive_adapter, "_sleep", record_sleep)
    store = CredentialsStore()
    expiry = token_expiry or datetime.now(timezone.utc) + timedelta(hours=1)
    handles, accounts = [], []
    for n, tag in enumerate(("a", "b"), start=1):
        accounts.append(fake.add_account(f"tok-{tag}", f"perm-{tag}", refresh_token=f"rt-{tag}"))
        credential_id = await store.repo.save(creds(
            "usr-1", "google_drive", access_token=f"tok-{tag}", refresh_token=f"rt-{tag}", token_expiry=expiry,
            account=ProviderAccount(realm="google", subject=f"sub-{tag}", label=f"alex{n}@example.com")))
        handles.append(drive_handle("usr-1", f"conn-{n}", credential_id, kind="google_drive"))
    spec = ConnectorRegistry().get("google_drive")
    adapter = GoogleDriveAdapter(store.repo, "cid", "csecret", connector=spec)
    return GoogleSetup(fake, adapter, store, handles, accounts, sleeps)
