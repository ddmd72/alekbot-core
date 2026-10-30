"""Wire tests: the index's content reads on OneDrive (USER_DRIVE_INDEXING_RFC §8.1 step 3, §24.5).
`download_to_file` streams the pre-authenticated download URL to disk and cuts the stream at the byte cap;
`get_thumbnail` reads one rendition (standard or custom `c{w}x{h}` size) through the thumbnail set and fetches its
URL without the bearer token. Response shapes follow the Graph reference (driveitem-list-thumbnails, example 5) and
spike S0f. Mock boundary: aiohttp.ClientSession."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from src.adapters.microsoft.onedrive_adapter import ONEDRIVE_PROVIDER, OneDriveAdapter
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.user_drive import DriveFileTooLarge, DriveItemNotFoundError, DriveThumbnailTooLarge
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.composition.connector_registry import ConnectorRegistry
from tests.support.oauth_credentials_store import mirror_get
from tests.support.drive_handles import drive_handle

HANDLE = drive_handle("u1")

_FILE = {"id": "f1", "name": "scan.pdf", "size": 10, "file": {"mimeType": "application/pdf"},
         "parentReference": {"path": "/drive/root:/Apps/Alek-bot"},
         "@microsoft.graph.downloadUrl": "https://dl.example/a"}


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


class _Content:
    def __init__(self, chunks):
        self._chunks = chunks

    def iter_chunked(self, size):
        async def gen():
            for chunk in self._chunks:
                yield chunk
        return gen()


def _resp(json_data=None, status=200, chunks=()):
    r = MagicMock()
    r.status = status
    r.ok = status < 300
    r.headers = {}
    r.json = AsyncMock(return_value=json_data)
    r.text = AsyncMock(return_value=str(json_data))
    r.content = _Content(list(chunks))
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _session(get):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.get.side_effect = get
    return s


class TestDownloadToFile:
    async def test_streams_to_disk_without_the_bearer_token(self, tmp_path):
        dest = tmp_path / "f1.bin"
        session = _session([_resp(_FILE), _resp(chunks=[b"hello ", b"world"])])
        with patch("aiohttp.ClientSession", return_value=session):
            written = await _adapter().download_to_file(HANDLE, "f1", str(dest), max_bytes=100)
        assert written == 11 and dest.read_bytes() == b"hello world"
        last = session.get.call_args_list[-1]
        assert last.args[0] == "https://dl.example/a" and "headers" not in last.kwargs
        assert last.kwargs["timeout"].total == 120

    async def test_stream_past_the_cap_raises_too_large(self, tmp_path):
        session = _session([_resp(_FILE), _resp(chunks=[b"12345", b"67890", b"x"])])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveFileTooLarge) as exc:
            await _adapter().download_to_file(HANDLE, "f1", str(tmp_path / "f"), max_bytes=8)
        assert exc.value.limit_bytes == 8
        assert len((tmp_path / "f").read_bytes()) <= 8

    async def test_missing_item_is_not_found(self, tmp_path):
        session = _session([_resp({}, status=404)])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveItemNotFoundError):
            await _adapter().download_to_file(HANDLE, "gone", str(tmp_path / "f"), max_bytes=8)

    async def test_item_without_content_is_not_found(self, tmp_path):
        folder = {k: v for k, v in _FILE.items() if k != "@microsoft.graph.downloadUrl"}
        with patch("aiohttp.ClientSession", return_value=_session([_resp(folder)])), \
                pytest.raises(DriveItemNotFoundError):
            await _adapter().download_to_file(HANDLE, "f1", str(tmp_path / "f"), max_bytes=8)

    async def test_failed_download_url_is_a_value_error_like_download(self, tmp_path):
        session = _session([_resp(_FILE), _resp(status=403)])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(ValueError):
            await _adapter().download_to_file(HANDLE, "f1", str(tmp_path / "f"), max_bytes=8)

    async def test_a_stalled_stream_is_a_timeout(self, tmp_path):
        session = _session([_resp(_FILE), asyncio.TimeoutError()])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(asyncio.TimeoutError):
            await _adapter().download_to_file(HANDLE, "f1", str(tmp_path / "f"), max_bytes=8)

    async def test_a_transport_failure_is_an_os_error(self, tmp_path):
        session = _session([_resp(_FILE), aiohttp.ClientPayloadError("cut")])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(OSError):
            await _adapter().download_to_file(HANDLE, "f1", str(tmp_path / "f"), max_bytes=8)


def _thumbs(size, url="https://thumbs.example/t1"):
    return {"value": [{"id": "0", size: {"height": 117, "width": 176, "url": url}}]}


class TestGetThumbnail:
    async def test_an_edge_through_the_thumbnail_set(self):
        # MULTI_DRIVE_RFC §6: a thumbnail is asked by its longest edge in px; Graph's named sizes left the port
        session = _session([_resp(_thumbs("c176x176")), _resp(chunks=[b"\xff\xd8\xff", b"jpeg"])])
        with patch("aiohttp.ClientSession", return_value=session):
            data = await _adapter().get_thumbnail(HANDLE, "f1", 176)
        assert data == b"\xff\xd8\xffjpeg"
        first, last = session.get.call_args_list
        assert first.args[0].endswith("/me/drive/items/f1/thumbnails?select=c176x176")
        assert last.args[0] == "https://thumbs.example/t1" and "headers" not in last.kwargs

    async def test_custom_size(self):
        session = _session([_resp(_thumbs("c2048x2048")), _resp(chunks=[b"\xff\xd8\xffbig"])])
        with patch("aiohttp.ClientSession", return_value=session):
            assert await _adapter().get_thumbnail(HANDLE, "f1", 2048) == b"\xff\xd8\xffbig"
        assert session.get.call_args_list[0].args[0].endswith("/thumbnails?select=c2048x2048")

    @pytest.mark.parametrize("page", [{"value": []}, {"value": [{"id": "0"}]}, {}])
    async def test_no_rendition_is_none(self, page):
        with patch("aiohttp.ClientSession", return_value=_session([_resp(page)])):
            assert await _adapter().get_thumbnail(HANDLE, "f1", 176) is None

    async def test_rendition_url_gone_is_none(self):
        session = _session([_resp(_thumbs("c176x176")), _resp(status=404)])
        with patch("aiohttp.ClientSession", return_value=session):
            assert await _adapter().get_thumbnail(HANDLE, "f1", 176) is None

    async def test_rendition_over_the_vision_cap_raises_too_large(self):
        big = [b"x" * (1024 * 1024)] * 6
        session = _session([_resp(_thumbs("c2048x2048")), _resp(chunks=big)])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveThumbnailTooLarge):
            await _adapter().get_thumbnail(HANDLE, "f1", 2048)

    async def test_missing_item_is_not_found(self):
        with patch("aiohttp.ClientSession", return_value=_session([_resp({}, status=404)])), \
                pytest.raises(DriveItemNotFoundError):
            await _adapter().get_thumbnail(HANDLE, "gone", 176)

    async def test_failed_rendition_fetch_is_a_value_error(self):
        session = _session([_resp(_thumbs("c176x176")), _resp(status=500)])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(ValueError):
            await _adapter().get_thumbnail(HANDLE, "f1", 176)

    async def test_a_url_outside_https_is_never_fetched(self):
        session = _session([_resp(_thumbs("c176x176", url="http://thumbs.example/t1"))])
        with patch("aiohttp.ClientSession", return_value=session):
            assert await _adapter().get_thumbnail(HANDLE, "f1", 176) is None
        assert session.get.call_count == 1

    @pytest.mark.parametrize("size", ["", "huge", "c2048x2048&select=x", "c0x", "../content"])
    async def test_an_unknown_size_is_refused_before_any_call(self, size):
        session = _session([])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(ValueError):
            await _adapter().get_thumbnail(HANDLE, "f1", size)
        session.get.assert_not_called()
