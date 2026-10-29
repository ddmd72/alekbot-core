"""
UserDrivePort — the user's long-term file area (docs/10_rfcs/USER_DRIVE_RFC.md §4.2).

Provider-neutral: ids are opaque, paths are relative to the area root and readable
(never percent-encoded). An implementation raises DriveNotConnectedError when access is
missing or expired, DriveItemNotFoundError for a missing item and DriveNameConflictError
when a create hits a taken name. Every write may also raise DriveStorageFull (the drive is full) or
DrivePathTooLong (the resulting path exceeds the provider's limit).
"""
from abc import ABC, abstractmethod
from typing import List, Literal, Optional

from src.domain.drive_index import DriveChange, DriveChanges, DriveChildrenPage
from src.domain.user_drive import DriveConnection, DriveItem


class UserDrivePort(ABC):

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human name of the provider, for the Cabinet only."""

    @abstractmethod
    async def is_connected(self, user_id: str) -> bool:
        """True when credentials are stored (token validity is not checked)."""

    @abstractmethod
    async def disconnect(self, user_id: str) -> None:
        """Forget the user's credentials."""

    @abstractmethod
    async def get_root(self, user_id: str) -> DriveItem:
        """The area root folder (created by the provider on first access)."""

    @abstractmethod
    async def get_item(self, user_id: str, item_id: str) -> DriveItem:
        """Metadata of one item, path included."""

    @abstractmethod
    async def list_children(self, user_id: str, folder_id: str) -> List[DriveItem]:
        """All direct children of a folder (every page)."""

    @abstractmethod
    async def search(self, user_id: str, query: str, limit: int) -> List[DriveItem]:
        """Items in the area matching `query`, at most `limit`, paths included."""

    @abstractmethod
    async def download(self, user_id: str, item_id: str) -> bytes:
        """File content. Callers check size from get_item first."""

    @abstractmethod
    async def upload(self, user_id: str, parent_id: str, filename: str, data: bytes, content_type: str,
                     conflict: Literal["rename", "replace", "fail"] = "rename") -> DriveItem:
        """New file in `parent_id`. `conflict` says what a taken name does: "rename" (the default, every existing
        caller) gets a provider-suffixed name and never overwrites; "replace" overwrites the file; "fail" raises
        DriveNameConflictError. May raise DriveStorageFull or DrivePathTooLong."""

    @abstractmethod
    async def replace_content(self, user_id: str, item_id: str, data: bytes, content_type: str) -> DriveItem:
        """Overwrite an existing file's content, keeping its id and name. May raise DriveStorageFull."""

    @abstractmethod
    async def move(self, user_id: str, item_id: str,
                   new_parent_id: Optional[str] = None, new_name: Optional[str] = None) -> DriveItem:
        """Move and/or rename; the id is unchanged. DriveNameConflictError when the target name is taken."""

    @abstractmethod
    async def create_folder(self, user_id: str, parent_id: str, name: str) -> DriveItem:
        """Create one folder; DriveNameConflictError if the name is taken. May raise DrivePathTooLong (the
        provider refuses a folder whose path is too long) or DriveStorageFull."""

    @abstractmethod
    async def delete(self, user_id: str, item_id: str) -> None:
        """Delete a file or folder (the provider keeps it recoverable: the recycle bin, never beyond)."""

    @abstractmethod
    async def child_by_name(self, user_id: str, parent_id: str, name: str) -> Optional[DriveChange]:
        """The child of `parent_id` with exactly that name, None when there is none. Never a search."""

    @abstractmethod
    async def area_path(self, user_id: str) -> str:
        """The decoded absolute path of the area root from the drive root, for the path-length rule
        (USER_DRIVE_INDEXING_RFC §27.9, Δ193). Never logged in full: log its length."""

    # -- change capture for the index (USER_DRIVE_INDEXING_RFC §4) --------------------------

    @abstractmethod
    async def get_changes(self, user_id: str, cursor: Optional[str]) -> DriveChanges:
        """One page of the change feed from `cursor` (None: a full enumeration from scratch).
        Raises DriveResyncRequired when the cursor expired, DriveThrottled when the provider keeps
        throttling, DriveReauthRequired when the refresh token is rejected."""

    @abstractmethod
    async def get_change(self, user_id: str, item_id: str) -> DriveChange:
        """One item as an observation, read by id. Raises DriveItemNotFoundError when the provider answers "not found":
        never a deletion, because a provider may report "no access" as "not found" for an item its change feed still
        lists (USER_DRIVE_INDEXING_RFC §26.2, Δ48). Existence is decided by the change feed; the caller decides what a
        "not found" means for its call."""

    @abstractmethod
    async def list_children_changes(self, user_id: str, folder_id: str, limit: int,
                                    cursor: Optional[str] = None) -> DriveChildrenPage:
        """One page (at most `limit`) of a folder's direct children as observations, starting at `cursor`
        (None: the first page). `next_cursor` is None on the last page. Raises DriveItemNotFoundError
        when the folder is gone."""

    @abstractmethod
    async def connection(self, user_id: str) -> DriveConnection:
        """Connected?, refresh rejected earlier?, and the connected drive's id (§10.1 step 0)."""

    @abstractmethod
    async def flag_reconnect(self, user_id: str) -> bool:
        """Mark the stored grant as needing a reconnect; True only on the False->True transition (§7.7)."""

    # -- content for the index (USER_DRIVE_INDEXING_RFC §8.1, §24.5) -----------------------------

    @abstractmethod
    async def download_to_file(self, user_id: str, item_id: str, dest_path: str, max_bytes: int) -> int:
        """Stream a file's content to `dest_path` (never held whole in memory) and return the bytes written. Raises
        DriveFileTooLarge as soon as the stream passes `max_bytes`, DriveItemNotFoundError for a missing item."""

    @abstractmethod
    async def download_url(self, user_id: str, item_id: str) -> str:
        """The provider's short-lived, pre-authenticated download URL for a file, for a tool that streams it (ffmpeg,
        USER_DRIVE_INDEXING_RFC §27.4). The URL is a secret: never log it, never put it in a document or an error.
        Raises DriveItemNotFoundError for a missing item or one with no downloadable content."""

    @abstractmethod
    async def get_thumbnail(self, user_id: str, item_id: str, size: str) -> Optional[bytes]:
        """A provider-rendered image of the item (`size` e.g. "medium" or "c2048x2048"), JPEG or PNG; None when the
        provider has no rendition for it. Raises DriveThumbnailTooLarge when the rendition passes the vision byte cap.
        The indexer never decodes or scales an image itself (§5.3)."""
