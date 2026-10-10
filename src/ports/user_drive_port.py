"""
UserDrivePort — the user's long-term file area (docs/10_rfcs/USER_DRIVE_RFC.md §4.2).

Provider-neutral: ids are opaque, paths are relative to the area root and readable
(never percent-encoded). An implementation raises DriveNotConnectedError when access is
missing or expired, DriveItemNotFoundError for a missing item and DriveNameConflictError
when a create hits a taken name.
"""
from abc import ABC, abstractmethod
from typing import List, Optional

from src.domain.user_drive import DriveItem


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
    async def upload(self, user_id: str, parent_id: str, filename: str, data: bytes, content_type: str) -> DriveItem:
        """New file in `parent_id`; never overwrites (a clash gets a provider-suffixed name)."""

    @abstractmethod
    async def replace_content(self, user_id: str, item_id: str, data: bytes, content_type: str) -> DriveItem:
        """Overwrite an existing file's content, keeping its id and name."""

    @abstractmethod
    async def move(self, user_id: str, item_id: str,
                   new_parent_id: Optional[str] = None, new_name: Optional[str] = None) -> DriveItem:
        """Move and/or rename; the id is unchanged."""

    @abstractmethod
    async def create_folder(self, user_id: str, parent_id: str, name: str) -> DriveItem:
        """Create one folder; DriveNameConflictError if the name is taken."""

    @abstractmethod
    async def delete(self, user_id: str, item_id: str) -> None:
        """Delete a file or folder (the provider keeps it recoverable)."""
