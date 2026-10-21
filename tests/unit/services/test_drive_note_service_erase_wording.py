"""A partial erase is refused with neutral wording (core re-review 2): the notes left may have failed on a concurrent
write or on a store outage, and the owner does the same thing either way — he asks again."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_note import DriveNoteRefused, EraseResult, NoteRefusal
from src.ports.drive_index_repository import DriveIndexRepository
from src.ports.drive_note_repository import DriveNoteRepository
from src.ports.embedding_service import EmbeddingService
from src.ports.ephemeral_store import EphemeralStore
from src.ports.security_port import SecurityPort
from src.ports.user_drive_port import UserDrivePort
from src.ports.user_repository import UserRepository
from src.services.drive_note_service import DriveNoteService


async def test_a_partial_erase_names_the_counts_and_no_cause():
    notes = AsyncMock(spec=DriveNoteRepository)
    notes.get_many.return_value = {}
    notes.erase.return_value = EraseResult(erased=2, left=("n3",))
    store = AsyncMock(spec=EphemeralStore)
    store.get_and_delete.return_value = {"scope": "all", "note_ids": ["n1", "n2", "n3"]}
    users = AsyncMock(spec=UserRepository)
    users.get_user.return_value = SimpleNamespace(config=SimpleNamespace(drive_indexing_enabled=True))
    service = DriveNoteService(notes, AsyncMock(spec=UserDrivePort), AsyncMock(spec=DriveIndexRepository),
                               AsyncMock(spec=EmbeddingService), AsyncMock(spec=SecurityPort), users,
                               embedding_space="s", erase_requests=store)
    with pytest.raises(DriveNoteRefused) as info:
        await service.erase("u", "c1")
    assert info.value.reason == NoteRefusal.CONFLICT
    assert (info.value.detail["erased"], info.value.detail["left"]) == (2, 1)
    text = str(info.value)
    assert "2 notes were erased" in text and "1 could not be erased right now" in text and "again" in text
    assert "same time" not in text
