"""DriveTranscriptMirror: root setup, seed, marks, due work, restore, detach, prune (USER_DRIVE_INDEXING_RFC §27.9.5-
§27.9.7; plan Task 21A). Synthetic items only, on the in-memory repositories and a scripted drive."""
from __future__ import annotations

import logging
import random
from dataclasses import replace
from datetime import timedelta

import pytest

from src.domain import drive_mirror_plan
from src.domain.drive_mirror import (
    ARCHIVE_NAME, KIND_ARCHIVE, KIND_FOLDER, LONG_PATHS_NAME, MIRROR_ACTIVE, MIRROR_ARCHIVED, MIRROR_DETACHED,
    GONE, README_NAME, ROOT_NAME, UNKNOWN, UNSTORED, DeletionNotice, MirrorRoot,
)
from src.domain.drive_mirror_names import root_name_candidates
from src.domain.drive_mirror_plan import local_day, mirror_digest
from src.domain.drive_mirror_render import RENDER_VERSION
from src.services.drive_transcript_mirror import seed_version

from .drive_mirror_fakes import AREA, Crash, DRIVE_IDENTITY, MirrorWorld, START, UK, USER, identity, mirror_key, stored

HOUR = timedelta(hours=1)


async def mirrored(world: MirrorWorld, *names: str, parent: str = AREA):
    """Recordings created and mirrored by one run; their ids."""
    ids = [await world.recording(n, parent) for n in names]
    await world.run()
    return ids


# -- step 1: the root -----------------------------------------------------------------------------------------------

async def test_root_created_and_recorded_before_any_other_write():
    w = MirrorWorld()
    await w.recording("a.mp4")
    await w.run()
    ops = [e for e in w.events if e[0] in ("drive.create_folder", "drive.upload", "save_root", "save_doc")]
    assert ops[0] == ("drive.create_folder", ROOT_NAME)
    assert ops[1][0] == "save_root"
    assert w.root.root_name == ROOT_NAME and w.root.created_at == START
    assert w.drive.nodes[w.root.root_item_id].parent_id == AREA
    assert w.drive.path_of(w.root.root_item_id) == ROOT_NAME


async def test_taken_name_gets_alek_never_written_into():
    w = MirrorWorld()
    foreign = w.drive.add_folder(AREA, ROOT_NAME)
    await w.recording("a.mp4")
    await w.run()
    assert w.root.root_name == f"{ROOT_NAME} (Alek)"
    assert w.drive.children(foreign) == []
    assert not [c for c in w.drive.writes() if foreign in (c[1].get("parent_id"), c[1].get("new_parent_id"))]


async def test_alek_suffix_numbered_then_error(caplog):
    w = MirrorWorld()
    for name in root_name_candidates():
        w.drive.add_folder(AREA, name)
    await w.recording("a.mp4")
    with caplog.at_level(logging.WARNING):
        counters = await w.run()
    assert w.root is None
    assert counters["mirror_root_names_taken"] == 1
    assert w.drive.writes() == []                         # fix round 1 (M7): every name is read first, none written
    assert [c[1]["name"] for c in w.drive.calls if c[0] == "child_by_name"] == list(root_name_candidates())
    assert [r for r in caplog.records if r.levelno == logging.WARNING and "every root name" in r.getMessage()]


async def test_readme_written_once_in_the_ui_language():
    w = MirrorWorld()
    await w.recording("a.mp4")
    await w.run(texts=UK, language="uk")
    readme = w.drive.child(w.root.root_item_id, README_NAME)
    assert readme is not None and "Archive/" in readme.data.decode("utf-8")
    assert "Розшифровки" in readme.data.decode("utf-8")
    assert w.root.readme_item_id == readme.item_id and w.root.readme_ctag == readme.ctag
    w.reset_calls()
    await w.run()                                         # another language later: never rewritten
    assert not [c for c in w.drive.writes() if c[1].get("name") == README_NAME]
    assert "Розшифровки" in w.drive.nodes[readme.item_id].data.decode("utf-8")


async def test_readme_crash_rerun_adopts_its_own_file():
    w = MirrorWorld()
    await w.recording("a.mp4")
    w.drive.crash_after("upload", lambda a: a["name"] == README_NAME)
    with pytest.raises(Crash):
        await w.run()
    assert w.root is not None and not w.root.readme_item_id
    await w.run()
    readmes = [n for n in w.drive.children(w.root.root_item_id) if n.name.startswith("_README")]
    assert len(readmes) == 1 and w.root.readme_item_id == readmes[0].item_id


# -- step 2: the seed -----------------------------------------------------------------------------------------------

async def _bare_root(w: MirrorWorld, **kw) -> MirrorRoot:
    root_id = w.drive.add_folder(AREA, ROOT_NAME)
    readme = w.drive.add_file(root_id, README_NAME, b"readme")
    root = MirrorRoot(USER, DRIVE_IDENTITY, root_id, ROOT_NAME, readme_item_id=readme, created_at=START, **kw)
    await w.mirror.save_root(root)
    return root


async def test_seed_marks_existing_current_due():
    w = MirrorWorld()
    await _bare_root(w)
    a = await w.recording("a.mp4")
    await w.repo.upsert_records([replace(w.rec(a), mirror_due=False)])     # written before T2: never planned
    assert not w.rec(a).mirror_due
    counters = await w.run(ops=0)
    assert counters["mirror_seeded"] == 1 and w.rec(a).mirror_due
    assert w.root.seed_version == seed_version() and w.root.seed_cursor == ""
    b = await w.recording("b.mp4")
    await w.repo.upsert_records([replace(w.rec(b), mirror_due=False)])
    await w.mirror.save_root(replace(w.root, seed_version=""))
    await w.run()                                         # seeded records are worked in the same run
    assert w.mirror_path(a) == "a.mp4.md" and w.mirror_path(b) == "b.mp4.md"


