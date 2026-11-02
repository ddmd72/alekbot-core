"""Wire — the change feed and its cursor (GOOGLE_DRIVE_RFC §3.3, GE-14, GE-34, GE-35). The emulated enumeration takes
the start token first, lists `corpora=user` with `orderBy=folder`, then per member shared drive emits the drive's root
and lists `corpora=drive`; its last page is `complete` and switches the cursor to the feed at the start token. Feed
rounds ask for removed items across all drives and are `complete` only on `newStartPageToken`. Served by the stateful
Drive fake; page sizes shrunk where a test needs several pages."""
import json
import logging

import pytest

from src.adapters.google import google_drive_adapter
from src.adapters.google.google_drive_cursor import FEED, FeedCursor, decode, encode
from src.adapters.google.google_drive_mapping import FILE_FIELDS
from src.domain.user_drive import DriveResyncRequired
from tests.contracts.user_drive_contract import drain
from tests.support.google_drive_fake import FOLDER, google_setup

_ID = "google_drive:account:perm-a"


@pytest.fixture
async def setup(monkeypatch):
    return await google_setup(monkeypatch)


def _plant_my_drive(account):
    folder = account.add_folder(account.root.id, "Projects")
    talk = account.add_file(folder.id, "Hamburg talk.mp4", b"talk", mime="video/mp4")
    return folder, talk


async def test_a_fresh_cursor_takes_the_start_token_before_listing_the_user_corpus(setup):
    _plant_my_drive(setup.a)
    page = await setup.adapter.get_changes(setup.handle, None)
    routes = [r.route for r in setup.fake.requests if r.route in ("changes.getStartPageToken", "files.list")]
    assert routes[0] == "changes.getStartPageToken"
    listing = setup.fake.calls("files.list")[0].params
    assert listing["corpora"] == "user" and listing["orderBy"] == "folder" and listing["q"] == "trashed=false"
    assert listing["pageSize"] == "1000"
    assert listing["supportsAllDrives"] == "true" and listing["includeItemsFromAllDrives"] == "true"
    assert page.full_enumeration and page.complete             # no member drive: one pass, one page
    assert page.root_item_id == setup.a.root.id and page.drive_identity == _ID


async def test_folders_come_first_in_an_enumeration_page(setup):
    talk = setup.a.add_file(setup.a.root.id, "Hamburg talk.mp4", b"talk")
    folder = setup.a.add_folder(setup.a.root.id, "Projects")
    page = await setup.adapter.get_changes(setup.handle, None)
    assert [c.item_id for c in page.changes] == [folder.id, talk.id]


async def test_each_member_drive_gets_its_root_change_and_a_drive_pass(setup, monkeypatch):
    monkeypatch.setattr(google_drive_adapter, "PAGE_SIZE", 2)
    _plant_my_drive(setup.a)
    setup.a.add_file(setup.a.root.id, "brief.docx", b"brief")
    drive_id = setup.a.add_shared_drive("Acme Hamburg")
    q3 = setup.a.add_folder(drive_id, "Q3", drive_id=drive_id)
    budget = setup.a.add_file(q3.id, "Q3 budget.xlsx", b"budget", drive_id=drive_id)
    pages = []
    cursor = None
    while True:
        page = await setup.adapter.get_changes(setup.handle, cursor)
        pages.append(page)
        cursor = page.cursor
        if page.complete:
            break
    assert all(p.full_enumeration for p in pages) and [p.complete for p in pages] == [False, False, True]
    assert all(p.root_item_id == setup.a.root.id and p.drive_identity == _ID for p in pages)
    root_change = pages[2].changes[0]
    assert (root_change.item_id, root_change.parent_id, root_change.name, root_change.is_folder) == \
        (drive_id, "", "Acme Hamburg", True)
    assert [c.item_id for c in pages[2].changes[1:]] == [q3.id, budget.id]
    drive_pass = setup.fake.calls("files.list")[-1].params
    assert drive_pass["corpora"] == "drive" and drive_pass["driveId"] == drive_id and drive_pass["orderBy"] == "folder"
    user_ids = {c.item_id for p in pages[:2] for c in p.changes}
    assert budget.id not in user_ids and q3.id not in user_ids


