"""The T2 final fix round (USER_DRIVE_INDEXING_RFC §27.9.5, §27.12; Δ235, Δ238, Δ240): a rename-only PATCH never
renames our file or folder where the owner moved it outside the root (it is moved back first); an intent's own size
decides an adoption after a crash; a gone folder held for the documents beneath it waits until the earliest of them can
move; a record whose ledger document cannot be read backs off and parks. Synthetic items only."""
from __future__ import annotations

from datetime import timedelta

import pytest

from src.domain.drive_index import DriveIndexRecord, Reason, RecordState, Scope
from src.domain.drive_index_params import MIRROR_ARCHIVE_GRACE, MIRROR_FAILED_RETRY, MIRROR_MAX_ATTEMPTS
from src.domain.drive_index_planner import backoff
from src.domain.drive_mirror import KIND_FILE

from .drive_mirror_fakes import AREA, Crash, DRIVE_IDENTITY, MirrorWorld, UK, USER, identity, mirror_key
from .test_drive_transcript_mirror import _bare_root
from .test_drive_transcript_mirror_audit import mirrored, writes_outside

MIN = timedelta(minutes=1)


async def _owner_folder(w: MirrorWorld, name: str = "Docs") -> str:
    """A folder of the owner's own at the App Folder root, as the index records it."""
    item = w.drive.add_folder(AREA, name)
    await w.repo.upsert_records([DriveIndexRecord(USER, item, connection_id=USER, identity=identity(item), parent_id=AREA, name=name, is_folder=True,
                                                  state=RecordState.METADATA_ONLY, reason=Reason.FOLDER,
                                                  scope=Scope.NORMAL)])
    return item


async def _move_out(w: MirrorWorld, item: str, into: str) -> None:
    """The owner moves our file out of `_Transcripts` into his folder; the tick observes it as his file."""
    node = w.drive.nodes[item]
    node.parent_id = into
    await w.repo.upsert_records([DriveIndexRecord(USER, item, connection_id=USER, identity=identity(item), parent_id=into, name=node.name,
                                                  state=RecordState.METADATA_ONLY, scope=Scope.NORMAL)])


# -- I1: a rename in place checks where the item is (Δ235) -----------------------------------------------------------

async def test_rename_of_a_file_moved_out_moves_it_back_first():
    """The owner moves `_Transcripts/a.mp4.md` into his own `Docs/`, then renames `a.mp4`: no rename happens in
    `Docs/`; the file is moved back under the root first, then renamed there."""
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    item = w.doc(a).mirror_item_id
    docs = await _owner_folder(w)
    await _move_out(w, item, docs)
    await w.update(a, name="b.mp4")
    w.reset_calls()
    await w.run()
    assert writes_outside(w, w.root.root_item_id) == []
    moves = [c[1] for c in w.drive.calls if c[0] == "move"]
    assert moves and moves[0]["item_id"] == item and moves[0]["new_parent_id"] == w.root.root_item_id
    assert w.drive.children(docs) == []
    assert w.mirror_path(a) == "b.mp4.md"


async def test_rename_blocked_back_waits_and_writes_nothing_outside():
    """The way back is taken by an owner's file of the same name: the rename waits (nothing written in `Docs/`)."""
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    item = w.doc(a).mirror_item_id
    docs = await _owner_folder(w)
    await _move_out(w, item, docs)
    w.drive.add_file(w.root.root_item_id, "a.mp4.md", b"owner")
    await w.update(a, name="b.mp4")
    w.reset_calls()
    await w.run()
    assert writes_outside(w, w.root.root_item_id) == []
    assert w.drive.nodes[item].parent_id == docs and w.drive.nodes[item].name == "a.mp4.md"


async def test_sibling_rename_back_of_a_file_moved_out_moves_it_back_first():
    """A suffixed file whose collision goes is renamed back by the settle step (a sibling's rename): when the owner had
    moved it into `Docs/`, it is moved back under the root first and never renamed in `Docs/`."""
    w = MirrorWorld()
    (x,) = await mirrored(w, "X")
    folder = await w.folder("X.md")
    await w.recording("in.mp4", folder)
    await w.run()
    assert w.doc(x).mirror_name != "X.md"                   # yielded to the folder: suffixed
    item = w.doc(x).mirror_item_id
    docs = await _owner_folder(w)
    await _move_out(w, item, docs)
    await w.update(folder, name="Y")                         # the collision goes: X is renamed back
    w.reset_calls()
    await w.run()
    assert writes_outside(w, w.root.root_item_id) == []
    assert w.drive.children(docs) == []
    assert w.drive.nodes[item].parent_id == w.root.root_item_id


