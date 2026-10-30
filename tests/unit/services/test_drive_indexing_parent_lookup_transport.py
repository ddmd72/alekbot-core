"""The real OneDriveAdapter behind a fake HTTP layer, driven by a real DriveIndexingService
(USER_DRIVE_INDEXING_RFC §8.2).

A timeout or transport failure on a call about ONE item (here: the parent lookup of an unresolved record) is the
per-item class: the record backs off, the tick goes on. It must not end the tick as throttling, or a failure that
repeats on every call would stall the user's whole index.

Mock boundary: aiohttp.ClientSession (docs/how_to/ADAPTER_WIRE_TESTING.md), never UserDrivePort.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.adapters.microsoft.onedrive_adapter import ONEDRIVE_PROVIDER, OneDriveAdapter
from src.domain.drive_index import Priority, RecordState
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.services.drive_indexing_service import DriveIndexingService

from .drive_index_fakes import Clock, FakeEmbeddings, FakeQueue, FakeUsers
from src.composition.connector_registry import ConnectorRegistry
from tests.support.oauth_credentials_store import mirror_get
from .drive_index_fakes import LegacyConnections
from .drive_index_fakes import U_SCOPE

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "onedrive_delta"
APPROOT = json.loads((_FIX / "approot.json").read_text(encoding="utf-8"))
ROOT_ID = APPROOT["id"]
DRIVE_ID = APPROOT["parentReference"]["driveId"]
GHOST = "GHOSTPARENT!7"   # a parent the feed never delivered: the child is stored unresolved (§3.4)
CHILD = "CHILD!8"


def _creds():
    return OAuthCredential(
        user_id="u",
        account_id="acct-1",
        kind=ONEDRIVE_PROVIDER,
        integration=Integration.DRIVE,
        account=ProviderAccount(realm="microsoft", subject="", label=""),
        access_token="tok",
        refresh_token="r",
        token_expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        scopes=[],
        drive_id=DRIVE_ID,
    )


def _resp(json_data):
    r = MagicMock()
    r.status = 200
    r.ok = True
    r.headers = {}
    r.json = AsyncMock(return_value=json_data)
    r.text = AsyncMock(return_value=json.dumps(json_data))
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


class _FakeGraph:
    """Routes GETs by URL: the approot, one delta page with an orphan child, then an empty delta round. Every
    item lookup fails with `item_error`."""

    def __init__(self, item_error):
        self.item_error = item_error
        self.item_gets = 0

    def get(self, url, **kwargs):
        if url.endswith("/me/drive/special/approot"):
            return _resp(APPROOT)
        if "/me/drive/items/" in url:
            self.item_gets += 1
            raise self.item_error
        if "token=round" in url:
            return _resp({"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/drive/special/"
                                                           "approot/delta?token=round"})
        child = {"id": CHILD, "name": "a.pdf", "size": 10, "cTag": "c1", "parentReference": {"id": GHOST},
                 "file": {"mimeType": "application/pdf"}, "lastModifiedDateTime": "2026-10-01T09:30:00Z",
                 "fileSystemInfo": {"lastModifiedDateTime": "2026-10-01T09:30:00Z"}}
        return _resp({"value": [{**APPROOT}, child],
                      "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/drive/special/approot/delta?token=round"})

    def session(self):
        s = MagicMock()
        s.__aenter__ = AsyncMock(return_value=s)
        s.__aexit__ = AsyncMock(return_value=False)
        s.get.side_effect = self.get
        return s


class TestParentLookupTransportFailureIsPerItem:
    @pytest.mark.parametrize("error", [asyncio.TimeoutError(), aiohttp.ServerTimeoutError("read timed out"),
                                       aiohttp.ServerDisconnectedError()],
                             ids=["timeout", "socket-timeout", "disconnected"])
    async def test_get_change_failure_in_a_parent_lookup_backs_off_the_record_not_the_tick(self, error):
        oauth = AsyncMock(spec=OAuthCredentialsPort)
        oauth.get.return_value = _creds()
        mirror_get(oauth)
        adapter = OneDriveAdapter(oauth, "cid", "csecret", connector=ConnectorRegistry().get("microsoft_onedrive"))
        repo, queue, users, clock = InMemoryDriveIndexRepository(), FakeQueue(), FakeUsers(), Clock()
        svc = DriveIndexingService(adapter, repo, FakeEmbeddings(), queue, users, lambda lat, lon: "", "test-space/8",
                                   connections=LegacyConnections(users, adapter), clock=clock, monotonic=clock.monotonic)
        graph = _FakeGraph(error)
        with patch("aiohttp.ClientSession", side_effect=lambda *a, **k: graph.session()):
            result = await svc.tick(U_SCOPE)
        assert graph.item_gets >= 1, "the parent lookup never ran"
        assert result.status == "done", f"a per-item transport failure ended the tick: {result.status}"
        rec = repo.records[("u", CHILD)]
        assert rec.state is RecordState.UNRESOLVED
        assert rec.attempts == 1 and rec.priority == int(Priority.RETRY)
        assert rec.due_at is not None and rec.due_at > clock.now, "the record did not back off"
        state = await repo.get_sync_state(U_SCOPE)
        assert state.last_error is None and state.systemic_failures == 0