async def test_the_last_enumeration_page_switches_to_the_feed_at_the_start_token(setup):
    page = await setup.adapter.get_changes(setup.handle, None)
    cursor = decode(page.cursor)
    start = setup.fake.calls("changes.getStartPageToken")
    assert start and cursor.phase == FEED and cursor.page == cursor.start and cursor.drive == _ID


async def test_the_first_feed_round_replays_what_changed_during_the_enumeration(setup):
    page = await setup.adapter.get_changes(setup.handle, None)
    talk = setup.a.add_file(setup.a.root.id, "Hamburg talk.mp4", b"talk")   # after the start token
    round_ = await setup.adapter.get_changes(setup.handle, page.cursor)
    assert not round_.full_enumeration and round_.complete
    assert [c.item_id for c in round_.changes] == [talk.id]


async def test_a_feed_round_asks_for_removed_items_across_all_drives(setup):
    _, cursor = await drain(setup.adapter, setup.handle)
    await setup.adapter.get_changes(setup.handle, cursor)
    params = setup.fake.calls("changes.list")[-1].params
    assert params["includeRemoved"] == "true" and params["restrictToMyDrive"] == "false"
    assert params["supportsAllDrives"] == "true" and params["includeItemsFromAllDrives"] == "true"
    assert params["pageToken"] == decode(cursor).page and params["pageSize"] == "1000"
    assert f"file({FILE_FIELDS})" in params["fields"] and "newStartPageToken" in params["fields"]


async def test_a_round_is_complete_only_on_a_new_start_token(setup, monkeypatch):
    _, cursor = await drain(setup.adapter, setup.handle)
    for name in ("a.txt", "b.txt", "c.txt"):
        setup.a.add_file(setup.a.root.id, name, b"x")
    monkeypatch.setattr(google_drive_adapter, "PAGE_SIZE", 2)
    first = await setup.adapter.get_changes(setup.handle, cursor)
    second = await setup.adapter.get_changes(setup.handle, first.cursor)
    assert (first.complete, second.complete) == (False, True)
    assert len(first.changes) + len(second.changes) == 3
    assert all(not p.full_enumeration and p.root_item_id == setup.a.root.id and p.drive_identity == _ID
               for p in (first, second))


async def test_removed_and_trashed_items_are_deletions(setup):
    folder, talk = _plant_my_drive(setup.a)
    gone = setup.a.add_file(setup.a.root.id, "brief.docx", b"brief")
    _, cursor = await drain(setup.adapter, setup.handle)
    setup.a.lose(gone)
    setup.a.trash(talk)
    round_ = await setup.adapter.get_changes(setup.handle, cursor)
    deleted = {c.item_id: c for c in round_.changes if c.deleted}
    assert set(deleted) == {gone.id, talk.id}
    assert deleted[gone.id].identity == f"google_drive:{gone.id}"


async def test_a_lost_shared_drive_membership_restarts_the_enumeration(setup):
    drive_id = setup.a.add_shared_drive("Acme Hamburg")
    _, cursor = await drain(setup.adapter, setup.handle)
    setup.a.leave_drive(drive_id)
    with pytest.raises(DriveResyncRequired) as caught:
        await setup.adapter.get_changes(setup.handle, cursor)
    restart = await setup.adapter.get_changes(setup.handle, caught.value.restart_cursor)
    assert restart.full_enumeration
    assert drive_id not in {c.item_id for c in restart.changes}


async def test_a_renamed_shared_drive_renames_its_root(setup):
    drive_id = setup.a.add_shared_drive("Acme Hamburg")
    _, cursor = await drain(setup.adapter, setup.handle)
    setup.a.rename_drive(drive_id, "Acme Hamburg 2027")
    round_ = await setup.adapter.get_changes(setup.handle, cursor)
    root = [c for c in round_.changes if c.item_id == drive_id]
    assert len(root) == 1 and (root[0].name, root[0].parent_id, root[0].is_folder) == ("Acme Hamburg 2027", "", True)