async def test_seed_page_bounded(monkeypatch):
    from src.services import drive_transcript_mirror as mod
    monkeypatch.setattr(mod, "MIRROR_SEED_PAGE", 3)
    w = MirrorWorld()
    await _bare_root(w)
    ids = [await w.recording(f"r{i}.mp4") for i in range(5)]
    await w.repo.upsert_records([replace(w.rec(i), mirror_due=False) for i in ids])
    await w.run(ops=0)
    assert sum(w.rec(i).mirror_due for i in ids) == 3
    assert w.root.seed_version != seed_version() and w.root.seed_cursor
    await w.run(ops=0)
    assert all(w.rec(i).mirror_due for i in ids) and w.root.seed_version == seed_version()


async def test_seed_reruns_after_a_render_version_bump(monkeypatch):
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    assert not w.rec(a).mirror_due and w.root.seed_version == seed_version()
    monkeypatch.setattr(drive_mirror_plan, "RENDER_VERSION", RENDER_VERSION + 1)
    assert w.root.seed_version != seed_version()
    await w.run(ops=0, stamps=0)
    assert w.rec(a).mirror_due
    assert w.root.seed_version == seed_version()
    w.reset_calls()
    await w.run()                                         # the same bytes: stamped, no write
    assert not w.rec(a).mirror_due and w.drive.writes() == []


# -- step 4: creates, chains, names ---------------------------------------------------------------------------------

async def test_create_ensures_the_chain_then_uploads_with_fail():
    w = MirrorWorld()
    videos = await w.folder("Videos")
    y2024 = await w.folder("2024", videos)
    a = await w.recording("Interview.mp4", y2024)
    await w.run()
    assert w.mirror_path(a) == "Videos/2024/Interview.mp4.md"
    kinds = [(c[0], c[1].get("name")) for c in w.drive.writes()]
    assert kinds.index(("create_folder", "Videos")) < kinds.index(("create_folder", "2024")) \
        < kinds.index(("upload", "Interview.mp4.md"))
    assert all(c[1]["conflict"] == "fail" for c in w.drive.writes() if c[0] == "upload")
    assert not w.rec(a).mirror_due and w.rec(a).mirrored_digest == mirror_digest(w.rec(a))
    text = w.content(a)
    assert text.startswith("# Interview.mp4\nVideos/2024 · [open the recording]")
    assert "hello there" in text
    doc = w.doc(a)
    assert doc.source_path == "Videos/2024" and doc.speakers == 2 and doc.render_language == "en"


async def test_folder_record_stamped_when_its_mirror_folder_is_created():
    w = MirrorWorld()
    videos = await w.folder("Videos")
    assert w.rec(videos).mirrored_digest == ""
    await w.recording("a.mp4", videos)
    await w.run()
    assert w.rec(videos).mirrored_digest == mirror_digest(w.rec(videos))
    assert w.folder_doc(videos).mirror_name == "Videos"
    await w.update(videos, name="Clips")
    assert w.rec(videos).mirror_due                        # its later rename is due


async def test_source_folder_rename_one_move():
    w = MirrorWorld()
    videos = await w.folder("Videos")
    ids = [await w.recording(f"r{i}.mp4", videos) for i in range(3)]
    await w.run()
    w.reset_calls()
    await w.update(videos, name="Clips")
    await w.run()
    assert [c[0] for c in w.drive.writes()] == ["move"]
    assert all(w.mirror_path(i).startswith("Clips/") for i in ids)
    assert not w.rec(videos).mirror_due


async def test_misplaced_folder_moved_before_a_create_under_it():
    w = MirrorWorld()
    videos = await w.folder("Videos")
    await w.recording("old.mp4", videos)
    await w.run()
    w.reset_calls()
    await w.update(videos, name="Clips")
    new = await w.recording("new.mp4", videos, day="2026-10-20")      # newer: the queue reaches it first
    await w.run()
    writes = [(c[0], c[1].get("name")) for c in w.drive.writes()]
    assert writes.index(("move", "Clips")) < writes.index(("upload", "new.mp4.md"))
    assert w.mirror_path(new) == "Clips/new.mp4.md"


async def test_file_yields_to_a_new_folder_of_its_name():
    w = MirrorWorld()
    (x,) = await mirrored(w, "X")
    assert w.mirror_path(x) == "X.md"
    w.reset_calls()
    folder = await w.folder("X.md")
    inner = await w.recording("in.mp4", folder)
    await w.run()
    writes = [(c[0], c[1].get("name")) for c in w.drive.writes()]
    yielded = w.doc(x).mirror_name
    assert yielded.startswith("X~") and yielded.endswith(".md")
    assert writes.index(("move", yielded)) < writes.index(("create_folder", "X.md"))
    assert w.mirror_path(inner) == "X.md/in.mp4.md"


async def test_suffix_renamed_back_when_the_collision_goes():
    w = MirrorWorld()
    (x,) = await mirrored(w, "X")
    folder = await w.folder("X.md")
    await w.recording("in.mp4", folder)
    await w.run()
    assert w.doc(x).mirror_name != "X.md"
    await w.update(folder, name="Y")
    await w.run()
    assert w.mirror_path(x) == "X.md"
    assert w.folder_doc(folder).mirror_name == "Y"


