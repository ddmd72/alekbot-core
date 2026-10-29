"""DriveOperations: a video open reports the upload's result, never "sent" for an upload that failed."""
import pytest

from src.domain.drive_op import DriveOp
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.user_drive import DriveItem
from src.infrastructure.drive_operations import VIDEO_NOT_SENT, DriveOperations

CTX = {"user_id": "u1", "account_id": "a1", "origin_channel_id": "C1", "origin_platform": "slack"}
CLIP = DriveItem(item_id="v1", name="clip.mp4", path="Videos/clip.mp4", is_folder=False, size_bytes=5,
                 mime_type="video/mp4")


class Conversion:
    async def get_drive_item(self, ref, user_id):
        return CLIP

    async def resolve_bytes(self, ref, user_id):
        return b"frames"


class Drive:
    """Only present so the drive is configured; opening a video never calls it."""


class Notification:
    def __init__(self, result: bool) -> None:
        self.result, self.files = result, []

    async def notify_file_bytes(self, **kw) -> bool:
        self.files.append(kw)
        return self.result


def _ops(result: bool) -> tuple[DriveOperations, Notification]:
    note = Notification(result)
    return DriveOperations(conversion_service=Conversion(), notification=note, drive_service=Drive()), note


@pytest.mark.parametrize("call", ["open", "workspace"])
async def test_a_failed_upload_is_a_failure_outcome(call):
    ops, note = _ops(False)
    if call == "open":
        out = await ops.open(CTX, "u1", "drive:v1")
    else:
        out = await ops.fetch_stored(CTX, "u1", "workspace:files/clip.mp4")
    assert (out.op, out.cls, out.success) == (DriveOp.OPEN, OutcomeClass.FAILED, False)
    assert out.text == VIDEO_NOT_SENT and "sent to the user" not in out.text
    assert len(note.files) == 1


@pytest.mark.parametrize("call,tail", [("open", "has been sent to the user as a file."),
                                       ("workspace", "has been sent to the user as a file attachment.")])
async def test_a_successful_upload_is_unchanged(call, tail):
    ops, _ = _ops(True)
    if call == "open":
        out = await ops.open(CTX, "u1", "drive:v1")
    else:
        out = await ops.fetch_stored(CTX, "u1", "workspace:files/clip.mp4")
    assert out.cls is OutcomeClass.OK and out.success and out.text.endswith(tail)
