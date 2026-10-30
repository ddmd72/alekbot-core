"""DriveNoteService and DriveNoteDeletedSearch by connection (MULTI_DRIVE_RFC §7.2, §9, §11.6; slice 1b part D): the live
path resolves the ref's connection and binds by identity and tag; the store-alone paths look the tagged ref key up; a
ref of no live connection is refused with its code-built line; the receipt status and the nudge are the note's
connection's; the deleted-files source calls a note of another connection dormant."""
from datetime import datetime, timezone
from typing import Dict, List, Sequence
from unittest.mock import AsyncMock

import pytest

from src.adapters.in_memory_drive_note_repo import InMemoryDriveNoteRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import DriveIndexRecord
from src.domain.drive_note import (
    STATUS_OTHER_DRIVE, DriveNote, DriveNoteRefused, NoteAction, NoteBinding, NoteContent, NoteProvenance,
    NoteRefusal, NoteSnapshot, NoteState, next_version, note_after_write, note_content,
)
from src.domain.drive_ref_refusal import stale_ref_line
from src.domain.drive_task_keys import parse_nudge, payload_scope
from src.domain.prompt_v3.security import RiskLevel, ValidationResult
from src.ports.embedding_service import EmbeddingService
from src.ports.security_port import SecurityPort
from src.services.drive_note_deleted_search import DriveNoteDeletedSearch
from src.services.drive_note_service import DriveNoteService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service, seed
from tests.support.fake_drive_adapter import FakeDriveAdapter
from tests.unit.services.drive_index_fakes import FakeQueue

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 1, 2, tzinfo=timezone.utc)


class _ScopedIndex:
    """The index read side the note service uses, keyed by connection scope (part C's port convention)."""

    def __init__(self) -> None:
        self.records: Dict[ConnectionScope, Dict[str, DriveIndexRecord]] = {}
        self.asked: List[ConnectionScope] = []

    async def get_records(self, scope: ConnectionScope, item_ids: Sequence[str]) -> Dict[str, DriveIndexRecord]:
        self.asked.append(scope)
        mine = self.records.get(scope, {})
        return {i: mine[i] for i in item_ids if i in mine}


@pytest.fixture
async def world():
    class World:
        pass
    w = World()
    w.repo = FakeDriveConnectionRepository()
    w.drive = FakeDriveAdapter("fake_a")
    w.notes = InMemoryDriveNoteRepository()
    w.index = _ScopedIndex()
    w.queue = FakeQueue()
    w.one = await seed(w.repo, connection_id="conn-1", tag="a1")
    w.two = await seed(w.repo, connection_id="conn-2", tag="b2", kind="fake_b", primary=False, indexing_enabled=False,
                       created_at=T1)
    for connection, name, data in ((w.one, "on-one.pdf", b"one"), (w.two, "on-two.pdf", b"two")):
        w.drive.tree(connection.handle, drive_id=connection.connection_id)   # item identities fake_a:<connection>:<item>
        assert w.drive.plant(connection.handle, "root", name, data) == "item-1"   # the same item id on purpose (T-4)
    embeddings = AsyncMock(spec=EmbeddingService)
    embeddings.get_embedding.return_value = [0.1, 0.2]
    security = AsyncMock(spec=SecurityPort)
    security.validate.return_value = ValidationResult(sanitized_text="", risk_level=RiskLevel.SAFE, risk_score=0.0,
                                                      patterns_detected=[], action_taken="passed", metadata={})
    w.service = DriveNoteService(w.notes, w.drive, w.index, embeddings, security,
                                 connection_service(w.repo), embedding_space="space-a",
                                 nudge_queue=w.queue, clock=lambda: T1)
    return w


async def _only(notes: InMemoryDriveNoteRepository, user_id: str = "usr-1") -> DriveNote:
    ids = await notes.note_ids(user_id)
    assert len(ids) == 1
    found = await notes.get(user_id, ids[0])
    assert found is not None
    return found


