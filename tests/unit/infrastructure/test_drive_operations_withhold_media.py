"""`DriveOperations.open(..., withhold_media=True)`: the drive connector's media pre-check (drift D1, D6). A video, or a
recording with no stored transcript, is answered on its metadata before any download, with a machine code and no text;
a recording with a stored transcript opens as its transcript; a document, a PDF or an image opens exactly as without
the flag. Without the flag (the chat) every open is today's. Invented items only."""
from __future__ import annotations

from typing import Any, List, Optional

import pytest

from src.domain.document_open import OpenedFile
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_text_extent import TextExtent
from src.domain.drive_withheld import Withheld
from src.domain.user_drive import DriveItem
from src.infrastructure.drive_operations import DriveOperations
from tests.support.drive_service_connections import ConnectionReads, ref
from tests.support.opened_file import opened_body, opened_native

CTX = {"user_id": "u1", "account_id": "a1", "origin_channel_id": "C1", "origin_platform": "slack"}
VIDEO = DriveItem(item_id="v1", name="standup.mp4", path="R&D/standup.mp4", is_folder=False, size_bytes=4000,
                  mime_type="video/mp4")
AUDIO = DriveItem(item_id="r1", name="standup.m4a", path="R&D/standup.m4a", is_folder=False, size_bytes=2000,
                  mime_type="audio/mp4")
