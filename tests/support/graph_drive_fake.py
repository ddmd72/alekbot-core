"""A stateful Microsoft Graph drive at the aiohttp boundary, for the OneDrive adapter's contract suite (MULTI_DRIVE_RFC T-3).

`GraphDriveFake.install(monkeypatch)` replaces `aiohttp.ClientSession`. Each bearer token is one account's drive
(`add_drive(token, drive_id)`), so a call that carries the wrong grant lands in the wrong drive and a test sees it.
It serves what the adapter calls: the App Folder (`approot`), items by id, children pages, path-addressed child reads
and uploads with the three conflict modes, folder create, PATCH move/rename, DELETE into a recycle bin, the delta feed
(a full enumeration, then delta rounds; an expired link answers 410), thumbnails, the pre-authenticated download URL
(no bearer), the 400 `pathIsTooLong` of a path past `MAX_PATH`, and a 404 for an item closed by `drive.closed` that
the delta feed still lists.

`GraphDrive.add(..., extra=...)` plants an item with any raw Graph facets: a test writes the provider's shape and
reads the typed one.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

import aiohttp

GRAPH = "https://graph.microsoft.com/v1.0"
DL = "https://dl.example.invalid"
THUMB = "https://thumb.example.invalid"
UP = "https://up.example.invalid/"
MAX_PATH = 400
AREA_PARENT = "/Apps"            # the App Folder's parent, from the drive root
AREA_NAME = "Alek-bot"
_APPROOT = "/me/drive/special/approot"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class _Node:
    item_id: str
    name: str
    parent_id: Optional[str]
    is_folder: bool
    data: bytes = b""
    seq: int = 0
    created: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)       # raw facets planted by a test
    hashes: Optional[Dict[str, str]] = None
    mime: str = ""
    thumbnail: Optional[bytes] = None


def _hash(data: bytes) -> Dict[str, str]:
    return {"quickXorHash": hashlib.sha1(data).hexdigest()[:27]}


class GraphDrive:
    """One account's drive."""

    def __init__(self, drive_id: str) -> None:
        self.drive_id = drive_id
        self._ids = itertools.count(1)
        self._seq = itertools.count(1)
        self._clock = itertools.count(1)
        self.root = _Node(f"{drive_id}-root", AREA_NAME, None, True, created=_now())
        self.nodes: Dict[str, _Node] = {self.root.item_id: self.root}
        self.recycle: Dict[str, _Node] = {}
        self.closed: set = set()
        self.log: List[Tuple[int, str, bool]] = []        # (position, item_id, deleted)
        self.expired = False

    # -- tree -----------------------------------------------------------------------------------------------

    def children(self, parent_id: str) -> List[_Node]:
        return sorted((n for n in self.nodes.values() if n.parent_id == parent_id), key=lambda n: n.created)

    def by_name(self, parent_id: str, name: str) -> Optional[_Node]:
        return next((n for n in self.children(parent_id) if n.name.lower() == name.lower()), None)

    def path_of(self, node: _Node) -> str:
        """Absolute path of the node from the drive root."""
        names: List[str] = []
        cur: Optional[_Node] = node
        while cur is not None and cur.parent_id is not None:
            names.append(cur.name)
            cur = self.nodes.get(cur.parent_id)
        return f"{AREA_PARENT}/{AREA_NAME}" + "".join(f"/{n}" for n in reversed(names))

    def touch(self, node: _Node, deleted: bool = False) -> None:
        node.seq = next(self._seq)
        self.log.append((len(self.log) + 1, node.item_id, deleted))

    def add(self, parent_id: str, name: str, *, folder: bool = False, data: bytes = b"", mime: str = "",
            hashes: Optional[Dict[str, str]] = None, extra: Optional[Dict[str, Any]] = None,
            thumbnail: Optional[bytes] = None, hashed: bool = True) -> _Node:
        node = _Node(f"item-{next(self._ids)}", name, parent_id, folder, data,        # the same ids in every drive (T-4)
                     created=f"{next(self._clock):012d}", extra=dict(extra or {}),
                     hashes=hashes if hashes is not None else (_hash(data) if hashed and not folder else None),
                     mime=mime, thumbnail=thumbnail)
        self.nodes[node.item_id] = node
        self.touch(node)
        return node

    def delete(self, node: _Node) -> None:
        for child in list(self.children(node.item_id)):
            self.delete(child)
        self.nodes.pop(node.item_id, None)
        self.recycle[node.item_id] = node
        self.touch(node, deleted=True)

    def raw(self, node: _Node) -> Dict[str, Any]:
        parent = self.nodes.get(node.parent_id) if node.parent_id else None
        stamp = "2026-01-15T09:00:00Z"
        out: Dict[str, Any] = {
            "id": node.item_id, "name": node.name, "size": len(node.data),
            "webUrl": f"https://web.example.invalid/{node.item_id}",
            "cTag": f"c:{node.item_id}:{node.seq}", "createdDateTime": stamp, "lastModifiedDateTime": stamp,
            "fileSystemInfo": {"lastModifiedDateTime": stamp}, **node.extra}
        if node.is_folder:
            out["folder"] = {"childCount": len(self.children(node.item_id))}
        else:
            out["file"] = {"mimeType": node.mime or "application/octet-stream",
                           **({"hashes": dict(node.hashes)} if node.hashes else {})}
            out["@microsoft.graph.downloadUrl"] = f"{DL}/{self.drive_id}/{node.item_id}"
        if node is self.root:
            out["parentReference"] = {"driveId": self.drive_id, "path": f"/drive/root:{AREA_PARENT}"}
        else:
            out["parentReference"] = {"driveId": self.drive_id, "id": parent.item_id if parent else "",
                                      "path": "/drive/root:" + self.path_of(parent) if parent else ""}
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
        return self._body

    async def text(self) -> str:
        return self._body.decode() if isinstance(self._body, bytes) else json.dumps(self._body)

    async def read(self) -> bytes:
        return self._body if isinstance(self._body, bytes) else (await self.text()).encode()

    @property
    def content(self) -> _Content:
        return _Content(self._body if isinstance(self._body, bytes) else b"")

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False


