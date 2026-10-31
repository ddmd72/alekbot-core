"""Wire tests: a path-addressed name (`items/{id}:/{name}:`) never reaches a log line or an error text of the OneDrive
adapter (USER_DRIVE_INDEXING_RFC §27.12; final review I2). The mirror uploads and reads by name the `.md` files named
after other people's recordings; `_where` replaces the segment with its length. Mock boundary: aiohttp.ClientSession."""
from unittest.mock import patch

import pytest

from src.adapters.microsoft.onedrive_adapter import _where
from src.domain.user_drive import DriveItemNotFoundError, DriveNameConflictError, DrivePathTooLong, DriveStorageFull

from tests.unit.adapters.test_onedrive_mirror_writes_wire import _PLAIN_400, _TOO_LONG, _adapter, _resp, _session
from tests.support.drive_handles import drive_handle

HANDLE = drive_handle("u1")

NAME = "Secret Interview Ганна.mp4.md"


def _logged(log) -> str:
    return " ".join(str(a) for call in log.method_calls for a in call.args)


def test_where_replaces_a_named_segment_by_its_length():
    assert _where("/me/drive/items/p1:/a%20b.md:/content?@microsoft.graph.conflictBehavior=fail") == \
        "/me/drive/items/p1:/<6 chars>:/content"
    assert _where("/me/drive/items/p1:/abc.md:") == "/me/drive/items/p1:/<6 chars>:"
    assert _where("https://graph.microsoft.com/v1.0/me/drive/special/approot/delta?token=x") == \
        "https://graph.microsoft.com/v1.0/me/drive/special/approot/delta"
    assert _where("/me/drive/items/p1/children") == "/me/drive/items/p1/children"


@pytest.mark.parametrize("status, body, raises", [
    (507, {"error": {"code": "quotaLimitReached"}}, DriveStorageFull),
    (400, _TOO_LONG, DrivePathTooLong),
    (400, _PLAIN_400, ValueError),
    (500, {"error": {"code": "generalException"}}, ValueError),
    (409, {"error": {"code": "nameAlreadyExists"}}, DriveNameConflictError),
    (404, {"error": {"code": "itemNotFound"}}, DriveItemNotFoundError),
])
async def test_upload_never_logs_the_file_name(status, body, raises):
    session = _session(put=_resp(body, status))
    adapter = _adapter()
    with patch("aiohttp.ClientSession", return_value=session), \
            patch("src.adapters.microsoft.onedrive_adapter.logger") as log, pytest.raises(raises) as exc:
        await adapter.upload(HANDLE, "p1", NAME, b"x", "text/markdown", conflict="fail")
    text = _logged(log) + " " + str(exc.value)
    assert "Secret" not in text and "Interview" not in text and "%D0" not in text and "Secret%20" not in text
    if log.method_calls:
        assert f"<{len(NAME)} chars>" in _logged(log)


@pytest.mark.parametrize("status, body", [(400, _PLAIN_400), (500, {"error": {"code": "generalException"}}),
                                          (507, {"error": {"code": "quotaLimitReached"}})])
async def test_child_by_name_never_logs_the_name(status, body):
    session = _session(get=_resp(body, status))
    adapter = _adapter()
    with patch("aiohttp.ClientSession", return_value=session), \
            patch("src.adapters.microsoft.onedrive_adapter.logger") as log, pytest.raises(Exception) as exc:
        await adapter.child_by_name(HANDLE, "p1", NAME)
    text = _logged(log) + " " + str(exc.value)
    assert log.method_calls and f"<{len(NAME)} chars>" in _logged(log)
    assert "Secret" not in text and "Interview" not in text and "%D0" not in text
