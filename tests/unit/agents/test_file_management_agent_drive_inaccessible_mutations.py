"""Mutations on a `drive:` ref the index knows the bot cannot open (USER_DRIVE_INDEXING_RFC §26.2, Δ55, review of S1):
move, delete and update answer the index's access sentence instead of "no longer on the drive"; an item the index does
not know keeps that answer. The mutations are DriveOperations' since the cut (DRIVE_SPECIALIST_RFC §5.5)."""
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_search_texts import ACCESS_NOTE_FILE
from src.domain.user_drive import DriveItemNotFoundError
from src.infrastructure.drive_operations import GONE, DriveOperations
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService


CTX = {"user_id": "u1", "account_id": "a1"}


@pytest.fixture
def drive():
    d = AsyncMock(spec=UserDriveService)
    for name in ("move", "delete", "append_text", "get_item"):
        getattr(d, name).side_effect = DriveItemNotFoundError("f1")
    return d


@pytest.fixture
def search():
    s = AsyncMock(spec=DriveSearchService)
    s.access_note.side_effect = lambda user_id, item_id=None, path=None: ACCESS_NOTE_FILE if item_id == "f1" else None
    return s


@pytest.fixture
def agent(drive, search):
    return DriveOperations(conversion_service=AsyncMock(spec=FileConversionService), drive_service=drive,
                           drive_search=search)


@pytest.mark.parametrize("method, args, kwargs", [
    ("move", (), {"folder": "Archive"}),
    ("delete", (), {}),
    ("append", ("x",), {}),
])
async def test_a_mutation_of_a_closed_file_answers_the_access_sentence(agent, method, args, kwargs):
    resp = await getattr(agent, method)(CTX, "u1", "drive:f1", *args, **kwargs, wait_s=100.0)
    assert not resp.success and resp.text == ACCESS_NOTE_FILE


async def test_an_unknown_item_still_answers_gone(agent, drive):
    drive.delete.side_effect = DriveItemNotFoundError("f9")
    resp = await agent.delete(CTX, "u1", "drive:f9", wait_s=100.0)
    assert resp.text == GONE
