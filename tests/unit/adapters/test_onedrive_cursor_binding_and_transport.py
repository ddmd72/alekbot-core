"""
Wire tests for the OneDriveAdapter cursor binding, timeouts and transport errors (USER_DRIVE_INDEXING_RFC §4,
§7.2, §7.5, §8.2).

- A stored delta cursor is bound to the drive and App Folder that served it.
  A cursor minted under another drive or a re-created App Folder is never followed: the adapter
  restarts the enumeration, so the restart page carries the fresh drive id (the tick's per-page check).
- A token-endpoint timeout is systemic (DriveThrottled), never per-item.
- fs_modified_at comes from fileSystemInfo, not the server timestamp.
- An aiohttp transport error leaves the adapter as an OSError, the per-item class
  of §8.2 (the tick decides by the call's scope), never a raw aiohttp error; Step 1 callers get a "failed" answer.
- Every Graph call is bounded by GRAPH_CALL_TIMEOUT_S, well under
  TICK_DEADLINE_S, and a timeout stays a TimeoutError (per-item on a call about one item, §8.2).

Mock boundary: aiohttp.ClientSession (docs/how_to/ADAPTER_WIRE_TESTING.md), never UserDrivePort.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from src.domain.drive_card import effective_date
from src.domain.drive_connection import DriveConnection
from src.domain.drive_index_params import TICK_DEADLINE_S
from src.domain.drive_user_settings import DriveUserSettings
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.user_drive import DriveNotConnectedError, DriveResyncRequired, DriveThrottled
from src.domain.drive_outcome_class import OutcomeClass
from src.infrastructure.drive_operations import DriveOperations
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.services.drive_indexing_service import _PER_ITEM_ERRORS
from src.services.file_conversion_service import FileConversionService
from src.services.drive_connection_service import DriveConnectionService
from src.services.user_drive_service import UserDriveService
from src.adapters.microsoft.onedrive_adapter import ONEDRIVE_PROVIDER, OneDriveAdapter
from src.composition.connector_registry import ConnectorRegistry
from tests.support.oauth_credentials_store import mirror_get
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_handles import drive_handle

HANDLE = drive_handle("u1")

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "onedrive_delta"
_GRAPH = "https://graph.microsoft.com/v1.0"
_DELTA_START = "/me/drive/special/approot/delta?$top=200"


def _load(name):
    return json.loads((_FIX / name).read_text(encoding="utf-8"))


APPROOT = _load("approot.json")
ROOT_ID = APPROOT["id"]
DRIVE_ID = APPROOT["parentReference"]["driveId"]
APPROOT_OTHER_DRIVE = {**APPROOT, "parentReference": {**APPROOT["parentReference"], "driveId": "DRIVE-B"}}
APPROOT_RECREATED = {**APPROOT, "id": "NEWROOT!1"}


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


def _resp(json_data=None, status=200, headers=None):
    r = MagicMock()
    r.status = status
    r.ok = status < 300
    r.headers = headers or {}
    r.json = AsyncMock(return_value=json_data)
    r.text = AsyncMock(return_value=json.dumps(json_data))
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _session(get=(), **methods):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.get.side_effect = get
    for name, effect in methods.items():
        getattr(s, name).side_effect = effect
    return s


def _adapter():
    oauth = AsyncMock(spec=OAuthCredentialsPort)
    oauth.get.return_value = _creds()
    mirror_get(oauth)
    return OneDriveAdapter(oauth, "cid", "csecret", connector=ConnectorRegistry().get("microsoft_onedrive"))


async def _connection_service_of(handle) -> DriveConnectionService:
    """The real connection service over the in-memory repository, with `handle`'s connection as the primary (tag a1)."""
    repo = FakeDriveConnectionRepository()
    await repo.create(DriveConnection(
        connection_id=handle.connection_id, user_id=handle.user_id, account_id="a1",
        credential_id=handle.credential_id, kind=handle.kind, drive_identity="microsoft_onedrive:DRIVE-A",
        ref_tag="a1", created_at=datetime(2026, 10, 1, tzinfo=timezone.utc)))
    await repo.save_settings(DriveUserSettings(user_id=handle.user_id, primary_connection_id=handle.connection_id))
    return DriveConnectionService(repo, AsyncMock(spec=OAuthCredentialsPort))


