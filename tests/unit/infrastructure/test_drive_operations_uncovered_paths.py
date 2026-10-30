"""DriveOperations: listing tails, empty append, non-text workspace opens, a video with no delivery, ambiguous search.

Pins today's behaviour of paths a coverage run found untested. Fakes carry the semantics; assertions are on the
outcome (class, text) and on what reached the drive or the notification fake."""
import pytest

from src.domain.drive_op import DriveOp
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_search import STATUS_AMBIGUOUS_FOLDER, DriveSearchResult
from src.domain.user_drive import DriveItem, ListOutcome
from src.infrastructure.drive_operations import MAX_VIDEO_ATTACH_BYTES, DriveOperations
from tests.support.drive_service_connections import ConnectionReads

CTX = {"user_id": "u1", "account_id": "a1", "origin_channel_id": "C1", "origin_platform": "slack"}
SHED = DriveItem(item_id="d1", name="Garden shed", path="Garden shed", is_folder=True)
PLAN = DriveItem(item_id="f1", name="plan.txt", path="Garden shed/plan.txt", is_folder=False, size_bytes=10,
                 mime_type="text/plain")
CLIP = DriveItem(item_id="v1", name="shed-tour.mp4", path="Garden shed/shed-tour.mp4", is_folder=False,
                 size_bytes=5, mime_type="video/mp4")


class FakeDrive(ConnectionReads):
    def __init__(self, listing: ListOutcome | None = None, items=None) -> None:
        self.listing = listing
        self.items = items or {}
        self.appends: list[tuple] = []

    async def list_folder(self, handle, folder):
        return self.listing

    async def get_item(self, handle, item_id):
        return self.items[item_id]

    async def append_text(self, handle, item_id, text):  # pragma: no cover - must not be reached
        self.appends.append((item_id, text))
        raise AssertionError("an empty append reached the drive")


class FakeConversion:
    def __init__(self, items=None, data: bytes = b"bytes", text: str = "plain text") -> None:
        self.items, self.data, self.text = items or {}, data, text
        self.byte_reads: list[str] = []
        self.text_reads: list[str] = []

    async def get_drive_item(self, ref, user_id):
        return self.items[ref]

    async def resolve_bytes(self, ref, user_id):
        self.byte_reads.append(ref)
        return self.data

    async def resolve_content(self, ref, user_id):
        self.text_reads.append(ref)
        return self.text


class FakeNotification:
    def __init__(self) -> None:
        self.files: list[dict] = []

    async def notify_file_bytes(self, **kw) -> bool:
        self.files.append(kw)
        return True  # UserNotificationService.notify_file_bytes returns True when the upload succeeded


class FakeSearch:
    def __init__(self, result) -> None:
        self.result = result

    async def search(self, user_id, request):
        return self.result

    async def archived_text(self, *a, **kw):
        return None


def _ops(**kw) -> DriveOperations:
    kw.setdefault("conversion_service", FakeConversion())
    return DriveOperations(**kw)


# -- folder listing tails ------------------------------------------------------------------------------------------

async def test_an_empty_folder_listing_says_empty():
    ops = _ops(drive_service=FakeDrive(ListOutcome(folder=SHED, items=[], truncated=False)))
    out = await ops.list_folder(CTX, "u1", "Garden shed")
    assert out.cls is OutcomeClass.OK and out.success
    lines = out.text.split("\n")
    assert lines[1:] == ["  (empty)"]
    assert "more items not shown" not in out.text


async def test_a_truncated_listing_says_more_items_are_not_shown_after_the_items():
    ops = _ops(drive_service=FakeDrive(ListOutcome(folder=SHED, items=[PLAN], truncated=True)))
    out = await ops.list_folder(CTX, "u1", "Garden shed")
    lines = out.text.split("\n")
    assert lines[-1] == "  … more items not shown; list a subfolder."
    assert "(empty)" not in out.text and "plan.txt" in lines[1]


async def test_a_truncated_empty_listing_carries_both_lines():
    ops = _ops(drive_service=FakeDrive(ListOutcome(folder=SHED, items=[], truncated=True)))
    out = await ops.list_folder(CTX, "u1", "Garden shed")
    assert out.text.split("\n")[1:] == ["  (empty)", "  … more items not shown; list a subfolder."]


# -- append with empty text ----------------------------------------------------------------------------------------

