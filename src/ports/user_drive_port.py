"""
UserDrivePort — one connection's drive (docs/10_rfcs/USER_DRIVE_RFC.md §4.2, MULTI_DRIVE_RFC §6).

Provider-neutral: ids are opaque, paths are relative to the area root and readable
(never percent-encoded). Every method takes the `DriveHandle` of the connection it acts on
(MULTI_DRIVE_RFC §6, E6); the composition's `DriveProviderRouter` dispatches it to the adapter of
`handle.kind`. An implementation finds its grant by `handle.credential_id` and keys every cache, lock
and token cache by `(user_id, credential_id)` or `connection_id`, never by `user_id` alone (T-3).

An implementation raises DriveNotConnectedError when access is missing or expired,
DriveItemNotFoundError for a missing item and DriveNameConflictError when a create hits a taken name.
Every write may also raise DriveStorageFull (the drive is full) or DrivePathTooLong (the resulting path
exceeds the provider's limit).

TRANSITIONAL (slice 1b, removed when the callers move; listed in the C1b report): the block at the end
keeps the single-drive members `display_name`, `is_connected`, `disconnect`, `connection`,
`flag_reconnect` and `download_url` (addressed by `user_id`, no longer abstract), and declares
`media_source`, `capabilities` and `forget` with a NotImplementedError body instead of abstract, so the
existing adapters stay instantiable until they implement the handle form. Neither is part of the final
port.
"""
from abc import ABC, abstractmethod
from typing import List, Literal, Optional

from src.domain.drive_capabilities import DriveCapabilities
from src.domain.drive_handle import DriveHandle
from src.domain.drive_index import DriveChange, DriveChanges, DriveChildrenPage
from src.domain.drive_media_source import MediaSource
from src.domain.user_drive import DriveConnection, DriveItem


