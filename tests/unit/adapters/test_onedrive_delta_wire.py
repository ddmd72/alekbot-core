"""
Wire tests for OneDriveAdapter's change capture (USER_DRIVE_INDEXING_RFC §4, §7.2, §17.3).

Mock boundary: aiohttp.ClientSession (docs/how_to/ADAPTER_WIRE_TESTING.md), never UserDrivePort.
Responses are the S0a recordings, sanitised by scripts/onedrive/sanitize_delta_fixtures.py
(tests/fixtures/onedrive_delta/). The 410 fixture is synthetic: S0a never saw one.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.microsoft.onedrive_adapter import ONEDRIVE_PROVIDER, OneDriveAdapter
from src.domain.drive_card import content_hash_of, effective_date, photo_location
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.user_drive import DriveConnection, DriveItemNotFoundError, DriveResyncRequired
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.composition.connector_registry import ConnectorRegistry
from tests.support.oauth_credentials_store import mirror_get
from tests.support.drive_handles import drive_handle

HANDLE = drive_handle("u1")

_FIX = Path(__file__).resolve().parents[2] / "fixtures" / "onedrive_delta"
_GRAPH = "https://graph.microsoft.com/v1.0"
# Observed 2026-10-12 after the owner moved the App Folder to the drive root (RFC Δ46).
_LIVE_410_LOCATION = f"{_GRAPH}/drive/root/view.delta"


def _load(name):
    return json.loads((_FIX / name).read_text(encoding="utf-8"))


APPROOT = _load("approot.json")
ROOT_ID = APPROOT["id"]
DRIVE_ID = APPROOT["parentReference"]["driveId"]


def _creds(**kw):
    base = dict(user_id="u1", account_id="acct-1", kind=ONEDRIVE_PROVIDER, integration=Integration.DRIVE,
                account=ProviderAccount(realm="microsoft", subject="", label=""), access_token="tok", refresh_token="r",
                token_expiry=datetime.now(timezone.utc) + timedelta(hours=1), scopes=[])
    return OAuthCredential(**{**base, **kw})


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


def _session(get):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.get.side_effect = get
    return s


def _adapter(creds="default"):
    oauth = AsyncMock(spec=OAuthCredentialsPort)
    oauth.get.return_value = _creds() if creds == "default" else creds
    mirror_get(oauth)
    return OneDriveAdapter(oauth, "cid", "csecret", connector=ConnectorRegistry().get("microsoft_onedrive")), oauth


async def _drain(adapter, pages, cursor=None):
    """Feed recorded pages one call each (approot, then the page); return results and URLs."""
    results, urls = [], []
    for page in pages:
        session = _session([_resp(APPROOT), _resp(page)])
        with patch("aiohttp.ClientSession", return_value=session):
            result = await adapter.get_changes(HANDLE, cursor)
        urls.append(session.get.call_args_list[-1].args[0])
        results.append(result)
        cursor = result.cursor
    return results, urls


class TestEnumeration:
    async def test_none_starts_a_full_enumeration_of_the_app_folder(self):
        adapter, _ = _adapter()
        (first, *_), urls = await _drain(adapter, _load("initial_enumeration.json")["pages"])
        assert urls[0] == f"{_GRAPH}/me/drive/special/approot/delta?$top=200"
        assert first.full_enumeration is True and first.complete is False

    async def test_pages_follow_next_link_then_end_on_delta_link(self):
        pages = _load("initial_enumeration.json")["pages"]
        adapter, _ = _adapter()
        results, urls = await _drain(adapter, pages)
        assert [r.complete for r in results] == [False, False, True]
        assert all(r.full_enumeration for r in results)       # every page of the enumeration
        assert urls[1] == pages[0]["@odata.nextLink"] and urls[2] == pages[1]["@odata.nextLink"]
        # A page cursor also names the drive and App Folder that served it.
        assert json.loads(results[-1].cursor) == {"url": pages[-1]["@odata.deltaLink"], "full": False,
                                                  "drive_id": DRIVE_ID, "root_id": ROOT_ID}

    async def test_drive_and_root_ids_come_from_the_approot_item(self):
        adapter, _ = _adapter()
        results, _ = await _drain(adapter, _load("initial_enumeration.json")["pages"])
        assert {(r.drive_id, r.root_item_id) for r in results} == {(DRIVE_ID, ROOT_ID)}

    async def test_approot_is_read_fresh_for_every_page(self):
        # The indexer service never sees a disconnect, so a cached root could name the previous drive.
        other = {**APPROOT, "parentReference": {**APPROOT["parentReference"], "driveId": "OTHER"}}
        pages = _load("initial_enumeration.json")["pages"]
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session([_resp(APPROOT), _resp(pages[0])])):
            first = await adapter.get_changes(HANDLE, None)
        # A cursor bound to another drive is never followed (it restarts), so the
        # fresh read is shown with a cursor written before the binding existed, which is still followed.
        unbound = json.dumps({k: v for k, v in json.loads(first.cursor).items() if k in ("url", "full")})
        session = _session([_resp(other), _resp(pages[1])])
        with patch("aiohttp.ClientSession", return_value=session):
            second = await adapter.get_changes(HANDLE, unbound)
        assert (first.drive_id, second.drive_id) == (DRIVE_ID, "OTHER")
        assert session.get.call_args_list[0].args[0] == f"{_GRAPH}/me/drive/special/approot"

    async def test_approot_is_reported_on_every_page_as_an_ordinary_change(self):
        # S0a: the root has a specialFolder facet and no root facet; the planner skips it by id.
        adapter, _ = _adapter()
        results, _ = await _drain(adapter, _load("initial_enumeration.json")["pages"])
        assert all(any(c.item_id == ROOT_ID for c in r.changes) for r in results)

    async def test_items_keep_feed_order_and_parent_ids(self):
        pages = _load("initial_enumeration.json")["pages"]
        adapter, _ = _adapter()
        results, _ = await _drain(adapter, pages)
        assert [c.item_id for r in results for c in r.changes] == [i["id"] for p in pages for i in p["value"]]
        live = [c for r in results for c in r.changes if c.item_id != ROOT_ID]
        assert live and all(c.parent_id for c in live)

    async def test_delta_link_round_is_not_a_full_enumeration(self):
        pages = _load("folder_delete.json")["pages"]
        adapter, _ = _adapter()
        start = json.dumps({"url": f"{_GRAPH}/me/drive/special/approot/delta?token=prev", "full": False})
        results, urls = await _drain(adapter, pages, cursor=start)
        assert urls[0] == f"{_GRAPH}/me/drive/special/approot/delta?token=prev"
        assert [r.full_enumeration for r in results] == [False, False]
        assert [r.complete for r in results] == [False, True]


class TestChangeMapping:
    async def test_folder_delete_gives_bare_tombstones_for_folder_and_child(self):
        fixture = _load("folder_delete.json")
        adapter, _ = _adapter()
        results, _ = await _drain(adapter, fixture["pages"])
        dead = {c.item_id: c for r in results for c in r.changes if c.deleted}
        assert set(dead) == {fixture["_meta"]["folder_id"], fixture["_meta"]["child_id"]}
        for c in dead.values():   # only item_id and deleted are relied on (§3.1)
            assert (c.name, c.parent_id, c.is_folder, c.content_version, c.facets) == ("", "", False, "", {})

    async def test_delete_then_restore_in_one_round_keeps_feed_order(self):
        fixture = _load("delete_restore.json")
        item = fixture["_meta"]["item_id"]
        adapter, _ = _adapter()
        results, _ = await _drain(adapter, fixture["pages"])
        seen = [c.deleted for r in results for c in r.changes if c.item_id == item]
        assert seen == [True, False]      # tombstone first, then the live item with the same id

    async def test_move_out_is_a_tombstone_and_move_in_a_first_sighting(self):
        fixture = _load("move_out_in.json")
        meta = fixture["_meta"]
        adapter, _ = _adapter()
        results, _ = await _drain(adapter, fixture["pages"])
        changes = {c.item_id: c for r in results for c in r.changes}
        assert changes[meta["moved_out_id"]].deleted is True
        moved_in = changes[meta["moved_in_id"]]
        assert moved_in.deleted is False and moved_in.name and moved_in.content_version

    async def test_reemitted_items_are_passed_through_not_deduplicated(self):
        pages = _load("reemitted.json")["pages"]
        adapter, _ = _adapter()
        results, _ = await _drain(adapter, pages)
        ids = [c.item_id for r in results for c in r.changes]
        assert len(ids) == sum(len(p["value"]) for p in pages) == 41
        assert len(ids) > len(set(ids))   # the planner, not the adapter, collapses re-emissions

    async def test_file_fields(self):
        raw = _load("move_out_in.json")
        moved_in_id = raw["_meta"]["moved_in_id"]
        item = next(i for p in raw["pages"] for i in p["value"] if i["id"] == moved_in_id)
        adapter, _ = _adapter()
        results, _ = await _drain(adapter, raw["pages"])
        c = next(c for r in results for c in r.changes if c.item_id == moved_in_id)
        assert c.name == item["name"] and c.size_bytes == item["size"]
        assert c.mime_type == item["file"]["mimeType"] and c.content_version == item["cTag"]
        assert c.parent_id == item["parentReference"]["id"] and c.web_url == item["webUrl"]
        assert c.modified_at == datetime.fromisoformat(item["lastModifiedDateTime"].replace("Z", "+00:00"))
        assert c.fs_modified_at == datetime.fromisoformat(
            item["fileSystemInfo"]["lastModifiedDateTime"].replace("Z", "+00:00"))
        assert c.facets["hashes"] == item["file"]["hashes"] and c.is_folder is False

    async def test_folder_fields(self):
        pages = _load("initial_enumeration.json")["pages"]
        raw = next(i for p in pages for i in p["value"] if "folder" in i and "specialFolder" not in i)
        adapter, _ = _adapter()
        results, _ = await _drain(adapter, pages)
        c = next(c for r in results for c in r.changes if c.item_id == raw["id"])
        assert c.is_folder and c.child_count == raw["folder"]["childCount"]
        assert c.content_version == "" and c.mime_type == ""

    async def test_photo_and_location_facets_feed_the_domain(self):
        heic = _load("facets.json")["heic_photo_location"]
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session([_resp(heic)])):
            c = await adapter.get_change(HANDLE, heic["id"])
        assert {"photo", "image", "location", "hashes"} <= set(c.facets)
        assert photo_location(c.facets) == (heic["location"]["latitude"], heic["location"]["longitude"])
        assert effective_date(c, datetime(2000, 1, 1, tzinfo=timezone.utc)) == datetime(
            2025, 8, 14, 18, 22, 5, tzinfo=timezone.utc)                      # takenDateTime wins
        assert content_hash_of(c) == f"{heic['file']['hashes']['quickXorHash']}:{heic['size']}"

    async def test_empty_photo_facet_is_kept_and_dates_fall_back(self):
        jpeg = _load("facets.json")["jpeg_empty_photo"]
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session([_resp(jpeg)])):
            c = await adapter.get_change(HANDLE, jpeg["id"])
        assert c.facets["photo"] == {} and "image" in c.facets
        assert effective_date(c, datetime(2000, 1, 1, tzinfo=timezone.utc)) == c.fs_modified_at

    async def test_video_facet(self):
        video = _load("facets.json")["video"]
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session([_resp(video)])):
            c = await adapter.get_change(HANDLE, video["id"])
        assert c.facets["video"]["duration"] == video["video"]["duration"]

    async def test_remote_package_malware_facets_are_carried(self):
        raw = {"id": "X!1", "name": "n", "file": {"mimeType": "x"}, "parentReference": {"id": "P!1"},
               "remoteItem": {"id": "R!1"}, "package": {"type": "oneNote"}, "malware": {}}
        adapter, _ = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session([_resp(raw)])):
            c = await adapter.get_change(HANDLE, "X!1")
        assert {"remoteItem", "package", "malware"} <= set(c.facets) and "hashes" not in c.facets


class TestResync:
    async def test_410_restarts_from_location_as_full_enumeration(self):
        gone = _load("resync_410.json")
        adapter, _ = _adapter()
        session = _session([_resp(APPROOT), _resp(gone["body"], status=410, headers=gone["headers"])])
        cursor = json.dumps({"url": f"{_GRAPH}/me/drive/special/approot/delta?token=old", "full": False})
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveResyncRequired) as exc:
            await adapter.get_changes(HANDLE, cursor)
        assert json.loads(exc.value.restart_cursor) == {"url": gone["headers"]["Location"], "full": True}

    async def test_restart_cursor_pages_report_full_enumeration(self):
        gone = _load("resync_410.json")
        pages = _load("initial_enumeration.json")["pages"]
        adapter, _ = _adapter()
        restart = json.dumps({"url": gone["headers"]["Location"], "full": True})
        results, urls = await _drain(adapter, pages, cursor=restart)
        assert urls[0] == gone["headers"]["Location"]
        assert [r.full_enumeration for r in results] == [True, True, True]

    async def test_410_without_location_restarts_from_a_fresh_delta(self):
        adapter, _ = _adapter()
        session = _session([_resp(APPROOT), _resp({}, status=410)])
        cursor = json.dumps({"url": f"{_GRAPH}/me/drive/special/approot/delta?token=old", "full": False})
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveResyncRequired) as exc:
            await adapter.get_changes(HANDLE, cursor)
        assert json.loads(exc.value.restart_cursor) == {"url": "/me/drive/special/approot/delta?$top=200",
                                                        "full": True}

    async def test_location_outside_graph_is_not_followed(self):
        adapter, _ = _adapter()
        session = _session([_resp(APPROOT), _resp({}, status=410, headers={"Location": "https://evil.example/x"})])
        cursor = json.dumps({"url": f"{_GRAPH}/me/drive/special/approot/delta?token=old", "full": False})
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveResyncRequired) as exc:
            await adapter.get_changes(HANDLE, cursor)
        assert "evil" not in exc.value.restart_cursor

    async def test_410_location_on_another_feed_restarts_from_a_fresh_delta(self):
        """RFC Δ46: a live 410 named the whole drive's legacy feed; following it failed every tick."""
        adapter, _ = _adapter()
        session = _session([_resp(APPROOT), _resp({}, status=410, headers={"Location": _LIVE_410_LOCATION})])
        cursor = json.dumps({"url": f"{_GRAPH}/me/drive/special/approot/delta?token=old", "full": False})
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveResyncRequired) as exc:
            await adapter.get_changes(HANDLE, cursor)
        assert json.loads(exc.value.restart_cursor) == {"url": "/me/drive/special/approot/delta?$top=200",
                                                        "full": True}

    async def test_stored_cursor_on_another_feed_restarts_without_a_call(self):
        """The state the 2026-10-12 incident left behind heals on the next tick."""
        adapter, _ = _adapter()
        session = _session([])
        cursor = json.dumps({"url": _LIVE_410_LOCATION, "full": True})
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveResyncRequired) as exc:
            await adapter.get_changes(HANDLE, cursor)
        assert json.loads(exc.value.restart_cursor) == {"url": "/me/drive/special/approot/delta?$top=200",
                                                        "full": True}
        session.get.assert_not_called()

    @pytest.mark.parametrize("url", [f"{_GRAPH}/me/drive/special/approot/delta?token=abc",
                                     f"{_GRAPH}/me/drive/special/approot/delta",
                                     "/me/drive/special/approot/delta?$top=200"])
    async def test_app_folder_feed_cursor_is_followed(self, url):
        page = {"value": [], "@odata.deltaLink": f"{_GRAPH}/me/drive/special/approot/delta?token=next"}
        adapter, _ = _adapter()
        session = _session([_resp(APPROOT), _resp(page)])
        with patch("aiohttp.ClientSession", return_value=session):
            result = await adapter.get_changes(HANDLE, json.dumps({"url": url, "full": False}))
        assert session.get.call_args_list[1].args[0].endswith(url.lstrip("/").split("v1.0/")[-1])
        assert result.complete is True

    @pytest.mark.parametrize("cursor", ["not json", json.dumps({"url": 1}), json.dumps(["x"]),
                                        json.dumps({"url": "https://evil.example/delta", "full": False}),
                                        json.dumps({"url": f"{_GRAPH}/me/drive/special/approot/delta",
                                                    "full": "false"})])
    async def test_unreadable_cursor_restarts_without_a_call(self, cursor):
        adapter, _ = _adapter()
        session = _session([])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveResyncRequired) as exc:
            await adapter.get_changes(HANDLE, cursor)
        assert json.loads(exc.value.restart_cursor)["full"] is True
        session.get.assert_not_called()

    async def test_page_without_any_link_is_an_error(self):
        adapter, _ = _adapter()
        session = _session([_resp(APPROOT), _resp({"value": []})])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(ValueError):
            await adapter.get_changes(HANDLE, None)