# -- M1: the intent's size decides an adoption (Δ238) ----------------------------------------------------------------

async def test_crash_after_upload_then_a_byte_change_still_adopts():
    """A crash after the upload, then the UI language changes before the re-run (the render's size changes): the file
    at the intent's name is the bot's own by the intent's size, so it is adopted and rewritten, never judged foreign."""
    w = MirrorWorld()
    await _bare_root(w)
    a = await w.recording("x.mp4")
    w.drive.crash_after("upload", lambda args: args["name"] == "x.mp4.md")
    with pytest.raises(Crash):
        await w.run()
    assert w.doc(a).pending_op and w.doc(a).pending_size
    counters = await w.run(texts=UK, language="uk")
    names = [n.name for n in w.drive.children(w.root.root_item_id) if not n.is_folder]
    assert names.count("x.mp4.md") == 1 and not [n for n in names if "~" in n or "(1)" in n]
    assert counters["mirror_adopted"] == 1 and not counters.get("mirror_foreign")
    assert w.mirror_path(a) == "x.mp4.md" and "відкрити запис" in w.content(a)


# -- Important 1: a gone folder waits for its children's own time (Δ240) ------------------------------------------------

async def test_gone_folder_over_a_held_child_takes_no_reserve_every_tick():
    """The owner keeps his own file in `_Transcripts/Videos/2024/` and deletes the source `Videos/`: `2024` is kept for
    his file (held a week); its parent `Videos` waits for `2024`'s time, not ten minutes, so the step has no ready mark
    on the ticks between."""
    w = MirrorWorld()
    videos = await w.folder("Videos")
    y2024 = await w.folder("2024", videos)
    a = await w.recording("Interview.mp4", y2024)
    await w.run()
    w.drive.add_file(w.folder_doc(y2024).mirror_item_id, "mine.txt", b"owner")
    for item in (a, y2024, videos):
        await w.delete_source(item)
    w.advance(MIRROR_ARCHIVE_GRACE + MIN)
    ready_ticks = 0
    for _ in range(6 * 24):                                  # a day of 10-minute ticks
        if await w.mirror.ready_marks(USER, DRIVE_IDENTITY, w.clock, 10):
            ready_ticks += 1
        await w.run()
        w.advance(10 * MIN)
    assert ready_ticks <= 2
    held_2024 = w.folder_doc(y2024).mark_ready_at
    assert held_2024 is not None and held_2024 > w.clock + timedelta(days=5)
    assert w.folder_doc(videos).mark_ready_at >= held_2024
    assert w.folder_doc(y2024).mirror_item_id in w.drive.nodes            # kept for the owner's file


# -- deploy Minor 3: an unreadable document backs off, then parks -------------------------------------------------------

async def test_record_whose_document_cannot_be_read_backs_off_then_parks():
    w = MirrorWorld()
    await _bare_root(w)
    a = await w.recording("x.mp4")
    key = mirror_key(KIND_FILE, a)
    real = w.mirror.get_docs

    async def get_docs(user_id, keys):  # type: ignore[no-untyped-def]
        if key in keys:
            raise ValueError("the document cannot be decoded")
        return await real(user_id, keys)
    w.mirror.get_docs = get_docs  # type: ignore[method-assign]
    holds = []
    for _ in range(MIRROR_MAX_ATTEMPTS + 1):
        await w.run()
        rec = w.rec(a)
        assert not rec.mirror_due and rec.mirror_retry_at is not None
        holds.append(rec.mirror_retry_at - w.clock)
        w.advance(rec.mirror_retry_at - w.clock)
    assert holds[:MIRROR_MAX_ATTEMPTS - 1] == [backoff(n) for n in range(1, MIRROR_MAX_ATTEMPTS)]
    assert holds[MIRROR_MAX_ATTEMPTS - 1:] == [MIRROR_FAILED_RETRY, MIRROR_FAILED_RETRY]   # parked, and stays parked


# -- item 11: an archived file is the bot's (owner ruling 2026-10-21, Δ241) -------------------------------------------

