"""DriveOperations.search_deleted carries the search's has_passage onto the outcome (`stored_text_shown`)."""
from unittest.mock import AsyncMock

from src.domain.drive_deleted_result import DeletedSearchResult
from src.domain.user_drive import DriveItem
from src.infrastructure.drive_operations import DriveOperations
from src.services.drive_search_service import DriveSearchService

CTX = {"user_id": "u1", "account_id": "a1"}
MEMO = DriveItem(item_id="m1", name="meeting.md", path="_Transcripts/Archive/meeting.md", is_folder=False)


def _ops(answer):
    search = AsyncMock(spec=DriveSearchService)
    search.search_deleted.return_value = answer
    return DriveOperations(conversion_service=AsyncMock(), drive_service=AsyncMock(), drive_search=search)


async def test_a_result_with_a_passage_marks_the_outcome():
    ops = _ops(DeletedSearchResult("Deleted files (1):", [MEMO], has_passage=True))
    out = await ops.search_deleted(CTX, "u1", "meeting")
    assert out.success and out.stored_text_shown is True


async def test_a_result_without_a_passage_does_not():
    ops = _ops(DeletedSearchResult("Deleted files (1):", [MEMO]))
    assert (await ops.search_deleted(CTX, "u1", "meeting")).stored_text_shown is False