async def test_a_refused_stored_list_token_restarts_its_pass(setup, monkeypatch, caplog):
    monkeypatch.setattr(google_drive_adapter, "PAGE_SIZE", 2)
    for name in ("a.txt", "b.txt", "c.txt"):
        setup.a.add_file(setup.a.root.id, name, b"x")
    first = await setup.adapter.get_changes(setup.handle, None)
    setup.a.expire_list_tokens()
    with caplog.at_level(logging.WARNING):
        again = await setup.adapter.get_changes(setup.handle, first.cursor)
    assert "list page token refused" in caplog.text
    assert [c.item_id for c in again.changes] == [c.item_id for c in first.changes]   # the pass's first page again
    assert again.full_enumeration and not again.complete
    tokens = [r.params.get("pageToken") for r in setup.fake.calls("files.list")]
    assert tokens[1] and tokens[2] is None


async def test_a_refused_drive_pass_token_emits_the_drive_root_again(setup, monkeypatch):
    monkeypatch.setattr(google_drive_adapter, "PAGE_SIZE", 2)
    drive_id = setup.a.add_shared_drive("Acme Hamburg")
    for name in ("a.txt", "b.txt", "c.txt"):
        setup.a.add_file(drive_id, name, b"x", drive_id=drive_id)
    first = await setup.adapter.get_changes(setup.handle, None)        # the user pass: empty, one page
    drive_page = await setup.adapter.get_changes(setup.handle, first.cursor)
    assert drive_page.changes[0].item_id == drive_id
    setup.a.expire_list_tokens()
    again = await setup.adapter.get_changes(setup.handle, drive_page.cursor)
    assert again.changes[0].item_id == drive_id and again.changes[0].name == "Acme Hamburg"


async def test_a_refusal_of_a_pass_first_page_is_a_plain_failure(setup):
    setup.fake.fail("files.list", 400, "invalid")
    with pytest.raises(ValueError) as caught:
        await setup.adapter.get_changes(setup.handle, None)
    assert not isinstance(caught.value, DriveResyncRequired)


@pytest.mark.parametrize("cursor", [
    encode(FeedCursor(drive="google_drive:account:perm-b", phase=FEED, start="1", page="1")),
    "not a cursor",
    json.dumps({"v": 2, "drive": _ID, "phase": "feed", "start": "1", "corpora": None, "list": "", "page": "1"}),
    json.dumps({"v": 1, "drive": _ID, "phase": "sideways", "start": "1", "corpora": None, "list": "", "page": "1"}),
])
async def test_a_foreign_unreadable_or_other_version_cursor_restarts_fresh(setup, caplog, cursor):
    with caplog.at_level(logging.WARNING):
        with pytest.raises(DriveResyncRequired) as caught:
            await setup.adapter.get_changes(setup.handle, cursor)
    assert "restarting enumeration" in caplog.text
    fresh = decode(caught.value.restart_cursor)
    assert fresh.drive == _ID and fresh.fresh
    assert setup.fake.calls("changes.list") == [] and setup.fake.calls("files.list") == []
    page = await setup.adapter.get_changes(setup.handle, caught.value.restart_cursor)
    assert page.full_enumeration and setup.fake.calls("changes.getStartPageToken")


async def test_a_refused_change_token_is_resync(setup):
    _, cursor = await drain(setup.adapter, setup.handle)
    setup.a.expired = True
    with pytest.raises(DriveResyncRequired) as caught:
        await setup.adapter.get_changes(setup.handle, cursor)
    assert decode(caught.value.restart_cursor).fresh


async def test_a_folder_change_is_a_folder_with_its_parent(setup):
    folder, talk = _plant_my_drive(setup.a)
    page = await setup.adapter.get_changes(setup.handle, None)
    by_id = {c.item_id: c for c in page.changes}
    assert by_id[folder.id].is_folder and by_id[folder.id].parent_id == setup.a.root.id
    assert by_id[talk.id].parent_id == folder.id and by_id[talk.id].mime_type == "video/mp4"
    assert setup.a.files[folder.id].mime == FOLDER
