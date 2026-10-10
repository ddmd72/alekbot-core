"""
Wire tests for OneDriveAdapter. Mock boundary: aiohttp.ClientSession (HTTP layer).
Never mock at UserDrivePort level (docs/how_to/ADAPTER_WIRE_TESTING.md).
Fixtures cover percent-encoded parent paths (defensive) and the raw ones Graph returned in the spike
(RFC §7), in both prefix forms: /drive/root: and /drives/<id>/root:.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import pytest

from src.adapters.microsoft.onedrive_adapter import ONEDRIVE_PROVIDER, OneDriveAdapter
from src.domain.email import OAuthCredentials
from src.domain.user_drive import DriveItemNotFoundError, DriveNameConflictError, DriveNotConnectedError
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.ports.user_drive_port import UserDrivePort

_ROOT = {"id": "root1", "name": "Alek-bot", "folder": {"childCount": 1},
         "parentReference": {"path": "/drive/root:/Apps"}}
_ENC_PARENT = "/drive/root:/Apps/Alek-bot/" + quote("Встречи 2026")
_FILE = {"id": "f1", "name": "заметка.txt", "size": 12, "file": {"mimeType": "text/plain"},
         "parentReference": {"path": _ENC_PARENT}, "@microsoft.graph.downloadUrl": "https://dl.example/a"}


def _creds():
    return OAuthCredentials(user_id="u1", provider=ONEDRIVE_PROVIDER, access_token="tok", refresh_token="r",
                            token_expiry=datetime.now(timezone.utc) + timedelta(hours=1), scopes=[],
                            email_address="")


def _resp(json_data=None, status=200, body=b"", headers=None):
    r = MagicMock()
    r.status = status
    r.ok = status < 300
    r.headers = headers or {}
    r.json = AsyncMock(return_value=json_data)
    r.text = AsyncMock(return_value=str(json_data))
    r.read = AsyncMock(return_value=body)
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


def _adapter(creds="default"):
    oauth = AsyncMock(spec=OAuthCredentialsPort)
    oauth.get_credentials.return_value = _creds() if creds == "default" else creds
    oauth.is_connected.return_value = creds is not None
    return OneDriveAdapter(oauth, "cid", "csecret"), oauth


class TestPortCompliance:
    def test_is_user_drive_port(self):
        assert issubclass(OneDriveAdapter, UserDrivePort)

    def test_provider_key(self):
        assert ONEDRIVE_PROVIDER == "microsoft_onedrive"


class TestPaths:
    async def test_percent_encoded_path_is_readable_and_relative(self):
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session(get=[_resp(_FILE), _resp(_ROOT)])):
            item = await adapter.get_item("u1", "f1")
        assert item.path == "Встречи 2026/заметка.txt"
        assert item.mime_type == "text/plain" and item.size_bytes == 12

    async def test_raw_path_is_readable_and_relative(self):
        raw = {**_FILE, "parentReference": {"path": "/drive/root:/Apps/Alek-bot/Встречи 2026"}}
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session(get=[_resp(raw), _resp(_ROOT)])):
            item = await adapter.get_item("u1", "f1")
        assert item.path == "Встречи 2026/заметка.txt"

    async def test_file_directly_in_root_has_bare_name(self):
        raw = {**_FILE, "parentReference": {"path": "/drive/root:/Apps/Alek-bot"}}
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session(get=[_resp(raw), _resp(_ROOT)])):
            item = await adapter.get_item("u1", "f1")
        assert item.path == "заметка.txt"

    async def test_create_folder_response_with_drives_prefix_needs_no_reread(self):
        folder = {"id": "d1", "name": "Встречи 2026", "folder": {"childCount": 0},
                  "parentReference": {"path": "/drives/b!abc123/root:/Apps/Alek-bot"}}
        adapter, _ = _adapter()
        session = _session(post=_resp(folder, status=201), get=_resp(_ROOT))
        with patch("aiohttp.ClientSession", return_value=session), \
                patch("src.adapters.microsoft.onedrive_adapter.logger") as log:
            item = await adapter.create_folder("u1", "root1", "Встречи 2026")
        assert item.path == "Встречи 2026" and item.is_folder
        assert session.get.call_count == 1  # the approot read only: no re-read of the item
        log.warning.assert_not_called()

    async def test_drives_prefix_nested_parent(self):
        folder = {"id": "d2", "name": "Акты", "folder": {},
                  "parentReference": {"path": "/drives/b!abc123/root:/Apps/Alek-bot/Встречи 2026"}}
        adapter, _ = _adapter()
        session = _session(post=_resp(folder, status=201), get=_resp(_ROOT))
        with patch("aiohttp.ClientSession", return_value=session):
            item = await adapter.create_folder("u1", "p1", "Акты")
        assert item.path == "Встречи 2026/Акты"
        assert session.get.call_count == 1

    async def test_localised_app_folder_root_is_taken_from_approot(self):
        root = {"id": "root1", "name": "Alek-bot", "folder": {},
                "parentReference": {"path": "/drive/root:/ARCHIVE/Приложения"}}
        raw = {**_FILE, "parentReference": {"path": "/drive/root:/ARCHIVE/Приложения/Alek-bot/Встречи 2026"}}
        adapter, _ = _adapter()
        session = _session(get=[_resp(raw), _resp(root)])
        with patch("aiohttp.ClientSession", return_value=session), \
                patch("src.adapters.microsoft.onedrive_adapter.logger") as log:
            item = await adapter.get_item("u1", "f1")
        assert item.path == "Встречи 2026/заметка.txt"
        assert session.get.call_count == 2
        log.warning.assert_not_called()

    async def test_approot_directly_under_drive_root(self):
        root = {"id": "root1", "name": "Alek-bot", "folder": {},
                "parentReference": {"path": "/drive/root:"}}
        raw = {**_FILE, "parentReference": {"path": "/drive/root:/Alek-bot/Inbox"}}
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session(get=[_resp(raw), _resp(root)])):
            item = await adapter.get_item("u1", "f1")
        assert item.path == "Inbox/заметка.txt"

    async def test_root_has_empty_path(self):
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session(get=_resp(_ROOT))):
            root = await adapter.get_root("u1")
        assert root.path == "" and root.is_folder and root.item_id == "root1"

    async def test_prefix_mismatch_rereads_then_marks_unknown(self):
        odd = {**_FILE, "parentReference": {"path": "/drive/root:/Elsewhere"}}
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session(get=[_resp(odd), _resp(_ROOT), _resp(odd)])):
            item = await adapter.get_item("u1", "f1")
        assert item.path == "…/заметка.txt"

    async def test_search_hit_without_parent_path_is_reread(self):
        hit = {"id": "f1", "name": "заметка.txt", "file": {}, "parentReference": {}}
        adapter, _ = _adapter()
        session = _session(get=[_resp({"value": [hit]}), _resp(_ROOT), _resp(_FILE)])
        with patch("aiohttp.ClientSession", return_value=session):
            items = await adapter.search("u1", "заметка", limit=10)
        assert [i.path for i in items] == ["Встречи 2026/заметка.txt"]
        assert "/me/drive/special/approot/search(q=" in session.get.call_args_list[0].args[0]


class TestAuthAndErrors:
    async def test_no_credentials_raises_not_connected(self):
        adapter, _ = _adapter(creds=None)
        with pytest.raises(DriveNotConnectedError):
            await adapter.get_root("u1")

    async def test_401_after_forced_refresh_is_not_connected(self):
        adapter, _ = _adapter()
        adapter._tokens.headers = AsyncMock(return_value={"Authorization": "Bearer t"})
        adapter._tokens.invalidate = MagicMock()
        session = _session(get=[_resp({}, status=401), _resp({}, status=401)])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveNotConnectedError):
            await adapter.get_root("u1")
        forced = [c for c in adapter._tokens.headers.await_args_list if c.kwargs == {"force_refresh": True}]
        assert len(forced) == 1  # one forced refresh, then the second 401 gives up
        # The provider dedupes forced refreshes itself: the 401 path never invalidates.
        adapter._tokens.invalidate.assert_not_called()

    async def test_401_refresh_still_happens_after_throttling(self):
        adapter, _ = _adapter()
        adapter._tokens.headers = AsyncMock(return_value={"Authorization": "Bearer t"})
        throttled = [_resp({}, status=429, headers={"Retry-After": "0"}) for _ in range(3)]
        session = _session(get=[*throttled, _resp({}, status=401), _resp(_ROOT)])
        with patch("aiohttp.ClientSession", return_value=session):
            root = await adapter.get_root("u1")
        assert root.item_id == "root1"
        assert {"force_refresh": True} in [c.kwargs for c in adapter._tokens.headers.await_args_list]

    async def test_429_retried_with_retry_after(self):
        adapter, _ = _adapter()
        session = _session(get=[_resp({}, status=429, headers={"Retry-After": "0"}), _resp(_ROOT)])
        with patch("aiohttp.ClientSession", return_value=session):
            root = await adapter.get_root("u1")
        assert root.item_id == "root1"

    def test_retry_after_http_date_falls_back_to_backoff(self):
        from src.adapters.microsoft.onedrive_adapter import _retry_after
        assert _retry_after("Wed, 21 Oct 2026 07:28:00 GMT", 1) == 2.0
        assert _retry_after("120", 0) == 30.0

    async def test_404_raises_not_found(self):
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session(get=[_resp({}, status=404)])), \
                pytest.raises(DriveItemNotFoundError):
            await adapter.get_item("u1", "gone")

    async def test_409_on_create_folder_raises_conflict(self):
        adapter, _ = _adapter()
        session = _session(post=_resp({}, status=409))
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveNameConflictError):
            await adapter.create_folder("u1", "root1", "Inbox")

    async def test_one_session_per_call(self):
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session(get=[_resp(_FILE), _resp(_ROOT)])) as cs:
            await adapter.get_item("u1", "f1")
        assert cs.call_count == 1


class TestUpload:
    async def test_upload_uses_conflict_rename(self):
        # Spike A8: on a clash the provider names the copy "<stem> 1<ext>" (e.g. "заметка 1.txt").
        adapter, _ = _adapter()
        session = _session(put=_resp(_FILE, status=201), get=_resp(_ROOT))
        with patch("aiohttp.ClientSession", return_value=session):
            item = await adapter.upload("u1", "p1", "заметка.txt", b"hello", "text/plain")
        url = session.put.call_args.args[0]
        assert f"/me/drive/items/p1:/{quote('заметка.txt')}:/content" in url
        assert "conflictBehavior=rename" in url
        assert item.item_id == "f1"

    async def test_upload_returns_provider_suffixed_name(self):
        renamed = {**_FILE, "id": "f2", "name": "заметка 1.txt"}
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session(put=_resp(renamed, status=201), get=_resp(_ROOT))):
            item = await adapter.upload("u1", "p1", "заметка.txt", b"hello", "text/plain")
        assert item.name == "заметка 1.txt" and item.path == "Встречи 2026/заметка 1.txt"

    async def test_large_upload_uses_session_without_auth_header(self):
        adapter, _ = _adapter()
        data = b"0" * (6 * 1024 * 1024)
        session = _session(post=_resp({"uploadUrl": "https://up.example/s"}),
                           put=[_resp({}, status=202), _resp(_FILE, status=201)], get=_resp(_ROOT))
        with patch("aiohttp.ClientSession", return_value=session):
            await adapter.upload("u1", "p1", "big.bin", data, "application/octet-stream")
        assert "createUploadSession" in session.post.call_args.args[0]
        first = session.put.call_args_list[0]
        assert first.args[0] == "https://up.example/s"
        assert "Authorization" not in first.kwargs["headers"]
        assert first.kwargs["headers"]["Content-Range"] == f"bytes 0-{5 * 1024 * 1024 - 1}/{len(data)}"


class TestDisconnect:
    async def test_disconnect_revokes_and_invalidates(self):
        adapter, oauth = _adapter()
        adapter._tokens.invalidate = MagicMock()
        await adapter.disconnect("u1")
        oauth.revoke_credentials.assert_awaited_once_with("u1", ONEDRIVE_PROVIDER)
        adapter._tokens.invalidate.assert_called_once_with("u1")

    async def test_is_connected_delegates(self):
        adapter, oauth = _adapter()
        assert await adapter.is_connected("u1") is True
        oauth.is_connected.assert_awaited_once_with("u1", ONEDRIVE_PROVIDER)

    def test_display_name(self):
        assert _adapter()[0].display_name == "OneDrive"


class TestDownloadAndMutations:
    async def test_download_via_download_url_without_auth(self):
        adapter, _ = _adapter()
        session = _session(get=[_resp(_FILE), _resp(body=b"hello")])
        with patch("aiohttp.ClientSession", return_value=session):
            assert await adapter.download("u1", "f1") == b"hello"
        last = session.get.call_args_list[-1]
        assert last.args[0] == "https://dl.example/a" and "headers" not in last.kwargs

    async def test_move_patches_parent_and_name(self):
        adapter, _ = _adapter()
        session = _session(patch=_resp(_FILE), get=_resp(_ROOT))
        with patch("aiohttp.ClientSession", return_value=session):
            await adapter.move("u1", "f1", new_parent_id="p2", new_name="b.txt")
        assert session.patch.call_args.kwargs["json"] == {"parentReference": {"id": "p2"}, "name": "b.txt"}

    async def test_delete(self):
        adapter, _ = _adapter()
        session = _session(delete=_resp(status=204))
        with patch("aiohttp.ClientSession", return_value=session):
            await adapter.delete("u1", "f1")
        assert session.delete.call_args.args[0].endswith("/me/drive/items/f1")

    async def test_list_children_follows_next_link(self):
        adapter, _ = _adapter()
        page1 = {"value": [_FILE], "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"}
        page2 = {"value": [{**_FILE, "id": "f2", "name": "b.txt"}]}
        session = _session(get=[_resp(page1), _resp(_ROOT), _resp(page2)])
        with patch("aiohttp.ClientSession", return_value=session):
            kids = await adapter.list_children("u1", "p1")
        assert [k.item_id for k in kids] == ["f1", "f2"]