async def test_an_append_with_empty_text_is_a_request_error_and_never_reaches_the_drive():
    drive = FakeDrive()
    ops = _ops(drive_service=drive)
    out = await ops.append(CTX, "u1", "drive:f1", "", wait_s=5)
    assert (out.op, out.cls, out.success) == (DriveOp.APPEND, OutcomeClass.REQUEST_ERROR, False)
    assert out.text == "Give the text to add: a file_contents item with text."
    assert drive.appends == []


# -- opening non-text workspace:files/ items -----------------------------------------------------------------------

async def test_a_workspace_video_is_delivered_as_a_file_through_the_notification():
    conv, note = FakeConversion(data=b"frames"), FakeNotification()
    ops = _ops(conversion_service=conv, notification=note, drive_service=FakeDrive())
    out = await ops.fetch_stored(CTX, "u1", "workspace:files/shed-tour.mp4")
    assert out.cls is OutcomeClass.OK and out.success
    assert out.text.endswith("has been sent to the user as a file attachment.")
    assert note.files == [dict(user_id="u1", account_id="a1", file_bytes=b"frames", filename="shed-tour.mp4",
                               title="Video", channel_id_override="C1", platform_override="slack")]
    assert conv.text_reads == []


async def test_a_workspace_video_over_the_attach_cap_is_answered_with_text_and_not_sent():
    conv, note = FakeConversion(data=b"x" * (MAX_VIDEO_ATTACH_BYTES + 1)), FakeNotification()
    ops = _ops(conversion_service=conv, notification=note, drive_service=FakeDrive())
    out = await ops.fetch_stored(CTX, "u1", "workspace:files/shed-tour.mp4")
    assert out.cls is OutcomeClass.OK and "is too large" in out.text and note.files == []


async def test_a_workspace_video_with_no_notification_is_a_failure():
    ops = _ops(conversion_service=FakeConversion(), drive_service=FakeDrive())
    out = await ops.fetch_stored(CTX, "u1", "workspace:files/shed-tour.mp4")
    assert out.cls is OutcomeClass.FAILED and not out.success
    assert out.text == "File delivery is not configured — cannot resend the video as a file."


async def test_a_workspace_file_of_an_other_kind_is_read_as_text():
    conv = FakeConversion(text="boiler warranty, 5 years")
    ops = _ops(conversion_service=conv, drive_service=FakeDrive())
    out = await ops.fetch_stored(CTX, "u1", "workspace:files/boiler.warranty")
    assert out.cls is OutcomeClass.OK and out.body
    assert out.text == "boiler warranty, 5 years"
    assert conv.text_reads == ["workspace:files/boiler.warranty"] and conv.byte_reads == []


# -- a drive video, no notification --------------------------------------------------------------------------------

async def test_opening_a_drive_video_with_no_notification_fails_after_reading_the_bytes():
    conv = FakeConversion(items={"drive:v1": CLIP})
    ops = _ops(conversion_service=conv, drive_service=FakeDrive(items={"v1": CLIP}))
    out = await ops.open(CTX, "u1", "drive:v1")
    assert (out.op, out.cls, out.success) == (DriveOp.OPEN, OutcomeClass.FAILED, False)
    assert out.text == "File delivery is not configured — cannot send the video."
    assert conv.byte_reads == ["drive:v1"] and conv.text_reads == []


async def test_opening_a_drive_video_with_a_notification_sends_it():
    conv, note = FakeConversion(items={"drive:v1": CLIP}, data=b"frames"), FakeNotification()
    ops = _ops(conversion_service=conv, notification=note, drive_service=FakeDrive(items={"v1": CLIP}))
    out = await ops.open(CTX, "u1", "drive:v1")
    assert out.cls is OutcomeClass.OK and "has been sent to the user as a file" in out.text
    assert [(f["filename"], f["file_bytes"]) for f in note.files] == [("shed-tour.mp4", b"frames")]


# -- an ambiguous folder in a search -------------------------------------------------------------------------------

async def test_an_ambiguous_folder_search_is_ok_not_a_failure():
    """The candidate folders are shown: content the reader acts on, so the class is ok (the lone source of OK with no
    cards and no matches)."""
    result = DriveSearchResult(status=STATUS_AMBIGUOUS_FOLDER)
    ops = _ops(drive_service=FakeDrive(), drive_search=FakeSearch(result))
    out = await ops.search(CTX, "u1", query="lease", folder="Lisbon")
    assert (out.op, out.cls, out.success) == (DriveOp.SEARCH, OutcomeClass.OK, True)
    assert out.items == () and out.facts.cards == 0
