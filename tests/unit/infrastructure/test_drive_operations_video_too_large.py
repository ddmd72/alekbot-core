"""DriveOperations: opening a video above the download cap says so with the cap, and sends nothing."""
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.user_drive import DriveFileTooLargeError
from src.infrastructure.drive_operations import DriveOperations
from tests.unit.infrastructure.test_drive_operations_video_send import CLIP, CTX, Drive, Notification


class _TooLarge:
    async def get_drive_item(self, ref, user_id):
        return CLIP

    async def resolve_bytes(self, ref, user_id):
        raise DriveFileTooLargeError(CLIP, 2 * 1_048_576)


async def test_a_video_above_the_cap_is_an_ok_note_with_the_limit_and_nothing_is_sent():
    note = Notification(True)
    ops = DriveOperations(conversion_service=_TooLarge(), notification=note, drive_service=Drive())
    out = await ops.open(CTX, "u1", "drive:a1:v1")
    assert (out.op, out.cls) == (DriveOp.OPEN, OutcomeClass.OK)
    assert out.text.endswith("Videos/clip.mp4' is 5B; files above 2.0MB cannot be opened yet.")
    assert note.files == []