async def test_swap_through_a_temporary_name():
    w = MirrorWorld()
    a, b = await mirrored(w, "A.mp4", "B.mp4")
    w.reset_calls()
    await w.update(a, name="B.mp4")
    await w.update(b, name="A.mp4")
    await w.run()
    moves = [c[1]["name"] for c in w.drive.writes() if c[0] == "move"]
    assert any("~tmp" in (m or "") for m in moves) and len(moves) == 3
    assert w.mirror_path(a) == "B.mp4.md" and w.mirror_path(b) == "A.mp4.md"
    assert "# B.mp4" in w.content(a) and "# A.mp4" in w.content(b)


async def test_copy_gets_a_second_file_no_new_transcription():
    w = MirrorWorld()
    a = await w.recording("a.mp4", key="shared")
    b = await w.recording("copy of a.mp4", key="shared")
    await w.run()
    assert w.mirror_path(a) == "a.mp4.md" and w.mirror_path(b) == "copy of a.mp4.md"
    assert w.content(a).split("---", 1)[1] == w.content(b).split("---", 1)[1]
    assert len(w.repo.transcripts) == 1


async def test_foreign_occupant_never_overwritten():
    w = MirrorWorld()
    await _bare_root(w)
    foreign = w.drive.add_file(w.root.root_item_id, "a.mp4.md", b"the owner's own notes")
    a = await w.recording("a.mp4")
    await w.run()
    assert w.drive.nodes[foreign].data == b"the owner's own notes"
    assert w.mirror_path(a).startswith("a.mp4~") and w.mirror_path(a).endswith(".md")
    b = await w.recording("b.mp4")
    await w.run()
    other = w.drive.add_file(w.root.root_item_id, "c.mp4.md", b"mine too")
    await w.update(b, name="c.mp4")                       # a move onto a name a foreign item holds
    await w.run()
    assert w.drive.nodes[other].data == b"mine too" and w.mirror_path(b).startswith("c.mp4~")
    assert not [c for c in w.drive.writes() if c[0] == "upload" and c[1]["conflict"] != "fail"]


async def test_replace_only_when_the_hash_differs():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    w.reset_calls()
    await w.update(a, transcript_body_version=5)          # due, the bytes are the same
    assert w.rec(a).mirror_due
    await w.run()
    assert w.drive.writes() == [] and not w.rec(a).mirror_due
    await w.update(a, web_url="https://example.invalid/moved")
    await w.run()
    assert [c[0] for c in w.drive.writes()] == ["replace_content"]
    assert "https://example.invalid/moved" in w.content(a)


async def test_requeued_transcript_rewrites_the_file():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    key = w.rec(a).transcript_key
    await w.repo.put_transcript(USER, stored(key, "a better transcript"))
    await w.update(a, transcript_version=2)
    w.reset_calls()
    await w.run()
    assert [c[0] for c in w.drive.writes()] == ["replace_content"]
    assert "a better transcript" in w.content(a)


async def test_stamps_bounded_per_tick():
    w = MirrorWorld()
    ids = await mirrored(w, "a.mp4", "b.mp4", "c.mp4")
    for i in ids:
        await w.update(i, transcript_body_version=9)      # due, nothing to write
    await w.run(stamps=2)
    assert sum(not w.rec(i).mirror_due for i in ids) == 2
    await w.run()
    assert all(not w.rec(i).mirror_due for i in ids)


async def test_ops_reads_and_stamps_budgets_bound_a_tick():
    w = MirrorWorld()
    ids = [await w.recording(f"r{i}.mp4") for i in range(6)]
    await w.run(ops=4)
    assert len(w.drive.writes()) == 4
    w.reset_calls()
    await w.run(ops=40, reads=0)                          # the path needs the App Folder's path: a read
    assert w.drive.writes() == []
    await w.run()
    assert all(w.mirror_path(i) for i in ids)


async def test_newest_first():
    w = MirrorWorld()
    old = await w.recording("old.mp4", day="2020-01-01")
    new = await w.recording("new.mp4", day="2026-10-01")
    mid = await w.recording("mid.mp4", day="2024-05-05")
    await w.run(ops=4)                                     # root, read-me, then two files
    assert w.mirror_path(new) and w.mirror_path(mid) and not w.mirror_path(old)


async def test_no_graph_call_when_nothing_is_due():
    w = MirrorWorld()
    await mirrored(w, "a.mp4")
    w.reset_calls()
    await w.run()
    assert w.drive.calls == []


async def test_inaccessible_source_keeps_its_file():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    w.reset_calls()
    await w.update(a, inaccessible_since=START)
    assert not w.rec(a).mirror_due
    await w.run()
    assert w.drive.calls == [] and w.mirror_path(a) == "a.mp4.md"


# -- crash re-runs and intents (Δ210) -------------------------------------------------------------------------------

async def test_crash_after_upload_before_the_doc_adopts_its_own_file():
    w = MirrorWorld()
    await _bare_root(w)
    a = await w.recording("x.mp4")
    w.drive.crash_after("upload", lambda args: args["name"] == "x.mp4.md")
    with pytest.raises(Crash):
        await w.run()
    assert w.doc(a).pending_op and not w.doc(a).mirror_item_id
    await w.run()
    names = [n.name for n in w.drive.children(w.root.root_item_id) if not n.is_folder]
    assert names.count("x.mp4.md") == 1 and not [n for n in names if "(1)" in n or "~" in n]
    assert w.mirror_path(a) == "x.mp4.md" and not w.rec(a).mirror_due
    assert [c[0] for c in w.drive.writes()][-1] == "replace_content"