class TestItemReads:
    async def test_get_change_404_raises_not_found_and_is_never_a_deletion(self):
        # USER_DRIVE_INDEXING_RFC §26.2, Δ48: Graph answers 404 both for an item moved out (this recorded S0a
        # response) and for one the app has no access to, which delta still lists; the adapter cannot tell them
        # apart, so it raises and the caller decides. Existence is decided by delta's tombstone alone.
        fixture = _load("move_out_in.json")
        moved_out = fixture["_meta"]["moved_out_id"]
        adapter, _ = _adapter()
        session = _session([_resp(fixture["get_moved_out"]["body"], status=404)])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveItemNotFoundError):
            await adapter.get_change(HANDLE, moved_out)
        assert session.get.call_count == 1
        assert session.get.call_args.args[0].endswith("/me/drive/items/" + moved_out.replace("!", "%21"))

    async def test_list_children_first_page_asks_top_limit(self):
        pages = _load("initial_enumeration.json")["pages"]
        kids = [i for p in pages for i in p["value"] if "specialFolder" not in i]
        page1 = {"value": kids[:3], "@odata.nextLink": f"{_GRAPH}/me/drive/items/F/children?$skiptoken=2"}
        adapter, _ = _adapter()
        session = _session([_resp(page1)])
        with patch("aiohttp.ClientSession", return_value=session):
            page = await adapter.list_children_changes(HANDLE, "F!1", limit=3)
        assert [c.item_id for c in page.changes] == [k["id"] for k in kids[:3]]
        assert page.next_cursor == page1["@odata.nextLink"]
        assert session.get.call_count == 1      # one call per page: the caller resumes with next_cursor
        assert session.get.call_args.args[0].endswith("/me/drive/items/F%211/children?$top=3")

    async def test_list_children_resumes_from_the_cursor_until_the_last_page(self):
        pages = _load("initial_enumeration.json")["pages"]
        kids = [i for p in pages for i in p["value"] if "specialFolder" not in i]
        cursor = f"{_GRAPH}/me/drive/items/F/children?$skiptoken=2"
        adapter, _ = _adapter()
        session = _session([_resp({"value": kids[3:]})])
        with patch("aiohttp.ClientSession", return_value=session):
            page = await adapter.list_children_changes(HANDLE, "F!1", limit=3, cursor=cursor)
        assert [c.item_id for c in page.changes] == [k["id"] for k in kids[3:]]
        assert page.next_cursor is None
        assert session.get.call_args.args[0] == cursor

    async def test_list_children_cursor_outside_graph_restarts_the_listing(self):
        adapter, _ = _adapter()
        session = _session([_resp({"value": []})])
        with patch("aiohttp.ClientSession", return_value=session):
            page = await adapter.list_children_changes(HANDLE, "F", limit=200, cursor="https://evil.example/x")
        assert session.get.call_args.args[0] == f"{_GRAPH}/me/drive/items/F/children?$top=200"
        assert page.changes == () and page.next_cursor is None

    async def test_list_children_of_a_gone_folder_is_not_found_without_the_query(self):
        adapter, _ = _adapter()
        cursor = f"{_GRAPH}/me/drive/items/F/children?$skiptoken=SECRETISH"
        session = _session([_resp({"error": {"code": "itemNotFound"}}, status=404)])
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveItemNotFoundError) as exc:
            await adapter.list_children_changes(HANDLE, "F", limit=200, cursor=cursor)
        assert "SECRETISH" not in str(exc.value)     # the tick logs the message (§8.2 per-item)


