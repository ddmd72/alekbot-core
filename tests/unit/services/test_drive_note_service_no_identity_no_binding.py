"""DriveNoteService edge outcomes of slice 1b: an item the adapter built no identity for is refused as an unreachable
drive and nothing is written; a note with no binding (a store-only note) reads as "another drive" and is never nudged.
Technical ids only."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.adapters.in_memory_drive_note_repo import InMemoryDriveNoteRepository
from src.domain.drive_note import (
    STATUS_OTHER_DRIVE, DriveNote, DriveNoteRefused, NoteAction, NoteContent, NoteProvenance, NoteRefusal, NoteState,
    next_version, note_after_write,
)
from src.domain.prompt_v3.security import RiskLevel, ValidationResult
from src.ports.embedding_service import EmbeddingService
from src.ports.security_port import SecurityPort
from src.services.drive_note_service import DriveNoteService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service, seed
from tests.support.fake_drive_adapter import FakeDriveAdapter
from tests.unit.services.drive_index_fakes import FakeQueue

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _NoIdentityDrive(FakeDriveAdapter):
    """A drive whose adapter builds no item identity (neither on the item nor on the change row)."""

    def _item_identity(self, tree, item_id):   # type: ignore[no-untyped-def]
        return ""


class _EmptyIndex:
    async def get_records(self, scope, item_ids):   # type: ignore[no-untyped-def]
        return {}


async def _world(drive_cls=FakeDriveAdapter):
    class World:
        pass
    w = World()
    w.repo = FakeDriveConnectionRepository()
    w.drive = drive_cls("fake_a")
    w.notes = InMemoryDriveNoteRepository()
    w.queue = FakeQueue()
    w.one = await seed(w.repo, connection_id="conn-1", tag="a1")
    w.drive.tree(w.one.handle, drive_id=w.one.connection_id)
    assert w.drive.plant(w.one.handle, "root", "on-one.pdf", b"one") == "item-1"
    embeddings = AsyncMock(spec=EmbeddingService)
    embeddings.get_embedding.return_value = [0.1, 0.2]
    security = AsyncMock(spec=SecurityPort)
    security.validate.return_value = ValidationResult(sanitized_text="", risk_level=RiskLevel.SAFE, risk_score=0.0,
                                                      patterns_detected=[], action_taken="passed", metadata={})
    w.service = DriveNoteService(w.notes, w.drive, _EmptyIndex(), embeddings, security, connection_service(w.repo),
                                 embedding_space="space-a", nudge_queue=w.queue, clock=lambda: T0)
    return w


async def _store_unbound_note(notes: InMemoryDriveNoteRepository, note_id: str = "n-free") -> None:
    base = DriveNote(note_id=note_id, user_id="usr-1", account_id="acc-1", binding=None)
    content = NoteContent("a note held by the store alone")
    version = next_version(0, content, NoteAction.SET, NoteProvenance(), T0)
    written = note_after_write(base, version, NoteState.ACTIVE, NoteProvenance(), T0, vector=None, embedding_space="",
                               embedding_pending=True, binding=None)
    await notes.write_version(written, version, expected_version=0)


async def test_an_item_without_identity_is_refused_as_drive_unavailable_and_nothing_is_written():
    w = await _world(_NoIdentityDrive)
    with pytest.raises(DriveNoteRefused) as refused:
        await w.service.set("usr-1", "acc-1", "drive:a1:item-1", "text")
    assert refused.value.reason is NoteRefusal.DRIVE_UNAVAILABLE
    assert await w.notes.note_ids("usr-1") == [] and w.queue.tasks == []


async def test_a_note_with_no_binding_reports_another_drive_on_edit():
    w = await _world()
    await _store_unbound_note(w.notes)
    receipt = await w.service.set("usr-1", "acc-1", "drive_note:n-free", "edited by its note ref")
    assert receipt.status == STATUS_OTHER_DRIVE


async def test_clearing_a_note_with_no_binding_nudges_no_connection():
    w = await _world()
    await _store_unbound_note(w.notes)
    receipt = await w.service.clear("usr-1", "acc-1", "drive_note:n-free")
    assert receipt.action == "clear"
    assert (await w.notes.get("usr-1", "n-free")).state == "cleared"
    assert w.queue.tasks == []