class _Request:
    def __init__(self, fake: "GraphDriveFake", method: str, url: str, kw: Dict[str, Any]) -> None:
        self._fake, self._method, self._url, self._kw = fake, method, url, kw

    async def __aenter__(self) -> _Response:
        return self._fake.serve(self._method, self._url, self._kw)

    async def __aexit__(self, *a: Any) -> bool:
        return False


class _Session:
    def __init__(self, fake: "GraphDriveFake") -> None:
        self._fake = fake

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    def get(self, url: str, **kw: Any) -> _Request:
        return _Request(self._fake, "GET", url, kw)

    def put(self, url: str, **kw: Any) -> _Request:
        return _Request(self._fake, "PUT", url, kw)

    def post(self, url: str, **kw: Any) -> _Request:
        return _Request(self._fake, "POST", url, kw)

    def patch(self, url: str, **kw: Any) -> _Request:
        return _Request(self._fake, "PATCH", url, kw)

    def delete(self, url: str, **kw: Any) -> _Request:
        return _Request(self._fake, "DELETE", url, kw)


@dataclass(frozen=True)
class Seen:
    method: str
    url: str
    bearer: str


class GraphDriveFake:
    def __init__(self) -> None:
        self.drives: Dict[str, GraphDrive] = {}           # bearer token -> drive
        self.requests: List[Seen] = []
        self.unrouted: List[str] = []
        self._uploads: Dict[str, Dict[str, Any]] = {}

    def add_drive(self, token: str, drive_id: str) -> GraphDrive:
        self.drives[token] = GraphDrive(drive_id)
        return self.drives[token]

    def install(self, monkeypatch: Any) -> "GraphDriveFake":
        monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **k: _Session(self))
        return self

    def drive_of(self, drive_id: str) -> GraphDrive:
        return next(d for d in self.drives.values() if d.drive_id == drive_id)

    def bearers(self) -> List[str]:
        return [r.bearer for r in self.requests]

    def _unrouted(self, method: str, url: str) -> aiohttp.ClientConnectionError:
        self.unrouted.append(f"{method} {url}")
        return aiohttp.ClientConnectionError(f"no route in the fake: {method} {url}")

    # -- dispatch -------------------------------------------------------------------------------------------

    def serve(self, method: str, url: str, kw: Dict[str, Any]) -> _Response:
        headers = kw.get("headers") or {}
        bearer = str(headers.get("Authorization", "")).removeprefix("Bearer ")
        self.requests.append(Seen(method, url, bearer))
        if url.startswith(DL + "/"):
            drive_id, item_id = url[len(DL) + 1:].split("/", 1)
            node = self.drive_of(drive_id).nodes.get(item_id)
            return _Response(200, node.data) if node else _Response(404, b"")
        if url.startswith(THUMB + "/"):
            drive_id, item_id = url[len(THUMB) + 1:].split("/", 1)
            node = self.drive_of(drive_id).nodes.get(item_id)
            return _Response(200, node.thumbnail) if node and node.thumbnail else _Response(404, b"")
        if url.startswith(UP):
            return self._upload_chunk(url, kw)
        if not url.startswith(GRAPH):
            raise self._unrouted(method, url)
        drive = self.drives.get(bearer)
        if drive is None:
            return _Response(401, {"error": {"code": "InvalidAuthenticationToken"}})
        path = url[len(GRAPH):]
        return self._graph(drive, method, path, path.split("?", 1)[0], kw)

    def _graph(self, drive: GraphDrive, method: str, path: str, route: str, kw: Dict[str, Any]) -> _Response:
        if route == _APPROOT and method == "GET":
            return _Response(200, drive.raw(drive.root))
        if route == f"{_APPROOT}/delta":
            return self._delta(drive, path)
        if route.startswith(f"{_APPROOT}/search("):
            q = unquote(re.search(r"q='(.*)'\)", route).group(1)).replace("''", "'")        # type: ignore[union-attr]
            top = int(_param(path, "top") or 200)
            hits = [n for n in drive.nodes.values() if n is not drive.root and q.lower() in n.name.lower()]
            return _Response(200, {"value": [drive.raw(n) for n in hits[:top]]})
        m = re.fullmatch(r"/me/drive/items/([^/:]+)(?:(/children|/content|/thumbnails|/createUploadSession)"
                         r"|:/([^/:]+):(/content|/createUploadSession)?)?", route)
        if not m:
            raise self._unrouted(method, path)
        item_id, sub, named, named_sub = unquote(m.group(1)), m.group(2), m.group(3), m.group(4)
        if named is not None:
            return self._named(drive, method, item_id, unquote(named), named_sub, path, kw)
        node = drive.nodes.get(item_id)
        if node is None or item_id in drive.closed:
            return _Response(404, {"error": {"code": "itemNotFound"}})
        if sub is None:
            if method == "GET":
                return _Response(200, drive.raw(node))
            if method == "DELETE":
                drive.delete(node)
                return _Response(204)
            if method == "PATCH":
                return self._patch(drive, node, kw.get("json") or {})
        if sub == "/children" and method == "GET":
            return self._children(drive, node, path)
        if sub == "/children" and method == "POST":
            return self._create_folder(drive, node, kw.get("json") or {})
        if sub == "/content" and method == "PUT" and not node.is_folder:
            node.data = bytes(kw.get("data") or b"")
            node.hashes = _hash(node.data)
            drive.touch(node)
            return _Response(200, drive.raw(node))
        if sub == "/thumbnails" and method == "GET":
            size = _param(path, "select")
            if not node.thumbnail:
                return _Response(200, {"value": []})
            return _Response(200, {"value": [{size: {"url": f"{THUMB}/{drive.drive_id}/{node.item_id}"}}]})
        if sub == "/createUploadSession" and method == "POST":
            mode = (kw.get("json") or {}).get("item", {}).get("@microsoft.graph.conflictBehavior", "replace")
            return self._session(drive, node.item_id, None, mode)
        raise self._unrouted(method, path)

    def _too_long(self, drive: GraphDrive, parent: _Node, name: str) -> bool:
        return len(drive.path_of(parent)) + 1 + len(name) > MAX_PATH

    def _create_folder(self, drive: GraphDrive, parent: _Node, body: Dict[str, Any]) -> _Response:
        name = body["name"]
        if drive.by_name(parent.item_id, name):
            return _Response(409, {"error": {"code": "nameAlreadyExists"}})
        if self._too_long(drive, parent, name):
            return _Response(400, {"error": {"code": "invalidRequest"}})   # S1: a plain 400 for a folder
        raw = drive.raw(drive.add(parent.item_id, name, folder=True))
        raw["parentReference"]["path"] = f"/drives/{drive.drive_id}/root:{drive.path_of(parent)}"   # POST's own prefix
        return _Response(201, raw)

    def _named(self, drive: GraphDrive, method: str, parent_id: str, name: str, sub: Optional[str], path: str,
               kw: Dict[str, Any]) -> _Response:
        parent = drive.nodes.get(parent_id)
        if parent is None:
            return _Response(404, {"error": {"code": "itemNotFound"}})
        if method == "GET" and sub is None:
            existing = drive.by_name(parent_id, name)
            return _Response(200, drive.raw(existing)) if existing else _Response(404, {"error": {"code": "itemNotFound"}})
        if method == "POST" and sub == "/createUploadSession":
            mode = (kw.get("json") or {}).get("item", {}).get("@microsoft.graph.conflictBehavior", "rename")
            return self._session(drive, parent_id, name, mode)
        if method == "PUT" and sub == "/content":
            mode = _param(path, "@microsoft.graph.conflictBehavior") or "rename"
            mime = (kw.get("headers") or {}).get("Content-Type", "")
            return self._put(drive, parent, name, bytes(kw.get("data") or b""), mode, mime)
        raise self._unrouted(method, path)

    def _put(self, drive: GraphDrive, parent: _Node, name: str, data: bytes, mode: str, mime: str) -> _Response:
        existing = drive.by_name(parent.item_id, name)
        if existing and mode == "fail":
            return _Response(409, {"error": {"code": "nameAlreadyExists"}})
        if self._too_long(drive, parent, name):
            return _Response(400, {"error": {"code": "invalidRequest", "innerError": {"code": "pathIsTooLong"}}})
        if existing and mode == "replace" and not existing.is_folder:
            existing.data, existing.mime, existing.hashes = data, mime, _hash(data)
            drive.touch(existing)
            return _Response(200, drive.raw(existing))
        if existing:                                       # rename: "a 1.md"
            stem, dot, ext = name.rpartition(".") if "." in name else (name, "", "")
            n = 1
            while drive.by_name(parent.item_id, f"{stem} {n}{dot}{ext}"):
                n += 1
            name = f"{stem} {n}{dot}{ext}"
        return _Response(201, drive.raw(drive.add(parent.item_id, name, data=data, mime=mime)))

    def _session(self, drive: GraphDrive, target: str, name: Optional[str], mode: str) -> _Response:
        url = f"{UP}{len(self._uploads)}"
        self._uploads[url] = {"drive": drive, "target": target, "name": name, "mode": mode, "buf": bytearray()}
        return _Response(200, {"uploadUrl": url})

    def _upload_chunk(self, url: str, kw: Dict[str, Any]) -> _Response:
        up = self._uploads[url]
        up["buf"].extend(kw.get("data") or b"")
        total = int(str((kw.get("headers") or {})["Content-Range"]).split("/")[1])
        if len(up["buf"]) < total:
            return _Response(202, {})
        drive: GraphDrive = up["drive"]
        if up["name"] is not None:
            return self._put(drive, drive.nodes[up["target"]], up["name"], bytes(up["buf"]), up["mode"], "")
        node = drive.nodes[up["target"]]
        node.data, node.hashes = bytes(up["buf"]), _hash(bytes(up["buf"]))
        drive.touch(node)
        return _Response(200, drive.raw(node))

    def _patch(self, drive: GraphDrive, node: _Node, body: Dict[str, Any]) -> _Response:
        new_parent = body.get("parentReference", {}).get("id") or node.parent_id
        new_name = body.get("name") or node.name
        clash = drive.by_name(new_parent or "", new_name)
        if clash is not None and clash is not node:
            return _Response(409, {"error": {"code": "nameAlreadyExists"}})
        node.parent_id, node.name = new_parent, new_name
        drive.touch(node)
        return _Response(200, drive.raw(node))

    def _children(self, drive: GraphDrive, node: _Node, path: str) -> _Response:
        top, skip = int(_param(path, "top") or 200), int(_param(path, "skip") or 0)
        kids = drive.children(node.item_id)
        body: Dict[str, Any] = {"value": [drive.raw(k) for k in kids[skip:skip + top]]}
        if skip + top < len(kids):
            body["@odata.nextLink"] = f"{GRAPH}/me/drive/items/{node.item_id}/children?$top={top}&$skip={skip + top}"
        return _Response(200, body)

    # -- delta ----------------------------------------------------------------------------------------------

    def _delta(self, drive: GraphDrive, path: str) -> _Response:
        token = _param(path, "token")
        if drive.expired and token is not None:
            return _Response(410, {"error": {"code": "resyncRequired"}}, {"Location": f"{GRAPH}{_APPROOT}/delta?$top=200"})
        top, offset = int(_param(path, "top") or 200), int(_param(path, "skip") or 0)
        tomb = lambda i: {"id": i, "deleted": {}, "parentReference": {"driveId": drive.drive_id}}   # noqa: E731
        if token is None or token == "full":                # a full enumeration: current state, root first
            rows = [drive.raw(drive.root)] + [drive.raw(n) for n in drive.nodes.values() if n is not drive.root]
            start = 0
        else:
            start = int(token)
            latest: Dict[str, bool] = {}
            for pos, item_id, deleted in drive.log:
                if pos > start:
                    latest[item_id] = deleted
            rows = [tomb(i) if gone or i not in drive.nodes else drive.raw(drive.nodes[i]) for i, gone in latest.items()]
        page = rows[offset:offset + top]
        if offset + top < len(rows):
            nxt = f"{GRAPH}{_APPROOT}/delta?$top={top}&token={token or 'full'}&skip={offset + top}"
            return _Response(200, {"value": page, "@odata.nextLink": nxt})
        return _Response(200, {"value": page, "@odata.deltaLink": f"{GRAPH}{_APPROOT}/delta?token={len(drive.log)}"})


def _param(path: str, name: str) -> Optional[str]:
    m = re.search(rf"[?&]\$?{re.escape(name)}=([^&]+)", path)
    return unquote(m.group(1)) if m else None
