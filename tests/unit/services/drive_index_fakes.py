"""Fakes for the drive indexing tick: a scripted drive with a change log, a task queue with Cloud Tasks
name reservation, deterministic embeddings, a user flag store and a controllable clock."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import DriveChange, DriveChanges, DriveChildrenPage
from src.domain.provider_account import ProviderAccount
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

    def __init__(self, page_size: int = 50, reemit_parents: bool = False, drive_identity: str = "fake:d1") -> None:
        self.reemit_parents = reemit_parents   # Graph re-emits a folder whose children changed (S0a)
        self.drive_identity = drive_identity   # the adapter-built drive identity every page names (MULTI_DRIVE_RFC §7.2)
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
        kw.setdefault("identity", f"{self.drive_identity}:{item_id}")   # adapter-built, unique across providers
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

    async def get_changes(self, handle: Any, cursor: Optional[str]) -> DriveChanges:
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
            return DriveChanges(tuple(page), nxt, end, True, ROOT, drive_identity=self.drive_identity)
        pos = int(cursor.split(":")[1])
        page = self.log[pos:pos + self.page_size]
        end = pos + self.page_size >= len(self.log)
        return DriveChanges(tuple(page), f"delta:{pos + len(page)}", end, False, ROOT,
                            drive_identity=self.drive_identity)

    async def get_change(self, handle: Any, item_id: str) -> DriveChange:
        # the UserDrivePort contract of USER_DRIVE_INDEXING_RFC §26.2 (Δ48): "not found" by id raises, never a deletion
        found = self.items.get(item_id)
        if found is None:
            raise DriveItemNotFoundError(item_id)
        return found

    async def list_children_changes(self, handle: Any, folder_id: str, limit: int,
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


def legacy_account(user_id: str) -> ProviderAccount:
    """The provider account of a single-drive harness's connection (`LegacyConnections`): the initial build's key."""
    return ProviderAccount(realm="fake", subject=f"sub-{user_id}", label="")


class LegacyConnections:
    """The single-drive harnesses' connection, in the shape the tick reads now (MULTI_DRIVE_RFC §8): one connection per
    user whose id IS the user id (so every stored key reads as before), its switches from the user's profile flags
    (`drive_indexing_enabled`, `drive_transcribe_enabled`) and its state and drive identity from the scripted drive's
    `connection_state` (connected and not flagged: ACTIVE; otherwise NEEDS_RECONNECT; drive `d1` -> `fake:d1`).
    `flag_reconnect` counts on the drive's `flagged`, as the drive's own flag did. Read live, so a test that flips a
    flag or the drive's state mid-test is seen by the next read, as before. The connection holds the provider account
    activation stores (`legacy_account(user_id)`), so its initial build is open as the user's one build was."""

    def __init__(self, users: Any, drive: Any) -> None:
        self.users = users
        self.drive = drive

    async def get(self, user_id: str, connection_id: str) -> Optional[Any]:
        from src.domain.drive_connection import DriveConnection as Connection
        from src.domain.drive_connection_state import ConnectionState
        profile = await self.users.get_user(user_id)
        if profile is None or connection_id != user_id:
            return None
        legacy = getattr(self.drive, "connection_state", None)
        ok = legacy is None or (legacy.connected and not legacy.needs_reconnect)
        drive_id = (getattr(legacy, "drive_id", "") or "d1") if legacy is not None else "d1"
        return Connection(connection_id=user_id, user_id=user_id, account_id=str(getattr(profile, "account_id", "a")),
                          credential_id="cred", kind="fake", drive_identity=f"fake:{drive_id}", ref_tag="a1",
                          indexing_enabled=bool(getattr(profile.config, "drive_indexing_enabled", False)),
                          transcribe_enabled=bool(getattr(profile.config, "drive_transcribe_enabled", False)),
                          state=ConnectionState.ACTIVE if ok else ConnectionState.NEEDS_RECONNECT, created_at=T0,
                          provider_account=legacy_account(user_id))

    async def list_for_user(self, user_id: str, *, include_disconnected: bool = False) -> List[Any]:
        found = await self.get(user_id, user_id)
        return [found] if found is not None else []

    async def flag_reconnect(self, handle: Any) -> bool:
        flag = getattr(self.drive, "flag_reconnect", None)
        return bool(await flag(handle.user_id)) if flag is not None else False

    async def reconnect_needed(self, user_id: str) -> List[Any]:
        return []

# The single-drive harnesses' connection scope: user "u", whose connection id is "u" (`LegacyConnections`)
U_SCOPE = ConnectionScope(user_id="u", connection_id="u")