class UserDrivePort(ABC):

    @abstractmethod
    async def get_root(self, handle: DriveHandle) -> DriveItem:
        """The area root folder (created by the provider on first access)."""

    @abstractmethod
    async def get_item(self, handle: DriveHandle, item_id: str) -> DriveItem:
        """Metadata of one item, path included."""

    @abstractmethod
    async def list_children(self, handle: DriveHandle, folder_id: str) -> List[DriveItem]:
        """All direct children of a folder (every page)."""

    @abstractmethod
    async def search(self, handle: DriveHandle, query: str, limit: int) -> List[DriveItem]:
        """Items in the area matching `query`, at most `limit`, paths included."""

    @abstractmethod
    async def download(self, handle: DriveHandle, item_id: str) -> bytes:
        """File content. Callers check size from get_item first."""

    @abstractmethod
    async def upload(self, handle: DriveHandle, parent_id: str, filename: str, data: bytes, content_type: str,
                     conflict: Literal["rename", "replace", "fail"] = "rename") -> DriveItem:
        """New file in `parent_id`. `conflict` says what a taken name does: "rename" (the default, every existing
        caller) gets a provider-suffixed name and never overwrites; "replace" overwrites the file; "fail" raises
        DriveNameConflictError. May raise DriveStorageFull or DrivePathTooLong."""

    @abstractmethod
    async def replace_content(self, handle: DriveHandle, item_id: str, data: bytes, content_type: str) -> DriveItem:
        """Overwrite an existing file's content, keeping its id and name. May raise DriveStorageFull."""

    @abstractmethod
    async def move(self, handle: DriveHandle, item_id: str,
                   new_parent_id: Optional[str] = None, new_name: Optional[str] = None) -> DriveItem:
        """Move and/or rename; the id is unchanged. DriveNameConflictError when the target name is taken."""

    @abstractmethod
    async def create_folder(self, handle: DriveHandle, parent_id: str, name: str) -> DriveItem:
        """Create one folder; DriveNameConflictError if the name is taken. May raise DrivePathTooLong (the
        provider refuses a folder whose path is too long) or DriveStorageFull."""

    @abstractmethod
    async def delete(self, handle: DriveHandle, item_id: str) -> None:
        """Delete a file or folder (the provider keeps it recoverable: the recycle bin, never beyond)."""

    @abstractmethod
    async def child_by_name(self, handle: DriveHandle, parent_id: str, name: str) -> Optional[DriveChange]:
        """The child of `parent_id` with exactly that name, None when there is none. Never a search."""

    @abstractmethod
    async def area_path(self, handle: DriveHandle) -> str:
        """The decoded absolute path of the area root from the drive root, for the path-length rule
        (USER_DRIVE_INDEXING_RFC §27.9, Δ193). Never logged in full: log its length."""

    # -- change capture for the index (USER_DRIVE_INDEXING_RFC §4) --------------------------

    @abstractmethod
    async def get_changes(self, handle: DriveHandle, cursor: Optional[str]) -> DriveChanges:
        """One page of the change feed from `cursor` (None: a full enumeration from scratch).
        Raises DriveResyncRequired when the cursor expired, DriveThrottled when the provider keeps
        throttling, DriveReauthRequired when the refresh token is rejected."""

    @abstractmethod
    async def get_change(self, handle: DriveHandle, item_id: str) -> DriveChange:
        """One item as an observation, read by id. Raises DriveItemNotFoundError when the provider answers "not found":
        never a deletion, because a provider may report "no access" as "not found" for an item its change feed still
        lists (USER_DRIVE_INDEXING_RFC §26.2, Δ48). Existence is decided by the change feed; the caller decides what a
        "not found" means for its call."""

    @abstractmethod
    async def list_children_changes(self, handle: DriveHandle, folder_id: str, limit: int,
                                    cursor: Optional[str] = None) -> DriveChildrenPage:
        """One page (at most `limit`) of a folder's direct children as observations, starting at `cursor`
        (None: the first page). `next_cursor` is None on the last page. Raises DriveItemNotFoundError
        when the folder is gone."""

    # -- content for the index (USER_DRIVE_INDEXING_RFC §8.1, §24.5) -----------------------------

    @abstractmethod
    async def download_to_file(self, handle: DriveHandle, item_id: str, dest_path: str, max_bytes: int) -> int:
        """Stream a file's content to `dest_path` (never held whole in memory) and return the bytes written. Raises
        DriveFileTooLarge as soon as the stream passes `max_bytes`, DriveItemNotFoundError for a missing item."""

    @abstractmethod
    async def get_thumbnail(self, handle: DriveHandle, item_id: str, max_edge_px: int) -> Optional[bytes]:
        """A provider-rendered image of the item whose longer edge is at most `max_edge_px` (the adapter picks the
        provider's nearest rendition), JPEG or PNG; None when the provider has no rendition for it. Raises
        DriveThumbnailTooLarge when the rendition passes the vision byte cap. The indexer never decodes or scales an
        image itself (§5.3)."""

    # -- per connection (MULTI_DRIVE_RFC §6, §7.1) ---------------------------------------------------------------

    async def media_source(self, handle: DriveHandle, item_id: str) -> MediaSource:
        """Where a tool may stream the file's bytes from (replaces the bare download URL). The url and headers are
        secrets: never logged, stored or put in an error. Raises DriveItemNotFoundError for a missing item or one with
        no downloadable content. TRANSITIONAL: abstract once the adapters implement it (slice 1b)."""
        raise NotImplementedError

    async def capabilities(self, handle: DriveHandle) -> DriveCapabilities:
        """What this connection's drive can do (§7.1). TRANSITIONAL: abstract once the adapters implement it."""
        raise NotImplementedError

    async def forget(self, handle: DriveHandle) -> None:
        """Drop every cache, lock and token cache the adapter holds for this connection (after a disconnect).
        TRANSITIONAL: abstract once the adapters implement it."""
        raise NotImplementedError

    # -- TRANSITIONAL: the single-drive members, by user_id; they leave the port when their callers move (slice 1b).
    # Labels move to the registry, state to the drive connection service, the download URL to `media_source`.

    @property
    def display_name(self) -> str:
        """Human name of the provider, for the Cabinet only."""
        raise NotImplementedError

    async def is_connected(self, user_id: str) -> bool:
        """True when credentials are stored (token validity is not checked)."""
        raise NotImplementedError

    async def disconnect(self, user_id: str) -> None:
        """Forget the user's credentials."""
        raise NotImplementedError

    async def connection(self, user_id: str) -> DriveConnection:
        """Connected?, refresh rejected earlier?, and the connected drive's id (§10.1 step 0)."""
        raise NotImplementedError

    async def flag_reconnect(self, user_id: str) -> bool:
        """Mark the stored grant as needing a reconnect; True only on the False->True transition (§7.7)."""
        raise NotImplementedError

    async def download_url(self, user_id: str, item_id: str) -> str:
        """The provider's short-lived, pre-authenticated download URL for a file (USER_DRIVE_INDEXING_RFC §27.4). A
        secret: never log it. Raises DriveItemNotFoundError for a missing item."""
        raise NotImplementedError