async def test_edited_archived_file_is_rewritten_from_the_store():
    from src.domain.drive_mirror_render import render_archived
    from src.domain.drive_transcript_render import open_text
    from .test_drive_transcript_mirror_audit import archived, audit, item_of
    w = MirrorWorld()
    a = await archived(w)
    original = w.content(a)
    node = item_of(w, a)
    node.data, node.ctag = b"my own notes", "c-owner"
    w.reset_calls()
    counters = await audit(w)
    assert counters["mirror_archive_rewritten"] == 1
    assert [(c[0], c[1]["item_id"]) for c in w.drive.writes()] == [("replace_content", node.item_id)]
    doc = w.doc(a)
    stored_entry = await w.repo.get_transcript(USER, doc.content_key)
    assert node.data == render_archived(stored_entry, doc, w.last_host.texts)
    body = open_text(stored_entry, max_chars=10 ** 9)
    assert body in w.content(a) and body in original           # the body open and search serve
    assert "# a.mp4" in w.content(a) and "my own notes" not in w.content(a)
    assert doc.mirror_ctag == node.ctag and await w.mirror.pinned(USER, doc.content_key)
    w.reset_calls()
    await audit(w)                                             # its own cTag is the reference now
    assert w.drive.writes() == []


async def test_renamed_archived_file_is_moved_back():
    from .test_drive_transcript_mirror_audit import archived, audit, item_of
    w = MirrorWorld()
    a = await archived(w)
    place = (w.doc(a).mirror_parent_id, w.doc(a).mirror_name)
    node = item_of(w, a)
    node.name = "kept.md"
    w.reset_calls()
    counters = await audit(w)
    assert counters["mirror_moved_back"] == 1
    assert (node.parent_id, node.name) == place and w.doc(a).state == "archived"
    assert [c[0] for c in w.drive.writes()] == ["move"]


async def test_archived_file_moved_inside_the_mirror_is_moved_back_and_its_edit_rewritten():
    from .test_drive_transcript_mirror_audit import archived, audit, item_of
    w = MirrorWorld()
    a = await archived(w)
    place = (w.doc(a).mirror_parent_id, w.doc(a).mirror_name)
    node = item_of(w, a)
    node.parent_id = w.root.root_item_id                     # out of the archive, still in the mirror
    node.data, node.ctag = b"scribbled", "c-owner"
    w.reset_calls()
    await audit(w)
    assert (node.parent_id, node.name) == place
    assert [c[0] for c in w.drive.writes()] == ["replace_content", "move"]
    assert b"scribbled" not in node.data
    assert writes_outside(w, w.root.root_item_id) == []


async def test_edited_archived_stub_without_a_store_entry_is_rewritten_from_its_snapshot():
    from .test_drive_transcript_mirror_audit import audit, item_of
    w = MirrorWorld()
    a = await w.recording("quiet.mp4", stub="no_speech", store=False)
    await w.run()
    await w.delete_source(a)
    w.advance(2 * timedelta(hours=1))
    await w.run()
    assert w.doc(a).state == "archived" and w.doc(a).transcript_stub == "no_speech"
    node = item_of(w, a)
    node.data, node.ctag = b"mine", "c-owner"
    counters = await audit(w)
    assert counters["mirror_archive_rewritten"] == 1 and "No speech was detected" in w.content(a)


# -- round 2 ------------------------------------------------------------------------------------------------------------

async def _detached(w: MirrorWorld, name: str = "a.mp4") -> str:
    from src.domain.drive_mirror import MIRROR_DETACHED, UNSTORED
    (a,) = await mirrored(w, name)
    await w.delete_source(a, why=UNSTORED)
    w.advance(2 * timedelta(hours=1))
    await w.run()
    assert w.doc(a).state == MIRROR_DETACHED
    return a


async def test_edited_detached_file_is_rewritten_from_its_pinned_copy():
    """Δ243: a detached file keeps its pinned store entry (Δ190), so an edit is rewritten like an archived one's."""
    from src.domain.drive_mirror_render import render_archived
    from .test_drive_transcript_mirror_audit import audit, item_of
    w = MirrorWorld()
    a = await _detached(w)
    node = item_of(w, a)
    node.data, node.ctag = b"my notes", "c-owner"
    w.reset_calls()
    counters = await audit(w)
    assert counters["mirror_archive_rewritten"] == 1
    assert [(c[0], c[1]["item_id"]) for c in w.drive.writes()] == [("replace_content", node.item_id)]
    doc = w.doc(a)
    assert node.data == render_archived(await w.repo.get_transcript(USER, doc.content_key), doc, w.last_host.texts)
    assert doc.state == "detached" and await w.mirror.pinned(USER, doc.content_key)
    w.reset_calls()
    await audit(w)
    assert w.drive.writes() == []


