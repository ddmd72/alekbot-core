"""The drive-notes invariant matrix (DRIVE_FILE_NOTES_RFC §6.1, §8; plan part C, Task C9 "Invariant tests"): the
default drive search never returns a note that is not active on a live record of the connected drive, and no index
record carries such a note's id.

What it covers, through the rendered default search: the §8 rows that end with the note archived, dormant or gone
(delete, move out, folder cascade, D21 edit and undo of an archived note, clear then undo of an archived note, another
drive, re-upload, copy, erase of an archived note), the clear and the erase of an ACTIVE note on its live record, the
transcript-and-note row, and restore as the one inverse row. What it does not cover here: the `unstored` rows, opt-out
and opt-in, the kill switch and the rollback detector, which `test_drive_indexing_notes.py`,
`test_drive_note_lifecycle.py` and `test_drive_note_restore_fallback.py` test at the record and vector level.

Everything is real except the ports' edges: the tick (`DriveIndexingService` with part B's `notes=` and part C's
`note_lifecycle=`), the write path (`DriveNoteService`: `set`, `clear`, `undo`, `request_erase` + `erase`, refs as the
`annotate_drive_file` tool passes them) and the search (`DriveSearchService` with part C's deleted-files source added by
the composition function), over the in-memory index and note repositories and the scripted drive. Each row checks:

- **A1** — `DriveSearchService.search(<a note word>)` renders neither the note's text nor its `drive_note:` ref;
- **A2** — no index record carries the note's id (`owner_note_id`); for an archived note this holds by construction
  (its record is gone), for the active-note rows it is the real check;

and a positive control first: while the note is active on its live record, the same search does find it (so A1 can
never pass on an empty or gated search). Synthetic items and notes only."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.adapters.in_memory_drive_mirror_repo import InMemoryDriveMirrorRepository
from src.adapters.in_memory_drive_note_repo import InMemoryDriveNoteRepository
from src.composition.drive_note_setup import add_drive_note_deleted_source
from src.domain.drive_extraction import PARSER, READER
from src.domain.drive_index import DriveChange, DriveChanges
from src.domain.drive_keys import word_keys
from src.domain.drive_mirror import KIND_ARCHIVE, KIND_FILE, MIRROR_ACTIVE, MIRROR_ARCHIVED, MirrorDoc, MirrorRoot
from src.domain.drive_note import BoundBy, DriveNote, NoteProvenance, NoteState
from src.domain.drive_search import DriveSearchRequest, render_results
from src.domain.prompt_v3.security import RiskLevel, ValidationResult
from src.domain.user_drive import DriveConnection, DriveItem, DriveItemNotFoundError
from src.ports.ephemeral_store import EphemeralStore
from src.ports.security_port import SecurityPort
from src.services.drive_indexing_service import DriveIndexingService
from src.services.drive_note_lifecycle import DriveNoteLifecycle
from src.services.drive_note_service import DriveNoteService
from src.services.drive_search_service import DriveSearchService

from .drive_content_fakes import FakeContentDrive, FakeExtractor, FakeTriage
from .drive_index_fakes import ROOT, T0, Clock, FakeEmbeddings, FakeQueue, FakeUsers

SPACE = "test-space/8"
U, ACC = "u", "a"
CHAT = NoteProvenance(origin="chat", session_id="s1", turn_id="t1")
TEXT = "contractor bathroom invoice"
WORD = "bathroom"


class NoteDrive(FakeContentDrive):
    """The scripted drive with the two reads the note service makes (`get_item`) and a drive id that can change (a
    different drive connected)."""

    def __init__(self) -> None:
        super().__init__()
        self.drive_id = "d1"
        # items readable by id but never in the change feed: the transcript mirror's own files, which the indexer
        # keeps out of its records (the mirror root is opaque); here the tick has no mirror wired
        self.outside: Dict[str, DriveChange] = {}

    async def get_change(self, user_id: str, item_id: str) -> DriveChange:
        if item_id in self.outside:
            return self.outside[item_id]
        return await super().get_change(user_id, item_id)

    async def get_item(self, user_id: str, item_id: str) -> DriveItem:
        found = self.items.get(item_id)
        if found is None:
            raise DriveItemNotFoundError(item_id)
        return DriveItem(item_id=item_id, name=found.name or "", path=found.name or "", is_folder=found.is_folder)

    async def get_changes(self, user_id: str, cursor: Optional[str]) -> DriveChanges:
        page = await super().get_changes(user_id, cursor)
        return replace(page, drive_id=self.drive_id)

    def connect(self, drive_id: str) -> None:
        """Another drive: its own (empty) item set and change log, and the connection names it."""
        self.drive_id = drive_id
        self.items, self.log = {}, []
        self.connection_state = DriveConnection(connected=True, drive_id=drive_id)

    def move_out(self, item_id: str) -> None:
        """Moved out of the App Folder: the feed sends a tombstone and the item is not found by id any more."""
        self.items.pop(item_id, None)
        self.log.append(DriveChange(item_id=item_id, deleted=True))

    def delete_folder_only(self, folder_id: str) -> None:
        """A deleted folder whose feed carries only the folder's own tombstone: the tick cascades its children."""
        for kid in [i for i, c in self.items.items() if c.parent_id == folder_id]:
            self.items.pop(kid)
        self.items.pop(folder_id, None)
        self.log.append(DriveChange(item_id=folder_id, deleted=True))