async def test_crash_after_a_409_never_overwrites_the_foreign_file():
    w = MirrorWorld()
    await _bare_root(w)
    w.advance(-timedelta(days=1))
    foreign = w.drive.add_file(w.root.root_item_id, "x.mp4.md", b"owner")
    w.advance(timedelta(days=1))
    a = await w.recording("x.mp4")
    w.drive.crash_before("upload", lambda args: args["name"] != "x.mp4.md")      # after the 409, before the retry
    with pytest.raises(Crash):
        await w.run()
    await w.run()
    assert w.drive.nodes[foreign].data == b"owner"
    assert w.mirror_path(a).startswith("x.mp4~")
    assert not [c for c in w.drive.writes() if c[0] == "replace_content" and c[1]["item_id"] == foreign]


async def test_crash_before_the_put_creates_with_fail():
    w = MirrorWorld()
    await _bare_root(w)
    a = await w.recording("x.mp4")
    w.drive.crash_before("upload")
    with pytest.raises(Crash):
        await w.run()
    w.reset_calls()
    await w.run()
    assert [c[0] for c in w.drive.calls[:1]] == ["child_by_name"]
    uploads = [c for c in w.drive.writes() if c[0] == "upload"]
    assert len(uploads) == 1 and uploads[0][1]["conflict"] == "fail"
    assert w.mirror_path(a) == "x.mp4.md"


async def test_crash_after_folder_create_adopts_it_by_intent():
    w = MirrorWorld()
    await _bare_root(w)
    videos = await w.folder("Videos")
    a = await w.recording("a.mp4", videos)
    w.drive.crash_after("create_folder", lambda args: args["name"] == "Videos")
    with pytest.raises(Crash):
        await w.run()
    await w.run()
    folders = [n for n in w.drive.children(w.root.root_item_id) if n.is_folder]
    assert [n.name for n in folders] == ["Videos"]
    assert w.mirror_path(a) == "Videos/a.mp4.md"


@pytest.mark.parametrize("behind, adopted", [(timedelta(minutes=3), True), (timedelta(minutes=7), False)])
async def test_created_time_within_the_skew_still_adopted(behind, adopted):
    w = MirrorWorld()
    await _bare_root(w)
    a = await w.recording("x.mp4")
    w.drive.clock = w.clock - behind                      # Graph's clock behind the indexer's
    w.drive.crash_after("upload")
    with pytest.raises(Crash):
        await w.run()
    await w.run()
    names = sorted(n.name for n in w.drive.children(w.root.root_item_id) if not n.is_folder and n.name != README_NAME)
    if adopted:
        assert names == ["x.mp4.md"]
    else:                                                 # read as foreign: never written into, ours suffixed
        assert len(names) == 2 and w.mirror_path(a).startswith("x.mp4~")


# -- step 3: marks, the archive, restore, detach --------------------------------------------------------------------

async def test_archive_after_grace_into_the_dated_folder():
    w = MirrorWorld()
    videos = await w.folder("Videos")
    y2024 = await w.folder("2024", videos)
    a = await w.recording("Interview.mp4", y2024)
    await w.run()
    w.advance(timedelta(hours=11))                        # 21:00 UTC: already the next day in Kiritimati (UTC+14)
    gone_at = w.clock
    await w.delete_source(a)
    w.advance(timedelta(minutes=30))
    await w.run(tz_name="Pacific/Kiritimati")
    assert w.doc(a).state == MIRROR_ACTIVE                # inside the grace
    w.advance(timedelta(minutes=31))
    await w.run(tz_name="Pacific/Kiritimati")
    day = local_day(gone_at, "Pacific/Kiritimati")
    assert day == "2026-10-22"
    assert w.mirror_path(a) == f"{ARCHIVE_NAME}/{day}/Videos/2024/Interview.mp4.md"
    doc = w.doc(a)
    assert doc.state == MIRROR_ARCHIVED and doc.archive_day == day and not doc.gone_pending
    assert doc.archive_keys and doc.name_keys
    assert w.mirror.docs[next(k for k, d in w.mirror.docs.items() if d.kind == KIND_ARCHIVE
                              and d.source_name == ARCHIVE_NAME)].mirror_parent_id == w.root.root_item_id


async def test_delete_and_restore_in_one_round_no_drive_write():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    rec = w.rec(a)
    await w.delete_source(a, on_drive=False)
    await w.restore_source(rec)
    w.reset_calls()
    w.advance(2 * HOUR)
    await w.run()
    assert w.drive.writes() == []
    assert w.doc(a).state == MIRROR_ACTIVE and not w.doc(a).gone_pending
    assert w.mirror_path(a) == "a.mp4.md" and not w.rec(a).mirror_due


async def _archived(w: MirrorWorld, name: str = "a.mp4", parent: str = AREA):
    a = await w.recording(name, parent)
    await w.run()
    rec = w.rec(a)
    await w.delete_source(a)
    w.advance(2 * HOUR)
    await w.run()
    assert w.doc(a).state == MIRROR_ARCHIVED
    return a, rec


async def test_restore_moves_the_file_back():
    w = MirrorWorld()
    a, rec = await _archived(w)
    archived_item = w.doc(a).mirror_item_id
    w.reset_calls()
    await w.restore_source(rec)
    await w.run()
    assert w.mirror_path(a) == "a.mp4.md" and w.doc(a).mirror_item_id == archived_item
    assert w.doc(a).state == MIRROR_ACTIVE and not w.doc(a).archive_keys
    assert [c[0] for c in w.drive.writes()] == ["move"]


