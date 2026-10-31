"""A tick harness for files the bot cannot open (USER_DRIVE_INDEXING_RFC §26.2, D33): the content world's drive, where
an item in `closed` is still listed by delta but answers "not found" to every id-addressed call, as Graph does for an
item whose own permission list lacks the app's entry. `get_change` follows the port contract of §26.2 (Δ48): a
missing or closed item raises DriveItemNotFoundError, it is never reported as a deletion."""
from __future__ import annotations

from typing import List, Optional, Set

from src.domain.drive_index import DriveChange, DriveChildrenPage
from src.domain.user_drive import DriveItemNotFoundError
from src.services.drive_indexing_service import DriveIndexingService

from .drive_content_fakes import FakeContentDrive
from .drive_content_world import ContentWorld
from .drive_index_fakes import LegacyConnections


class ClosedDrive(FakeContentDrive):
    def __init__(self, page_size: int = 50, reemit_parents: bool = False) -> None:
        super().__init__(page_size=page_size, reemit_parents=reemit_parents)
        self.closed: Set[str] = set()
        self.get_change_calls: List[str] = []
        self.listing_calls: List[str] = []

    def _check(self, item_id: str) -> None:
        if item_id in self.closed:
            raise DriveItemNotFoundError(item_id)

    async def get_change(self, user_id: str, item_id: str) -> DriveChange:
        self.get_change_calls.append(item_id)
        self._check(item_id)
        found = self.items.get(item_id)
        if found is None:
            raise DriveItemNotFoundError(item_id)
        return found

    async def download_to_file(self, user_id: str, item_id: str, dest_path: str, max_bytes: int) -> int:
        self._check(item_id)
        return await super().download_to_file(user_id, item_id, dest_path, max_bytes)

    async def get_thumbnail(self, user_id: str, item_id: str, size: str) -> Optional[bytes]:
        self.thumbnail_calls.append((item_id, size))
        self._check(item_id)
        return await super().get_thumbnail(user_id, item_id, size)

    async def list_children_changes(self, user_id: str, folder_id: str, limit: int,
                                    cursor: Optional[str] = None) -> DriveChildrenPage:
        self.listing_calls.append(folder_id)
        self._check(folder_id)
        return await super().list_children_changes(user_id, folder_id, limit, cursor)

    def calls_for(self, item_id: str) -> int:
        return self.get_change_calls.count(item_id)


class ClosedWorld(ContentWorld):
    def __init__(self, page_size: int = 50) -> None:
        super().__init__(page_size=page_size)
        self.drive = ClosedDrive(page_size=page_size)
        self.svc = DriveIndexingService(self.drive, self.repo, self.emb, self.queue, self.users,
                                        lambda lat, lon: None, "test-space/8",
                                        extractors={"parser": self.parser, "reader": self.reader},
                                        triage=self.triage, connections=LegacyConnections(self.users, self.drive), clock=self.clock, monotonic=self.clock.monotonic)