async def _page(adapter, approot, page, cursor=None):
    session = _session([_resp(approot), _resp(page)])
    with patch("aiohttp.ClientSession", return_value=session):
        return await adapter.get_changes(HANDLE, cursor), session


# -- The delta cursor is bound to its drive and App Folder ------------------------------------

class TestCursorBoundToDriveAndRoot:
    async def test_cursor_from_another_drive_restarts_without_following_it(self):
        pages = _load("initial_enumeration.json")["pages"]
        adapter = _adapter()
        first, _ = await _page(adapter, APPROOT, pages[0])            # minted under drive A
        # A reconnect to account B: the old cursor must not be followed (Graph may answer 400/403/404).
        session = _session([_resp(APPROOT_OTHER_DRIVE), _resp(pages[1])])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveResyncRequired) as exc:
            await adapter.get_changes(HANDLE, first.cursor)
        assert [c.args[0] for c in session.get.call_args_list] == [f"{_GRAPH}/me/drive/special/approot"]
        restart = json.loads(exc.value.restart_cursor)
        assert restart["url"] == _DELTA_START and restart["full"] is True
        # The restart page names drive B, so the tick's per-page drive check can purge drive A.
        again, session = await _page(adapter, APPROOT_OTHER_DRIVE, pages[0], exc.value.restart_cursor)
        assert session.get.call_args_list[-1].args[0] == f"{_GRAPH}{_DELTA_START}"
        assert again.drive_identity == "microsoft_onedrive:DRIVE-B" and again.full_enumeration is True

    async def test_cursor_from_a_recreated_app_folder_restarts(self):
        pages = _load("initial_enumeration.json")["pages"]
        adapter = _adapter()
        first, _ = await _page(adapter, APPROOT, pages[0])
        session = _session([_resp(APPROOT_RECREATED), _resp(pages[1])])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveResyncRequired) as exc:
            await adapter.get_changes(HANDLE, first.cursor)
        assert session.get.call_count == 1                              # the approot only
        assert json.loads(exc.value.restart_cursor)["url"] == _DELTA_START

    async def test_cursor_round_trip_carries_drive_and_root(self):
        pages = _load("initial_enumeration.json")["pages"]
        adapter = _adapter()
        first, _ = await _page(adapter, APPROOT, pages[0])
        data = json.loads(first.cursor)
        assert (data["url"], data["full"]) == (pages[0]["@odata.nextLink"], True)
        assert (data["drive_id"], data["root_id"]) == (DRIVE_ID, ROOT_ID)
        second, session = await _page(adapter, APPROOT, pages[1], first.cursor)   # same root: followed
        assert session.get.call_args_list[-1].args[0] == pages[0]["@odata.nextLink"]
        last, _ = await _page(adapter, APPROOT, pages[2], second.cursor)
        data = json.loads(last.cursor)
        assert (data["url"], data["full"]) == (pages[2]["@odata.deltaLink"], False)
        assert (data["drive_id"], data["root_id"]) == (DRIVE_ID, ROOT_ID)

    async def test_cursor_written_before_the_binding_is_still_followed(self):
        pages = _load("folder_delete.json")["pages"]
        adapter = _adapter()
        legacy = json.dumps({"url": f"{_GRAPH}/me/drive/special/approot/delta?token=prev", "full": False})
        result, session = await _page(adapter, APPROOT_OTHER_DRIVE, pages[0], legacy)   # unknown: no mismatch
        assert session.get.call_args_list[-1].args[0] == f"{_GRAPH}/me/drive/special/approot/delta?token=prev"
        assert result.drive_identity == "microsoft_onedrive:DRIVE-B"
        assert json.loads(result.cursor)["drive_id"] == "DRIVE-B"       # bound from now on

    @pytest.mark.parametrize("binding", [{"drive_id": 1, "root_id": ROOT_ID}, {"drive_id": DRIVE_ID, "root_id": []}])
    async def test_malformed_binding_restarts_without_a_call(self, binding):
        adapter = _adapter()
        cursor = json.dumps({"url": f"{_GRAPH}/me/drive/special/approot/delta?token=x", "full": False, **binding})
        session = _session([])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveResyncRequired):
            await adapter.get_changes(HANDLE, cursor)
        session.get.assert_not_called()


