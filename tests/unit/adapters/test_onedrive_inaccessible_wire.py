"""
Wire tests for the 404 of an id-addressed call (USER_DRIVE_INDEXING_RFC §26.2, D33, Δ48).

An item with its own permission list that lacks the app's entry is listed by `approot/delta` but answers 404 to every
id-addressed call: Graph reports "no access" as "not found". The adapter therefore never turns a 404 into a deletion;
every id call raises DriveItemNotFoundError and the caller decides (existence is decided by delta alone).

Mock boundary: aiohttp.ClientSession (docs/how_to/ADAPTER_WIRE_TESTING.md), never UserDrivePort.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.microsoft.onedrive_adapter import ONEDRIVE_PROVIDER, OneDriveAdapter
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.user_drive import DriveItemNotFoundError
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.composition.connector_registry import ConnectorRegistry
from tests.support.oauth_credentials_store import mirror_get
from tests.support.drive_handles import drive_handle

HANDLE = drive_handle("u1")

_NOT_FOUND = {"error": {"code": "itemNotFound", "message": "The resource could not be found."}}


def _creds():
    return OAuthCredential(
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


def _session(responses):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.get.side_effect = responses
    return s


def _adapter():
    oauth = AsyncMock(spec=OAuthCredentialsPort)
    oauth.get.return_value = _creds()
    mirror_get(oauth)
    return OneDriveAdapter(oauth, "cid", "csecret", connector=ConnectorRegistry().get("microsoft_onedrive"))


async def test_get_change_404_raises_and_is_never_a_deletion():
    adapter = _adapter()
    session = _session([_resp(_NOT_FOUND, status=404)])
    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(DriveItemNotFoundError):
            await adapter.get_change(HANDLE, "CLOSED!7")
    assert session.get.call_args.args[0].endswith("/me/drive/items/CLOSED%217")


async def test_get_change_of_a_readable_item_is_an_observation():
    adapter = _adapter()
    raw = {"id": "OPEN!1", "name": "a.pdf", "size": 10, "file": {"mimeType": "application/pdf"}, "cTag": "c1",
           "parentReference": {"id": "ROOT!1"}}
    with patch("aiohttp.ClientSession", return_value=_session([_resp(raw)])):
        change = await adapter.get_change(HANDLE, "OPEN!1")
    assert change.item_id == "OPEN!1" and change.deleted is False and change.parent_id == "ROOT!1"


@pytest.mark.parametrize("call", ["get_item", "download_to_file", "get_thumbnail", "list_children_changes",
                                  "list_children"])
async def test_every_id_addressed_call_answers_404_as_not_found(call, tmp_path):
    adapter = _adapter()
    session = _session([_resp(_NOT_FOUND, status=404)])
    args = {"get_item": (HANDLE, "CLOSED!7"),
            "download_to_file": (HANDLE, "CLOSED!7", str(tmp_path / "f"), 1024),
            "get_thumbnail": (HANDLE, "CLOSED!7", "large"),
            "list_children_changes": (HANDLE, "CLOSED!7", 10),
            "list_children": (HANDLE, "CLOSED!7")}[call]
    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(DriveItemNotFoundError):
            await getattr(adapter, call)(*args)
    assert any("CLOSED%217" in c.args[0] for c in session.get.call_args_list)