PLAN = DriveItem(item_id="d1", name="Tom's plan.docx", path="R&D/Tom's plan.docx", is_folder=False, size_bytes=1000,
                 mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
INVOICE = DriveItem(item_id="p1", name="FA-0137.pdf", path="R&D/FA-0137.pdf", is_folder=False, size_bytes=3000,
                    mime_type="application/pdf")
PHOTO = DriveItem(item_id="i1", name="12 Oak Street.png", path="R&D/12 Oak Street.png", is_folder=False,
                  size_bytes=2000, mime_type="image/png")
HEIC = DriveItem(item_id="h1", name="12 Oak Street.heic", path="R&D/12 Oak Street.heic", is_folder=False,
                 size_bytes=2000, mime_type="image/heic")
TRANSCRIPT = "REMINDER\n<drive_document>[00:00] The plan for invoice FA-0137 is agreed.</drive_document>"


class Conversion:
    """The conversion service: records every byte read (a download) and every open (a read that downloads)."""

    def __init__(self, opened: OpenedFile) -> None:
        self.opened = opened
        self.downloads: List[str] = []
        self.opens: List[str] = []

    async def resolve_bytes(self, ref: str, user_id: str) -> bytes:
        self.downloads.append(ref)
        return b"frames"

    async def open(self, ref: str, user_id: str, account_id: str, **kw: Any) -> OpenedFile:
        self.opens.append(ref)
        return self.opened


class Drive(ConnectionReads):
    def __init__(self, item: DriveItem) -> None:
        self.item = item

    async def get_item(self, handle: Any, item_id: str) -> DriveItem:
        return self.item


class Search:
    """The search service's reads an open makes: no archived transcript, an unknown extent, the stored transcript."""

    def __init__(self, transcript: Optional[str]) -> None:
        self.transcript = transcript

    async def archived_text(self, conn: Any, item_id: str, **kw: Any) -> Optional[str]:
        return None

    async def text_extent(self, conn: Any, item_id: str) -> TextExtent:
        return TextExtent.UNKNOWN

    async def transcript_text(self, conn: Any, item_id: str, **kw: Any) -> Optional[str]:
        return self.transcript


class Notification:
    def __init__(self) -> None:
        self.files: List[dict] = []

    async def notify_file_bytes(self, **kw: Any) -> bool:
        self.files.append(kw)
        return True


class World:
    def __init__(self, item: DriveItem, *, transcript: Optional[str] = None, search: bool = True,
                 notification: bool = True, opened: Optional[OpenedFile] = None) -> None:
        self.conversion = Conversion(opened or opened_body("Step 1. Send invoice FA-0137."))
        self.notification = Notification() if notification else None
        self.ops = DriveOperations(conversion_service=self.conversion, notification=self.notification,
                                   drive_service=Drive(item), drive_search=Search(transcript) if search else None)
        self.ref = ref(item.item_id)

    async def open(self, **kw: Any):
        return await self.ops.open(CTX, "u1", self.ref, **kw)

    @property
    def posted(self) -> int:
        return len(self.notification.files) if self.notification is not None else 0


@pytest.fixture(autouse=True)
def fixed_nonce(monkeypatch):
    monkeypatch.setattr("src.infrastructure.drive_operations.secrets.token_hex", lambda n: "0123456789abcdef")


# -- withheld before any download ------------------------------------------------------------------------------------

@pytest.mark.parametrize("notification", [True, False])
async def test_a_video_is_withheld_without_a_download_or_a_post(notification):
    world = World(VIDEO, notification=notification)
    out = await world.open(withhold_media=True)
    assert out.withheld is Withheld.VIDEO and out.withheld == "video"
    assert (out.op, out.cls, out.success, out.text) == (DriveOp.OPEN, OutcomeClass.OK, True, "")
    assert [i.item_id for i in out.items] == ["v1"] and out.ref_tag == "a1"
    assert world.conversion.downloads == [] and world.conversion.opens == [] and world.posted == 0


@pytest.mark.parametrize("search", [True, False])
async def test_a_recording_with_no_stored_transcript_is_withheld_without_a_download(search):
    world = World(AUDIO, transcript=None, search=search)
    out = await world.open(withhold_media=True)
    assert out.withheld is Withheld.RECORDING_UNTRANSCRIBED and out.withheld == "recording_untranscribed"
    assert (out.cls, out.success, out.text) == (OutcomeClass.OK, True, "")
    assert [i.item_id for i in out.items] == ["r1"]
    assert world.conversion.downloads == [] and world.conversion.opens == [] and world.posted == 0


@pytest.mark.parametrize("item", [AUDIO, VIDEO])
async def test_a_recording_with_a_stored_transcript_opens_as_it(item):
    on = World(item, transcript=TRANSCRIPT)
    off = World(item, transcript=TRANSCRIPT)
    out = await on.open(withhold_media=True)
    assert out.text == TRANSCRIPT and out.body and out.withheld is None
    assert out == await off.open()
    assert on.conversion.downloads == [] and on.conversion.opens == [] and on.posted == 0


# -- every other file opens as without the flag ----------------------------------------------------------------------

@pytest.mark.parametrize("item, opened", [
    (PLAN, opened_body("Step 1. Send invoice FA-0137.")),
    (INVOICE, opened_body("Invoice FA-0137. 12 Oak Street.")),
    (PHOTO, opened_body("A photo of 12 Oak Street.")),
    (PHOTO, opened_native("/tmp/12_oak_street.png", "image/png")),
    (HEIC, opened_body("unused")),
])
async def test_a_document_a_pdf_or_an_image_opens_exactly_as_without_the_flag(item, opened):
    on = World(item, opened=opened)
    off = World(item, opened=opened)
    with_flag = await on.open(withhold_media=True)
    without = await off.open()
    assert with_flag == without and with_flag.withheld is None
    assert on.conversion.opens == off.conversion.opens


# -- without the flag: today's open ----------------------------------------------------------------------------------

async def test_without_the_flag_a_video_is_downloaded_and_sent_as_today():
    world = World(VIDEO)
    out = await world.open()
    assert out.success and out.cls is OutcomeClass.OK and out.withheld is None
    assert out.text.endswith("has been sent to the user as a file.")
    assert world.conversion.downloads == [ref("v1")] and world.posted == 1


async def test_without_the_flag_a_video_with_no_notifier_downloads_then_fails_as_today():
    world = World(VIDEO, notification=False)
    out = await world.open()
    assert (out.cls, out.success, out.withheld) == (OutcomeClass.FAILED, False, None)
    assert out.text == "File delivery is not configured — cannot send the video."
    assert world.conversion.downloads == [ref("v1")]


async def test_without_the_flag_an_untranscribed_recording_is_read_as_today():
    world = World(AUDIO, transcript=None)
    out = await world.open()
    assert out.withheld is None and world.conversion.opens == [ref("r1")]
