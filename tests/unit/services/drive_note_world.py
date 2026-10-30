"""The tick harness with the owner-notes lifecycle wired (DRIVE_FILE_NOTES_RFC §7): the in-memory note store, the
DriveNoteLifecycle over it, and helpers that put a note on an indexed item the way part B's merge does (the record's
`owner_note_id` and a note bound to the item on the harness's drive). A note is bound by the item's identity
(`fake:{drive}:{item}`, the harness drive's form, MULTI_DRIVE_RFC §7.2) on the connection mounting that drive: the
harness's own connection (tag `a1`) for DRIVE, another connection's tag for any other drive (§9)."""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.adapters.in_memory_drive_note_repo import InMemoryDriveNoteRepository
from src.domain.drive_extraction import PARSER, READER
from src.domain.drive_note import (
    DriveNote, NoteBinding, NoteSnapshot, NoteState, item_binding_key, tagged_ref_key,
)
from src.services.drive_indexing_service import DriveIndexingService
from src.services.drive_note_lifecycle import DriveNoteLifecycle

from .drive_content_fakes import FakeContentDrive, FakeExtractor, FakeTriage
from .drive_index_fakes import Clock, FakeEmbeddings, FakeQueue, FakeUsers
from .drive_index_fakes import LegacyConnections
from .drive_index_fakes import U_SCOPE

DRIVE = "d1"   # the fakes' drive id
TAGS = {DRIVE: "a1", "d2": "b2"}   # the ref tag of the connection mounting each drive (a1: the harness connection)


class NotesWorld:
    def __init__(self, page_size: int = 50, *, lifecycle: bool = True, reemit_parents: bool = False) -> None:
        self.drive = FakeContentDrive(page_size=page_size, reemit_parents=reemit_parents)
        self.repo = InMemoryDriveIndexRepository()
        self.emb = FakeEmbeddings()
        self.queue = FakeQueue()
        self.users = FakeUsers()
        self.clock = Clock()
        self.notes = InMemoryDriveNoteRepository(clock=self.clock)
        self.lifecycle: Optional[DriveNoteLifecycle] = (
            DriveNoteLifecycle(self.notes, self.repo, self.drive, clock=self.clock) if lifecycle else None)
        self.connections = LegacyConnections(self.users, self.drive)
        self.svc = DriveIndexingService(self.drive, self.repo, self.emb, self.queue, self.users,
                                        lambda lat, lon: "Rome (Roma), Italy", "test-space/8",
                                        extractors={PARSER: FakeExtractor(PARSER), READER: FakeExtractor(READER)},
                                        triage=FakeTriage(), note_lifecycle=self.lifecycle, connections=self.connections, clock=self.clock,
                                        monotonic=self.clock.monotonic)

    async def connection(self):
        """The harness's connection as the tick reads it (user `u`, connection `u`, tag `a1`)."""
        return await self.connections.get(U_SCOPE.user_id, U_SCOPE.connection_id)

    async def tick(self, nudge=None):
        return await self.svc.tick(U_SCOPE, nudge)

    async def settle(self, max_ticks: int = 50):
        for _ in range(max_ticks):
            before = len(self.queue.tasks)
            result = await self.tick()
            continued = [t for t in self.queue.tasks[before:] if ":cont:" in (t["key"] or "")]
            if not continued:
                return result
            self.clock.advance(max(1, max(t["delay"] for t in continued)))
        raise AssertionError("did not settle")

    def rec(self, item_id: str):
        return self.repo.records.get(("u", item_id))

    def note_on(self, item_id: str, note_id: Optional[str] = None, *, drive: str = DRIVE,
                path: str = "", merge: bool = True) -> str:
        """An active note bound to `item_id`; with `merge` the record carries it (what part B's merge writes)."""
        note_id = note_id or f"n-{item_id}"
        item = self.drive.items.get(item_id)
        name = item.name if item is not None else f"{item_id}.pdf"
        tag = TAGS[drive]
        binding = NoteBinding(identity=f"fake:{drive}:{item_id}", item_id=item_id, ref_tag=tag)
        self.notes.notes[("u", note_id)] = DriveNote(
            note_id=note_id, user_id="u", account_id="a", state=NoteState.ACTIVE, current_version=1, text="the invoice",
            binding=binding, snapshot=NoteSnapshot(name=name, path=path or name,
                                                   parent_item_id=item.parent_id if item is not None else "",
                                                   snapshot_at=self.clock.now),
            match_keys=[item_binding_key(binding.identity), tagged_ref_key(tag, item_id)],
            updated_at=self.clock.now, revision=1)
        if merge:
            rec = self.rec(item_id)
            assert rec is not None, f"{item_id} has no record"
            self.repo.records[("u", item_id)] = replace(rec, owner_note_id=note_id)
        return note_id

    def note(self, note_id: str) -> DriveNote:
        return self.notes.notes[("u", note_id)]

    async def state(self):
        return await self.repo.get_sync_state(U_SCOPE)
