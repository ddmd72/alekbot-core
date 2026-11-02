"""Wire — native documents (GOOGLE_DRIVE_RFC §3.5, G6, G7, GE-4, GE-6, GE-22). A Doc, Sheet, Slides or Drawing reads
through one export whose mime is the item's `mime_type`; a Form (or any other native) is metadata only. An over-cap
export maps to the size errors. A native's change key is `r:<head revision>` from `revisions.list` (all pages, the
last item) when the item allows reading revisions, else `m:<modifiedTime>` without a call; a refused list falls back
and never fails the page; at most 4 revisions calls run at once. Served by the stateful Drive fake."""
import pytest

from src.adapters.google import google_drive_adapter
from src.adapters.google.google_drive_adapter import GOOGLE_EXPORT_MAX_BYTES, REVISION_CALLS_IN_FLIGHT
from src.domain.user_drive import DriveFileTooLarge, DriveFileTooLargeError, DriveItemNotFoundError
from tests.support.google_drive_fake import DOC, DRAWING, FORM, SHEET, SLIDES, XLSX, google_setup

_EXPORTS = [(DOC, "text/markdown", "brief.docx"), (SHEET, XLSX, "budget.csv"), (SLIDES, "text/plain", "Hamburg talk"),
            (DRAWING, "application/pdf", "Garden plan 2026/27")]


@pytest.fixture
async def setup(monkeypatch):
    return await google_setup(monkeypatch)


@pytest.mark.parametrize("native,export,name", _EXPORTS)
async def test_each_native_reads_through_its_export(setup, tmp_path, native, export, name):
    node = setup.a.add_native(setup.a.root.id, name, native, exports={export: b"exported"}, size=1234)
    item = await setup.adapter.get_item(setup.handle, node.id)
    assert item.mime_type == export and item.name == name and item.size_bytes == 1234
    assert item.content_identity is None and item.access.can_write_content is False and item.access.can_download
    assert await setup.adapter.download(setup.handle, node.id) == b"exported"
    assert await setup.adapter.download_to_file(setup.handle, node.id, str(tmp_path / "x"), 1024) == len(b"exported")
    exports = setup.fake.calls("export")
    assert len(exports) == 2 and all(r.params["mimeType"] == export for r in exports)
    assert setup.fake.calls("media") == []                   # a native is never read with alt=media


async def test_a_form_is_metadata_only(setup, tmp_path):
    node = setup.a.add_native(setup.a.root.id, "Garden sign-up", FORM)
    item = await setup.adapter.get_item(setup.handle, node.id)
    change = await setup.adapter.get_change(setup.handle, node.id)
    assert (item.mime_type, change.mime_type, item.content_version) == ("", "", "")
    assert item.access.can_download is False
    with pytest.raises(DriveItemNotFoundError):
        await setup.adapter.download(setup.handle, node.id)
    with pytest.raises(DriveItemNotFoundError):
        await setup.adapter.download_to_file(setup.handle, node.id, str(tmp_path / "x"), 1024)
    with pytest.raises(DriveItemNotFoundError):
        await setup.adapter.media_source(setup.handle, node.id)
    assert setup.fake.calls("export") == [] and setup.fake.calls("revisions.list") == []


async def test_an_over_cap_export_is_too_large_in_both_downloads(setup, tmp_path):
    node = setup.a.add_native(setup.a.root.id, "brief.docx", DOC, too_large={"text/markdown"})
    with pytest.raises(DriveFileTooLargeError) as chat:
        await setup.adapter.download(setup.handle, node.id)
    assert chat.value.limit_bytes == GOOGLE_EXPORT_MAX_BYTES and chat.value.item.item_id == node.id
    with pytest.raises(DriveFileTooLarge) as index:
        await setup.adapter.download_to_file(setup.handle, node.id, str(tmp_path / "x"), 50 * 1024 * 1024)
    assert index.value.limit_bytes == GOOGLE_EXPORT_MAX_BYTES


async def test_the_change_key_is_the_last_revision_across_pages(setup, monkeypatch):
    monkeypatch.setattr(google_drive_adapter, "PAGE_SIZE", 2)
    node = setup.a.add_native(setup.a.root.id, "brief.docx", DOC, exports={"text/markdown": b"b"},
                              revisions=["rev-n1", "rev-n2", "rev-n3"])
    change = await setup.adapter.get_change(setup.handle, node.id)
    assert change.content_version == "r:rev-n3"
    calls = setup.fake.calls("revisions.list")
    assert len(calls) == 2 and calls[1].params["pageToken"]
    assert calls[0].params["fields"] == "nextPageToken,revisions(id)"


async def test_a_rename_keeps_the_change_key(setup):
    node = setup.a.add_native(setup.a.root.id, "brief.docx", DOC, exports={"text/markdown": b"b"})
    before = await setup.adapter.get_change(setup.handle, node.id)
    await setup.adapter.move(setup.handle, node.id, new_name="Hamburg brief.docx")
    after = await setup.adapter.get_change(setup.handle, node.id)
    assert after.name == "Hamburg brief.docx" and after.content_version == before.content_version == "r:rev-n1"


async def test_without_read_revisions_there_is_no_call_and_the_key_is_the_modified_time(setup):
    node = setup.a.add_native(setup.a.root.id, "budget.csv", SHEET, exports={XLSX: b"x"}, can_read_revisions=False)
    change = await setup.adapter.get_change(setup.handle, node.id)
    assert change.content_version == f"m:{node.modified}"
    assert setup.fake.calls("revisions.list") == []


@pytest.mark.parametrize("status,reason", [(403, "insufficientFilePermissions"), (404, "notFound"), (403, "")])
async def test_a_refused_revisions_list_falls_back_and_the_page_succeeds(setup, status, reason):
    doc = setup.a.add_native(setup.a.root.id, "brief.docx", DOC, exports={"text/markdown": b"b"})
    other = setup.a.add_file(setup.a.root.id, "Hamburg talk.mp4", b"talk")
    setup.fake.fail("revisions.list", status, reason)
    page = await setup.adapter.get_changes(setup.handle, None)
    by_id = {c.item_id: c for c in page.changes}
    assert by_id[doc.id].content_version == f"m:{doc.modified}"
    assert by_id[other.id].content_version == other.head


async def test_at_most_four_revisions_calls_run_at_once(setup):
    folder = setup.a.add_folder(setup.a.root.id, "Projects")
    for n in range(10):
        setup.a.add_native(folder.id, f"brief {n}.docx", DOC, exports={"text/markdown": b"b"})
    page = await setup.adapter.list_children_changes(setup.handle, folder.id, 100)
    assert all(c.content_version == "r:rev-n1" for c in page.changes)
    assert len(setup.fake.calls("revisions.list")) == 10
    assert setup.fake.max_in_flight["revisions.list"] == REVISION_CALLS_IN_FLIGHT == 4


async def test_binaries_never_read_revisions(setup):
    setup.a.add_file(setup.a.root.id, "Hamburg talk.mp4", b"talk")
    await setup.adapter.get_changes(setup.handle, None)
    assert setup.fake.calls("revisions.list") == []