async def test_restore_after_owner_deleted_the_archived_file_creates_from_store():
    w = MirrorWorld()
    a, rec = await _archived(w)
    w.drive.remove(w.doc(a).mirror_item_id)               # the owner deleted the archived file
    await w.restore_source(rec)
    await w.run()
    assert w.mirror_path(a) == "a.mp4.md" and "hello there" in w.content(a)
    assert w.doc(a).state == MIRROR_ACTIVE


async def test_unstored_detaches_in_place():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    await w.delete_source(a, why=UNSTORED)
    w.reset_calls()
    w.advance(2 * HOUR)
    await w.run()
    assert w.drive.writes() == [] and w.doc(a).state == MIRROR_DETACHED and w.mirror_path(a) == "a.mp4.md"
    assert await w.mirror.pinned(USER, w.doc(a).content_key)


async def test_detached_reattaches_when_the_record_returns():
    w = MirrorWorld()
    folder = await w.folder("Box")
    a = await w.recording("a.mp4", folder)
    await w.run()
    rec = w.rec(a)
    await w.delete_source(a, why=UNSTORED)
    w.advance(2 * HOUR)
    await w.run()
    assert w.doc(a).state == MIRROR_DETACHED
    await w.restore_source(replace(rec, parent_id=AREA))  # back, at the top this time
    w.drive.nodes[a].parent_id = AREA
    await w.run()
    assert w.doc(a).state == MIRROR_ACTIVE and w.mirror_path(a) == "a.mp4.md"


async def test_unknown_mark_checked_not_found_archives():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    await w.delete_source(a, why=UNKNOWN)
    w.drive.remove(a)
    w.advance(2 * HOUR)
    w.reset_calls()
    await w.run()
    assert [c for c in w.drive.calls if c[0] == "get_change"] == [("get_change", {"item_id": a})]
    assert w.doc(a).state == MIRROR_ARCHIVED


async def test_unknown_mark_found_without_record_detaches():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    await w.delete_source(a, why=UNKNOWN)
    w.advance(2 * HOUR)
    await w.run()
    assert w.doc(a).state == MIRROR_DETACHED


async def test_archived_frozen_on_a_render_bump(monkeypatch):
    w = MirrorWorld()
    a, _ = await _archived(w)
    before = w.content(a)
    other = await w.recording("b.mp4")
    await w.run()
    monkeypatch.setattr(drive_mirror_plan, "RENDER_VERSION", RENDER_VERSION + 1)
    w.reset_calls()
    await w.run()
    await w.run()
    assert w.content(a) == before
    assert all(c[1].get("item_id") != w.doc(a).mirror_item_id for c in w.drive.writes())
    assert w.drive.writes() == []                         # the same bytes: the active file is only stamped
    assert w.doc(other).render_version == RENDER_VERSION + 1 and not w.rec(other).mirror_due
    assert w.doc(a).render_version == RENDER_VERSION and w.doc(a).state == MIRROR_ARCHIVED


async def test_stub_file_archived_too():
    w = MirrorWorld()
    a = await w.recording("silence.mp4", stub="no_speech")
    await w.run()
    assert "No speech was detected" in w.content(a)
    await w.delete_source(a)
    w.advance(2 * HOUR)
    await w.run()
    doc = w.doc(a)
    assert doc.state == MIRROR_ARCHIVED and doc.archive_keys == () and doc.name_keys


async def test_archive_without_store_has_name_keys_only():
    w = MirrorWorld()
    (a,) = await mirrored(w, "talk.mp4")
    await w.repo.delete_transcript(USER, w.doc(a).content_key)
    await w.delete_source(a)
    w.advance(2 * HOUR)
    await w.run()
    assert w.doc(a).state == MIRROR_ARCHIVED and w.doc(a).archive_keys == () and w.doc(a).name_keys


# -- step 5: prune --------------------------------------------------------------------------------------------------

async def _gone_folder(w: MirrorWorld, foreign: bool = False):
    folder = await w.folder("Old")
    a = await w.recording("a.mp4", folder)
    await w.run()
    mirror_folder = w.folder_doc(folder).mirror_item_id
    if foreign:
        w.drive.add_file(mirror_folder, "owner.txt")
    await w.delete_source(a)
    await w.delete_source(folder)
    w.advance(2 * HOUR)
    await w.run()
    return folder, a, mirror_folder


async def test_prune_empty_folder_after_a_listing():
    w = MirrorWorld()
    folder, a, mirror_folder = await _gone_folder(w)
    assert w.doc(a).state == MIRROR_ARCHIVED
    assert mirror_folder not in w.drive.nodes and w.folder_doc(folder) is None
    assert ("list_children_changes", {"item_id": mirror_folder, "limit": 1}) in w.drive.calls


async def test_prune_keeps_a_folder_with_a_foreign_item():
    w = MirrorWorld()
    folder, a, mirror_folder = await _gone_folder(w, foreign=True)
    assert mirror_folder in w.drive.nodes and w.folder_doc(folder) is not None
    assert [n.name for n in w.drive.children(mirror_folder)] == ["owner.txt"]


async def test_archive_folders_never_pruned():
    w = MirrorWorld()
    a, rec = await _archived(w)
    await w.restore_source(rec)
    await w.run()
    w.advance(2 * HOUR)
    await w.run()
    archive = w.drive.child(w.root.root_item_id, ARCHIVE_NAME)
    assert archive is not None and w.drive.children(archive.item_id)          # the empty day folder stays
    assert not [c for c in w.drive.writes() if c[0] == "delete"]


# -- 404s ---------------------------------------------------------------------------------------------------------

