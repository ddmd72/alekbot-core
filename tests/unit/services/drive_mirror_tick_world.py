"""A tick harness with the transcript mirror's repository wired (USER_DRIVE_INDEXING_RFC §27.9; plan Task 20B): the real
DriveIndexingService and DriveTranscriptStage on the in-memory repositories, a spying mirror repository and one event log
shared with the record writes, so a test can see the marks land before the deletes. Synthetic items only."""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

from src.adapters.in_memory_drive_mirror_repo import InMemoryDriveMirrorRepository
from src.domain.drive_index import DriveChange
from src.domain.drive_index_params import TRANSCRIBE_USD_PER_MONTH
from src.domain.drive_mirror import KIND_FILE, KIND_FOLDER, MirrorDoc, MirrorRoot, doc_key
from src.domain.drive_mirror_plan import stamped
from src.services.drive_indexing_service import DriveIndexingService
from src.services.drive_transcript_stage import DriveTranscriptStage

from .test_drive_indexing_transcripts import SPACE, StageSpy, TranscriptWorld
from .drive_index_fakes import LegacyConnections

DRIVE = "d1"   # the drive id FakeDrive's pages and connection carry
MIRROR_WRITES = ("save_root", "mark_root_gone", "clear_root_gone", "save_doc", "delete_doc", "mark_gone", "clear_mark",
                 "hold_mark")


class SpyMirror(InMemoryDriveMirrorRepository):
    """Every call by name (`calls`), with the mark calls' arguments (`marks`); write calls also go to the shared event
    log. `fail_roots`: the next `roots` raises it."""

    def __init__(self, events: List[Tuple[str, Any]]) -> None:
        super().__init__()
        self.events = events
        self.calls: List[str] = []
        self.marks: List[Tuple[Any, ...]] = []
        self.fail_roots: Optional[Exception] = None

    def _note(self, name: str, detail: Any = None) -> None:
        self.calls.append(name)
        if name in MIRROR_WRITES:
            self.events.append((name, detail))

    async def roots(self, user_id: str):
        self._note("roots")
        if self.fail_roots is not None:
            exc, self.fail_roots = self.fail_roots, None
            raise exc
        return await super().roots(user_id)

    async def get_root(self, *a: Any, **kw: Any):
        self._note("get_root")
        return await super().get_root(*a, **kw)

    async def save_root(self, root):
        self._note("save_root")
        return await super().save_root(root)

    async def mark_root_gone(self, user_id, root_item_id, at):
        self._note("mark_root_gone", root_item_id)
        self.marks.append(("mark_root_gone", root_item_id))
        return await super().mark_root_gone(user_id, root_item_id, at)

    async def clear_root_gone(self, user_id, root_item_id):
        self._note("clear_root_gone", root_item_id)
        self.marks.append(("clear_root_gone", root_item_id))
        return await super().clear_root_gone(user_id, root_item_id)

    async def get_docs(self, *a: Any, **kw: Any):
        self._note("get_docs")
        return await super().get_docs(*a, **kw)

    async def save_doc(self, doc):
        self._note("save_doc")
        return await super().save_doc(doc)

    async def delete_doc(self, *a: Any, **kw: Any):
        self._note("delete_doc")
        return await super().delete_doc(*a, **kw)

    async def mark_gone(self, user_id, marks, *, drive_id=""):
        notices = tuple((m.item_id, m.why) for m in marks)
        self._note("mark_gone", notices)
        self.marks.append(("mark_gone", notices, drive_id))
        return await super().mark_gone(user_id, marks, drive_id=drive_id)

    async def clear_mark(self, *a: Any, **kw: Any):
        self._note("clear_mark")
        return await super().clear_mark(*a, **kw)

    async def hold_mark(self, *a: Any, **kw: Any):
        self._note("hold_mark")
        return await super().hold_mark(*a, **kw)

    async def pinned(self, user_id, content_key):
        self._note("pinned")
        return await super().pinned(user_id, content_key)

    async def pinned_keys(self, user_id):
        self._note("pinned_keys")
        return await super().pinned_keys(user_id)

    def writes(self) -> List[str]:
        return [c for c in self.calls if c in MIRROR_WRITES]


