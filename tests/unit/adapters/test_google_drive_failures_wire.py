"""Wire — failure and edge paths of the Google drive adapter (GOOGLE_DRIVE_RFC §3.2-§3.7, GE-17, GE-18). A trashed folder
or item is not found at every entry point; a resumable upload's chunk failures map to the port's errors and never quote
the file name; an upload session URI outside Drive's upload endpoint is refused; no credentials is DriveNotConnectedError;
a replace onto a folder's name conflicts; a foreign or unreadable listing cursor restarts the listing; a 400 on the feed
other than `invalid` is not a resync; pagination of `drives.list` and `files.list`; a Retry-After in HTTP-date form falls
back to the default; a 204 answers `{}`. Served by the stateful Drive fake."""
import asyncio
import json

import aiohttp
import pytest

from src.adapters.google import google_drive_adapter
from src.adapters.google.google_drive_adapter import (
    API, SIMPLE_UPLOAD_MAX, THROTTLE_HINT_S, _backoff, _hint,
)
from src.adapters.google.google_drive_cursor import FEED, FeedCursor, decode, encode
from src.domain.user_drive import (
    DriveItemNotFoundError, DriveNameConflictError, DriveNotConnectedError, DriveResyncRequired, DriveStorageFull,
    DriveThrottled,
)
from tests.support.google_drive_fake import _Response, google_setup

_BIG = b"x" * (SIMPLE_UPLOAD_MAX + 1)
_FILE_NAME = "Garden plan 2026/27"            # the fake's error messages quote it; it must never reach an error


@pytest.fixture
async def setup(monkeypatch):
    return await google_setup(monkeypatch)


# -- 1. trashed entry points -------------------------------------------------------------------------------------------


async def test_a_trashed_folder_is_not_found_by_list_children(setup):
    folder = setup.a.add_folder(setup.a.root.id, "Springfield")
    setup.a.trash(folder)
    with pytest.raises(DriveItemNotFoundError):
        await setup.adapter.list_children(setup.handle, folder.id)


async def test_a_trashed_folder_is_not_found_by_the_listing_page(setup):
    folder = setup.a.add_folder(setup.a.root.id, "Springfield")
    setup.a.trash(folder)
    with pytest.raises(DriveItemNotFoundError):
        await setup.adapter.list_children_changes(setup.handle, folder.id, 10)
    assert setup.fake.calls("files.list") == []


async def test_a_trashed_item_has_no_thumbnail_and_is_not_found(setup):
    node = setup.a.add_file(setup.a.root.id, "Hamburg talk.mp4", b"talk", mime="video/mp4", thumbnail=b"png")
    setup.a.trash(node)
    with pytest.raises(DriveItemNotFoundError):
        await setup.adapter.get_thumbnail(setup.handle, node.id, 220)
    assert setup.fake.calls("thumbnail") == []


# -- 2. resumable chunk failures ---------------------------------------------------------------------------------------


async def test_a_chunk_refused_for_storage_quota_is_storage_full_without_the_file_name(setup):
    setup.fake.fail("upload.chunk", 403, "storageQuotaExceeded")
    with pytest.raises(DriveStorageFull) as caught:
        await setup.adapter.upload(setup.handle, setup.a.root.id, "Hamburg talk.mp4", _BIG, "video/mp4")
    assert _FILE_NAME not in str(caught.value)


async def test_any_other_chunk_failure_is_a_value_error_with_status_and_reason_only(setup):
    setup.fake.fail("upload.chunk", 500, "backendError")
    with pytest.raises(ValueError) as caught:
        await setup.adapter.upload(setup.handle, setup.a.root.id, "Hamburg talk.mp4", _BIG, "video/mp4")
    assert "500" in str(caught.value) and "backendError" in str(caught.value) and _FILE_NAME not in str(caught.value)
    assert len(setup.fake.calls("upload.chunk")) == 1                  # a chunk is not retried


async def test_a_chunk_timeout_stays_a_timeout(setup):
    setup.fake.fail("upload.chunk", "timeout")
    with pytest.raises(asyncio.TimeoutError):
        await setup.adapter.upload(setup.handle, setup.a.root.id, "Hamburg talk.mp4", _BIG, "video/mp4")


