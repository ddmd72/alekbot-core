"""Wire tests: `OneDriveAdapter.read_drive_id` — the drive id of a just-granted token, read by the drive connect's
activation (moved from the old OneDrive callback; MULTI_DRIVE_RFC §5.1, slice 1a). It reads the App Folder item's
`parentReference.driveId` with the given token, bounded by its own timeout, and answers "" on any failure (the connect
never fails on it). Mock boundary: aiohttp.ClientSession (the shared routed fake)."""
import asyncio

from src.adapters.microsoft.onedrive_adapter import DRIVE_ID_READ_TIMEOUT_S, OneDriveAdapter
from src.composition.connector_registry import ConnectorRegistry
from tests.support.http_boundary_fake import install
from tests.support.oauth_credentials_store import CredentialsStore

APPROOT = "https://graph.microsoft.com/v1.0/me/drive/special/approot"


def _adapter() -> OneDriveAdapter:
    return OneDriveAdapter(CredentialsStore().repo, "cid", "csecret",
                           connector=ConnectorRegistry().get("microsoft_onedrive"))


async def test_the_approot_answer_gives_the_drive_id_read_with_the_new_token(monkeypatch):
    fake = install(monkeypatch)
    fake.route("GET", APPROOT, 200, {"id": "approot-1", "parentReference": {"driveId": "drive-b1", "path": "/drive/root:"}})
    assert await _adapter().read_drive_id("at-new") == "drive-b1"
    [request] = fake.requests
    assert request.headers == {"Authorization": "Bearer at-new"}
    assert fake.unrouted == []


async def test_a_refused_read_is_empty(monkeypatch):
    fake = install(monkeypatch)
    fake.route("GET", APPROOT, 401, {"error": {"code": "InvalidAuthenticationToken"}})
    assert await _adapter().read_drive_id("at-new") == ""


async def test_a_body_without_the_field_is_empty(monkeypatch):
    fake = install(monkeypatch)
    fake.route("GET", APPROOT, 200, {"id": "approot-1", "parentReference": {"driveId": 42}})
    assert await _adapter().read_drive_id("at-new") == ""
    fake.route("GET", APPROOT, 200, {"id": "approot-1"})
    assert await _adapter().read_drive_id("at-new") == ""


async def test_a_transport_failure_is_empty(monkeypatch):
    install(monkeypatch)                     # nothing routed: the fake raises ClientConnectionError
    assert await _adapter().read_drive_id("at-new") == ""


async def test_a_timeout_is_empty_and_the_read_carries_its_own_bound(monkeypatch):
    seen = {}

    class _Hanging:
        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, *a):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url, **kw):
            seen["url"], seen["timeout"] = url, kw.get("timeout")
            return _Hanging()

    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **k: _Session())
    assert await _adapter().read_drive_id("at-new") == ""
    assert seen["url"] == APPROOT
    assert seen["timeout"].total == DRIVE_ID_READ_TIMEOUT_S == 10.0
