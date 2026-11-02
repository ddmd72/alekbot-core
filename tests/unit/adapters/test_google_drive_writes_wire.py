"""Wire — writes (GOOGLE_DRIVE_RFC §3.2, §3.6, GE-15, GE-19). Uploads up to 5 MB are multipart, above resumable in 8 MiB
chunks (a multiple of 256 KiB); the metadata never carries a `mimeType` (that would convert the file) and names the
parent. Drive has no conflict parameter: modes are emulated by `name_key` over the parent's children (`Report.pdf`
conflicts with `report.pdf`), the oldest match first. `move` sends `addParents`/`removeParents`; `delete` trashes and
never deletes; a native refuses a content write before any upload. Served by the stateful Drive fake."""
import pytest

from src.adapters.google.google_drive_adapter import SIMPLE_UPLOAD_MAX, UPLOAD_CHUNK
from src.adapters.google.google_drive_mapping import FOLDER_MIME, escape_query
from src.domain.user_drive import DriveAccessDenied, DriveNameConflictError
from tests.support.google_drive_fake import DOC, google_setup

_KIB = 1024


@pytest.fixture
async def setup(monkeypatch):
    return await google_setup(monkeypatch)


async def test_a_small_upload_is_one_multipart_call_without_a_mime_type_in_the_metadata(setup):
    item = await setup.adapter.upload(setup.handle, setup.a.root.id, "brief.docx", b"brief", "application/pdf")
    calls = setup.fake.calls("upload.multipart")
    assert len(calls) == 1 and calls[0].params["uploadType"] == "multipart"
    assert calls[0].headers["Content-Type"].startswith("multipart/related; boundary=")
    assert setup.fake.upload_metas == [{"name": "brief.docx", "parents": [setup.a.root.id]}]
    assert setup.a.files[item.item_id].mime == "application/pdf" and setup.a.files[item.item_id].data == b"brief"
    assert item.path == "brief.docx" and item.mime_type == "application/pdf"


async def test_exactly_five_megabytes_is_still_multipart(setup):
    await setup.adapter.upload(setup.handle, setup.a.root.id, "a.bin", b"x" * SIMPLE_UPLOAD_MAX, "application/zip")
    assert len(setup.fake.calls("upload.multipart")) == 1 and setup.fake.calls("upload.resumable") == []
    assert SIMPLE_UPLOAD_MAX == 5 * 1024 * 1024


async def test_a_large_upload_is_resumable_in_256_kib_multiple_chunks_without_the_bearer(setup):
    data = bytes(range(256)) * (17 * 4 * _KIB)                 # 17 MiB: two full chunks and a tail
    item = await setup.adapter.upload(setup.handle, setup.a.root.id, "Hamburg talk.mp4", data, "video/mp4")
    start = setup.fake.calls("upload.resumable")
    assert len(start) == 1 and start[0].json == {"name": "Hamburg talk.mp4", "parents": [setup.a.root.id]}
    assert start[0].headers["X-Upload-Content-Type"] == "video/mp4"
    assert start[0].headers["X-Upload-Content-Length"] == str(len(data))
    chunks = setup.fake.calls("upload.chunk")
    sizes = [len(c.data) for c in chunks]
    assert UPLOAD_CHUNK == 8 * 1024 * _KIB and UPLOAD_CHUNK % (256 * _KIB) == 0
    assert sizes == [UPLOAD_CHUNK, UPLOAD_CHUNK, len(data) - 2 * UPLOAD_CHUNK]
    assert [c.headers["Content-Range"] for c in chunks] == [
        f"bytes 0-{UPLOAD_CHUNK - 1}/{len(data)}", f"bytes {UPLOAD_CHUNK}-{2 * UPLOAD_CHUNK - 1}/{len(data)}",
        f"bytes {2 * UPLOAD_CHUNK}-{len(data) - 1}/{len(data)}"]
    assert all("Authorization" not in c.headers for c in chunks)
    assert setup.fake.upload_metas == [{"name": "Hamburg talk.mp4", "parents": [setup.a.root.id]}]
    assert setup.a.files[item.item_id].data == data and item.size_bytes == len(data)


async def test_a_large_replace_is_a_resumable_update(setup):
    node = setup.a.add_file(setup.a.root.id, "Hamburg talk.mp4", b"old")
    data = b"y" * (SIMPLE_UPLOAD_MAX + 1)
    item = await setup.adapter.replace_content(setup.handle, node.id, data, "video/mp4")
    start = setup.fake.calls("upload.resumable")
    assert len(start) == 1 and start[0].method == "PATCH" and node.id in start[0].url
    assert item.item_id == node.id and setup.a.files[node.id].data == data


async def test_a_small_replace_is_one_media_patch(setup):
    node = setup.a.add_file(setup.a.root.id, "notes.md", b"old", mime="text/markdown")
    item = await setup.adapter.replace_content(setup.handle, node.id, b"new", "text/markdown")
    calls = setup.fake.calls("upload.media")
    assert len(calls) == 1 and calls[0].method == "PATCH" and calls[0].params["uploadType"] == "media"
    assert item.content_version == setup.a.files[node.id].head != "" and setup.a.files[node.id].data == b"new"


async def test_conflicts_are_by_name_key(setup):
    root = setup.a.root.id
    first = await setup.adapter.upload(setup.handle, root, "Report.pdf", b"1", "application/pdf")
    with pytest.raises(DriveNameConflictError):
        await setup.adapter.upload(setup.handle, root, "report.pdf", b"2", "application/pdf", conflict="fail")
    renamed = await setup.adapter.upload(setup.handle, root, "report.pdf", b"3", "application/pdf")
    assert renamed.name == "report (1).pdf" and renamed.item_id != first.item_id
    replaced = await setup.adapter.upload(setup.handle, root, "REPORT.PDF", b"4", "application/pdf",
                                          conflict="replace")
    assert replaced.item_id == first.item_id and setup.a.files[first.item_id].data == b"4"