class MirrorWorld(TranscriptWorld):
    """TranscriptWorld with the mirror repository handed to the service and the stage (`wired=False`: neither)."""

    def __init__(self, tmp_path: Any, *, wired: bool = True, switch: bool = True, flag: bool = True,
                 enrich: bool = False, cap: float = TRANSCRIBE_USD_PER_MONTH,
                 tick_cap: float = TRANSCRIBE_USD_PER_MONTH) -> None:
        super().__init__(tmp_path, switch=switch, flag=flag, enrich=enrich, cap=cap, tick_cap=tick_cap)
        self.events: List[Tuple[str, Any]] = []
        self.mirror = SpyMirror(self.events)
        repo = self.mirror if wired else None
        self.stage = DriveTranscriptStage(self.repo, self.urls, self.media, self.batch, self.runner, clock=self.clock,
                                          monotonic=self.clock.monotonic, monthly_cap_usd=cap,
                                          temp_root=str(tmp_path), mirror=repo)
        self.stage_spy = StageSpy(self.stage)
        self.svc = DriveIndexingService(
            self.drive, self.repo, self.emb, self.queue, self.users, lambda lat, lon: "Rome (Roma), Italy", SPACE,
            extractors={"parser": self.parser, "reader": self.reader}, triage=self.triage,
            profiler=self.profiler if enrich else None, passages=self.passages if enrich else None,
            transcripts=self.stage, transcribe_enabled=switch, transcribe_cap_usd=tick_cap, mirror_repo=repo,
            connections=LegacyConnections(self.users, self.drive), clock=self.clock, monotonic=self.clock.monotonic)
        self.commits: List[Tuple[Tuple[str, ...], Optional[dict]]] = []   # (deletes, gone) of every _commit with deletes
        real_commit = self.svc._commit

        async def commit(user_id, writes, deletes, *a: Any, **kw: Any):
            if deletes:
                gone = kw.get("gone")
                self.commits.append((tuple(sorted(deletes)), dict(gone) if gone is not None else None))
            return await real_commit(user_id, writes, deletes, *a, **kw)
        self.svc._commit = commit
        real_upsert = self.repo.upsert_records
        real_delete = self.repo.delete_records

        async def upsert(records, vectors=None):
            self.events.append(("upsert_records", tuple((r.item_id, r.state.value) for r in records)))
            return await real_upsert(records, vectors)

        async def delete(user_id, item_ids):
            self.events.append(("delete_records", tuple(item_ids)))
            return await real_delete(user_id, item_ids)
        self.repo.upsert_records = upsert
        self.repo.delete_records = delete

    # -- the mirror's side, written directly (as the mirror step of Task 21 would) --------------------------------

    def add_root(self, name: str = "_Transcripts", *, previous: Tuple[str, ...] = (), drive: str = DRIVE,
                 root_item_id: Optional[str] = None) -> str:
        item = root_item_id or self.drive.add(name, folder=True)
        self.mirror.root_docs[("u", drive)] = MirrorRoot(user_id="u", drive_id=drive, root_item_id=item,
                                                        root_name=name, previous_root_ids=previous)
        return item

    def mirrored(self, item_id: str, *, drive: str = DRIVE, content_key: str = "", state: str = "active") -> MirrorDoc:
        """The record stamped as the mirror step stamps it, and its ledger document."""
        rec = self.rec(item_id)
        assert rec is not None, item_id
        self.repo.records[("u", item_id)] = stamped(rec)
        doc = MirrorDoc(user_id="u", drive_id=drive, kind=KIND_FOLDER if rec.is_folder else KIND_FILE,
                        source_item_id=item_id, content_key=content_key or rec.transcript_key,
                        mirror_item_id=f"mf-{item_id}", state=state)
        self.mirror.docs[doc.key] = doc
        return doc

    def doc(self, item_id: str, kind: str = KIND_FILE) -> Optional[MirrorDoc]:
        return self.mirror.docs.get(doc_key("u", kind, item_id))

    def root_doc(self, drive: str = DRIVE) -> Optional[MirrorRoot]:
        return self.mirror.root_docs.get(("u", drive))

    def records_under(self, folder_id: str) -> List[str]:
        """Item ids of stored records whose parent chain reaches `folder_id`."""
        found = []
        for (u, item), rec in self.repo.records.items():
            parent, seen = rec.parent_id, 0
            while parent and seen < 20:
                if parent == folder_id:
                    found.append(item)
                    break
                nxt = self.repo.records.get(("u", parent))
                parent, seen = (nxt.parent_id if nxt is not None else ""), seen + 1
        return found

    def silent_drop(self, item_id: str) -> None:
        """The item leaves the drive with no tombstone (only a full enumeration can see it is gone)."""
        self.drive.items.pop(item_id, None)

    def tombstone_only(self, item_id: str) -> None:
        """A tombstone of the item alone, its children left as they are (a folder whose children the feed has not
        re-emitted): the folder cascade removes them (§7.6)."""
        self.drive.items.pop(item_id, None)
        self.drive.log.append(DriveChange(item_id=item_id, deleted=True))