# -- A token-endpoint timeout is systemic ---------------------------------------------------------

class TestTokenEndpointTimeout:
    async def test_token_endpoint_timeout_is_systemic_not_per_item(self):
        adapter = _adapter()
        adapter._tokens.headers = AsyncMock(side_effect=asyncio.TimeoutError())
        with patch("aiohttp.ClientSession", return_value=_session([])), pytest.raises(DriveThrottled) as exc:
            await adapter.get_change(HANDLE, "X!1")
        assert exc.value.retry_after_s == 60.0


# -- fs_modified_at is the file-system time ----------------------------------------------------------

class TestFsModifiedAt:
    async def test_fs_modified_at_comes_from_file_system_info(self):
        raw = {"id": "P!1", "name": "IMG_1.jpg", "size": 10, "parentReference": {"id": "R!1"},
               "file": {"mimeType": "image/jpeg"}, "image": {}, "photo": {},
               "lastModifiedDateTime": "2026-10-01T09:30:00Z",
               "fileSystemInfo": {"lastModifiedDateTime": "2025-08-14T18:22:05Z"}}
        adapter = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session([_resp(raw)])):
            c = await adapter.get_change(HANDLE, "P!1")
        august = datetime(2025, 8, 14, 18, 22, 5, tzinfo=timezone.utc)
        assert c.fs_modified_at == august
        assert c.modified_at == datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
        assert effective_date(c, datetime(2000, 1, 1, tzinfo=timezone.utc)) == august


# -- Transport errors are per-call (OSError), classified by the tick (§8.2) ------

def _transport_errors():
    return [aiohttp.ServerDisconnectedError(), aiohttp.ClientPayloadError("cut"),
            aiohttp.ClientOSError(104, "Connection reset by peer")]