async def test_a_chunk_transport_failure_is_an_os_error(setup):
    setup.fake.fail("upload.chunk", "transport")
    with pytest.raises(OSError) as caught:
        await setup.adapter.upload(setup.handle, setup.a.root.id, "Hamburg talk.mp4", _BIG, "video/mp4")
    assert not isinstance(caught.value, aiohttp.ClientError)


async def test_an_upload_that_never_completes_is_a_value_error(setup):
    setup.fake._chunk = lambda params, headers, data: _Response(308, b"", {})
    with pytest.raises(ValueError, match="without the file"):
        await setup.adapter.upload(setup.handle, setup.a.root.id, "Hamburg talk.mp4", _BIG, "video/mp4")


# -- 3. the upload session URI guard -----------------------------------------------------------------------------------


@pytest.mark.parametrize("location", ["https://evil.example.invalid/upload/drive/v3/files?upload_id=up-1",
                                      "http://127.0.0.1:9/steal", ""])
async def test_a_session_uri_outside_drives_upload_endpoint_is_refused_and_nothing_is_put(setup, location):
    setup.fake._upload_resumable = lambda *a: _Response(200, {}, {"Location": location})
    with pytest.raises(ValueError, match="valid URI"):
        await setup.adapter.upload(setup.handle, setup.a.root.id, "Hamburg talk.mp4", _BIG, "video/mp4")
    assert setup.fake.calls("upload.chunk") == [] and setup.fake.unrouted == []


# -- 4. no credentials -------------------------------------------------------------------------------------------------


async def test_a_handle_without_a_stored_credential_is_not_connected(setup):
    from dataclasses import replace
    gone = replace(setup.handle, credential_id="cred-unknown")
    with pytest.raises(DriveNotConnectedError):
        await setup.adapter.get_item(gone, setup.a.root.id)
    assert setup.fake.requests == []


async def test_media_source_without_a_token_is_not_connected_and_hands_out_no_header(setup, monkeypatch):
    node = setup.a.add_file(setup.a.root.id, "Hamburg talk.mp4", b"talk", mime="video/mp4")
    real = setup.adapter._tokens.token

    async def none_for_media(user_id, credential_id, **kwargs):
        return None if "min_life" in kwargs else await real(user_id, credential_id, **kwargs)

    monkeypatch.setattr(setup.adapter._tokens, "token", none_for_media)
    with pytest.raises(DriveNotConnectedError):
        await setup.adapter.media_source(setup.handle, node.id)


# -- 5. replace onto a folder ------------------------------------------------------------------------------------------


async def test_replace_onto_a_name_held_by_a_folder_conflicts_and_writes_nothing(setup):
    setup.a.add_folder(setup.a.root.id, "Springfield")
    with pytest.raises(DriveNameConflictError):
        await setup.adapter.upload(setup.handle, setup.a.root.id, "springfield", b"x", "text/plain",
                                   conflict="replace")
    assert not any(r.route.startswith("upload.") for r in setup.fake.requests)


# -- 6. listing cursor of another folder or unreadable -----------------------------------------------------------------


@pytest.mark.parametrize("make", [
    lambda folder: json.dumps({"folder_id": "other-folder", "page_token": "lt.0.1"}),
    lambda folder: "not json",
    lambda folder: json.dumps([folder]),
    lambda folder: json.dumps({"folder_id": folder, "page_token": 7}),
    lambda folder: json.dumps({"folder_id": folder}),
], ids=["other-folder", "not-json", "not-an-object", "token-not-a-string", "no-token"])
async def test_a_foreign_or_unreadable_listing_cursor_restarts_from_the_first_page(setup, make):
    folder = setup.a.add_folder(setup.a.root.id, "Springfield")
    names = [f"Garden plan {n}.md" for n in range(3)]
    for name in names:
        setup.a.add_file(folder.id, name, b"x")
    page = await setup.adapter.list_children_changes(setup.handle, folder.id, 2, cursor=make(folder.id))
    assert "pageToken" not in setup.fake.calls("files.list")[0].params
    assert [c.name for c in page.changes] == names[:2]
    assert json.loads(page.next_cursor) == {"folder_id": folder.id, "page_token": "lt.0.2"}


