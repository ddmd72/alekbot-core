"""Fakes for the transcript mirror step (USER_DRIVE_INDEXING_RFC §27.9.5; plan Task 21A). Synthetic items only.

- `ScriptedDrive`: a `UserDrivePort` holding a tree, with Graph's write semantics as S1 measured them (fail on a taken
  name, case-insensitive after NFC; PATCH fails on a taken name; 404 for a missing item or parent), recording every call,
  with scriptable faults (an exception before a call) and crash points (`Crash`, a BaseException no `except Exception`
  catches: the process dies before or after the call).
- `FakeMirrorHost`: the tick's side, with budgets, a settable clock and a commit that applies `mirror_plan`.
- `MirrorWorld`: the in-memory repositories, the drive and the service, with helpers that write records the way the tick
  does (through `mirror_plan`) and deletion marks the way `_commit` does.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Type

from src.adapters.microsoft.onedrive_adapter import onedrive_drive_identity, onedrive_item_identity
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.adapters.in_memory_drive_mirror_repo import InMemoryDriveMirrorRepository
from src.domain.drive_capabilities import DriveCapabilities
from src.domain.drive_handle import DriveHandle
from src.domain.drive_media_source import MediaSource
from src.domain.drive_index import (
    DriveChange, DriveChanges, DriveChildrenPage, DriveIndexRecord, DriveSyncState, Reason, RecordState,
)
from src.domain.drive_mirror import GONE, KIND_FILE, KIND_FOLDER, DeletionNotice, MirrorDoc, doc_key
from src.domain.drive_mirror_plan import mirror_plan
from src.domain.drive_mirror_render import MirrorStrings
from src.domain.drive_transcript import Segment, StitchedTranscript, StoredTranscript
from src.domain.user_drive import (
    DriveItem, DriveItemNotFoundError, DriveNameConflictError, name_key,
)
from src.ports.user_drive_port import UserDrivePort
from src.services.drive_transcript_mirror import DriveTranscriptMirror

USER = "user-1"
DRIVE = "drive-1"
AREA = "area-root"
AREA_PATH = "Apps/Alek"
START = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)
WRITES = ("upload", "replace_content", "move", "create_folder", "delete")

EN = MirrorStrings(
    open_recording="open the recording",
    managed_line="Managed by the bot: this file may be rewritten. Automatic transcript, may contain errors.",
    duration_minutes="{n} min", speakers_one="{n} speaker", speakers_many="{n} speakers",
    stub_no_speech="No speech was detected in this recording ({duration}).",
    stub_too_long="This recording is too long to transcribe ({duration}).",
    stub_too_large="This recording is too large to transcribe.",
    stub_no_audio="This recording has no audio track.",
    readme="# Transcripts\n\nManaged by the bot: files here may be rewritten; do not edit them. Transcripts of "
           "deleted recordings are in Archive/.",
)
UK = replace(EN, open_recording="відкрити запис",
             readme="# Розшифровки\n\nКерується ботом: файли тут можуть бути переписані; не редагуйте їх. "
                    "Розшифровки видалених записів лежать в Archive/.")


class Crash(BaseException):
    """The process dies at a scripted point: nothing under test catches it."""


@dataclass
class Node:
    item_id: str
    name: str
    parent_id: str
    is_folder: bool
    data: bytes = b""
    ctag: str = ""
    created_at: Optional[datetime] = None


@dataclass
class Fault:
    method: str
    action: Any                                   # an exception instance, "crash_before" or "crash_after"
    when: Callable[[Dict[str, Any]], bool]
    times: int = 1


class ScriptedDrive(UserDrivePort):
    def __init__(self, area_path: str = AREA_PATH) -> None:
        self.nodes: Dict[str, Node] = {AREA: Node(AREA, "", "", True)}
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        # (method, the ancestor chain of each folder the write touches, taken at the call, and the id a create made):
        # a later prune cannot hide where a write went
        self.write_parents: List[Tuple[str, Tuple[Tuple[str, ...], ...], Optional[str]]] = []
        self.planted: set = set()     # ids the TEST created (sources, foreign items): the step must never write them
        self.faults: List[Fault] = []
        self.clock: datetime = START
        self.area_path_value = area_path
        self._seq = itertools.count(1)
        self.events: Optional[List[Tuple[str, Any]]] = None

    # -- scripting ---------------------------------------------------------------------------------------------

    def fail(self, method: str, exc: BaseException, when: Optional[Callable[[Dict[str, Any]], bool]] = None,
             times: int = 1) -> None:
        self.faults.append(Fault(method, exc, when or (lambda a: True), times))

    def crash_before(self, method: str, when: Optional[Callable[[Dict[str, Any]], bool]] = None) -> None:
        self.faults.append(Fault(method, "crash_before", when or (lambda a: True)))

    def crash_after(self, method: str, when: Optional[Callable[[Dict[str, Any]], bool]] = None) -> None:
        self.faults.append(Fault(method, "crash_after", when or (lambda a: True)))

    def _fault(self, method: str, args: Dict[str, Any], phase: str) -> None:
        for fault in self.faults:
            if fault.method != method or fault.times <= 0 or not fault.when(args):
                continue
            if phase == "before" and fault.action == "crash_after":
                continue
            if phase == "after" and fault.action != "crash_after":
                continue
            fault.times -= 1
            if fault.action in ("crash_before", "crash_after"):
                raise Crash(f"{method} {phase}")
            raise fault.action

    def chain(self, item_id: Optional[str]) -> Tuple[str, ...]:
        out: List[str] = []
        node = self.nodes.get(item_id) if item_id else None
        while node is not None and node.item_id not in out:
            out.append(node.item_id)
            node = self.nodes.get(node.parent_id)
        return tuple(out)

    def _touch(self, method: str, *folders: Optional[str]) -> None:
        self.write_parents.append((method, tuple(self.chain(f) for f in folders if f is not None), None))

    def _created(self, item_id: str) -> None:
        method, chains, _ = self.write_parents[-1]
        self.write_parents[-1] = (method, chains, item_id)

    def _call(self, method: str, **args: Any) -> None:
        self.calls.append((method, args))
        if self.events is not None:
            self.events.append(("drive." + method, args.get("name")))
        self._fault(method, args, "before")

    # -- the tree ----------------------------------------------------------------------------------------------

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._seq)}"

    def add_folder(self, parent: str, name: str, created_at: Optional[datetime] = None) -> str:
        """A folder the test plants (a source folder, a foreign one): remembered in `planted`."""
        item_id = self._make_folder(parent, name, created_at)
        self.planted.add(item_id)
        return item_id

    def add_file(self, parent: str, name: str, data: bytes = b"foreign", created_at: Optional[datetime] = None) -> str:
        """A file the test plants (a recording, a foreign file): remembered in `planted`."""
        item_id = self._make_file(parent, name, data, created_at)
        self.planted.add(item_id)
        return item_id

    def _make_folder(self, parent: str, name: str, created_at: Optional[datetime] = None) -> str:
        item_id = self._new_id("F")
        self.nodes[item_id] = Node(item_id, name, parent, True, ctag=self._ctag(), created_at=created_at or self.clock)
        return item_id

    def _make_file(self, parent: str, name: str, data: bytes = b"foreign",
                   created_at: Optional[datetime] = None) -> str:
        item_id = self._new_id("X")
        self.nodes[item_id] = Node(item_id, name, parent, False, data, self._ctag(), created_at or self.clock)
        return item_id

    def _ctag(self) -> str:
        return f"c{next(self._seq)}"

    def children(self, folder_id: str) -> List[Node]:
        return sorted((n for n in self.nodes.values() if n.parent_id == folder_id and n.item_id != AREA),
                      key=lambda n: n.name)

    def child(self, folder_id: str, name: str) -> Optional[Node]:
        key = name_key(name)
        return next((n for n in self.children(folder_id) if name_key(n.name) == key), None)

    def path_of(self, item_id: str) -> str:
        names: List[str] = []
        node = self.nodes.get(item_id)
        while node is not None and node.item_id != AREA:
            names.append(node.name)
            node = self.nodes.get(node.parent_id)
        return "/".join(reversed(names))

    def find(self, path: str) -> Optional[Node]:
        node: Optional[Node] = self.nodes[AREA]
        for part in [p for p in path.split("/") if p]:
            node = next((n for n in self.children(node.item_id) if n.name == part), None) if node else None
        return node

    def under(self, item_id: str, ancestor: str) -> bool:
        seen = set()
        node = self.nodes.get(item_id)
        while node is not None and node.item_id not in seen:
            if node.item_id == ancestor:
                return True
            seen.add(node.item_id)
            node = self.nodes.get(node.parent_id)
        return False

    def files(self) -> List[str]:
        return sorted(self.path_of(n.item_id) for n in self.nodes.values() if not n.is_folder)

    def writes(self) -> List[Tuple[str, Dict[str, Any]]]:
        return [c for c in self.calls if c[0] in WRITES]

    def _item(self, node: Node) -> DriveItem:
        return DriveItem(node.item_id, node.name, self.path_of(node.item_id), node.is_folder, len(node.data),
                         identity=onedrive_item_identity(DRIVE, node.item_id),
                         drive_identity=onedrive_drive_identity(DRIVE),
                         content_version="" if node.is_folder else node.ctag)

    def _change(self, node: Node) -> DriveChange:
        return DriveChange(node.item_id, parent_id=node.parent_id, name=node.name, is_folder=node.is_folder,
                           size_bytes=len(node.data), content_version=node.ctag, created_at=node.created_at,
                           identity=onedrive_item_identity(DRIVE, node.item_id))

    def _folder(self, folder_id: str) -> Node:
        node = self.nodes.get(folder_id)
        if node is None or not node.is_folder:
            raise DriveItemNotFoundError(folder_id)
        return node

    # -- writes --------------------------------------------------------------------------------------------------

    async def create_folder(self, handle: DriveHandle, parent_id: str, name: str) -> DriveItem:
        self._call("create_folder", parent_id=parent_id, name=name)
        self._touch("create_folder", parent_id)
        self._folder(parent_id)
        if self.child(parent_id, name) is not None:
            raise DriveNameConflictError(name)
        item_id = self._make_folder(parent_id, name)
        self._created(item_id)
        self._fault("create_folder", {"parent_id": parent_id, "name": name}, "after")
        return self._item(self.nodes[item_id])

    async def upload(self, handle: DriveHandle, parent_id: str, filename: str, data: bytes, content_type: str,
                     conflict: str = "rename") -> DriveItem:
        self._call("upload", parent_id=parent_id, name=filename, conflict=conflict, size=len(data))
        self._touch("upload", parent_id)
        self._folder(parent_id)
        existing = self.child(parent_id, filename)
        if existing is not None:
            if conflict == "fail":
                raise DriveNameConflictError(filename)
            if conflict == "replace":
                existing.data, existing.ctag = data, self._ctag()
                return self._item(existing)
            stem, dot, ext = filename.rpartition(".")
            filename = f"{stem} (1).{ext}" if dot else f"{filename} (1)"
        item_id = self._make_file(parent_id, filename, data)
        self._created(item_id)
        self._fault("upload", {"parent_id": parent_id, "name": filename}, "after")
        return self._item(self.nodes[item_id])

    async def replace_content(self, handle: DriveHandle, item_id: str, data: bytes, content_type: str) -> DriveItem:
        node = self.nodes.get(item_id)
        self._call("replace_content", item_id=item_id, size=len(data))
        self._touch("replace_content", node.parent_id if node else None)
        if node is None or node.is_folder:
            raise DriveItemNotFoundError(item_id)
        node.data, node.ctag = data, self._ctag()
        self._fault("replace_content", {"item_id": item_id}, "after")
        return self._item(node)

    async def move(self, handle: DriveHandle, item_id: str, new_parent_id: Optional[str] = None,
                   new_name: Optional[str] = None) -> DriveItem:
        node = self.nodes.get(item_id)
        self._call("move", item_id=item_id, new_parent_id=new_parent_id, name=new_name)
        self._touch("move", node.parent_id if node else None, new_parent_id)
        if node is None:
            raise DriveItemNotFoundError(item_id)
        parent = new_parent_id or node.parent_id
        self._folder(parent)
        name = new_name or node.name
        other = self.child(parent, name)
        if other is not None and other.item_id != item_id:
            raise DriveNameConflictError(name)
        node.parent_id, node.name = parent, name
        self._fault("move", {"item_id": item_id}, "after")
        return self._item(node)

    async def delete(self, handle: DriveHandle, item_id: str) -> None:
        node = self.nodes.get(item_id)
        self._call("delete", item_id=item_id)
        self._touch("delete", node.parent_id if node else None)
        if node is None:
            raise DriveItemNotFoundError(item_id)
        self.remove(item_id)

    def remove(self, item_id: str) -> None:
        for child in [n for n in self.nodes.values() if n.parent_id == item_id]:
            self.remove(child.item_id)
        self.nodes.pop(item_id, None)

    # -- reads ---------------------------------------------------------------------------------------------------

    async def child_by_name(self, handle: DriveHandle, parent_id: str, name: str) -> Optional[DriveChange]:
        self._call("child_by_name", parent_id=parent_id, name=name)
        if parent_id not in self.nodes:
            return None
        node = self.child(parent_id, name)
        return self._change(node) if node is not None else None

    async def area_path(self, handle: DriveHandle) -> str:
        self._call("area_path")
        return self.area_path_value

    async def get_change(self, handle: DriveHandle, item_id: str) -> DriveChange:
        self._call("get_change", item_id=item_id)
        node = self.nodes.get(item_id)
        if node is None or item_id == AREA:
            raise DriveItemNotFoundError(item_id)
        return self._change(node)

    async def list_children_changes(self, handle: DriveHandle, folder_id: str, limit: int,
                                    cursor: Optional[str] = None) -> DriveChildrenPage:
        self._call("list_children_changes", item_id=folder_id, limit=limit)
        self._folder(folder_id)
        kids = self.children(folder_id)
        start = int(cursor or 0)
        page = kids[start:start + limit]
        nxt = str(start + limit) if start + limit < len(kids) else None
        return DriveChildrenPage(tuple(self._change(n) for n in page), nxt)

    async def get_root(self, handle: DriveHandle) -> DriveItem:
        self._call("get_root")
        return DriveItem(AREA, "", "", True, identity=onedrive_item_identity(DRIVE, AREA),
                         drive_identity=onedrive_drive_identity(DRIVE))

    async def get_item(self, handle: DriveHandle, item_id: str) -> DriveItem:
        self._call("get_item", item_id=item_id)
        node = self.nodes.get(item_id)
        if node is None:
            raise DriveItemNotFoundError(item_id)
        return self._item(node)

    # -- unused by the mirror ------------------------------------------------------------------------------------

    async def list_children(self, handle: DriveHandle, folder_id: str) -> List[DriveItem]:
        raise NotImplementedError

    async def search(self, handle: DriveHandle, query: str, limit: int) -> List[DriveItem]:
        raise NotImplementedError

    async def download(self, handle: DriveHandle, item_id: str) -> bytes:
        raise NotImplementedError

    async def get_changes(self, handle: DriveHandle, cursor: Optional[str]) -> DriveChanges:
        raise NotImplementedError

    async def download_to_file(self, handle: DriveHandle, item_id: str, dest_path: str, max_bytes: int) -> int:
        raise NotImplementedError

    async def get_thumbnail(self, handle: DriveHandle, item_id: str, max_edge_px: int) -> Optional[bytes]:
        raise NotImplementedError

    async def media_source(self, handle: DriveHandle, item_id: str) -> MediaSource:
        raise NotImplementedError

    async def capabilities(self, handle: DriveHandle) -> DriveCapabilities:
        return DriveCapabilities(max_path_chars=400)

    async def forget(self, handle: DriveHandle) -> None:
        self._call("forget")


@dataclass
class FakeMirrorHost:
    repo: InMemoryDriveIndexRepository
    clock: datetime = START
    drive_id: str = DRIVE
    tz_name: str = "UTC"
    language: str = "en"
    round_complete: bool = True
    epoch_idle: bool = True
    ops: int = 40
    reads: int = 10
    stamps: int = 200
    seconds: float = 100.0
    texts: MirrorStrings = EN
    commits: List[List[DriveIndexRecord]] = field(default_factory=list)
    fences: int = 0
    events: Optional[List[Tuple[str, Any]]] = None
    root_marks: List[Tuple[str, bool]] = field(default_factory=list)   # `root_mark_changed` calls, in order
    stop_exceptions: Tuple[Type[BaseException], ...] = ()   # MirrorHost: no host stop signal of its own

    def fence(self) -> None:
        self.fences += 1

    async def commit(self, records: Sequence[DriveIndexRecord]) -> None:
        self.commits.append(list(records))
        if self.events is not None:
            self.events.append(("commit", tuple(r.item_id for r in records)))
        await self.repo.upsert_records([mirror_plan(r) for r in records])

    def ops_left(self) -> int:
        return self.ops

    def spend_op(self) -> None:
        self.ops -= 1

    def reads_left(self) -> int:
        return self.reads

    def spend_read(self) -> None:
        self.reads -= 1

    def stamps_left(self) -> int:
        return self.stamps

    def spend_stamp(self) -> None:
        self.stamps -= 1

    def time_left(self) -> float:
        return self.seconds

    def now(self) -> datetime:
        return self.clock

    def day(self) -> str:
        return self.clock.date().isoformat()

    def strings(self) -> MirrorStrings:
        return self.texts

    def root_mark_changed(self, root_item_id: str, gone: bool) -> None:
        self.root_marks.append((root_item_id, gone))


class EventMirror(InMemoryDriveMirrorRepository):
    """The in-memory ledger, logging its writes into a shared event list."""

    def __init__(self, events: List[Tuple[str, Any]]) -> None:
        super().__init__()
        self.events = events

    async def save_root(self, root):  # type: ignore[no-untyped-def]
        self.events.append(("save_root", root.root_item_id))
        await super().save_root(root)

    async def save_doc(self, doc):  # type: ignore[no-untyped-def]
        self.events.append(("save_doc", doc.key))
        await super().save_doc(doc)


def stored(key: str, text: str = "hello there", speakers: int = 2) -> StoredTranscript:
    segs = (Segment(0.0, 1, text), Segment(65.0, 2, "and the answer"))
    return StoredTranscript(key, StitchedTranscript(segs, ("en",), speakers, 120.0))


class MirrorWorld:
    """The repositories, the drive and the service; helpers write records through `mirror_plan` as `_commit` does."""

    def __init__(self, area_path: str = AREA_PATH) -> None:
        self.events: List[Tuple[str, Any]] = []
        self.repo = InMemoryDriveIndexRepository()
        self.mirror = EventMirror(self.events)
        self.drive = ScriptedDrive(area_path)
        self.drive.events = self.events
        self.clock = START
        self.repo.states[USER] = DriveSyncState(USER, drive_id=DRIVE, root_item_id=AREA)
        self.service = DriveTranscriptMirror(self.repo, self.mirror, self.drive)
        self.last_host: Optional[FakeMirrorHost] = None

    def host(self, **kw: Any) -> FakeMirrorHost:
        kw.setdefault("clock", self.clock)
        return FakeMirrorHost(self.repo, events=self.events, **kw)

    async def run(self, **kw: Any) -> Dict[str, int]:
        host = self.host(**kw)
        self.last_host = host
        return await self.service.run(USER, host)

    def advance(self, delta: timedelta) -> None:
        self.clock += delta
        self.drive.clock = self.clock

    # -- records -------------------------------------------------------------------------------------------------

    async def put(self, rec: DriveIndexRecord) -> DriveIndexRecord:
        planned = mirror_plan(rec)
        await self.repo.upsert_records([planned])
        return planned

    def rec(self, item_id: str) -> Optional[DriveIndexRecord]:
        return self.repo.records.get((USER, item_id))

    async def folder(self, name: str, parent: str = AREA) -> str:
        item_id = self.drive.add_folder(parent, name)
        await self.put(DriveIndexRecord(USER, item_id, parent_id=parent, name=name, is_folder=True,
                                        state=RecordState.METADATA_ONLY, reason=Reason.FOLDER))
        return item_id

    async def recording(self, name: str, parent: str = AREA, text: str = "hello there", day: str = "2026-10-01",
                        stub: str = "", key: Optional[str] = None, store: bool = True) -> str:
        item_id = self.drive.add_file(parent, name, b"audio")
        content_key = key or f"k-{item_id}"
        if store:
            st = StoredTranscript(content_key, None, stub=stub) if stub else stored(content_key, text)
            await self.repo.put_transcript(USER, st)
        await self.put(DriveIndexRecord(
            USER, item_id, parent_id=parent, name=name, web_url=f"https://example.invalid/{item_id}",
            kind="audio", state=RecordState.INDEXED, effective_day=day, transcript_state="current",
            transcript_key=content_key, transcript_version=1, transcript_stub=stub, transcript_seconds=120,
            transcript_speakers=0 if stub else 2, transcript_languages=() if stub else ("en",)))
        return item_id

    async def update(self, item_id: str, **changes: Any) -> DriveIndexRecord:
        rec = self.rec(item_id)
        assert rec is not None
        if "name" in changes or "parent_id" in changes:
            node = self.drive.nodes.get(item_id)
            if node is not None:
                node.name = changes.get("name", node.name)
                node.parent_id = changes.get("parent_id", node.parent_id)
        return await self.put(replace(rec, **changes))

    async def delete_source(self, item_id: str, why: str = GONE, on_drive: bool = True) -> None:
        """What `_commit` does for a deleted mirrored record: the mark first, then the record delete."""
        await self.mirror.mark_gone(USER, [DeletionNotice(item_id, why, self.clock)], drive_id=DRIVE)
        await self.repo.delete_records(USER, [item_id])
        if on_drive and why == GONE:
            self.drive.remove(item_id)

    async def restore_source(self, rec: DriveIndexRecord) -> DriveIndexRecord:
        """A recycle-bin restore: the same item id is observed again as a new record (no mirrored digest)."""
        self.drive.nodes.setdefault(rec.item_id, Node(rec.item_id, rec.name, rec.parent_id, rec.is_folder))
        return await self.put(replace(rec, mirrored_digest="", mirror_due=False, mirror_retry_at=None,
                                      mirror_held_digest=""))

    # -- the mirror ----------------------------------------------------------------------------------------------

    @property
    def root(self):  # type: ignore[no-untyped-def]
        return self.mirror.root_docs.get((USER, DRIVE))

    def doc(self, item_id: str, kind: str = KIND_FILE) -> Optional[MirrorDoc]:
        return self.mirror.docs.get(doc_key(USER, kind, item_id))

    def folder_doc(self, item_id: str) -> Optional[MirrorDoc]:
        return self.doc(item_id, KIND_FOLDER)

    def mirror_path(self, item_id: str, kind: str = KIND_FILE) -> Optional[str]:
        """The drive path of the document's item, relative to the root."""
        doc = self.doc(item_id, kind)
        if doc is None or not doc.mirror_item_id or doc.mirror_item_id not in self.drive.nodes:
            return None
        full = self.drive.path_of(doc.mirror_item_id)
        root = self.drive.path_of(self.root.root_item_id)
        return full[len(root) + 1:] if full.startswith(root + "/") else full

    def content(self, item_id: str) -> str:
        doc = self.doc(item_id)
        assert doc is not None
        return self.drive.nodes[doc.mirror_item_id].data.decode("utf-8")

    def write_calls(self) -> List[Tuple[str, Dict[str, Any]]]:
        return self.drive.writes()

    def reset_calls(self) -> None:
        self.drive.calls.clear()
        self.drive.write_parents.clear()
        self.events.clear()
