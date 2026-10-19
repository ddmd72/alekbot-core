"""Fakes for the drive indexing tick: a scripted drive with a change log, a task queue with Cloud Tasks
name reservation, deterministic embeddings, a user flag store and a controllable clock."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from src.domain.drive_index import DriveChange, DriveChanges, DriveChildrenPage
from src.domain.user_drive import DriveConnection, DriveItemNotFoundError, DriveResyncRequired

ROOT = "root"
T0 = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0
        self.mono = 0.0

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.mono += seconds


class FakeDrive:
    """Items keyed by id; every mutation appends to a log that incremental delta replays."""

    def __init__(self, page_size: int = 50, reemit_parents: bool = False) -> None:
        self.reemit_parents = reemit_parents   # Graph re-emits a folder whose children changed (S0a)
        self.items: Dict[str, DriveChange] = {}
        self.log: List[DriveChange] = []
        self.page_size = page_size
        self.connection_state = DriveConnection(connected=True, drive_id="d1")
        self.resync_next = False
        self.raise_next: Optional[Exception] = None
        self.flagged = 0
        self.children_calls: List[str] = []
        self._seq = 0

    # -- mutations -------------------------------------------------------------------------------
    def _id(self) -> str:
        self._seq += 1
        return f"i{self._seq:05d}"

    def add(self, name: str, parent: str = ROOT, folder: bool = False, item_id: Optional[str] = None,
            **kw: Any) -> str:
        item_id = item_id or self._id()
        ch = DriveChange(item_id=item_id, name=name, parent_id=parent, is_folder=folder,
                         content_version="" if folder else "c1", modified_at=T0, fs_modified_at=T0, **kw)
        self.items[item_id] = ch
        self.log.append(ch)
        self._touch_parent(parent)
        return item_id

    def rename(self, item_id: str, name: str) -> None:
        self.items[item_id] = replace(self.items[item_id], name=name)
        self.log.append(self.items[item_id])

    def move(self, item_id: str, parent: str) -> None:
        self.items[item_id] = replace(self.items[item_id], parent_id=parent)
        self.log.append(self.items[item_id])

    def delete(self, item_id: str) -> None:
        """S0a: a deleted folder yields a tombstone for every descendant, the folder included."""
        for kid in [i for i, c in self.items.items() if c.parent_id == item_id]:
            self.delete(kid)
        self.items.pop(item_id, None)
        self.log.append(DriveChange(item_id=item_id, deleted=True))

    def reemit(self, item_id: str) -> None:
        self.log.append(self.items[item_id])

    def _touch_parent(self, parent: str) -> None:
        if parent in self.items:
            p = self.items[parent]
            self.items[parent] = replace(p, child_count=sum(1 for c in self.items.values() if c.parent_id == parent))
            if self.reemit_parents:
                self.log.append(self.items[parent])

    # -- port --------------------------------------------------------------------------------------
    async def connection(self, user_id: str) -> DriveConnection:
        return self.connection_state

    async def flag_reconnect(self, user_id: str) -> bool:
        self.flagged += 1
        return self.flagged == 1

    async def get_changes(self, user_id: str, cursor: Optional[str]) -> DriveChanges:
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        if self.resync_next and cursor:
            self.resync_next = False
            raise DriveResyncRequired("full:0")
        if cursor is None or cursor.startswith("full:"):
            start = int(cursor.split(":")[1]) if cursor else 0
            snapshot = sorted(self.items.values(), key=lambda c: c.item_id)
            page = snapshot[start:start + self.page_size]
            end = start + self.page_size >= len(snapshot)
            nxt = f"delta:{len(self.log)}" if end else f"full:{start + self.page_size}"
            return DriveChanges(tuple(page), nxt, end, True, "d1", ROOT)
        pos = int(cursor.split(":")[1])
        page = self.log[pos:pos + self.page_size]
        end = pos + self.page_size >= len(self.log)
        return DriveChanges(tuple(page), f"delta:{pos + len(page)}", end, False, "d1", ROOT)

    async def get_change(self, user_id: str, item_id: str) -> DriveChange:
        # the UserDrivePort contract of USER_DRIVE_INDEXING_RFC §26.2 (Δ48): "not found" by id raises, never a deletion
        found = self.items.get(item_id)
        if found is None:
            raise DriveItemNotFoundError(item_id)
        return found

    async def list_children_changes(self, user_id: str, folder_id: str, limit: int,
                                    cursor: Optional[str] = None) -> DriveChildrenPage:
        if folder_id not in self.items and folder_id != ROOT:
            raise DriveItemNotFoundError(folder_id)
        self.children_calls.append(folder_id)
        kids = sorted((c for c in self.items.values() if c.parent_id == folder_id), key=lambda c: c.item_id)
        start = int(cursor) if cursor else 0
        nxt = str(start + limit) if start + limit < len(kids) else None
        return DriveChildrenPage(tuple(kids[start:start + limit]), nxt)


class FakeQueue:
    def __init__(self) -> None:
        self.tasks: List[Dict[str, Any]] = []
        self.reserved: set = set()

    async def enqueue_worker_task(self, task_type: str, payload: Dict[str, Any], delay_seconds: int = 0,
                                  deadline_seconds: Optional[int] = None,
                                  dedup_key: Optional[str] = None) -> Optional[str]:
        if dedup_key is not None:
            if dedup_key in self.reserved:
                return None
            self.reserved.add(dedup_key)
        self.tasks.append({"task_type": task_type, "payload": payload, "delay": delay_seconds,
                           "deadline": deadline_seconds, "key": dedup_key})
        return f"t{len(self.tasks)}"


class FakeEmbeddings:
    def __init__(self) -> None:
        self.calls: List[str] = []

    async def get_embedding(self, text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
        self.calls.append(text)
        digest = hashlib.sha256(text.encode()).digest()
        return [b / 255.0 for b in digest[:8]]

    async def get_embeddings_batch(self, texts: List[str], task_type: str = "RETRIEVAL_DOCUMENT") -> List[List[float]]:
        return [await self.get_embedding(t, task_type) for t in texts]


@dataclass
class FakeUsers:
    enabled: Dict[str, bool] = field(default_factory=lambda: {"u": True})
    reads: int = 0

    async def get_user(self, user_id: str) -> Any:
        self.reads += 1
        if user_id not in self.enabled:
            return None
        return SimpleNamespace(user_id=user_id, account_id="a",
                               config=SimpleNamespace(drive_indexing_enabled=self.enabled[user_id]))