async def test_the_listing_cursor_of_this_folder_continues(setup):
    folder = setup.a.add_folder(setup.a.root.id, "Springfield")
    for n in range(3):
        setup.a.add_file(folder.id, f"Garden plan {n}.md", b"x")
    first = await setup.adapter.list_children_changes(setup.handle, folder.id, 2)
    second = await setup.adapter.list_children_changes(setup.handle, folder.id, 2, cursor=first.next_cursor)
    assert [c.name for c in second.changes] == ["Garden plan 2.md"] and second.next_cursor is None


# -- 7. a 400 on the feed that is not `invalid` ------------------------------------------------------------------------


async def test_a_feed_400_with_another_reason_is_raised_not_turned_into_a_resync(setup):
    cursor = encode(FeedCursor(drive="google_drive:account:perm-a", phase=FEED, start="1", page="1"))
    setup.fake.fail("changes.list", 400, "badRequest")
    with pytest.raises(ValueError) as caught:
        await setup.adapter.get_changes(setup.handle, cursor)
    assert not isinstance(caught.value, DriveResyncRequired)


async def test_a_feed_400_invalid_is_still_a_resync(setup):
    cursor = encode(FeedCursor(drive="google_drive:account:perm-a", phase=FEED, start="1", page="1"))
    setup.fake.fail("changes.list", 400, "invalid")
    with pytest.raises(DriveResyncRequired):
        await setup.adapter.get_changes(setup.handle, cursor)


# -- 8. pagination -----------------------------------------------------------------------------------------------------


async def test_member_drives_are_read_across_pages(setup):
    ids = [setup.a.add_shared_drive(f"Acme {n:03}") for n in range(101)]
    page = await setup.adapter.get_changes(setup.handle, None)
    calls = setup.fake.calls("drives.list")
    assert len(calls) == 2 and "pageToken" not in calls[0].params and calls[1].params["pageToken"] == "100"
    corpora = decode(page.cursor).corpora
    assert [c["id"] for c in corpora] == ids and len(corpora) == 101     # the user pass ended on this page


async def test_children_are_read_across_pages(setup, monkeypatch):
    monkeypatch.setattr(google_drive_adapter, "PAGE_SIZE", 2)
    folder = setup.a.add_folder(setup.a.root.id, "Springfield")
    for n in range(5):
        setup.a.add_file(folder.id, f"Garden plan {n}.md", b"x")
    items = await setup.adapter.list_children(setup.handle, folder.id)
    assert sorted(i.name for i in items) == [f"Garden plan {n}.md" for n in range(5)]
    calls = [c for c in setup.fake.calls("files.list")]
    assert len(calls) == 3 and "pageToken" not in calls[0].params
    assert calls[1].params["pageToken"] == "lt.0.2" and calls[2].params["pageToken"] == "lt.0.4"


# -- 9. Retry-After that is not a number -------------------------------------------------------------------------------


def test_an_http_date_retry_after_falls_back_to_the_defaults():
    date = "Wed, 21 Oct 2026 07:28:00 GMT"
    assert _backoff(date, 2) == 4.0 and _hint(date) == THROTTLE_HINT_S


async def test_a_throttle_with_an_http_date_retry_after_backs_off_exponentially_and_hints_the_default(setup):
    node = setup.a.add_file(setup.a.root.id, "Hamburg talk.mp4", b"talk")
    setup.fake.fail("files.get", 429, "", times=4, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    with pytest.raises(DriveThrottled) as caught:
        await setup.adapter.get_item(setup.handle, node.id)
    assert setup.sleeps == [1.0, 2.0, 4.0] and caught.value.retry_after_s == THROTTLE_HINT_S


# -- 10. 204 -----------------------------------------------------------------------------------------------------------


async def test_a_204_answer_is_an_empty_object(setup):
    node = setup.a.add_file(setup.a.root.id, "Hamburg talk.mp4", b"talk")
    async with aiohttp.ClientSession() as session:
        out = await setup.adapter._call(session, setup.handle, "DELETE", f"{API}/files/{node.id}", "files.delete")
    assert out == {}