class TestConnection:
    async def test_not_connected(self):
        adapter, _ = _adapter(creds=None)
        assert await adapter.connection("u1") == DriveConnection(connected=False)

    async def test_reads_flag_and_drive_id(self):
        adapter, oauth = _adapter(creds=_creds(needs_reconnect=True, drive_id="D1"))
        assert await adapter.connection("u1") == DriveConnection(connected=True, needs_reconnect=True,
                                                                 drive_id="D1")
        oauth.list_keys.assert_awaited_with("u1", ONEDRIVE_PROVIDER)

    async def test_a_changed_stored_token_drops_the_token_and_root_caches(self):
        # A reconnect (maybe to another account) writes a new token; the indexer must use it at once.
        adapter, oauth = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session([_resp(APPROOT)])):
            await adapter.get_change(HANDLE, ROOT_ID)                       # caches the "tok" bearer
        assert adapter._tokens.cached_token("u1", "") == "tok"
        oauth.get.return_value = _creds(access_token="B")
        mirror_get(oauth)
        adapter._root_cache["u1"] = (0.0, APPROOT)
        await adapter.connection("u1")
        assert adapter._tokens.cached_token("u1", "") is None and "u1" not in adapter._root_cache
        session = _session([_resp(APPROOT), _resp(_load("folder_delete.json")["pages"][-1])])
        with patch("aiohttp.ClientSession", return_value=session):
            await adapter.get_changes(HANDLE, None)
        bearers = [c.kwargs["headers"]["Authorization"] for c in session.get.call_args_list]
        assert bearers == ["Bearer B", "Bearer B"]                       # approot and delta page

    async def test_an_unchanged_token_keeps_the_caches(self):
        adapter, oauth = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session([_resp(APPROOT)])):
            await adapter.get_change(HANDLE, ROOT_ID)
        reads = oauth.list_keys.await_count
        await adapter.connection("u1")
        with patch("aiohttp.ClientSession", return_value=_session([_resp(APPROOT)])):
            await adapter.get_change(HANDLE, ROOT_ID)
        assert oauth.list_keys.await_count == reads + 1             # only connection() read the store

    async def test_disconnected_drops_the_caches(self):
        adapter, oauth = _adapter()
        with patch("aiohttp.ClientSession", return_value=_session([_resp(APPROOT)])):
            await adapter.get_change(HANDLE, ROOT_ID)
        oauth.get.return_value = None
        mirror_get(oauth)
        assert await adapter.connection("u1") == DriveConnection(connected=False)
        assert adapter._tokens.cached_token("u1", "") is None

    async def test_flag_reconnect_true_only_on_transition(self):
        adapter, oauth = _adapter(creds=_creds(drive_id="D1"))
        oauth.update.return_value = True                          # the record still exists
        assert await adapter.flag_reconnect("u1") is True
        oauth.save.assert_not_called()                            # a flag write never creates a record
        saved = oauth.update.call_args.args[0]
        assert saved.needs_reconnect is True and saved.drive_id == "D1" and saved.refresh_token == "r"

    async def test_flag_reconnect_already_flagged_writes_nothing(self):
        adapter, oauth = _adapter(creds=_creds(needs_reconnect=True))
        assert await adapter.flag_reconnect("u1") is False
        oauth.save.assert_not_called()
        oauth.update.assert_not_called()

    async def test_flag_reconnect_without_credentials(self):
        adapter, oauth = _adapter(creds=None)
        assert await adapter.flag_reconnect("u1") is False
        oauth.save.assert_not_called()
        oauth.update.assert_not_called()


