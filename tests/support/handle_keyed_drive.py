"""A small in-memory drive addressed by `DriveHandle` (MULTI_DRIVE_RFC §6), one tree per connection.

TRANSITIONAL (slice 1b, part D): part B moves the shared drive fakes onto the handle-keyed port; until then the tests of
part D's services need a fake that evaluates per connection. Two connections may hold the same item id on purpose
(T-4): every call reads and writes only the tree of `handle.connection_id`, so a call on the wrong connection is
visible in what is stored. Items carry adapter-style identities `fake:{connection_id}:{item_id}` and the drive identity
`fake:{connection_id}`.

    drive = HandleKeyedDrive()
    drive.add_file(handle, "item-1", "report.pdf", b"...", parent="root")
"""
from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Literal, Optional

from src.domain.drive_handle import DriveHandle
from src.domain.drive_index import DriveChange, DriveChanges, DriveChildrenPage
from src.domain.user_drive import DriveItem, DriveItemNotFoundError, DriveNameConflictError, join_drive_path, name_key
from src.ports.user_drive_port import UserDrivePort

ROOT = "root"


class HandleKeyedDrive(UserDrivePort):

    def __init__(self) -> None:
        self.items: Dict[str, Dict[str, DriveItem]] = {}
        self.parents: Dict[str, Dict[str, str]] = {}
        self.data: Dict[str, Dict[str, bytes]] = {}
        self.deleted: Dict[str, List[str]] = {}
        self._n = 0

    # -- building the trees ----------------------------------------------------------------------------------------

    def _tree(self, handle: DriveHandle) -> Dict[str, DriveItem]:
        cid = handle.connection_id
        if cid not in self.items:
            self.items[cid] = {ROOT: self._item(handle, ROOT, "", "", True)}
            self.parents[cid] = {}
            self.data[cid] = {}
            self.deleted[cid] = []
        return self.items[cid]

    @staticmethod
    def _item(handle: DriveHandle, item_id: str, name: str, path: str, is_folder: bool,
              size: int = 0, mime: str = "") -> DriveItem:
        return DriveItem(item_id=item_id, name=name, path=path, is_folder=is_folder, size_bytes=size, mime_type=mime,
                         identity=f"fake:{handle.connection_id}:{item_id}",
                         drive_identity=f"fake:{handle.connection_id}")

    def add_folder(self, handle: DriveHandle, item_id: str, name: str, parent: str = ROOT) -> DriveItem:
        tree = self._tree(handle)
        item = self._item(handle, item_id, name, join_drive_path(tree[parent].path, name), True)
        tree[item_id] = item
        self.parents[handle.connection_id][item_id] = parent
        return item

    def add_file(self, handle: DriveHandle, item_id: str, name: str, data: bytes = b"", parent: str = ROOT,
                 mime: str = "text/plain") -> DriveItem:
        tree = self._tree(handle)
        item = self._item(handle, item_id, name, join_drive_path(tree[parent].path, name), False, len(data), mime)
        tree[item_id] = item
        self.parents[handle.connection_id][item_id] = parent
        self.data[handle.connection_id][item_id] = data
        return item

    def _get(self, handle: DriveHandle, item_id: str) -> DriveItem:
        found = self._tree(handle).get(item_id)
        if found is None:
            raise DriveItemNotFoundError(item_id)
        return found

    def _new_id(self) -> str:
        self._n += 1
        return f"new-{self._n}"

    # -- the port --------------------------------------------------------------------------------------------------

    async def get_root(self, handle: DriveHandle) -> DriveItem:
        return self._tree(handle)[ROOT]

    async def get_item(self, handle: DriveHandle, item_id: str) -> DriveItem:
        return self._get(handle, item_id)

    async def list_children(self, handle: DriveHandle, folder_id: str) -> List[DriveItem]:
        self._get(handle, folder_id)
        tree = self._tree(handle)
        return [tree[i] for i, p in self.parents[handle.connection_id].items() if p == folder_id]

    async def search(self, handle: DriveHandle, query: str, limit: int) -> List[DriveItem]:
        return [i for i in self._tree(handle).values() if query.casefold() in i.name.casefold()][:limit]

    async def download(self, handle: DriveHandle, item_id: str) -> bytes:
        self._get(handle, item_id)
        return self.data[handle.connection_id].get(item_id, b"")

    async def upload(self, handle: DriveHandle, parent_id: str, filename: str, data: bytes, content_type: str,
                     conflict: Literal["rename", "replace", "fail"] = "rename") -> DriveItem:
        taken = {name_key(c.name) for c in await self.list_children(handle, parent_id)}
        name = filename
        if name_key(name) in taken:
            if conflict == "fail":
                raise DriveNameConflictError(name)
            name = f"{filename} 1"
        return self.add_file(handle, self._new_id(), name, data, parent_id, content_type)

    async def replace_content(self, handle: DriveHandle, item_id: str, data: bytes, content_type: str) -> DriveItem:
        item = self._get(handle, item_id)
        self.data[handle.connection_id][item_id] = data
        updated = replace(item, size_bytes=len(data))
        self._tree(handle)[item_id] = updated
        return updated

    async def move(self, handle: DriveHandle, item_id: str,
                   new_parent_id: Optional[str] = None, new_name: Optional[str] = None) -> DriveItem:
        item = self._get(handle, item_id)
        parent = new_parent_id or self.parents[handle.connection_id][item_id]
        name = new_name or item.name
        moved = replace(item, name=name, path=join_drive_path(self._get(handle, parent).path, name))
        self._tree(handle)[item_id] = moved
        self.parents[handle.connection_id][item_id] = parent
        return moved

    async def create_folder(self, handle: DriveHandle, parent_id: str, name: str) -> DriveItem:
        if any(name_key(c.name) == name_key(name) for c in await self.list_children(handle, parent_id)):
            raise DriveNameConflictError(name)
        return self.add_folder(handle, self._new_id(), name, parent_id)

    async def delete(self, handle: DriveHandle, item_id: str) -> None:
        self._get(handle, item_id)
        del self._tree(handle)[item_id]
        self.parents[handle.connection_id].pop(item_id, None)
        self.deleted[handle.connection_id].append(item_id)

    async def child_by_name(self, handle: DriveHandle, parent_id: str, name: str) -> Optional[DriveChange]:
        for child in await self.list_children(handle, parent_id):
            if child.name == name:
                return await self.get_change(handle, child.item_id)
        return None

    async def area_path(self, handle: DriveHandle) -> str:
        return "Apps/Bot"

    async def get_changes(self, handle: DriveHandle, cursor: Optional[str]) -> DriveChanges:
        raise NotImplementedError

    async def get_change(self, handle: DriveHandle, item_id: str) -> DriveChange:
        item = self._get(handle, item_id)
        return DriveChange(item_id=item.item_id, name=item.name, is_folder=item.is_folder,
                           parent_id=self.parents[handle.connection_id].get(item_id, ""),
                           size_bytes=item.size_bytes, mime_type=item.mime_type, identity=item.identity)

    async def list_children_changes(self, handle: DriveHandle, folder_id: str, limit: int,
                                    cursor: Optional[str] = None) -> DriveChildrenPage:
        raise NotImplementedError

    async def download_to_file(self, handle: DriveHandle, item_id: str, dest_path: str, max_bytes: int) -> int:
        raise NotImplementedError

    async def get_thumbnail(self, handle: DriveHandle, item_id: str, max_edge_px: int) -> Optional[bytes]:
        return None