class TestLivePath:
    async def test_a_note_binds_to_the_refs_connection_by_identity_and_tag(self, world):
        receipt = await world.service.set("usr-1", "acc-1", "drive:b2:item-1", "the second drive's file")
        note = await _only(world.notes)
        assert note.binding is not None
        assert (note.binding.identity, note.binding.item_id, note.binding.ref_tag) == ("fake_a:conn-2:item-1",
                                                                                      "item-1", "b2")
        assert note.match_keys == ["i:fake_a:conn-2:item-1", "r:b2:item-1"]
        assert note.snapshot is not None and note.snapshot.name == "on-two.pdf"
        assert receipt.ref == "drive:b2:item-1"

    async def test_the_receipt_status_and_nudge_are_the_connections(self, world):
        await world.service.set("usr-1", "acc-1", "drive:a1:item-1", "first")
        receipt = await world.service.set("usr-1", "acc-1", "drive:b2:item-1", "second")
        assert receipt.status == "indexing_off"                       # conn-2's switch is off
        assert [payload_scope(t["payload"]) for t in world.queue.tasks] == [world.one.scope, world.two.scope]
        assert parse_nudge(world.queue.tasks[1]["payload"]).item_ids == ("item-1",)
        assert world.index.asked == [world.one.scope, world.two.scope]   # the D20 read in each connection's index

    async def test_the_same_item_id_on_two_connections_is_two_notes(self, world):
        await world.service.set("usr-1", "acc-1", "drive:a1:item-1", "first")
        await world.service.set("usr-1", "acc-1", "drive:b2:item-1", "second")
        assert len(await world.notes.note_ids("usr-1")) == 2

    async def test_an_untagged_ref_is_refused_with_the_stale_line_and_nothing_written(self, world):
        with pytest.raises(DriveNoteRefused) as refused:
            await world.service.set("usr-1", "acc-1", "drive:item-1", "text")
        assert refused.value.reason is NoteRefusal.INVALID_REF and str(refused.value) == stale_ref_line("drive:item-1")
        assert await world.notes.note_ids("usr-1") == [] and world.queue.tasks == []

    async def test_an_unknown_tag_is_refused_with_the_stale_line(self, world):
        with pytest.raises(DriveNoteRefused) as refused:
            await world.service.create("usr-1", "acc-1", "drive:zz:item-1", note_content("text"))
        assert str(refused.value) == stale_ref_line("drive:zz:item-1")


class TestStoreAlone:
    async def test_get_and_clear_find_the_note_by_its_tagged_ref(self, world):
        await world.service.set("usr-1", "acc-1", "drive:b2:item-1", "second")
        view = await world.service.get("usr-1", "drive:b2:item-1")
        assert view is not None and view.text == "second" and view.ref == "drive:b2:item-1"
        assert await world.service.get("usr-1", "drive:a1:item-1") is None    # same item id, other connection
        cleared = await world.service.clear("usr-1", "acc-1", "drive:b2:item-1")
        assert cleared.action == "clear" and (await _only(world.notes)).state == "cleared"

    async def test_an_untagged_ref_is_refused_on_the_store_path_too(self, world):
        with pytest.raises(DriveNoteRefused) as refused:
            await world.service.get("usr-1", "drive:item-1")
        assert str(refused.value) == stale_ref_line("drive:item-1")

    async def test_a_note_of_a_connection_that_is_gone_is_another_drives(self, world):
        await world.notes.write_version(*_stored_note("n-old", tag="z9"), expected_version=0)
        receipt = await world.service.set("usr-1", "acc-1", "drive_note:n-old", "edited by its note ref")
        assert receipt.status == STATUS_OTHER_DRIVE
        assert world.queue.tasks == []                                 # no live connection: nothing nudged

    async def test_note_for_item_is_by_identity(self, world):
        await world.service.set("usr-1", "acc-1", "drive:a1:item-1", "first")
        found = await world.service.note_for_item("usr-1", "fake_a:conn-1:item-1")
        assert found is not None and found.text == "first"
        assert await world.service.note_for_item("usr-1", "fake_a:conn-2:item-1") is None
        assert await world.service.note_for_item("usr-1", "") is None


def _stored_note(note_id: str, *, tag: str, state: str = "active"):
    base = DriveNote(note_id=note_id, user_id="usr-1", account_id="acc-1",
                     binding=NoteBinding(identity=f"fake:{note_id}:item-9", item_id="item-9", ref_tag=tag),
                     snapshot=NoteSnapshot(name="gone.pdf", path="gone.pdf", snapshot_at=T0))
    content = NoteContent("an old note")
    version = next_version(0, content, NoteAction.SET, NoteProvenance(), T0)
    written = note_after_write(base, version, NoteState(state), NoteProvenance(), T0, vector=None,
                               embedding_space="", embedding_pending=True, binding=base.binding)
    return written, version


class TestDeletedSourceByConnection:
    async def test_a_note_of_another_connection_is_dormant_and_this_ones_is_not(self, world):
        await world.notes.write_version(*_stored_note("n-here", tag="a1"), expected_version=0)
        await world.notes.write_version(*_stored_note("n-there", tag="b2"), expected_version=0)
        source = DriveNoteDeletedSearch(world.notes, AsyncMock(spec=EmbeddingService), embedding_space="space-a",
                                        nonce=lambda: "n0nce000")
        hits, coverage = await source("usr-1", [], world.one)
        assert [h.ref for h in hits] == ["drive_note:n-there"] and not hits[0].on_connected_drive
        assert hits[0].item_key == "fake:n-there:item-9"

    async def test_with_no_connection_no_dormant_note_is_shown(self, world):
        await world.notes.write_version(*_stored_note("n-there", tag="b2"), expected_version=0)
        source = DriveNoteDeletedSearch(world.notes, AsyncMock(spec=EmbeddingService), embedding_space="space-a")
        hits, coverage = await source("usr-1", [], None)
        assert hits == [] and coverage

