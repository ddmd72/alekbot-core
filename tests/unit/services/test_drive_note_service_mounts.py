"""Note receipts print the mount (MULTI_DRIVE_RFC §11.1, §11.10; slice 2a E-11): `<mount>/<path>` with the current name
of the connection the note's tag names, DISCONNECTED included; the stored snapshot stays connection-relative."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.adapters.in_memory_drive_note_repo import InMemoryDriveNoteRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_connection_state import ConnectionState
from src.domain.prompt_v3.security import RiskLevel, ValidationResult
from src.ports.embedding_service import EmbeddingService
from src.ports.ephemeral_store import EphemeralStore
from src.ports.security_port import SecurityPort
from src.services.drive_note_service import DriveNoteService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service, seed
from tests.support.fake_drive_adapter import FakeDriveAdapter
from tests.unit.services.drive_index_fakes import FakeQueue

T1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
REL = "R&D/Tom's plan.docx"


class _MemoryStore(EphemeralStore):
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


class _EmptyIndex:
    async def get_records(self, scope: ConnectionScope, item_ids):
        return {}


@pytest.fixture
async def world():
    class World:
        pass
    w = World()
    w.repo = FakeDriveConnectionRepository()
    w.drive = FakeDriveAdapter("fake_a")
    w.notes = InMemoryDriveNoteRepository()
    w.queue = FakeQueue()
    w.one = await seed(w.repo, connection_id="conn-1", tag="a1")
    w.two = await seed(w.repo, connection_id="conn-2", tag="b2", primary=False, created_at=T1)
    await w.repo.update("usr-1", "conn-2", mount_name="Drive 2")
    w.ids = {}
    for connection in (w.one, w.two):
        w.drive.tree(connection.handle, drive_id=connection.connection_id)
        folder = w.drive.plant(connection.handle, "root", "R&D", folder=True)
        w.ids[connection.ref_tag] = w.drive.plant(connection.handle, folder, "Tom's plan.docx", b"plan")
    embeddings = AsyncMock(spec=EmbeddingService)
    embeddings.get_embedding.return_value = [0.1, 0.2]
    security = AsyncMock(spec=SecurityPort)
    security.validate.return_value = ValidationResult(sanitized_text="", risk_level=RiskLevel.SAFE, risk_score=0.0,
                                                      patterns_detected=[], action_taken="passed", metadata={})
    w.service = DriveNoteService(w.notes, w.drive, _EmptyIndex(), embeddings, security, connection_service(w.repo),
                                 access=AsyncMock(), embedding_space="space-a", nudge_queue=w.queue,
                                 erase_requests=_MemoryStore(), clock=lambda: T1)
    w.ref = lambda tag: f"drive:{tag}:{w.ids[tag]}"
    return w


async def _stored(world):
    ids = await world.notes.note_ids("usr-1")
    return await world.notes.get("usr-1", ids[0])


class TestReceiptsCarryTheMount:
    async def test_set_prints_the_mount_and_the_snapshot_stays_relative(self, world):
        receipt = await world.service.set("usr-1", "acc-1", world.ref("a1"), "the plan")
        assert receipt.path == f"Drive/{REL}"
        assert (await _stored(world)).snapshot.path == REL

    async def test_a_noop_set_prints_the_mount(self, world):
        await world.service.set("usr-1", "acc-1", world.ref("a1"), "the plan")
        again = await world.service.set("usr-1", "acc-1", world.ref("a1"), "the plan")
        assert again.noop and again.path == f"Drive/{REL}"

    async def test_a_note_ref_edit_prints_the_mount_of_the_notes_connection(self, world):
        await world.service.set("usr-1", "acc-1", world.ref("b2"), "the plan")
        note = await _stored(world)
        receipt = await world.service.set("usr-1", "acc-1", f"drive_note:{note.note_id}", "the plan, edited")
        assert receipt.path == f"Drive 2/{REL}"

    async def test_clear_undo_and_erase_request_print_the_mount(self, world):
        await world.service.set("usr-1", "acc-1", world.ref("a1"), "the plan")
        cleared = await world.service.clear("usr-1", "acc-1", world.ref("a1"))
        assert cleared.path == f"Drive/{REL}"
        again = await world.service.clear("usr-1", "acc-1", world.ref("a1"))
        assert again.noop and again.path == f"Drive/{REL}"
        undone = await world.service.undo("usr-1", "acc-1", world.ref("a1"))
        assert undone.path == f"Drive/{REL}"
        erase = await world.service.request_erase("usr-1", world.ref("a1"))
        assert erase.path == f"Drive/{REL}"

    async def test_a_renamed_drive_prints_its_current_name(self, world):
        await world.service.set("usr-1", "acc-1", world.ref("a1"), "the plan")
        await world.repo.update("usr-1", "conn-1", mount_name="Acme")
        cleared = await world.service.clear("usr-1", "acc-1", world.ref("a1"))
        assert cleared.path == f"Acme/{REL}"
        assert (await _stored(world)).snapshot.path == REL

    async def test_a_disconnected_connection_prints_with_its_last_name(self, world):
        await world.service.set("usr-1", "acc-1", world.ref("b2"), "the plan")
        await world.repo.update("usr-1", "conn-2", mount_name="Acme Hamburg")
        stored = world.repo.connections[("usr-1", "conn-2")]
        from dataclasses import replace
        world.repo.connections[("usr-1", "conn-2")] = replace(stored, state=ConnectionState.DISCONNECTED,
                                                              disconnected_at=T1)
        cleared = await world.service.clear("usr-1", "acc-1", world.ref("b2"))
        assert cleared.path == f"Acme Hamburg/{REL}"

    async def test_a_note_of_a_tag_with_no_connection_keeps_its_stored_path(self, world):
        await world.service.set("usr-1", "acc-1", world.ref("a1"), "the plan")
        note = await _stored(world)
        world.repo.connections.pop(("usr-1", "conn-1"))
        receipt = await world.service.clear("usr-1", "acc-1", f"drive_note:{note.note_id}")
        assert receipt.path == REL
