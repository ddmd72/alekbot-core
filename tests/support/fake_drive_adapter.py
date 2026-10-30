"""FakeDriveAdapter — an in-memory `UserDrivePort` for a second provider (MULTI_DRIVE_RFC §13 T-3, T-4).

One instance serves any number of connections, each with its own tree (`handle.connection_id` is the key), as a real
adapter serves many grants. It registers under an `id_space` of its own (`fake_a`, `fake_b`) and builds identities as
`{id_space}:{drive}:{item}`; item ids restart at `item-1` in every connection on purpose, so a key built from the item
id alone collides across connections and a test sees it. Provider behaviour: unique names (fail on a taken one;
`rename` suffixes "name 1"), recoverable delete (`recycle`), a change feed with cursors that can be expired, an item
closed to id reads while the feed still lists it (404 is not deletion), typed media facts planted by the test, and a
token cache keyed `(user_id, credential_id)` that `forget(handle)` drops (T-3: two handles share no cache).
Reads and writes by a handle whose connection has no tree open one.
"""
from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional, Set, Tuple

from src.domain.drive_capabilities import DriveCapabilities
from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_handle import DriveHandle
from src.domain.drive_index import DriveChange, DriveChanges, DriveChildrenPage
from src.domain.drive_media_facts import DriveMediaFacts
from src.domain.drive_media_source import MediaSource
from src.domain.user_drive import (
    DriveFileTooLarge, DriveItem, DriveItemNotFoundError, DriveNameConflictError, DrivePathTooLong,
    DriveResyncRequired, join_drive_path,
)
from src.ports.user_drive_port import UserDrivePort

_T0 = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)


@dataclass
class _Node:
    item_id: str
    name: str
    parent_id: Optional[str]
    is_folder: bool
    data: bytes = b""
    version: int = 1
    order: int = 0
    media: DriveMediaFacts = field(default_factory=DriveMediaFacts)
    hashed: bool = True
    thumbnail: Optional[bytes] = None


class _Tree:
    def __init__(self, drive_id: str) -> None:
        self.drive_id = drive_id
        self._ids = itertools.count(1)
        self._order = itertools.count(1)
        self.root = _Node("root", "", None, True)
        self.nodes: Dict[str, _Node] = {"root": self.root}
        self.recycle: Dict[str, _Node] = {}
        self.closed: Set[str] = set()
        self.log: List[Tuple[int, str, bool]] = []
        self.epoch = 0                                   # bumped by `expire_cursors`

    def new(self, parent: str, name: str, folder: bool, data: bytes = b"") -> _Node:
        node = _Node(f"item-{next(self._ids)}", name, parent, folder, data, order=next(self._order))
        self.nodes[node.item_id] = node
        self.log.append((len(self.log) + 1, node.item_id, False))
        return node

    def kids(self, parent: str) -> List[_Node]:
        return sorted((n for n in self.nodes.values() if n.parent_id == parent), key=lambda n: n.order)

    def named(self, parent: str, name: str) -> Optional[_Node]:
        return next((n for n in self.kids(parent) if n.name == name), None)

    def path(self, node: _Node) -> str:
        parts: List[str] = []
        cur: Optional[_Node] = node
        while cur is not None and cur.parent_id is not None:
            parts.append(cur.name)
            cur = self.nodes.get(cur.parent_id)
        return "/".join(reversed(parts))