class MemoryStore(EphemeralStore):
    def __init__(self) -> None:
        self.data: dict = {}

    async def set(self, key, value, ttl_s):
        self.data[key] = value

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)

    async def get_and_delete(self, key):
        return self.data.pop(key, None)


def _security() -> SecurityPort:
    security = AsyncMock(spec=SecurityPort)
    security.validate.return_value = ValidationResult(
        sanitized_text="", risk_level=RiskLevel.SAFE, risk_score=0.0, patterns_detected=[], action_taken="passed",
        metadata={})
    return security


class World:
    def __init__(self, *, mirror: Optional[InMemoryDriveMirrorRepository] = None) -> None:
        self.drive = NoteDrive()
        self.repo = InMemoryDriveIndexRepository()
        self.emb = FakeEmbeddings()
        self.queue = FakeQueue()
        self.users = FakeUsers()
        self.clock = Clock()
        self.notes = InMemoryDriveNoteRepository(clock=self.clock)
        self.lifecycle = DriveNoteLifecycle(self.notes, self.repo, self.drive, clock=self.clock)
        self.tick_svc = DriveIndexingService(
            self.drive, self.repo, self.emb, self.queue, self.users, lambda lat, lon: None, SPACE,
            extractors={PARSER: FakeExtractor(PARSER), READER: FakeExtractor(READER)}, triage=FakeTriage(),
            notes=self.notes, note_lifecycle=self.lifecycle, clock=self.clock, monotonic=self.clock.monotonic)
        codes = iter(f"c{i:04d}" for i in range(100))
        self.note_svc = DriveNoteService(
            self.notes, self.drive, self.repo, self.emb, _security(), self.users, embedding_space=SPACE,
            nudge_queue=self.queue, erase_requests=MemoryStore(), clock=self.clock, new_code=lambda: next(codes))
        self.search_svc = DriveSearchService(self.repo, self.emb, self.users, self.drive, embedding_space=SPACE,
                                             clock=self.clock, mirror=mirror)
        add_drive_note_deleted_source(self.search_svc, self.notes, self.emb, embedding_space=SPACE)

    # -- driving -------------------------------------------------------------------------------------------------

    async def settle(self, max_ticks: int = 60) -> Any:
        for _ in range(max_ticks):
            before = len(self.queue.tasks)
            result = await self.tick_svc.tick(U, None)
            continued = [t for t in self.queue.tasks[before:] if ":cont:" in (t["key"] or "")]
            if not continued:
                return result
            self.clock.advance(max(1, max(t["delay"] for t in continued)))
        raise AssertionError("did not settle")

    async def next_round(self) -> None:
        """Settle, then one more fan-out: a delete the work-off decides (a cascade) is confirmed at the next round end
        (part C default D-C2)."""
        await self.settle()
        self.clock.advance(600)
        await self.settle()

    async def file(self, name: str = "scan.pdf", parent: str = ROOT, item_id: Optional[str] = None) -> str:
        item = self.drive.add(name, parent=parent, item_id=item_id)
        await self.settle()
        return item

    async def note(self, ref: str, text: str = TEXT) -> str:
        receipt = await self.note_svc.set(U, ACC, ref, text, provenance=CHAT)
        self.clock.advance(1)
        await self.settle()
        return receipt.note_id

    # -- reading -------------------------------------------------------------------------------------------------

    async def stored(self, note_id: str) -> Optional[DriveNote]:
        return await self.notes.get(U, note_id)

    async def found(self, word: str = WORD) -> str:
        """The default search, rendered as Smart reads it."""
        return render_results(await self.search_svc.search(U, DriveSearchRequest(query=word)))

    async def deleted(self, word: str = WORD) -> str:
        return (await self.search_svc.search_deleted(U, word))[0]

    def carriers(self, note_id: str) -> List[str]:
        return sorted(item for (user, item), rec in self.repo.records.items()
                      if user == U and rec.owner_note_id == note_id)

    # -- the two assertions --------------------------------------------------------------------------------------

    async def a1(self, note_id: str, *texts: str, word: str = WORD) -> None:
        out = await self.found(word)
        for text in texts or (TEXT,):
            assert text not in out, out
        assert f"drive_note:{note_id}" not in out, out

    def a2(self, note_id: str) -> None:
        assert self.carriers(note_id) == []

    async def control(self, note_id: str, text: str = TEXT, word: str = WORD) -> None:
        """The positive control: the active note on its live record is found by the default search."""
        out = await self.found(word)
        assert f"noted by the owner: «{text}»" in out, out
        assert self.carriers(note_id)


