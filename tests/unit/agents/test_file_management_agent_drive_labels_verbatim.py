"""An attached or sent drive file is named by its real path (USER_DRIVE_INDEXING_RFC §10.4, D2): the label sits
outside any wrapper, next to the reference-material reminder, and is not entity-escaped, so the path the model
copies back resolves. The open is DriveOperations.open since the cut (DRIVE_SPECIALIST_RFC §5.5)."""
from unittest.mock import AsyncMock

import pytest

from src.domain import drive_ref
from src.domain.drive_search import REFERENCE_REMINDER_ATTACHED
from src.domain.user_drive import DriveItem
from src.infrastructure.drive_operations import DriveOperations
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from tests.support.drive_service_spec_mock import spec_drive_service

PDF = DriveItem(item_id="p1", name="Tom's plan.pdf", path="R&D/Tom's plan.pdf", is_folder=False,
                size_bytes=2048, mime_type="application/pdf")
CLIP = DriveItem(item_id="v1", name="Q&A.mp4", path="R&D/Q&A.mp4", is_folder=False, size_bytes=100,
                 mime_type="video/mp4")


@pytest.mark.parametrize("item", [PDF, CLIP], ids=["attached", "sent"])
async def test_the_label_keeps_the_real_path(item):
    conversion = AsyncMock(spec=FileConversionService)
    drive = spec_drive_service("u1")
    drive.get_item.return_value = item
    conversion.resolve_bytes.return_value = b"%PDF" if item is PDF else b"video"
    search = AsyncMock(spec=DriveSearchService)
    search.transcript_text.return_value = None   # §27.11: an untranscribed recording keeps the file branch
    search.archived_text.return_value = None   # §27.9.11: not a file of the bot's _Transcripts folder
    agent = DriveOperations(conversion_service=conversion, notification=AsyncMock(),
                            drive_service=drive, drive_search=search)
    resp = await agent.open({"user_id": "u1", "account_id": "a1"}, "u1", drive_ref.format("a1", item.item_id))
    assert resp.text.startswith(REFERENCE_REMINDER_ATTACHED + "\n")
    assert f"[Drive: {item.path} (" in resp.text and "&amp;" not in resp.text and "&#x27;" not in resp.text
