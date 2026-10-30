"""
Wire tests: a stored change cursor that Graph answers with 404 restarts the enumeration
(USER_DRIVE_INDEXING_RFC §4, §7.5).

A delta link that is no longer known to Graph is the same situation as a 410: the cursor is dead and
only a full enumeration from the delta start recovers. Left as DriveItemNotFoundError it would fail every
tick as an internal error, forever. Mock boundary: aiohttp.ClientSession (docs/how_to/ADAPTER_WIRE_TESTING.md).
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.microsoft.onedrive_adapter import ONEDRIVE_PROVIDER, OneDriveAdapter
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.user_drive import DriveItemNotFoundError, DriveResyncRequired
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.composition.connector_registry import ConnectorRegistry
from tests.support.oauth_credentials_store import mirror_get
from tests.support.drive_handles import drive_handle

HANDLE = drive_handle("u1")

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "onedrive_delta"
_GRAPH = "https://graph.microsoft.com/v1.0"
_DELTA_START = "/me/drive/special/approot/delta?$top=200"

APPROOT = json.loads((_FIX / "approot.json").read_text(encoding="utf-8"))
ROOT_ID = APPROOT["id"]
DRIVE_ID = APPROOT["parentReference"]["driveId"]
_NOT_FOUND = {"error": {"code": "itemNotFound", "message": "Item not found"}}


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


def _session(get):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.get.side_effect = get
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


def _cursor(url, full=False, bound=True):
    data = {"url": url, "full": full}
    if bound:
        data.update(drive_id=DRIVE_ID, root_id=ROOT_ID)
    return json.dumps(data)


async def test_404_on_a_stored_delta_link_restarts_from_the_delta_start():
    adapter = _adapter()
    cursor = _cursor(f"{_GRAPH}/me/drive/special/approot/delta?token=EXPIRED")
    session = _session([_resp(APPROOT), _resp(_NOT_FOUND, status=404)])
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveResyncRequired) as exc:
        await adapter.get_changes(HANDLE, cursor)
    restart = json.loads(exc.value.restart_cursor)
    assert restart == {"url": _DELTA_START, "full": True}
    assert session.get.call_args_list[-1].args[0].endswith("token=EXPIRED")   # the stored link was followed


async def test_404_on_a_stored_next_link_mid_enumeration_restarts_too():
    adapter = _adapter()
    cursor = _cursor(f"{_GRAPH}/me/drive/special/approot/delta?$skiptoken=GONE", full=True)
    session = _session([_resp(APPROOT), _resp(_NOT_FOUND, status=404)])
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveResyncRequired) as exc:
        await adapter.get_changes(HANDLE, cursor)
    assert json.loads(exc.value.restart_cursor) == {"url": _DELTA_START, "full": True}


async def test_404_on_a_fresh_enumeration_is_not_a_resync():
    # No stored cursor: a restart would ask for the very same URL again.
    adapter = _adapter()
    session = _session([_resp(APPROOT), _resp(_NOT_FOUND, status=404)])
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveItemNotFoundError):
        await adapter.get_changes(HANDLE, None)


async def test_404_on_a_stored_restart_cursor_is_not_a_resync():
    # A restart cursor already points at the delta start: restarting again would loop.
    adapter = _adapter()
    session = _session([_resp(APPROOT), _resp(_NOT_FOUND, status=404)])
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveItemNotFoundError):
        await adapter.get_changes(HANDLE, _cursor(_DELTA_START, full=True, bound=False))


async def test_404_on_the_app_folder_read_is_not_a_resync():
    # Only the stored link is dead on a 404 of the feed call; the App Folder read is another matter.
    adapter = _adapter()
    cursor = _cursor(f"{_GRAPH}/me/drive/special/approot/delta?token=OK")
    session = _session([_resp(_NOT_FOUND, status=404)])
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveItemNotFoundError):
        await adapter.get_changes(HANDLE, cursor)