async def _archived(w: World, name: str = "scan.pdf") -> Tuple[str, str]:
    """A noted file deleted in OneDrive and archived by the round end (row 1's end state)."""
    item = await w.file(name)
    nid = await w.note(f"drive:{item}")
    await w.control(nid)
    w.drive.delete(item)
    await w.settle()
    note = await w.stored(nid)
    assert note is not None and note.state == NoteState.ARCHIVED.value
    return item, nid


@pytest.fixture
def w() -> World:
    return World()


class TestArchivedRows:
    """§8 rows that end with the note archived (or gone): A1 and A2."""

    async def test_delete_in_onedrive(self, w):
        item, nid = await _archived(w)
        await w.a1(nid)
        w.a2(nid)
        out = await w.deleted()
        assert out.startswith("Deleted files (1):") and f"drive_note:{nid}" in out and TEXT in out

    async def test_move_out_of_the_app_folder(self, w):
        item = await w.file()
        nid = await w.note(f"drive:{item}")
        await w.control(nid)
        w.drive.move_out(item)
        await w.settle()
        assert (await w.stored(nid)).state == NoteState.ARCHIVED.value
        await w.a1(nid)
        w.a2(nid)

    async def test_folder_cascade(self, w):
        folder = w.drive.add("Contracts", folder=True)
        await w.settle()
        child = await w.file("lease.pdf", parent=folder)
        n_folder = await w.note(f"drive:{folder}", "bathroom folder of contracts")
        n_child = await w.note(f"drive:{child}")
        await w.control(n_child)
        w.drive.delete_folder_only(folder)
        await w.next_round()
        for nid in (n_folder, n_child):
            assert (await w.stored(nid)).state == NoteState.ARCHIVED.value
            w.a2(nid)
        await w.a1(n_folder, "bathroom folder of contracts")
        await w.a1(n_child)

    async def test_an_archived_note_edited_by_its_drive_note_ref(self, w):
        item, nid = await _archived(w)
        await w.note(f"drive_note:{nid}", "bathroom painting invoice")
        assert (await w.stored(nid)).state == NoteState.ARCHIVED.value
        await w.a1(nid, TEXT, "bathroom painting invoice")
        await w.a1(nid, TEXT, "bathroom painting invoice", word="painting")
        w.a2(nid)
        out = await w.deleted("painting")
        assert "bathroom painting invoice" in out and f"drive_note:{nid}" in out

    async def test_an_archived_note_undone_by_its_ref(self, w):
        item = await w.file()
        nid = await w.note(f"drive:{item}", "bathroom first invoice")
        await w.note(f"drive:{item}")
        await w.control(nid)
        w.drive.delete(item)
        await w.settle()
        await w.note_svc.undo(U, ACC, f"drive_note:{nid}", provenance=CHAT)
        await w.settle()
        note = await w.stored(nid)
        assert note.state == NoteState.ARCHIVED.value and note.text == "bathroom first invoice"
        await w.a1(nid, TEXT, "bathroom first invoice")
        w.a2(nid)

    async def test_an_archived_note_cleared_then_undone(self, w):
        item, nid = await _archived(w)
        await w.note_svc.clear(U, ACC, f"drive_note:{nid}", provenance=CHAT)
        await w.settle()
        note = await w.stored(nid)
        assert note.state == NoteState.CLEARED.value and note.archived_at is not None
        await w.a1(nid)
        w.a2(nid)
        await w.note_svc.undo(U, ACC, f"drive_note:{nid}", provenance=CHAT)
        await w.settle()
        note = await w.stored(nid)
        assert note.state == NoteState.ARCHIVED.value and note.text == TEXT
        await w.a1(nid)
        w.a2(nid)

    async def test_another_drive_connected_leaves_the_note_dormant(self, w):
        item = await w.file()
        nid = await w.note(f"drive:{item}")
        await w.control(nid)
        w.drive.connect("d2")
        w.clock.advance(600)
        await w.settle()
        same = w.drive.add("scan.pdf", item_id=item)   # even the same item id: a binding names its drive (D15, D16)
        await w.settle()
        assert w.repo.records.get((U, same)) is not None and (await w.stored(nid)).state == NoteState.ACTIVE.value
        assert "scan.pdf |" in await w.found("scan")   # the search answers on d2 (not gated by the rebuild)
        await w.a1(nid)
        w.a2(nid)
        out = await w.deleted()
        assert f"drive_note:{nid}" in out and "the file is on a drive that is not connected" in out   # labelled

    async def test_a_re_upload_with_the_same_name_and_content_gets_no_note(self, w):
        item, nid = await _archived(w)
        again = await w.file("scan.pdf")
        assert again != item and w.repo.records.get((U, again)) is not None
        assert (await w.stored(nid)).state == NoteState.ARCHIVED.value
        assert "scan.pdf |" in await w.found("scan")   # the new record is searchable
        await w.a1(nid)
        w.a2(nid)

    async def test_erase_of_an_archived_note(self, w):
        item, nid = await _archived(w)
        request = await w.note_svc.request_erase(U, f"drive_note:{nid}")
        assert await w.note_svc.erase(U, request.code) == 1
        await w.settle()
        assert await w.stored(nid) is None   # the stub is deleted by the next tick
        await w.a1(nid)
        w.a2(nid)
        assert (await w.deleted()).startswith("Deleted files (0):")


