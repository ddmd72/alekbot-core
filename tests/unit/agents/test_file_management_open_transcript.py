"""Opening a recording (USER_DRIVE_INDEXING_RFC §27.11, Δ121): open_file_from_drive until the cut, DriveOperations.open
since (DRIVE_SPECIALIST_RFC §5.5, §8.1: `open_drive_file`, and `get_recording_link` for `original`)."""
from unittest.mock import AsyncMock

import pytest

from src.domain.user_drive import DriveItem, drive_label
from src.infrastructure.agent_manifest import Intent
from src.infrastructure.drive_operations import DriveOperations
from src.services.drive_search_service import DriveSearchService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService

VIDEO = DriveItem(item_id="v1", name="clip.mp4", path="Rec/clip.mp4", is_folder=False, size_bytes=5000,
                  mime_type="video/mp4", web_url="https://drive.example/clip")
AUDIO = DriveItem(item_id="s1", name="call.m4a", path="Rec/call.m4a", is_folder=False, size_bytes=5000,
                  mime_type="audio/mp4", web_url="https://drive.example/call")


CTX = {"user_id": "u1", "account_id": "a1"}


@pytest.fixture
def conversion():
    return AsyncMock(spec=FileConversionService)


@pytest.fixture
def search():
    svc = AsyncMock(spec=DriveSearchService)
    svc.archived_text.return_value = None   # §27.9.11: not a file of the bot's _Transcripts folder
    return svc


@pytest.fixture
def notifier():
    return AsyncMock()


@pytest.fixture
def agent(conversion, search, notifier):
    return DriveOperations(conversion_service=conversion, notification=notifier,
                           drive_service=AsyncMock(spec=UserDriveService), drive_search=search)


async def test_open_transcribed_audio_returns_wrapped_transcript(agent, conversion, search):
    conversion.get_drive_item.return_value = AUDIO
    search.transcript_text.return_value = "REMINDER\n<drive_document>[00:00] hi</drive_document>"
    resp = await agent.open(CTX, "u1", "drive:s1")
    assert resp.success and resp.text == search.transcript_text.return_value
    search.transcript_text.assert_awaited_once_with("u1", "s1", path="Rec/call.m4a",
                                                   label=drive_label(AUDIO))
    conversion.resolve_bytes.assert_not_called()
    conversion.resolve_content.assert_not_called()


async def test_open_transcribed_video_never_sends_bytes(agent, conversion, search, notifier):
    conversion.get_drive_item.return_value = VIDEO
    search.transcript_text.return_value = "wrapped transcript"
    resp = await agent.open(CTX, "u1", "drive:v1")
    assert resp.text == "wrapped transcript"
    conversion.resolve_bytes.assert_not_called()
    notifier.notify_file_bytes.assert_not_called()


@pytest.mark.parametrize("item", [AUDIO, VIDEO])
async def test_original_true_returns_link_only(agent, conversion, search, notifier, item):
    conversion.get_drive_item.return_value = item
    resp = await agent.open(CTX, "u1", item.ref, original=True)
    assert resp.success and item.web_url in resp.text
    conversion.resolve_bytes.assert_not_called()
    conversion.resolve_content.assert_not_called()
    notifier.notify_file_bytes.assert_not_called()
    search.transcript_text.assert_not_called()


async def test_generic_mime_ogg_opens_as_transcript_and_honours_original(agent, conversion, search):
    ogg = DriveItem(item_id="o1", name="memo.ogg", path="Rec/memo.ogg", is_folder=False, size_bytes=5000,
                    mime_type="application/octet-stream", web_url="https://drive.example/memo")
    conversion.get_drive_item.return_value = ogg
    search.transcript_text.return_value = "wrapped ogg transcript"
    resp = await agent.open(CTX, "u1", ogg.ref)
    assert resp.success and resp.text == "wrapped ogg transcript"
    conversion.resolve_bytes.assert_not_called()
    resp = await agent.open(CTX, "u1", ogg.ref, original=True)
    assert ogg.web_url in resp.text


async def test_untranscribed_video_keeps_file_branch(agent, conversion, search, notifier):
    conversion.get_drive_item.return_value = VIDEO
    conversion.resolve_bytes.return_value = b"video"
    search.transcript_text.return_value = None
    resp = await agent.open(CTX, "u1", "drive:v1")
    assert resp.text.endswith("has been sent to the user as a file.")
    notifier.notify_file_bytes.assert_awaited_once()


async def test_a_lookup_failure_keeps_the_file_branch(agent, conversion, search, notifier):
    conversion.get_drive_item.return_value = VIDEO
    conversion.resolve_bytes.return_value = b"video"
    search.transcript_text.side_effect = RuntimeError("boom")
    resp = await agent.open(CTX, "u1", "drive:v1")
    assert resp.text.endswith("has been sent to the user as a file.")


def test_manifest_original_param_described():
    from src.infrastructure.agent_manifest import FILE_MANAGEMENT
    schema = FILE_MANAGEMENT.context_schemas[Intent.OPEN_FILE_FROM_DRIVE]["original"]
    assert schema["type"] == "boolean"
    assert schema["description"] == ("open_file_from_drive only: set true only when the user asks for the recording "
                                     "itself, not its content")