async def test_404_on_a_create_verifies_the_root():
    w = MirrorWorld()
    await _bare_root(w)
    w.drive.remove(w.root.root_item_id)                   # the owner deleted the root meanwhile
    await w.recording("a.mp4")
    counters = await w.run()
    assert counters["mirror_root_missing"] == 1 and w.root.root_gone_at is not None
    w.reset_calls()
    await w.run()                                         # the step stops while the mark is set
    assert w.drive.calls == []


async def test_404_on_a_create_under_a_gone_folder_resets_that_folder():
    w = MirrorWorld()
    videos = await w.folder("Videos")
    await w.recording("a.mp4", videos)
    await w.run()
    w.drive.remove(w.folder_doc(videos).mirror_item_id)   # the mirror folder vanished
    b = await w.recording("b.mp4", videos)
    counters = await w.run()
    assert counters["mirror_chain_reset"] == 1 and not w.folder_doc(videos).mirror_item_id
    await w.run()
    assert w.mirror_path(b) == "Videos/b.mp4.md"


async def test_404_on_a_patch_tells_item_from_destination():
    w = MirrorWorld()
    box = await w.folder("Box")
    a, b = await mirrored(w, "a.mp4", "b.mp4")
    await w.run()
    w.drive.remove(w.doc(a).mirror_item_id)               # the item is gone: re-created from the store
    await w.update(a, name="a2.mp4")
    await w.run()
    assert w.mirror_path(a) == "a2.mp4.md"
    other = await w.recording("c.mp4", box)
    await w.run()
    w.drive.remove(w.folder_doc(box).mirror_item_id)      # the destination is gone: its chain is reset
    await w.update(b, parent_id=box)
    counters = await w.run()
    assert counters["mirror_chain_reset"] == 1
    await w.run()
    assert w.mirror_path(b) == "Box/b.mp4.md"
    assert w.mirror_path(other) is None                   # went with the folder; the audit re-creates it (Task 21B)


# -- the property: every write ends under the root -----------------------------------------------------------------

@pytest.mark.parametrize("seed", range(12))
async def test_every_write_ends_under_the_root(seed):
    """Every Graph write lands under the root (its own creation excepted, and only that), no write ever targets an
    item the test planted (a source, or a foreign item put into the mirror), and crashes at random points heal."""
    rnd = random.Random(seed)
    w = MirrorWorld()
    folders = [AREA]
    files = []
    for step in range(30):
        roll = rnd.random()
        if roll < 0.2:
            folders.append(await w.folder(rnd.choice(["A", "B", "X.md", "Archive", "Long paths", "c"]),
                                          rnd.choice(folders)))
        elif roll < 0.55 or not files:
            files.append(await w.recording(rnd.choice(["x", "X", "a.mp4", "b.mp4", "c"]), rnd.choice(folders)))
        elif roll < 0.7:
            f = rnd.choice(files)
            if w.rec(f) is not None:
                await w.update(f, name=rnd.choice(["x", "a.mp4", "B.MP4", "z.mp4"]))
        elif roll < 0.8:
            f = rnd.choice(files)
            if w.rec(f) is not None:
                await w.delete_source(f, why=rnd.choice(["gone", "unstored", "unknown"]))
        elif roll < 0.88 and w.root is not None:
            parent = rnd.choice([w.root.root_item_id] + [d.mirror_item_id for d in w.mirror.docs.values()
                                                          if d.kind == KIND_FOLDER and d.mirror_item_id])
            if parent in w.drive.nodes:
                w.drive.add_file(parent, rnd.choice(["a.mp4.md", "x.md", "junk"]))
        if rnd.random() < 0.15:
            w.drive.crash_after(rnd.choice(["upload", "create_folder", "move"]))
        w.advance(timedelta(minutes=rnd.choice([1, 10, 70])))
        try:
            counters = await w.run(ops=rnd.choice([3, 10, 40]))
        except Crash:
            continue
        assert not counters.get("mirror_errors") and not counters.get("mirror_charged"), counters
    for _ in range(3):                                    # let leftover crash points fire and heal
        try:
            await w.run()
        except Crash:
            pass
    root_id = w.root.root_item_id
    assert w.drive.write_parents
    for method, chains, created in w.drive.write_parents:
        if method == "create_folder" and chains == ((AREA,),) and created is not None:
            node = w.drive.nodes.get(created)
            assert node is not None and node.name in root_name_candidates()
            # the root's own creation; or the named crash orphan of §27.9.2 (created, the process died before the
            # root document was written): never adopted, never written into, so it stays empty
            assert created == root_id or w.drive.children(created) == [], "a non-root folder under the App Folder"
            continue
        assert chains and all(root_id in chain for chain in chains), method
    names = [c[1]["name"] for c in w.drive.calls if c[0] == "create_folder" and c[1]["parent_id"] == AREA]
    assert names and set(names) <= set(root_name_candidates())
    touched = [c for c in w.drive.calls if c[0] in ("replace_content", "move", "delete")]
    assert not [c for c in touched if c[1]["item_id"] in w.drive.planted]
    uploads = [c for c in w.drive.calls if c[0] == "upload"]
    assert uploads and all(c[1]["conflict"] == "fail" for c in uploads)
    archived = [d for d in w.mirror.docs.values() if d.state == MIRROR_ARCHIVED]
    for d in archived:
        assert w.drive.path_of(d.mirror_item_id).startswith(f"{w.drive.path_of(root_id)}/{ARCHIVE_NAME}/")
    held = [d.mirror_item_id for d in w.mirror.docs.values() if d.mirror_item_id]
    assert len(held) == len(set(held))                    # no two ledger documents claim one item


# -- further rules of the step ---------------------------------------------------------------------------------------