class FakeDriveAdapter(UserDrivePort):
    def __init__(self, id_space: str = "fake_a", *, max_path_chars: int = 255, area_path: str = "/Fake") -> None:
        self.id_space = id_space
        self.max_path_chars = max_path_chars
        self._area_path = area_path
        self._trees: Dict[str, _Tree] = {}
        self.token_cache: Dict[Tuple[str, str], str] = {}     # (user_id, credential_id) -> token
        self.forgotten: List[str] = []
        self.calls: List[Tuple[str, str]] = []                # (method, connection_id)

    # -- provider-side scripting ----------------------------------------------------------------------------

    def tree(self, handle: DriveHandle, drive_id: Optional[str] = None) -> _Tree:
        tree = self._trees.get(handle.connection_id)
        if tree is None:
            tree = self._trees[handle.connection_id] = _Tree(drive_id or f"drv-{handle.credential_id}")
        return tree

    def plant(self, handle: DriveHandle, parent_id: str, name: str, data: bytes = b"", *, folder: bool = False,
              media: Optional[DriveMediaFacts] = None, hashed: bool = True,
              thumbnail: Optional[bytes] = None) -> str:
        node = self.tree(handle).new(parent_id, name, folder, data)
        node.media, node.hashed, node.thumbnail = media or DriveMediaFacts(), hashed, thumbnail
        return node.item_id

    def close(self, handle: DriveHandle, item_id: str) -> None:
        """The item stays in the change feed and answers "not found" to every id read."""
        self.tree(handle).closed.add(item_id)

    def expire_cursors(self, handle: DriveHandle) -> None:
        self.tree(handle).epoch += 1

    def in_recycle_bin(self, handle: DriveHandle, item_id: str) -> bool:
        return item_id in self.tree(handle).recycle

    def holds_state_for(self, handle: DriveHandle) -> bool:
        return (handle.user_id, handle.credential_id) in self.token_cache

    # -- shapes ---------------------------------------------------------------------------------------------

    def _enter(self, method: str, handle: DriveHandle) -> _Tree:
        self.calls.append((method, handle.connection_id))
        self.token_cache[(handle.user_id, handle.credential_id)] = f"token-{handle.credential_id}"
        return self.tree(handle)

    def _content_identity(self, node: _Node) -> Optional[ContentIdentity]:
        if node.is_folder or not node.hashed:
            return None
        return ContentIdentity("fakehash", hashlib.sha1(node.data).hexdigest()[:16], len(node.data))

    def _item_identity(self, tree: _Tree, item_id: str) -> str:
        return f"{self.id_space}:{tree.drive_id}:{item_id}"

    def _item(self, tree: _Tree, node: _Node) -> DriveItem:
        return DriveItem(
            item_id=node.item_id, name=node.name, path=tree.path(node), is_folder=node.is_folder,
            size_bytes=len(node.data), modified_at=_T0, child_count=len(tree.kids(node.item_id)) if node.is_folder else 0,
            identity=self._item_identity(tree, node.item_id), drive_identity=f"{self.id_space}:{tree.drive_id}",
            content_version="" if node.is_folder else f"v{node.version}", content_identity=self._content_identity(node),
            media=node.media)

    def _change(self, tree: _Tree, node: _Node) -> DriveChange:
        return DriveChange(
            item_id=node.item_id, parent_id=node.parent_id or "", name=node.name, is_folder=node.is_folder,
            size_bytes=len(node.data), content_version="" if node.is_folder else f"v{node.version}",
            modified_at=_T0, fs_modified_at=_T0, created_at=_T0,
            child_count=len(tree.kids(node.item_id)) if node.is_folder else 0,
            identity=self._item_identity(tree, node.item_id), content_identity=self._content_identity(node),
            media=node.media)

    def _get(self, tree: _Tree, item_id: str) -> _Node:
        node = tree.nodes.get(item_id)
        if node is None or item_id in tree.closed:
            raise DriveItemNotFoundError(item_id)
        return node

    def _folder(self, tree: _Tree, item_id: str) -> _Node:
        node = self._get(tree, item_id)
        if not node.is_folder:
            raise DriveItemNotFoundError(item_id)
        return node

    def _check_path(self, tree: _Tree, parent: _Node, name: str) -> None:
        if len(self._area_path) + len(join_drive_path(tree.path(parent), name)) + 1 > self.max_path_chars:
            raise DrivePathTooLong(name)

    # -- reads ----------------------------------------------------------------------------------------------

    async def get_root(self, handle: DriveHandle) -> DriveItem:
        tree = self._enter("get_root", handle)
        return self._item(tree, tree.root)

    async def get_item(self, handle: DriveHandle, item_id: str) -> DriveItem:
        tree = self._enter("get_item", handle)
        return self._item(tree, self._get(tree, item_id))

    async def list_children(self, handle: DriveHandle, folder_id: str) -> List[DriveItem]:
        tree = self._enter("list_children", handle)
        return [self._item(tree, n) for n in tree.kids(self._folder(tree, folder_id).item_id) if n.item_id not in tree.closed]

    async def search(self, handle: DriveHandle, query: str, limit: int) -> List[DriveItem]:
        tree = self._enter("search", handle)
        hits = [n for n in tree.nodes.values() if n is not tree.root and query.lower() in n.name.lower()
                and n.item_id not in tree.closed]
        return [self._item(tree, n) for n in sorted(hits, key=lambda n: n.order)[:limit]]

    async def download(self, handle: DriveHandle, item_id: str) -> bytes:
        tree = self._enter("download", handle)
        node = self._get(tree, item_id)
        if node.is_folder:
            raise DriveItemNotFoundError(item_id)
        return node.data

    async def child_by_name(self, handle: DriveHandle, parent_id: str, name: str) -> Optional[DriveChange]:
        tree = self._enter("child_by_name", handle)
        node = tree.named(self._folder(tree, parent_id).item_id, name)
        return self._change(tree, node) if node is not None and node.item_id not in tree.closed else None

    async def area_path(self, handle: DriveHandle) -> str:
        self._enter("area_path", handle)
        return self._area_path

    # -- writes ---------------------------------------------------------------------------------------------

    async def upload(self, handle: DriveHandle, parent_id: str, filename: str, data: bytes, content_type: str,
                     conflict: Literal["rename", "replace", "fail"] = "rename") -> DriveItem:
        tree = self._enter("upload", handle)
        parent = self._folder(tree, parent_id)
        self._check_path(tree, parent, filename)
        existing = tree.named(parent_id, filename)
        if existing is not None:
            if conflict == "fail":
                raise DriveNameConflictError(filename)
            if conflict == "replace" and not existing.is_folder:
                existing.data, existing.version = data, existing.version + 1
                tree.log.append((len(tree.log) + 1, existing.item_id, False))
                return self._item(tree, existing)
            stem, dot, ext = filename.rpartition(".") if "." in filename else (filename, "", "")
            n = 1
            while tree.named(parent_id, f"{stem} {n}{dot}{ext}"):
                n += 1
            filename = f"{stem} {n}{dot}{ext}"
        return self._item(tree, tree.new(parent_id, filename, False, data))

    async def replace_content(self, handle: DriveHandle, item_id: str, data: bytes, content_type: str) -> DriveItem:
        tree = self._enter("replace_content", handle)
        node = self._get(tree, item_id)
        if node.is_folder:
            raise DriveItemNotFoundError(item_id)
        node.data, node.version = data, node.version + 1
        tree.log.append((len(tree.log) + 1, node.item_id, False))
        return self._item(tree, node)

    async def move(self, handle: DriveHandle, item_id: str, new_parent_id: Optional[str] = None,
                   new_name: Optional[str] = None) -> DriveItem:
        tree = self._enter("move", handle)
        node = self._get(tree, item_id)
        parent = self._folder(tree, new_parent_id or node.parent_id or "root")
        name = new_name or node.name
        clash = tree.named(parent.item_id, name)
        if clash is not None and clash is not node:
            raise DriveNameConflictError(name)
        node.parent_id, node.name = parent.item_id, name
        tree.log.append((len(tree.log) + 1, node.item_id, False))
        return self._item(tree, node)

    async def create_folder(self, handle: DriveHandle, parent_id: str, name: str) -> DriveItem:
        tree = self._enter("create_folder", handle)
        parent = self._folder(tree, parent_id)
        if tree.named(parent_id, name) is not None:
            raise DriveNameConflictError(name)
        self._check_path(tree, parent, name)
        return self._item(tree, tree.new(parent_id, name, True))

    async def delete(self, handle: DriveHandle, item_id: str) -> None:
        tree = self._enter("delete", handle)
        node = self._get(tree, item_id)

        def drop(n: _Node) -> None:
            for kid in tree.kids(n.item_id):
                drop(kid)
            tree.nodes.pop(n.item_id, None)
            tree.recycle[n.item_id] = n
            tree.log.append((len(tree.log) + 1, n.item_id, True))

        drop(node)

    # -- change feed ----------------------------------------------------------------------------------------

    async def get_changes(self, handle: DriveHandle, cursor: Optional[str]) -> DriveChanges:
        tree = self._enter("get_changes", handle)
        identity = f"{self.id_space}:{tree.drive_id}"
        if cursor is None:
            rows = [self._change(tree, tree.root)] + [self._change(tree, n) for n in tree.nodes.values() if n is not tree.root]
            return DriveChanges(tuple(rows), f"{tree.epoch}:{len(tree.log)}", True, True, tree.root.item_id, identity)
        epoch, _, pos = cursor.partition(":")
        if int(epoch) != tree.epoch:
            raise DriveResyncRequired(f"{tree.epoch}:0")
        latest: Dict[str, bool] = {}
        for p, item_id, deleted in tree.log:
            if p > int(pos):
                latest[item_id] = deleted
        rows = [DriveChange(item_id=i, deleted=True, identity=self._item_identity(tree, i)) if gone or i not in tree.nodes
                else self._change(tree, tree.nodes[i]) for i, gone in latest.items()]
        return DriveChanges(tuple(rows), f"{tree.epoch}:{len(tree.log)}", True, False, tree.root.item_id, identity)

    async def get_change(self, handle: DriveHandle, item_id: str) -> DriveChange:
        tree = self._enter("get_change", handle)
        return self._change(tree, self._get(tree, item_id))

    async def list_children_changes(self, handle: DriveHandle, folder_id: str, limit: int,
                                    cursor: Optional[str] = None) -> DriveChildrenPage:
        tree = self._enter("list_children_changes", handle)
        kids = tree.kids(self._folder(tree, folder_id).item_id)
        start = int(cursor or 0)
        nxt = str(start + limit) if start + limit < len(kids) else None
        return DriveChildrenPage(tuple(self._change(tree, n) for n in kids[start:start + limit]), nxt)

    # -- content --------------------------------------------------------------------------------------------

    async def download_to_file(self, handle: DriveHandle, item_id: str, dest_path: str, max_bytes: int) -> int:
        tree = self._enter("download_to_file", handle)
        node = self._get(tree, item_id)
        if node.is_folder:
            raise DriveItemNotFoundError(item_id)
        with open(dest_path, "wb") as fh:
            fh.write(node.data[:max_bytes])
        if len(node.data) > max_bytes:
            raise DriveFileTooLarge(max_bytes)
        return len(node.data)

    async def get_thumbnail(self, handle: DriveHandle, item_id: str, max_edge_px: int) -> Optional[bytes]:
        tree = self._enter("get_thumbnail", handle)
        if max_edge_px <= 0:
            raise ValueError("a thumbnail edge is positive")
        return self._get(tree, item_id).thumbnail

    async def media_source(self, handle: DriveHandle, item_id: str) -> MediaSource:
        tree = self._enter("media_source", handle)
        node = self._get(tree, item_id)
        if node.is_folder:
            raise DriveItemNotFoundError(item_id)
        return MediaSource(url=f"https://media.example.invalid/{tree.drive_id}/{item_id}")

    # -- per connection -------------------------------------------------------------------------------------

    async def capabilities(self, handle: DriveHandle) -> DriveCapabilities:
        self._enter("capabilities", handle)
        return DriveCapabilities(max_path_chars=self.max_path_chars)

    async def forget(self, handle: DriveHandle) -> None:
        self.calls.append(("forget", handle.connection_id))
        self.token_cache.pop((handle.user_id, handle.credential_id), None)
        self.forgotten.append(handle.connection_id)
