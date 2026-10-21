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
from src.domain.drive_mirror import KIND_FILE, doc_key

from .drive_mirror_fakes import AREA, DRIVE, UK, USER, Crash, MirrorWorld
from .test_drive_transcript_mirror import _bare_root
from .test_drive_transcript_mirror_audit import mirrored, writes_outside

MIN = timedelta(minutes=1)


async def _owner_folder(w: MirrorWorld, name: str = "Docs") -> str:
    """A folder of the owner's own at the App Folder root, as the index records it."""
    item = w.drive.add_folder(AREA, name)
    await w.repo.upsert_records([DriveIndexRecord(USER, item, parent_id=AREA, name=name, is_folder=True,
                                                  state=RecordState.METADATA_ONLY, reason=Reason.FOLDER,
                                                  scope=Scope.NORMAL)])
    return item


async def _move_out(w: MirrorWorld, item: str, into: str) -> None:
    """The owner moves our file out of `_Transcripts` into his folder; the tick observes it as his file."""
    node = w.drive.nodes[item]
    node.parent_id = into
    await w.repo.upsert_records([DriveIndexRecord(USER, item, parent_id=into, name=node.name,
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
        if await w.mirror.ready_marks(USER, DRIVE, w.clock, 10):
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
    key = doc_key(USER, KIND_FILE, a)
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