async def test_marks_wait_for_a_complete_round():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    await w.delete_source(a)
    w.advance(2 * HOUR)
    await w.run(round_complete=False)
    assert w.doc(a).state == MIRROR_ACTIVE and w.doc(a).gone_pending
    await w.run()
    assert w.doc(a).state == MIRROR_ARCHIVED


async def test_mark_waits_while_an_ancestor_reobserves():
    w = MirrorWorld()
    big = await w.folder("Big")
    a = await w.recording("a.mp4", big)
    await w.run()
    await w.update(big, reobserve_pending=True)            # a restored large folder is still being re-listed
    await w.delete_source(a, on_drive=False)
    w.advance(2 * HOUR)
    w.reset_calls()
    await w.run()
    doc = w.doc(a)
    assert doc.state == MIRROR_ACTIVE and doc.gone_pending and doc.mark_ready_at == w.clock + timedelta(minutes=10)
    assert w.drive.writes() == []


async def test_same_path_archived_twice_on_one_day_takes_the_suffix():
    w = MirrorWorld()
    v = await w.folder("V")
    first, _ = await _archived(w, "a.mp4", v)
    second = await w.recording("a.mp4", v)
    await w.run()
    await w.delete_source(second)
    w.advance(2 * HOUR)
    await w.run()
    day = w.doc(first).archive_day
    assert w.doc(second).archive_day == day
    assert w.mirror_path(first) == f"{ARCHIVE_NAME}/{day}/V/a.mp4.md"
    second_path = w.mirror_path(second)
    assert second_path.startswith(f"{ARCHIVE_NAME}/{day}/V/a.mp4~") and second_path.endswith(".md")


async def test_document_of_another_drive_is_never_written():
    w = MirrorWorld()
    await mirrored(w, "x.mp4")
    a = await w.recording("a.mp4")
    from src.domain.drive_mirror import KIND_FILE, MirrorDoc
    other = MirrorDoc(USER, "drive-2", KIND_FILE, a, source_name="a.mp4", state=MIRROR_ARCHIVED,
                      mirror_item_id="elsewhere", source_identity=identity(a))
    w.mirror.docs[mirror_key(KIND_FILE, a)] = other     # the key carries the drive: only corrupt data puts it there
    w.reset_calls()
    counters = await w.run()
    assert counters["mirror_other_drive"] == 1 and w.drive.writes() == []
    assert w.mirror.docs[mirror_key(KIND_FILE, a)] == other


async def test_long_paths_file_not_moved_back_every_tick():
    w = MirrorWorld()
    videos = await w.folder("Videos")
    a = await w.recording("a.mp4", videos)
    b = await w.recording("b.mp4", videos)
    await w.run()
    from src.domain.user_drive import DrivePathTooLong
    w.drive.fail("move", DrivePathTooLong(), times=1)
    await w.update(a, name="b2.mp4")                      # a rename that answers path-too-long once
    await w.run()
    assert w.mirror_path(a) == f"{LONG_PATHS_NAME}/b2.mp4.md"
    w.reset_calls()
    for _ in range(2):
        await w.run()
    assert w.drive.writes() == [] and not w.rec(a).mirror_due
    assert w.mirror_path(b) == "Videos/b.mp4.md"


async def test_intent_name_held_by_a_file_of_another_size_is_foreign():
    w = MirrorWorld()
    await _bare_root(w)
    a = await w.recording("x.mp4")
    w.drive.crash_before("upload")
    with pytest.raises(Crash):
        await w.run()
    stray = w.drive.add_file(w.root.root_item_id, "x.mp4.md", b"dropped in by the owner meanwhile")
    w.reset_calls()
    await w.run()
    assert w.drive.nodes[stray].data == b"dropped in by the owner meanwhile"
    assert [c[1]["name"] for c in w.drive.writes() if c[0] == "upload"] == [w.doc(a).mirror_name]   # no 409 first
    assert w.doc(a).mirror_item_id != stray and w.mirror_path(a).startswith("x.mp4~")


async def test_a_file_moves_out_of_long_paths_when_its_path_fits_again():
    w = MirrorWorld(area_path="Apps/" + "A" * 365)
    videos = await w.folder("Videos")
    a = await w.recording("a.mp4", videos)
    await w.run()
    assert w.mirror_path(a) == f"{LONG_PATHS_NAME}/a.mp4.md"
    w.drive.area_path_value = "Apps/Alek"                 # the path fits now; the source itself did not change
    await w.update(a, web_url="https://example.invalid/again")
    await w.run()
    assert w.mirror_path(a) == "Videos/a.mp4.md"


async def test_suffix_renamed_back_when_the_folder_moves_away():
    w = MirrorWorld()
    other = await w.folder("Other")
    (x,) = await mirrored(w, "X")
    folder = await w.folder("X.md")
    await w.recording("in.mp4", folder)
    await w.run()
    assert w.doc(x).mirror_name != "X.md"
    await w.update(folder, parent_id=other)               # the folder leaves the root: the old name is free
    await w.run()
    assert w.mirror_path(x) == "X.md"
    assert w.drive.path_of(w.folder_doc(folder).mirror_item_id).endswith("Other/X.md")


async def test_archive_frees_a_name_and_the_suffix_goes():
    w = MirrorWorld()
    (old,) = await mirrored(w, "a.mp4")
    await w.delete_source(old, why=UNSTORED)              # detached: its file keeps the name
    w.advance(2 * HOUR)
    await w.run()
    new = await w.recording("a.mp4")
    await w.run()
    assert w.mirror_path(new).startswith("a.mp4~")
    await w.mirror.mark_gone(USER, [DeletionNotice(old, GONE, w.clock, source_identity=identity(old))],
                             drive_identity=DRIVE_IDENTITY)
    w.advance(2 * HOUR)
    await w.run()
    assert w.doc(old).state == MIRROR_ARCHIVED and w.mirror_path(new) == "a.mp4.md"


