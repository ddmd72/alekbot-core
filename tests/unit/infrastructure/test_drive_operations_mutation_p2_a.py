"""DriveOperations, mutation survivors (weekly run P2-a): a video open names the user it delivers to and reports a file
above the cap; `_place` honours an explicit default mount; a drive receipt survives a failing UI-language lookup and
logs a failing delivery. Technical ids and generic data only."""
import logging

import pytest

from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_mount import Mount
from src.domain.drive_mount_table import MountTable
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.language import LanguageCode
from src.domain.ui_messages import UIMessage
from src.domain.user_drive import DriveFileTooLargeError
from src.infrastructure.drive_operations import DriveOperations
from tests.support.drive_service_connections import ConnectionReads
from tests.support.drive_connection_service_fake import connection
from tests.unit.infrastructure.test_drive_operations_video_send import CLIP, CTX, Conversion, Drive, Notification


# -- the video open names the user it delivers to --------------------------------------------------------------------

class _StrictNotification:
    """Takes the keywords the real `notify_file_bytes` requires: a call without `user_id` fails like production."""

    def __init__(self) -> None:
        self.sent: list = []

    async def notify_file_bytes(self, *, user_id, account_id, file_bytes, filename, title,
                                channel_id_override=None, platform_override=None) -> bool:
        self.sent.append({"user_id": user_id, "account_id": account_id, "file_bytes": file_bytes,
                          "filename": filename, "title": title, "channel": channel_id_override,
                          "platform": platform_override})
        return True


async def test_a_video_open_delivers_to_the_requesting_user_in_the_origin_channel():
    note = _StrictNotification()
    ops = DriveOperations(conversion_service=Conversion(), notification=note, drive_service=Drive())
    out = await ops.open(CTX, "user-1", "drive:a1:v1")
    assert out.cls is OutcomeClass.OK and out.success
    assert note.sent == [{"user_id": "user-1", "account_id": "a1", "file_bytes": b"frames", "filename": "clip.mp4",
                          "title": "Video", "channel": "C1", "platform": "slack"}]


class _TooLarge:
    async def get_drive_item(self, ref, user_id):
        return CLIP

    async def resolve_bytes(self, ref, user_id):
        raise DriveFileTooLargeError(CLIP, 3 * 1_048_576)


async def test_a_video_above_the_cap_answers_with_the_limit_and_does_not_deliver():
    note = Notification(True)
    ops = DriveOperations(conversion_service=_TooLarge(), notification=note, drive_service=Drive())
    out = await ops.open(CTX, "user-1", "drive:a1:v1")
    assert out is not None
    assert (out.op, out.cls, out.success) == (DriveOp.OPEN, OutcomeClass.OK, True)
    assert "files above 3.0MB cannot be opened yet." in out.text
    assert note.files == []


# -- `_place` with an explicit default mount -------------------------------------------------------------------------

class _Mounts(ConnectionReads):
    def __init__(self, table: MountTable) -> None:
        self.table = table

    async def mounts(self, user_id: str) -> MountTable:
        return self.table


async def test_place_puts_an_unnamed_path_on_the_given_default_not_the_primary():
    home = Mount(connection(connection_id="conn-1", ref_tag="a1"), primary=True)
    other = Mount(connection(connection_id="conn-2", ref_tag="b2", drive_identity="fake_a:drive-2",
                             credential_id="cred-2"), primary=False)
    assert home.connection.state is ConnectionState.ACTIVE
    ops = DriveOperations(conversion_service=Conversion(), drive_service=_Mounts(MountTable((home, other))))
    by_default = await ops._place("user-1", "Reports/2026", default=other)
    assert by_default.mount is other and by_default.path == "Reports/2026"
    without = await ops._place("user-1", "Reports/2026")
    assert without.mount is home


# -- the receipt is best effort and says so in the log ---------------------------------------------------------------

class _Localization:
    def __init__(self) -> None:
        self.langs: list = []

    def get_ui_string(self, lang, ui) -> str:
        self.langs.append(lang)
        return "Deleted: {path}"


class _Language:
    def __init__(self, fail: bool) -> None:
        self.fail = fail

    async def resolve_ui_language(self, user_id: str):
        if self.fail:
            raise RuntimeError("language store down")
        return LanguageCode.EN


class _Raw:
    def __init__(self, fail: bool = False) -> None:
        self.fail, self.sent = fail, []

    async def notify_raw(self, user_id, account_id, text, **kw) -> None:
        if self.fail:
            raise RuntimeError("channel down")
        self.sent.append((user_id, account_id, text, kw))


def _receipt_ops(raw: _Raw, language: _Language) -> tuple[DriveOperations, _Localization]:
    loc = _Localization()
    return DriveOperations(conversion_service=Conversion(), notification=raw, localization=loc,
                           language_service=language), loc


async def test_a_failed_language_lookup_still_posts_the_receipt_in_english_and_logs(caplog):
    raw = _Raw()
    ops, loc = _receipt_ops(raw, _Language(fail=True))
    with caplog.at_level(logging.WARNING):
        await ops._receipt(CTX, "user-12345678", UIMessage.DRIVE_DELETED_FILE, path="Inbox/a.txt")
    assert loc.langs == [LanguageCode.EN]
    assert raw.sent == [("user-12345678", "a1", "Deleted: Inbox/a.txt",
                         {"channel_id_override": "C1", "platform_override": "slack", "literal": True})]
    assert any("UI language lookup failed for user-123" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("fail_language", [False, True])
async def test_a_failed_delivery_is_logged_and_never_raised(caplog, fail_language):
    raw = _Raw(fail=True)
    ops, _ = _receipt_ops(raw, _Language(fail=fail_language))
    with caplog.at_level(logging.WARNING):
        await ops._receipt(CTX, "user-1", UIMessage.DRIVE_DELETED_FILE, path="Inbox/a.txt")
    assert raw.sent == []
    failures = [r for r in caplog.records if "drive receipt failed (" in r.getMessage()]
    assert len(failures) == 1 and failures[0].exc_info is not None