async def test_renamed_detached_file_is_moved_back():
    from .test_drive_transcript_mirror_audit import audit, item_of
    w = MirrorWorld()
    a = await _detached(w)
    place = (w.doc(a).mirror_parent_id, w.doc(a).mirror_name)
    node = item_of(w, a)
    node.name = "kept.md"
    w.reset_calls()
    counters = await audit(w)
    assert counters["mirror_moved_back"] == 1 and (node.parent_id, node.name) == place
    assert [c[0] for c in w.drive.writes()] == ["move"]


async def test_detached_file_moved_out_keeps_its_pin_and_is_not_written():
    """Deletion and move-out handling stay as they were: a detached document loses its item, keeps its pin."""
    from .test_drive_transcript_mirror_audit import audit, item_of
    w = MirrorWorld()
    a = await _detached(w)
    node = item_of(w, a)
    node.parent_id = AREA
    w.reset_calls()
    await audit(w)
    assert w.drive.writes() == [] and node.parent_id == AREA
    doc = w.doc(a)
    assert doc is not None and not doc.mirror_item_id and await w.mirror.pinned(USER, doc.content_key)


async def test_moved_and_edited_active_file_is_moved_back_then_rewritten():
    """Δ242: the move-back keeps the recorded cTag, clears the rendered hash and makes the record due, so step 4
    rewrites the owner's edit."""
    from .test_drive_transcript_mirror_audit import audit, item_of
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    original, ctag = w.content(a), w.doc(a).mirror_ctag
    node = item_of(w, a)
    node.name, node.data, node.ctag = "renamed.md", b"edited", "c-owner"
    w.reset_calls()
    counters = await audit(w)
    assert counters["mirror_moved_back"] == 1 and node.name == "a.mp4.md"
    assert w.doc(a).mirror_ctag == ctag and w.doc(a).rendered_hash == "" and w.rec(a).mirror_due
    await w.run()
    assert w.content(a) == original


async def test_moved_and_edited_active_file_with_a_blocked_way_back_is_rewritten():
    from .test_drive_transcript_mirror_audit import audit, item_of
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    original = w.content(a)
    node = item_of(w, a)
    node.name, node.data, node.ctag = "renamed.md", b"edited", "c-owner"
    owner = w.drive.add_file(w.root.root_item_id, "a.mp4.md", b"mine")
    counters = await audit(w)
    assert counters["mirror_move_back_blocked"] == 1
    assert w.doc(a).rendered_hash == "" and w.rec(a).mirror_due
    await w.run()
    assert b"edited" not in node.data and w.content(a) == original and w.drive.nodes[owner].data == b"mine"
    assert w.mirror_path(a).startswith("a.mp4~")


async def test_a_sibling_rename_keeps_the_recorded_ctag():
    """Δ242: `_patch` keeps the recorded cTag, so an edit made before a sibling rename is still found by the audit."""
    from .test_drive_transcript_mirror_audit import audit, item_of
    w = MirrorWorld()
    (x,) = await mirrored(w, "X")
    folder = await w.folder("X.md")
    await w.recording("in.mp4", folder)
    await w.run()
    ctag = w.doc(x).mirror_ctag
    node = item_of(w, x)
    node.data, node.ctag = b"edited", "c-owner"
    await w.update(folder, name="Y")                         # the collision goes: X is renamed back by the settle
    await w.run()
    assert node.name == "X.md" and w.doc(x).mirror_ctag == ctag
    counters = await audit(w)
    assert counters["mirror_rewrite_due"] == 1
    await w.run()
    assert b"edited" not in node.data


async def test_archived_edit_with_nothing_to_rewrite_from_records_the_listed_ctag():
    from .test_drive_transcript_mirror_audit import archived, audit, item_of
    w = MirrorWorld()
    a = await archived(w)
    w.repo.transcripts.pop((USER, w.doc(a).content_key))     # no store entry, and no stub
    node = item_of(w, a)
    node.data, node.ctag = b"mine", "c-owner"
    counters = await audit(w)
    assert counters["mirror_edit_left"] == 1 and w.doc(a).mirror_ctag == "c-owner"
    w.reset_calls()
    counters = await audit(w)
    assert not counters.get("mirror_edit_left") and w.drive.writes() == []