class TestActiveNoteRows:
    """Clear and erase of an ACTIVE note whose record is live: the record holds the copy, so A2 is a real check."""

    async def test_clear_of_an_active_note(self, w):
        item = await w.file()
        nid = await w.note(f"drive:{item}")
        await w.control(nid)
        await w.note_svc.clear(U, ACC, f"drive:{item}", provenance=CHAT)
        w.clock.advance(1)
        await w.settle()
        assert w.repo.records.get((U, item)) is not None
        await w.a1(nid)
        w.a2(nid)

    async def test_erase_of_an_active_note(self, w):
        item = await w.file()
        nid = await w.note(f"drive:{item}")
        await w.control(nid)
        request = await w.note_svc.request_erase(U, f"drive:{item}")
        assert await w.note_svc.erase(U, request.code) == 1
        w.clock.advance(1)
        await w.settle()
        assert w.repo.records.get((U, item)) is not None and await w.stored(nid) is None
        await w.a1(nid)
        w.a2(nid)


class TestCopy:
    async def test_a_copy_of_a_noted_live_file_carries_no_note(self, w):
        original = await w.file()
        nid = await w.note(f"drive:{original}")
        copy = await w.file("scan - Copy.pdf")
        assert w.carriers(nid) == [original]   # A2 for the copy's record; the original keeps the note
        out = await w.found()
        assert out.count(f"noted by the owner: «{TEXT}»") == 1   # A1 shows only the original
        assert (await w.stored(nid)).state == NoteState.ACTIVE.value and w.repo.records[(U, copy)].owner_note_id == ""


