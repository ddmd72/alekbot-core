"""
Wire tests for the OneDriveAdapter pieces the bot-managed mirror folder needs (USER_DRIVE_INDEXING_RFC §27.9,
Δ191-Δ196, Δ216): the upload conflict mode, 409/507/400 mapping, child_by_name, area_path, cTag and created time.
Mock boundary: aiohttp.ClientSession (docs/how_to/ADAPTER_WIRE_TESTING.md).
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import pytest

from src.adapters.microsoft.onedrive_adapter import ONEDRIVE_PROVIDER, OneDriveAdapter
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.user_drive import (
    DriveItemNotFoundError, DriveNameConflictError, DrivePathTooLong, DriveStorageFull,
)
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.composition.connector_registry import ConnectorRegistry
from tests.support.oauth_credentials_store import mirror_get
from tests.support.drive_handles import drive_handle

HANDLE = drive_handle("u1")

_ROOT = {"id": "root1", "name": "Alek-bot", "folder": {"childCount": 1},
         "parentReference": {"path": "/drive/root:"}}
_FILE = {"id": "f1", "name": "a.md", "size": 3, "file": {"mimeType": "text/markdown"}, "cTag": "c1,2",
         "createdDateTime": "2026-10-01T08:00:00Z", "lastModifiedDateTime": "2026-10-02T08:00:00Z",
         "parentReference": {"id": "p1", "path": "/drive/root:/Alek-bot/_Transcripts"}}
_TOO_LONG = {"error": {"code": "invalidRequest", "innerError": {"code": "pathIsTooLong"}}}
_PLAIN_400 = {"error": {"code": "invalidRequest", "message": "bad"}}


def _resp(json_data=None, status=200):
    r = MagicMock()
    r.status = status
    r.ok = status < 300
    r.headers = {}
    r.json = AsyncMock(return_value=json_data)
    r.text = AsyncMock(return_value=json.dumps(json_data))
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _session(**methods):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    for name, value in methods.items():
        target = getattr(s, name)
        if isinstance(value, list):
            target.side_effect = value
        else:
            target.return_value = value
    return s


def _adapter():
    oauth = AsyncMock(spec=OAuthCredentialsPort)
    oauth.get.return_value = OAuthCredential(
        user_id="u1",
        account_id="acct-1",
        kind=ONEDRIVE_PROVIDER,
        integration=Integration.DRIVE,
        account=ProviderAccount(realm="microsoft", subject="", label=""),
        access_token="tok",
        refresh_token="r",
        token_expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        scopes=[],
    )
    mirror_get(oauth)
    return OneDriveAdapter(oauth, "cid", "csecret", connector=ConnectorRegistry().get("microsoft_onedrive"))


async def test_default_conflict_is_rename():
    session = _session(put=_resp(_FILE, 201), get=_resp(_ROOT))
    with patch("aiohttp.ClientSession", return_value=session):
        await _adapter().upload(HANDLE, "p1", "a.md", b"x", "text/markdown")
    assert session.put.call_args.args[0] == (
        "https://graph.microsoft.com/v1.0/me/drive/items/p1:/a.md:/content?@microsoft.graph.conflictBehavior=rename")


@pytest.mark.parametrize("mode", ["fail", "replace"])
async def test_fail_and_replace_in_simple_put(mode):
    session = _session(put=_resp(_FILE, 201), get=_resp(_ROOT))
    with patch("aiohttp.ClientSession", return_value=session):
        await _adapter().upload(HANDLE, "p1", "a.md", b"x", "text/markdown", conflict=mode)
    assert session.put.call_args.args[0].endswith(f"/content?@microsoft.graph.conflictBehavior={mode}")


@pytest.mark.parametrize("mode", ["fail", "replace", "rename"])
async def test_fail_and_replace_in_upload_session(mode):
    data = b"0" * (6 * 1024 * 1024)
    session = _session(post=_resp({"uploadUrl": "https://up.example/s"}),
                       put=[_resp({}, 202), _resp(_FILE, 201)], get=_resp(_ROOT))
    with patch("aiohttp.ClientSession", return_value=session):
        await _adapter().upload(HANDLE, "p1", "big.md", data, "text/markdown", conflict=mode)
    assert session.post.call_args.kwargs["json"] == {"item": {"@microsoft.graph.conflictBehavior": mode}}


async def test_put_fail_409_raises_name_conflict():
    session = _session(put=_resp({"error": {"code": "nameAlreadyExists"}}, 409))
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveNameConflictError):
        await _adapter().upload(HANDLE, "p1", "a.md", b"x", "text/markdown", conflict="fail")


async def test_patch_rename_409_raises_name_conflict():
    session = _session(patch=_resp({"error": {"code": "nameAlreadyExists"}}, 409))
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveNameConflictError):
        await _adapter().move(HANDLE, "f1", new_name="b.md")


async def test_507_raises_storage_full():
    session = _session(put=_resp({"error": {"code": "quotaLimitReached"}}, 507))
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveStorageFull):
        await _adapter().upload(HANDLE, "p1", "a.md", b"x", "text/markdown")


async def test_400_path_is_too_long_raises_path_too_long():
    session = _session(put=_resp(_TOO_LONG, 400))
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DrivePathTooLong):
        await _adapter().upload(HANDLE, "p1", "a.md", b"x", "text/markdown")


async def test_other_400_stays_a_value_error():
    session = _session(put=_resp(_PLAIN_400, 400))
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(ValueError) as exc:
        await _adapter().upload(HANDLE, "p1", "a.md", b"x", "text/markdown")
    assert not isinstance(exc.value, DrivePathTooLong)


async def test_folder_create_400_invalid_request_raises_path_too_long():
    session = _session(post=_resp(_PLAIN_400, 400))
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DrivePathTooLong):
        await _adapter().create_folder(HANDLE, "root1", "deep")


async def test_folder_create_under_root_uses_the_item_path():
    folder = {"id": "d1", "name": "_Transcripts", "folder": {"childCount": 0},
              "parentReference": {"id": "root1", "path": "/drives/xyz/root:/Alek-bot"}}
    session = _session(post=_resp(folder, 201), get=_resp(_ROOT))
    with patch("aiohttp.ClientSession", return_value=session):
        item = await _adapter().create_folder(HANDLE, "root1", "_Transcripts")
    url = session.post.call_args.args[0]
    assert url.endswith("/me/drive/items/root1/children") and "special/approot/children" not in url
    assert session.post.call_args.kwargs["json"]["@microsoft.graph.conflictBehavior"] == "fail"
    assert item.path == "_Transcripts"


async def test_child_by_name_found():
    session = _session(get=_resp(_FILE))
    with patch("aiohttp.ClientSession", return_value=session):
        change = await _adapter().child_by_name(HANDLE, "p1", "a.md")
    assert session.get.call_args.args[0].endswith("/me/drive/items/p1:/a.md:")
    assert change is not None and change.item_id == "f1" and change.content_version == "c1,2"


async def test_child_by_name_404_is_none():
    session = _session(get=_resp({"error": {"code": "itemNotFound"}}, 404))
    with patch("aiohttp.ClientSession", return_value=session):
        assert await _adapter().child_by_name(HANDLE, "p1", "a.md") is None


async def test_child_by_name_quotes_the_name():
    name = "_Встреча #1 100% a.md"
    session = _session(get=_resp(_FILE))
    with patch("aiohttp.ClientSession", return_value=session):
        await _adapter().child_by_name(HANDLE, "p1", name)
    assert session.get.call_args.args[0].endswith(f"/me/drive/items/p1:/{quote(name, safe='')}:")
    assert "_%D0" in session.get.call_args.args[0] and "%23" in session.get.call_args.args[0]


async def test_child_by_name_other_errors_propagate():
    session = _session(get=_resp(_PLAIN_400, 400))
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(ValueError):
        await _adapter().child_by_name(HANDLE, "p1", "a.md")


async def test_area_path_from_approot():
    session = _session(get=_resp(_ROOT))
    with patch("aiohttp.ClientSession", return_value=session):
        assert await _adapter().area_path(HANDLE) == "/Alek-bot"
    nested = {**_ROOT, "name": "Приложения", "parentReference": {"path": "/drive/root:/Apps"}}
    with patch("aiohttp.ClientSession", return_value=_session(get=_resp(nested))):
        assert await _adapter().area_path(HANDLE) == "/Apps/Приложения"


async def test_item_carries_ctag():
    session = _session(get=[_resp(_FILE), _resp(_ROOT)])
    with patch("aiohttp.ClientSession", return_value=session):
        item = await _adapter().get_item(HANDLE, "f1")
    assert item.ctag == "c1,2"


async def test_upload_item_carries_ctag():
    session = _session(put=_resp(_FILE, 201), get=_resp(_ROOT))
    with patch("aiohttp.ClientSession", return_value=session):
        item = await _adapter().upload(HANDLE, "p1", "a.md", b"x", "text/markdown", conflict="fail")
    assert item.ctag == "c1,2"


async def test_change_carries_created_at():
    session = _session(get=_resp(_FILE))
    with patch("aiohttp.ClientSession", return_value=session):
        change = await _adapter().get_change(HANDLE, "f1")
    assert change.created_at == datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


async def test_change_without_created_time_is_none():
    raw = {k: v for k, v in _FILE.items() if k != "createdDateTime"}
    with patch("aiohttp.ClientSession", return_value=_session(get=_resp(raw))):
        assert (await _adapter().get_change(HANDLE, "f1")).created_at is None
