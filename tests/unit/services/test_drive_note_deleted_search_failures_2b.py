"""The deleted-files search for notes when the live-connections read fails (2b, E-9): the failure is logged and the
old rule applies, so a note on a connection that is not live is not called dormant."""
from unittest.mock import AsyncMock, MagicMock

from src.adapters.in_memory_drive_note_repo import InMemoryDriveNoteRepository
from src.ports.embedding_service import EmbeddingService
from src.services import drive_note_deleted_search
from src.services.drive_note_deleted_search import DriveNoteDeletedSearch
from tests.unit.services.test_drive_note_deleted_search_2b import U, _item_note


async def test_a_failing_live_read_is_logged_and_no_note_is_dormant(monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(drive_note_deleted_search, "logger", log)
    notes = InMemoryDriveNoteRepository()
    notes.notes[(U, "n-b2")] = _item_note("b2")

    async def live_tags(user_id):
        raise RuntimeError("store down")

    search = DriveNoteDeletedSearch(notes, AsyncMock(spec=EmbeddingService), embedding_space="s",
                                    mount_name_of=AsyncMock(return_value="Drive"), nonce=lambda: "n0nce000",
                                    live_tags=live_tags)

    hits, _ = await search(U, [], None)

    assert hits == []
    assert log.warning.call_count == 1 and "live connections read failed" in log.warning.call_args[0][0]