async def test_a_file_leaving_long_paths_frees_its_name_there():
    w = MirrorWorld(area_path="Apps/" + "A" * 270)
    one = await w.folder("B" * 100)
    two = await w.folder("C" * 100)
    short = await w.folder("S")
    first = await w.recording("a.mp4", one)
    second = await w.recording("a.mp4", two)
    await w.run()
    names = sorted([w.mirror_path(first), w.mirror_path(second)])
    assert names[0] == f"{LONG_PATHS_NAME}/a.mp4.md" and names[1].startswith(f"{LONG_PATHS_NAME}/a.mp4~")
    winner = first if w.mirror_path(first) == f"{LONG_PATHS_NAME}/a.mp4.md" else second
    loser = second if winner == first else first
    await w.update(winner, parent_id=short)               # fits under the short folder: leaves Long paths
    await w.run()
    assert w.mirror_path(winner) == "S/a.mp4.md" and w.mirror_path(loser) == f"{LONG_PATHS_NAME}/a.mp4.md"


async def test_prune_frees_a_name_and_the_suffix_goes():
    w = MirrorWorld()
    (x,) = await mirrored(w, "X")
    folder = await w.folder("X.md")
    inner = await w.recording("in.mp4", folder)
    await w.run()
    assert w.doc(x).mirror_name != "X.md"
    await w.delete_source(inner)
    await w.delete_source(folder)
    w.advance(2 * HOUR)
    await w.run()
    assert w.folder_doc(folder) is None and w.mirror_path(x) == "X.md"


async def test_gone_folder_over_detached_files_is_detached():
    w = MirrorWorld()
    folder = await w.folder("Repo")
    a = await w.recording("a.mp4", folder)
    await w.run()
    await w.delete_source(a, why=UNSTORED)
    await w.delete_source(folder, why=GONE, on_drive=False)
    w.advance(2 * HOUR)
    await w.run()
    assert w.doc(a).state == MIRROR_DETACHED and w.folder_doc(folder).state == MIRROR_DETACHED
    assert not w.folder_doc(folder).gone_pending
    assert w.folder_doc(folder).mirror_item_id in w.drive.nodes


# -- fix round 1 -----------------------------------------------------------------------------------------------------

async def test_intent_never_adopts_a_file_another_document_holds():
    w = MirrorWorld()
    await _bare_root(w)
    a = await w.recording("x.mp4")
    w.drive.crash_before("upload")
    with pytest.raises(Crash):
        await w.run()
    size = w.doc(a).pending_size
    assert size > 0
    theirs = w.drive.add_file(w.root.root_item_id, "x.mp4.md", b"y" * size)      # ours by time and size, but ...
    from src.domain.drive_mirror import KIND_FILE, MirrorDoc
    await w.mirror.save_doc(MirrorDoc(USER, DRIVE_IDENTITY, KIND_FILE, "ghost", source_identity=identity("ghost"),
                                      source_name="other.mp4",
                                      mirror_item_id=theirs, mirror_parent_id=w.root.root_item_id,
                                      mirror_name="x.mp4.md"))                 # ... another document's item
    await w.run()
    assert w.doc(a).mirror_item_id != theirs and w.mirror_path(a).startswith("x.mp4~")
    assert w.drive.nodes[theirs].data == b"y" * size


async def test_intent_doc_with_a_gone_source_adopts_then_archives():
    w = MirrorWorld()
    await _bare_root(w)
    a = await w.recording("x.mp4")
    w.drive.crash_after("upload")                         # the file landed, the document never learned its id
    with pytest.raises(Crash):
        await w.run()
    assert not w.doc(a).mirror_item_id and w.doc(a).pending_op
    await w.delete_source(a)
    w.advance(2 * HOUR)
    await w.run()
    doc = w.doc(a)
    assert doc.state == MIRROR_ARCHIVED and w.mirror_path(a).startswith(f"{ARCHIVE_NAME}/") \
        and w.mirror_path(a).endswith("/x.mp4.md")
    assert not [n for n in w.drive.children(w.root.root_item_id) if not n.is_folder and n.name != README_NAME]


async def test_intent_doc_with_nothing_on_the_drive_is_deleted():
    w = MirrorWorld()
    await _bare_root(w)
    a = await w.recording("x.mp4")
    w.drive.crash_before("upload")
    with pytest.raises(Crash):
        await w.run()
    await w.delete_source(a, why=UNSTORED)
    w.advance(2 * HOUR)
    counters = await w.run()
    assert w.doc(a) is None and counters["mirror_intent_dropped"] == 1
    assert [c[0] for c in w.drive.calls].count("child_by_name") == 1               # one read resolved it


async def test_archive_never_keeps_a_temporary_name():
    w = MirrorWorld()
    a, b = await mirrored(w, "A.mp4", "B.mp4")
    await w.update(a, name="B.mp4")
    await w.update(b, name="A.mp4")
    await w.run(ops=1)                                    # the swap stops after its first move: a temporary name
    stuck = next(i for i in (a, b) if "~tmp" in w.doc(i).mirror_name)
    old_name = w.doc(stuck).source_name
    await w.delete_source(stuck)
    w.advance(2 * HOUR)
    await w.run()
    doc = w.doc(stuck)
    assert doc.state == MIRROR_ARCHIVED and doc.mirror_name == f"{old_name}.md" and "~tmp" not in w.mirror_path(stuck)