class TestDriveIdSource:
    def test_connect_and_change_feed_read_the_same_field_of_the_same_call(self):
        # The drive-switch check compares these two strings; both readers must stay on one source. The connect side
        # moved from the OneDrive callback to the adapter's own `read_drive_id` (MULTI_DRIVE_RFC §5.1), which reads
        # the same `_APPROOT` constant the change feed starts from.
        import inspect
        from src.adapters.microsoft import onedrive_adapter
        approot = f"{onedrive_adapter._GRAPH}{onedrive_adapter._APPROOT}"
        assert approot == f"{_GRAPH}/me/drive/special/approot"
        connect = inspect.getsource(onedrive_adapter.OneDriveAdapter.read_drive_id)
        assert 'f"{_GRAPH}{_APPROOT}"' in connect
        assert '.get("driveId")' in connect
        assert '.get("driveId"' in inspect.getsource(onedrive_adapter.OneDriveAdapter.get_changes)


class TestLogging:
    async def test_delta_url_query_never_reaches_the_logs(self):
        adapter, _ = _adapter()
        cursor = json.dumps({"url": f"{_GRAPH}/me/drive/special/approot/delta?token=SECRETISH", "full": False})
        session = _session([_resp(APPROOT), _resp({"error": {}}, status=500)])
        with patch("aiohttp.ClientSession", return_value=session), \
                patch("src.adapters.microsoft.onedrive_adapter.logger") as log, pytest.raises(ValueError):
            await adapter.get_changes(HANDLE, cursor)
        assert "SECRETISH" not in log.error.call_args.args[0]