class TestTransportErrors:
    @pytest.mark.parametrize("error", _transport_errors(), ids=lambda e: type(e).__name__)
    async def test_transport_error_on_a_read_is_per_item(self, error):
        adapter = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session([error])), \
                pytest.raises(OSError) as exc:
            await adapter.get_change(HANDLE, "X!1")
        assert type(exc.value) is OSError and exc.value.__cause__ is error
        assert isinstance(exc.value, _PER_ITEM_ERRORS)
        assert not isinstance(exc.value, (DriveThrottled, DriveNotConnectedError))

    async def test_transport_error_while_reading_the_body_is_per_item(self):
        adapter = _adapter()
        broken = _resp({})
        broken.json = AsyncMock(side_effect=aiohttp.ClientPayloadError("body cut"))
        with patch("aiohttp.ClientSession", return_value=_session([broken])), pytest.raises(OSError) as exc:
            await adapter.list_children_changes(HANDLE, "F!1", limit=10)
        assert type(exc.value) is OSError and isinstance(exc.value.__cause__, aiohttp.ClientPayloadError)

    async def test_transport_error_on_the_delta_page_is_an_os_error(self):
        # The feed is drive-wide: the tick's generic handler continues with exponential backoff (§8.2).
        adapter = _adapter()
        session = _session([_resp(APPROOT), aiohttp.ServerDisconnectedError()])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(OSError) as exc:
            await adapter.get_changes(HANDLE, None)
        assert type(exc.value) is OSError and isinstance(exc.value.__cause__, aiohttp.ServerDisconnectedError)

    async def test_transport_error_on_a_mutation_is_an_os_error(self):
        adapter = _adapter()
        session = _session(delete=[aiohttp.ServerDisconnectedError()])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(OSError) as exc:
            await adapter.delete(HANDLE, "X!1")
        assert type(exc.value) is OSError and isinstance(exc.value.__cause__, aiohttp.ServerDisconnectedError)

    async def test_transport_log_line_carries_no_query(self):
        adapter = _adapter()
        cursor = f"{_GRAPH}/me/drive/items/F/children?$skiptoken=SECRETISH"
        with patch("aiohttp.ClientSession", return_value=_session([aiohttp.ServerDisconnectedError()])), \
                patch("src.adapters.microsoft.onedrive_adapter.logger") as log, pytest.raises(OSError) as exc:
            await adapter.list_children_changes(HANDLE, "F", limit=10, cursor=cursor)
        logged = " ".join(str(c.args[0]) for c in log.warning.call_args_list)
        assert logged and "SECRETISH" not in logged
        assert "SECRETISH" not in str(exc.value)   # the tick logs the exception text too

    @pytest.mark.parametrize("method,args,kwargs", [
        ("list_folder", ("",), {}),
        ("delete", ("drive:a1:X!1",), {"wait_s": 100.0}),   # a shielded mutation: never "still running"
    ], ids=["read", "mutation"])
    @pytest.mark.parametrize("error,answer", [
        (aiohttp.ServerDisconnectedError(), "Drive operation failed: OSError."),
        (asyncio.TimeoutError(), "Drive operation failed: TimeoutError."),
    ], ids=["transport", "timeout"])
    async def test_step1_caller_answers_failed_not_disconnected(self, method, args, kwargs, error, answer):
        # Real adapter → real UserDriveService → DriveOperations (the Step 1 chain, moved there at the cut).
        adapter = _adapter()
        localization = MagicMock()
        language = AsyncMock()
        connections = await _connection_service_of(HANDLE)
        ops = DriveOperations(
            conversion_service=AsyncMock(spec=FileConversionService), notification=AsyncMock(),
            drive_service=UserDriveService(adapter, connections), localization=localization,
            language_service=language,
        )
        context = {"user_id": "u1", "account_id": "a1", "origin_channel_id": "C1", "origin_platform": "slack"}
        with patch("aiohttp.ClientSession", return_value=_session([error])):
            outcome = await getattr(ops, method)(context, "u1", *args, **kwargs)
        assert outcome.cls is OutcomeClass.FAILED and not outcome.success
        assert outcome.text.startswith("Drive operation failed")
        assert outcome.text == answer
        assert "not connected" not in outcome.text


# -- A bounded per-call timeout ---------------------------------------------------

class TestPerCallTimeout:
    def test_the_bound_is_well_under_the_tick_deadline(self):
        from src.adapters.microsoft.onedrive_adapter import GRAPH_CALL_TIMEOUT_S
        assert 0 < GRAPH_CALL_TIMEOUT_S <= 60 < TICK_DEADLINE_S

    async def test_every_graph_call_carries_the_timeout(self):
        from src.adapters.microsoft.onedrive_adapter import GRAPH_CALL_TIMEOUT_S
        adapter = _adapter()
        page = {"value": [], "@odata.deltaLink": f"{_GRAPH}/me/drive/special/approot/delta?token=n"}
        session = _session([_resp(APPROOT), _resp(page)])
        with patch("aiohttp.ClientSession", return_value=session):
            await adapter.get_changes(HANDLE, None)
        session.delete.side_effect = [_resp(status=204)]
        with patch("aiohttp.ClientSession", return_value=session):
            await adapter.delete(HANDLE, "X!1")
        calls = session.get.call_args_list + session.delete.call_args_list
        assert len(calls) == 3
        for call in calls:
            timeout = call.kwargs["timeout"]
            assert isinstance(timeout, aiohttp.ClientTimeout) and timeout.total == GRAPH_CALL_TIMEOUT_S

    @pytest.mark.parametrize("error", [asyncio.TimeoutError(), aiohttp.ServerTimeoutError("read timed out")],
                             ids=["total", "socket"])
    async def test_a_call_that_times_out_stays_a_timeout(self, error):
        adapter = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session([error])), \
                pytest.raises(asyncio.TimeoutError) as exc:
            await adapter.get_change(HANDLE, "X!1")
        assert type(exc.value) is asyncio.TimeoutError and exc.value.__cause__ is error
        assert isinstance(exc.value, _PER_ITEM_ERRORS)
        assert not isinstance(exc.value, (DriveThrottled, DriveNotConnectedError))