async def test_rename_takes_the_first_free_numbered_name(setup):
    root = setup.a.root.id
    setup.a.add_file(root, "brief.docx", b"1")
    setup.a.add_file(root, "brief (1).docx", b"2")
    setup.a.add_file(root, "notes", b"3")
    assert (await setup.adapter.upload(setup.handle, root, "Brief.docx", b"x", "text/plain")).name == "Brief (2).docx"
    assert (await setup.adapter.upload(setup.handle, root, "notes", b"x", "text/plain")).name == "notes (1)"


async def test_replace_overwrites_the_oldest_match(setup):
    root = setup.a.root.id
    oldest = setup.a.add_file(root, "Report.pdf", b"old")
    newer = setup.a.add_file(root, "report.pdf", b"newer")
    item = await setup.adapter.upload(setup.handle, root, "report.pdf", b"fresh", "application/pdf",
                                      conflict="replace")
    assert item.item_id == oldest.id and setup.a.files[oldest.id].data == b"fresh"
    assert setup.a.files[newer.id].data == b"newer"


async def test_child_by_name_escapes_the_query_and_returns_the_oldest(setup):
    odd = setup.a.add_folder(setup.a.root.id, "Clients: 2026")
    odd_id = "fid-o'k\\x"
    setup.a.files[odd_id] = setup.a.files.pop(odd.id)
    setup.a.files[odd_id].id = odd_id
    oldest = setup.a.add_file(odd_id, "Q3 Budget.xlsx", b"1")
    setup.a.add_file(odd_id, "q3 budget.xlsx", b"2")
    found = await setup.adapter.child_by_name(setup.handle, odd_id, "Q3 BUDGET.xlsx")
    assert found is not None and found.item_id == oldest.id
    q = setup.fake.calls("files.list")[-1].params["q"]
    assert q == "'fid-o\\'k\\\\x' in parents and trashed=false"
    assert await setup.adapter.child_by_name(setup.handle, odd_id, "Q4 budget.xlsx") is None


async def test_search_escapes_quotes_and_backslashes(setup):
    setup.a.add_file(setup.a.root.id, "o'clock \\ notes.md", b"n")
    hits = await setup.adapter.search(setup.handle, "o'clock \\", 5)
    assert [h.name for h in hits] == ["o'clock \\ notes.md"]
    q = setup.fake.calls("files.list")[-1].params["q"]
    value = escape_query("o'clock \\")
    assert value == "o\\'clock \\\\"
    assert q == f"(name contains '{value}' or fullText contains '{value}') and trashed=false"


async def test_move_sends_add_and_remove_parents_and_checks_the_target_name(setup):
    root = setup.a.root.id
    folder = setup.a.add_folder(root, "Projects")
    talk = setup.a.add_file(root, "Hamburg talk.mp4", b"talk")
    setup.a.add_file(folder.id, "brief.docx", b"b")
    moved = await setup.adapter.move(setup.handle, talk.id, new_parent_id=folder.id, new_name="talk.mp4")
    update = setup.fake.calls("files.update")[-1]
    assert update.params["addParents"] == folder.id and update.params["removeParents"] == root
    assert update.json == {"name": "talk.mp4"} and "mimeType" not in update.json
    assert moved.path == "Projects/talk.mp4" and setup.a.files[talk.id].parents == [folder.id]
    with pytest.raises(DriveNameConflictError):
        await setup.adapter.move(setup.handle, talk.id, new_name="Brief.docx")
    renamed = await setup.adapter.move(setup.handle, talk.id, new_name="Talk.mp4")    # its own name: no clash
    assert renamed.name == "Talk.mp4" and "addParents" not in setup.fake.calls("files.update")[-1].params


async def test_create_folder_names_the_folder_mime_and_refuses_a_taken_name(setup):
    folder = await setup.adapter.create_folder(setup.handle, setup.a.root.id, "Garden plan 2026/27")
    create = setup.fake.calls("files.create")[-1]
    assert create.json == {"name": "Garden plan 2026/27", "mimeType": FOLDER_MIME, "parents": [setup.a.root.id]}
    assert folder.is_folder and folder.name == "Garden plan 2026/27" and folder.path == "Garden plan 2026∕27"
    with pytest.raises(DriveNameConflictError):
        await setup.adapter.create_folder(setup.handle, setup.a.root.id, "garden plan 2026/27")


async def test_delete_trashes_and_never_deletes(setup):
    node = setup.a.add_file(setup.a.root.id, "brief.docx", b"b")
    await setup.adapter.delete(setup.handle, node.id)
    update = setup.fake.calls("files.update")[-1]
    assert update.method == "PATCH" and update.json == {"trashed": True}
    assert setup.fake.calls("files.delete") == [] and setup.fake.permanent_deletes == []
    assert setup.a.files[node.id].trashed


async def test_a_native_refuses_a_content_write_before_any_upload(setup):
    node = setup.a.add_native(setup.a.root.id, "brief.docx", DOC, exports={"text/markdown": b"b"})
    with pytest.raises(DriveAccessDenied):
        await setup.adapter.replace_content(setup.handle, node.id, b"x", "text/markdown")
    assert not any(r.route.startswith("upload.") for r in setup.fake.requests)
    with pytest.raises(DriveAccessDenied):
        await setup.adapter.upload(setup.handle, setup.a.root.id, "Brief.docx", b"x", "text/plain",
                                   conflict="replace")
    assert not any(r.route.startswith("upload.") for r in setup.fake.requests)
