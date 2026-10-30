"""DriveProviderRouter — the one `UserDrivePort` services hold (MULTI_DRIVE_RFC §6, E6, T-8).

Holds `{kind: UserDrivePort}` (from `ConnectorRegistry.drive_adapters()`) and forwards every call to the adapter of
`handle.kind`. Every method body is one dispatch call (T-8): no logic, no caching, no state of its own. Adapters are
injected, never imported (REQ-ARCH-23). A handle whose kind has no adapter raises DriveNotConnectedError: the
connection is unusable, which every caller already handles.

Precedent for a composition class implementing a port: `UserAgentFactory(AgentFactoryPort)`.

Public interface:
    DriveProviderRouter(adapters: Mapping[str, UserDrivePort])
    kinds() -> List[str]
    every UserDrivePort method by handle (the single-drive members of the port's transitional block are not routed)
"""
from types import MappingProxyType
from typing import List, Literal, Mapping, Optional

from ..domain.drive_capabilities import DriveCapabilities
from ..domain.drive_handle import DriveHandle
from ..domain.drive_index import DriveChange, DriveChanges, DriveChildrenPage
from ..domain.drive_media_source import MediaSource
from ..domain.user_drive import DriveItem, DriveNotConnectedError
from ..ports.user_drive_port import UserDrivePort
from ..utils.logger import logger


class DriveProviderRouter(UserDrivePort):

    def __init__(self, adapters: Mapping[str, UserDrivePort]) -> None:
        for kind, adapter in adapters.items():
            if isinstance(adapter, DriveProviderRouter):
                raise ValueError(f"a router is not an adapter (kind {kind!r})")
        self._adapters: Mapping[str, UserDrivePort] = MappingProxyType(dict(adapters))

    def kinds(self) -> List[str]:
        return list(self._adapters)

    def _route(self, handle: DriveHandle) -> UserDrivePort:
        adapter = self._adapters.get(handle.kind)
        if adapter is None:
            logger.error(f"[DriveRouter] no drive adapter for kind {handle.kind!r} "
                         f"(connection {handle.connection_id[:8]})")
            raise DriveNotConnectedError(f"No drive adapter is registered for kind {handle.kind!r}")
        return adapter

    async def get_root(self, handle: DriveHandle) -> DriveItem:
        return await self._route(handle).get_root(handle)

    async def get_item(self, handle: DriveHandle, item_id: str) -> DriveItem:
        return await self._route(handle).get_item(handle, item_id)

    async def list_children(self, handle: DriveHandle, folder_id: str) -> List[DriveItem]:
        return await self._route(handle).list_children(handle, folder_id)

    async def search(self, handle: DriveHandle, query: str, limit: int) -> List[DriveItem]:
        return await self._route(handle).search(handle, query, limit)

    async def download(self, handle: DriveHandle, item_id: str) -> bytes:
        return await self._route(handle).download(handle, item_id)

    async def upload(self, handle: DriveHandle, parent_id: str, filename: str, data: bytes, content_type: str,
                     conflict: Literal["rename", "replace", "fail"] = "rename") -> DriveItem:
        return await self._route(handle).upload(handle, parent_id, filename, data, content_type, conflict)

    async def replace_content(self, handle: DriveHandle, item_id: str, data: bytes, content_type: str) -> DriveItem:
        return await self._route(handle).replace_content(handle, item_id, data, content_type)

    async def move(self, handle: DriveHandle, item_id: str,
                   new_parent_id: Optional[str] = None, new_name: Optional[str] = None) -> DriveItem:
        return await self._route(handle).move(handle, item_id, new_parent_id, new_name)

    async def create_folder(self, handle: DriveHandle, parent_id: str, name: str) -> DriveItem:
        return await self._route(handle).create_folder(handle, parent_id, name)

    async def delete(self, handle: DriveHandle, item_id: str) -> None:
        return await self._route(handle).delete(handle, item_id)

    async def child_by_name(self, handle: DriveHandle, parent_id: str, name: str) -> Optional[DriveChange]:
        return await self._route(handle).child_by_name(handle, parent_id, name)

    async def area_path(self, handle: DriveHandle) -> str:
        return await self._route(handle).area_path(handle)

    async def get_changes(self, handle: DriveHandle, cursor: Optional[str]) -> DriveChanges:
        return await self._route(handle).get_changes(handle, cursor)

    async def get_change(self, handle: DriveHandle, item_id: str) -> DriveChange:
        return await self._route(handle).get_change(handle, item_id)

    async def list_children_changes(self, handle: DriveHandle, folder_id: str, limit: int,
                                    cursor: Optional[str] = None) -> DriveChildrenPage:
        return await self._route(handle).list_children_changes(handle, folder_id, limit, cursor)

    async def download_to_file(self, handle: DriveHandle, item_id: str, dest_path: str, max_bytes: int) -> int:
        return await self._route(handle).download_to_file(handle, item_id, dest_path, max_bytes)

    async def get_thumbnail(self, handle: DriveHandle, item_id: str, max_edge_px: int) -> Optional[bytes]:
        return await self._route(handle).get_thumbnail(handle, item_id, max_edge_px)

    async def media_source(self, handle: DriveHandle, item_id: str) -> MediaSource:
        return await self._route(handle).media_source(handle, item_id)

    async def capabilities(self, handle: DriveHandle) -> DriveCapabilities:
        return await self._route(handle).capabilities(handle)

    async def forget(self, handle: DriveHandle) -> None:
        return await self._route(handle).forget(handle)