class TestRestore:
    """The inverse row (§7.2): the same item id comes back, and so does the note."""

    async def test_the_same_item_id_reappears(self, w):
        item, nid = await _archived(w)
        await w.a1(nid)
        w.a2(nid)
        w.drive.add("scan.pdf", item_id=item)
        await w.settle()
        await w.control(nid)                           # inverse A1: found on the record's card
        assert w.carriers(nid) == [item]               # inverse A2: the record carries the id
        note = await w.stored(nid)
        assert note.state == NoteState.ACTIVE.value and note.binding.bound_by == BoundBy.RESTORE.value


async def _mirror_with_transcript(item_id: str, name: str) -> InMemoryDriveMirrorRepository:
    """The main service's view of an archived transcript of `item_id` (what T2's mirror step writes when the recording
    is deleted; the tick here has no transcript stage wired)."""
    repo = InMemoryDriveMirrorRepository()
    await repo.save_root(MirrorRoot(user_id=U, drive_id="d1", root_item_id="tr-root", root_name="_Transcripts"))
    await repo.save_doc(MirrorDoc(user_id=U, drive_id="d1", kind=KIND_ARCHIVE, source_item_id="day",
                                  mirror_item_id="day", mirror_parent_id="tr-root", mirror_name="2026-10-11",
                                  state=MIRROR_ACTIVE))
    await repo.save_doc(MirrorDoc(
        user_id=U, drive_id="d1", kind=KIND_FILE, source_item_id=item_id, source_name=name, source_path=name,
        content_key="k1", mirror_item_id="m1", mirror_parent_id="day", mirror_name="call1.md", state=MIRROR_ARCHIVED,
        archive_day="2026-10-11", archived_at=T0, seconds=600.0, speakers=1, languages=("en",),
        archive_keys=tuple(sorted(word_keys("bathroom budget"))), name_keys=tuple(sorted(word_keys("call1")))))
    return repo


class TestTranscriptAndNote:
    async def test_a_transcript_and_a_note_on_one_gone_item_are_one_deleted_entry(self):
        probe = World()
        item = probe.drive.add("call1.m4a")
        w = World(mirror=await _mirror_with_transcript(item, "call1.m4a"))
        assert w.drive.add("call1.m4a") == item          # the same scripted id in the world that has the mirror
        w.drive.outside["m1"] = DriveChange(item_id="m1", name="call1.md", parent_id="day")   # the transcript file
        await w.settle()
        nid = await w.note(f"drive:{item}")
        await w.control(nid)
        w.drive.delete(item)
        await w.settle()
        assert (await w.stored(nid)).state == NoteState.ARCHIVED.value
        await w.a1(nid)
        w.a2(nid)
        out = await w.deleted()
        assert out.startswith("Deleted files (1):")
        assert "archived transcript: drive:m1" in out and f"drive_note:{nid}" in out and "noted by the owner" in out

