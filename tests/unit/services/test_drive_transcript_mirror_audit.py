"""DriveTranscriptMirror: the audit and the rotating scan (USER_DRIVE_INDEXING_RFC §27.9.9, §27.9.6; Δ117, Δ201; plan
Task 21B). Synthetic items only, on the in-memory repositories and a scripted drive."""
from __future__ import annotations

import logging
import random
from dataclasses import replace
from datetime import timedelta

import pytest

from src.domain.drive_index_params import MIRROR_AUDIT_DAYS
from src.domain.drive_mirror import (
    ARCHIVE_NAME, GONE, KIND_ARCHIVE, KIND_FILE, LONG_PATHS_NAME, MIRROR_ACTIVE, MIRROR_ARCHIVED, MIRROR_DETACHED,
    README_NAME, ROOT_NAME, UNKNOWN, UNSTORED, MirrorDoc, MirrorRoot, archive_folder_key,
)
from src.domain.drive_mirror_names import is_root_name
from src.domain.drive_mirror_plan import audit_due, cleared_item, lost_root_reset
from src.domain.drive_mirror_render import readme_bytes

from .drive_mirror_fakes import AREA, DRIVE, EN, START, USER, Crash, MirrorWorld

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
WEEK = timedelta(days=MIRROR_AUDIT_DAYS)
GRAPH_READS = ("list_children_changes", "get_change", "child_by_name", "area_path", "get_root", "get_item")


async def mirrored(world: MirrorWorld, *names: str, parent: str = AREA):
    ids = [await world.recording(n, parent) for n in names]
    await world.run()
    return ids


async def archived(world: MirrorWorld, name: str = "a.mp4", parent: str = AREA):
    a = await world.recording(name, parent)
    await world.run()
    await world.delete_source(a)
    world.advance(2 * HOUR)
    await world.run()
    assert world.doc(a).state == MIRROR_ARCHIVED
    return a


async def audit(world: MirrorWorld, **kw):
    """A run past the audit's interval (the default budgets finish a small walk)."""
    world.advance(WEEK + HOUR)
    counters = await world.run(**kw)
    return counters


def item_of(world: MirrorWorld, item_id: str):
    doc = world.doc(item_id)
    assert doc is not None and doc.mirror_item_id
    return world.drive.nodes[doc.mirror_item_id]


def touched_ids(world: MirrorWorld):
    return {c[1].get("item_id") for c in world.drive.writes()} | {c[1].get("parent_id") for c in world.drive.writes()} \
        | {c[1].get("new_parent_id") for c in world.drive.writes()}


# -- the pure helpers -----------------------------------------------------------------------------------------------

def test_audit_due_every_week_from_creation_then_from_the_last_cycle():
    root = MirrorRoot(USER, DRIVE, "r", ROOT_NAME, created_at=START)
    assert not audit_due(root, START + WEEK - HOUR) and audit_due(root, START + WEEK)
    done = replace(root, audited_at=START + 10 * DAY)
    assert not audit_due(done, START + 16 * DAY) and audit_due(done, START + 17 * DAY)
    assert audit_due(MirrorRoot(USER, DRIVE, "r", ROOT_NAME), START)


def test_root_names_recognised():
    assert all(is_root_name(n) for n in ("_Transcripts", "_transcripts (alek)", "_Transcripts (Alek 2)",
                                         "_Transcripts (Alek 12)"))
    assert not any(is_root_name(n) for n in ("Transcripts", "_Transcripts copy", "_Transcripts (Bob)", "x"))


def test_lost_root_reset_drops_the_archive_and_clears_the_rest():
    doc = MirrorDoc(USER, DRIVE, KIND_FILE, "s", mirror_item_id="m", mirror_parent_id="p", mirror_name="a.md",
                    mirror_ctag="c", rendered_hash="h", pending_op="create", pending_name="a.md", attempts=3,
                    gone_pending=True, gone_why=GONE)
    new = lost_root_reset(doc)
    assert new is not None and new == replace(cleared_item(doc), attempts=0)
    assert not (new.mirror_item_id or new.mirror_parent_id or new.rendered_hash or new.pending_op)
    assert new.gone_pending                                # the mark stays: the mark step decides the source
    assert lost_root_reset(replace(doc, state=MIRROR_ARCHIVED)) is None
    assert lost_root_reset(MirrorDoc(USER, DRIVE, KIND_ARCHIVE, "k", mirror_item_id="m")) is None
    assert lost_root_reset(replace(doc, state=MIRROR_DETACHED)).state == MIRROR_DETACHED


# -- the audit: the active tree --------------------------------------------------------------------------------------

async def test_user_edit_of_an_active_file_rewritten():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    original = w.content(a)
    node = item_of(w, a)
    node.data, node.ctag = b"edited by hand", "c-owner"
    counters = await audit(w)
    assert counters["mirror_rewrite_due"] == 1 and w.rec(a).mirror_due and w.root.audited_at == w.clock
    w.reset_calls()
    await w.run()
    assert w.content(a) == original
    assert [(c[0], c[1]["item_id"]) for c in w.drive.writes()] == [("replace_content", node.item_id)]
    w.reset_calls()
    await audit(w)                                         # the rewrite's own cTag is the reference now
    assert w.drive.writes() == [] and not w.rec(a).mirror_due


async def test_user_deletion_of_an_active_file_recreated():
    w = MirrorWorld()
    videos = await w.folder("Videos")
    (a,) = await mirrored(w, "a.mp4", parent=videos)
    old = w.doc(a).mirror_item_id
    w.drive.remove(old)
    counters = await audit(w)
    assert counters["mirror_recreate_due"] == 1 and not w.doc(a).mirror_item_id
    await w.run()
    assert w.mirror_path(a) == "Videos/a.mp4.md" and "hello there" in w.content(a)
    assert w.doc(a).mirror_item_id != old


async def test_user_deletion_of_an_active_folder_recreates_what_was_in_it():
    w = MirrorWorld()
    videos = await w.folder("Videos")
    a, b = await mirrored(w, "a.mp4", "b.mp4", parent=videos)
    w.drive.remove(w.folder_doc(videos).mirror_item_id)    # the whole mirror folder, files and all
    counters = await audit(w)
    assert counters.get("mirror_recreate_due") == 2
    await w.run()
    assert w.mirror_path(a) == "Videos/a.mp4.md" and w.mirror_path(b) == "Videos/b.mp4.md"


async def test_user_move_of_an_active_file_moved_back():
    w = MirrorWorld()
    box = await w.folder("Box")
    a, b = await mirrored(w, "a.mp4", "b.mp4")
    (c,) = await mirrored(w, "c.mp4", parent=box)
    box_mirror = w.folder_doc(box).mirror_item_id
    na, nb, nc = item_of(w, a), item_of(w, b), item_of(w, c)
    na.name = "renamed by hand.md"                         # renamed in place
    nb.parent_id = box_mirror                              # moved into another mirror folder
    nc.parent_id = AREA                                    # moved out of the mirror altogether
    w.reset_calls()
    counters = await audit(w)
    assert counters["mirror_moved_back"] == 3
    assert (w.mirror_path(a), w.mirror_path(b), w.mirror_path(c)) == ("a.mp4.md", "b.mp4.md", "Box/c.mp4.md")
    moves = [c[1]["item_id"] for c in w.drive.writes() if c[0] == "move"]
    assert sorted(moves) == sorted([na.item_id, nb.item_id, nc.item_id])
    assert [c for c in w.drive.writes() if c[0] != "move"] == []
    assert not set(moves) & w.drive.planted


async def test_move_back_onto_a_taken_name_goes_through_the_name_rules():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    node = item_of(w, a)
    node.name = "elsewhere.md"
    owner = w.drive.add_file(w.root.root_item_id, "a.mp4.md", b"mine")   # the owner's own file takes the name
    counters = await audit(w)
    assert counters["mirror_move_back_blocked"] == 1 and w.rec(a).mirror_due
    await w.run()
    assert w.mirror_path(a).startswith("a.mp4~") and w.drive.nodes[owner].data == b"mine"
    assert owner not in touched_ids(w)


# -- the audit: the archive ------------------------------------------------------------------------------------------

async def test_owner_deleted_an_archived_file_doc_deleted_and_unpinned():
    w = MirrorWorld()
    a = await archived(w)
    key = w.doc(a).content_key
    assert await w.mirror.pinned(USER, key)
    w.drive.remove(w.doc(a).mirror_item_id)
    w.reset_calls()
    counters = await audit(w)
    assert w.doc(a) is None and counters["mirror_archive_owner_deleted"] == 1
    assert not await w.mirror.pinned(USER, key)
    assert w.drive.writes() == []                          # never re-created: that would be data invention


async def test_owner_deleted_a_day_folder_discards_beneath():
    w = MirrorWorld()
    videos = await w.folder("Videos")
    a = await w.recording("a.mp4", videos)
    b = await w.recording("b.mp4", videos)
    c = await w.recording("c.mp4")
    await w.run()
    for item in (a, b, c):
        await w.delete_source(item)
    w.advance(2 * HOUR)
    await w.run()
    assert all(w.doc(i).state == MIRROR_ARCHIVED for i in (a, b, c))
    day = w.doc(a).archive_day
    day_key = archive_folder_key(USER, DRIVE, day, "")
    keys = [w.doc(i).content_key for i in (a, b, c)]
    w.drive.remove(w.mirror.docs[day_key].mirror_item_id)  # the owner deletes the whole day
    w.reset_calls()
    await audit(w)
    assert all(w.doc(i) is None for i in (a, b, c))
    assert day_key not in w.mirror.docs
    assert archive_folder_key(USER, DRIVE, day, "Videos") not in w.mirror.docs
    assert archive_folder_key(USER, DRIVE, "", ARCHIVE_NAME) in w.mirror.docs      # the archive itself stays
    assert not [k for k in keys if await w.mirror.pinned(USER, k)]
    assert w.drive.writes() == []


async def test_archived_edit_left_alone():
    w = MirrorWorld()
    a = await archived(w)
    node = item_of(w, a)
    node.data, node.ctag, node.name = b"my own notes", "c-owner", "kept.md"
    w.reset_calls()
    counters = await audit(w)
    assert w.drive.writes() == [] and node.data == b"my own notes" and node.name == "kept.md"
    doc = w.doc(a)
    assert doc.state == MIRROR_ARCHIVED and doc.mirror_name == "kept.md" and doc.mirror_ctag == "c-owner"
    assert counters["mirror_followed"] == 1 and counters["mirror_edit_left"] == 1
    assert await w.mirror.pinned(USER, doc.content_key)


async def test_archived_moved_inside_the_mirror_followed():
    w = MirrorWorld()
    a = await archived(w)
    node = item_of(w, a)
    node.parent_id = w.root.root_item_id                   # out of the archive, still in the mirror
    w.reset_calls()
    await audit(w)
    assert w.drive.writes() == []
    doc = w.doc(a)
    assert doc.state == MIRROR_ARCHIVED and doc.mirror_parent_id == w.root.root_item_id


async def test_archived_moved_out_of_the_mirror_doc_deleted():
    w = MirrorWorld()
    a = await archived(w)
    node = item_of(w, a)
    key = w.doc(a).content_key
    node.parent_id = AREA                                  # the owner's file now
    w.reset_calls()
    counters = await audit(w)
    assert w.doc(a) is None and counters["mirror_moved_out"] == 1
    assert not await w.mirror.pinned(USER, key)
    assert w.drive.writes() == [] and w.drive.nodes[node.item_id].parent_id == AREA


# -- the audit: the read-me, Long paths, foreign items, the orphan root --------------------------------------------

@pytest.mark.parametrize("how", ["missing", "changed"])
async def test_readme_missing_or_changed_rewritten(how):
    w = MirrorWorld()
    await mirrored(w, "a.mp4")
    readme = w.root.readme_item_id
    if how == "missing":
        w.drive.remove(readme)
    else:
        node = w.drive.nodes[readme]
        node.data, node.ctag = b"scribbled over", "c-owner"
    w.reset_calls()
    await audit(w)
    found = w.drive.child(w.root.root_item_id, README_NAME)
    assert found is not None and found.data == readme_bytes(EN)
    assert w.root.readme_item_id == found.item_id and w.root.readme_ctag == found.ctag
    kinds = [c[0] for c in w.drive.writes()]
    if how == "missing":
        assert kinds == ["upload"] and w.drive.writes()[0][1]["conflict"] == "fail"
    else:
        assert kinds == ["replace_content"] and found.item_id == readme
    w.reset_calls()
    await audit(w)
    assert w.drive.writes() == []


async def test_missing_long_paths_folder_created_on_demand_only(monkeypatch):
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    from src.domain.drive_mirror import MirrorDoc as Doc
    key = archive_folder_key(USER, DRIVE, "", LONG_PATHS_NAME)
    lp = w.drive._make_folder(w.root.root_item_id, LONG_PATHS_NAME)
    await w.mirror.save_doc(Doc(USER, DRIVE, KIND_ARCHIVE, key.rsplit(":", 1)[1], source_name=LONG_PATHS_NAME,
                                mirror_item_id=lp, mirror_parent_id=w.root.root_item_id, mirror_name=LONG_PATHS_NAME))
    w.drive.remove(lp)
    w.reset_calls()
    await audit(w)
    assert key not in w.mirror.docs and w.drive.writes() == []
    assert w.drive.child(w.root.root_item_id, LONG_PATHS_NAME) is None


async def test_foreign_item_kept_and_counted():
    w = MirrorWorld()
    box = await w.folder("Box")
    (c,) = await mirrored(w, "c.mp4", parent=box)
    a = await archived(w)
    day_folder = w.doc(a).mirror_parent_id
    foreign = [
        w.drive.add_file(w.folder_doc(box).mirror_item_id, "owner.txt", b"mine"),   # the active tree
        w.drive.add_file(day_folder, "notes.txt", b"mine too"),                       # the archive
        w.drive.add_folder(w.root.root_item_id, "My folder"),                         # the root
    ]
    w.reset_calls()
    counters = await audit(w)
    assert counters["mirror_foreign"] >= 3
    assert all(f in w.drive.nodes for f in foreign)
    assert not touched_ids(w) & set(foreign)
    assert w.drive.nodes[foreign[0]].data == b"mine" and w.drive.nodes[foreign[1]].data == b"mine too"
    assert w.mirror_path(c) == "Box/c.mp4.md" and w.doc(a).state == MIRROR_ARCHIVED


async def test_orphan_root_reported_once_never_adopted(caplog):
    w = MirrorWorld()
    await w.recording("a.mp4")
    w.drive.crash_after("create_folder", lambda args: args["parent_id"] == AREA and args["name"] == ROOT_NAME)
    with pytest.raises(Crash):
        await w.run()                                      # created, then the process died before the root document
    orphan = w.drive.child(AREA, ROOT_NAME).item_id
    await w.run()
    assert w.root.root_item_id != orphan and w.root.root_name == f"{ROOT_NAME} (Alek)"
    with caplog.at_level(logging.WARNING):
        counters = await audit(w)
    assert counters["mirror_orphan_roots"] == 1 and w.root.orphans_reported
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "named like the transcript root" in
                r.getMessage()]
    assert len(warnings) == 1 and ROOT_NAME not in warnings[0].getMessage()
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        again = await audit(w)
    assert not again.get("mirror_orphan_roots")
    assert not [r for r in caplog.records if "named like the transcript root" in r.getMessage()]
    assert w.drive.children(orphan) == [] and orphan not in touched_ids(w)
    assert w.root.root_item_id != orphan and orphan not in w.root.previous_root_ids


# -- the audit walk ----------------------------------------------------------------------------------------------------

async def test_audit_walk_resumes_across_ticks_and_ends_the_cycle(monkeypatch):
    from src.services import drive_transcript_mirror as mod
    monkeypatch.setattr(mod, "AUDIT_PAGE", 2)
    w = MirrorWorld()
    box = await w.folder("Box")
    files = await mirrored(w, "a.mp4", "b.mp4", "c.mp4")
    inner = await mirrored(w, "d.mp4", "e.mp4", "f.mp4", parent=box)
    original = w.content(inner[2])
    node = item_of(w, inner[2])
    node.data, node.ctag = b"edited", "c-owner"            # found on the folder's second page
    w.advance(WEEK + HOUR)
    runs, stages = 0, []
    while w.root.audited_at is None:
        runs += 1
        assert runs < 40
        counters = await w.run(reads=1)
        assert counters.get("mirror_reads", 0) <= 1
        stages.append((w.root.audit_folder_key, bool(w.root.audit_page_cursor)))
    assert runs > 4                                        # pages of two, one read a tick
    assert ("", True) in stages                            # the root's walk stopped mid-folder and resumed
    assert w.root.audited_at == w.clock and w.root.audit_folder_key == "" and w.root.audit_page_cursor == ""
    await w.run()
    assert w.content(inner[2]) == original                 # rewritten from the store by a later tick's step 4
    assert [c[1]["item_id"] for c in w.drive.calls if c[0] == "replace_content"] == [node.item_id]
    w.reset_calls()
    await w.run(reads=1)                                   # the next cycle waits a week
    assert not [c for c in w.drive.calls if c[0] == "list_children_changes"]


async def test_audit_spends_only_the_mirror_reads():
    w = MirrorWorld()
    box = await w.folder("Box")
    await mirrored(w, "a.mp4", "b.mp4")
    await mirrored(w, "c.mp4", parent=box)
    w.advance(WEEK + HOUR)
    w.reset_calls()
    counters = await w.run(reads=3)
    reads = [c for c in w.drive.calls if c[0] in GRAPH_READS]
    assert len(reads) == counters["mirror_reads"] == 3 and w.last_host.reads == 0
    w.reset_calls()
    await w.run(reads=0)
    assert w.drive.calls == []                             # no read budget, no Graph call
    w.reset_calls()
    await w.run()
    assert w.root.audited_at is not None


async def test_audit_waits_for_a_complete_round():
    w = MirrorWorld()
    await mirrored(w, "a.mp4")
    w.advance(WEEK + HOUR)
    w.reset_calls()
    await w.run(round_complete=False)
    assert w.drive.calls == [] and w.root.audited_at is None


# -- the rotating scan -----------------------------------------------------------------------------------------------

async def test_scan_marks_a_missing_record_unknown():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    # carry: a record whose digest was never stamped sends no deletion notice when it goes (§27.9.9's "a record
    # re-created after a purge and deleted before its digest was stamped")
    await w.repo.upsert_records([replace(w.rec(a), mirrored_digest="")])
    await w.repo.delete_records(USER, [a])
    w.drive.remove(a)
    counters = await w.run()
    doc = w.doc(a)
    assert counters["mirror_scan_marked"] == 1 and doc.gone_pending and doc.gone_why == UNKNOWN
    w.advance(2 * HOUR)
    await w.run()
    assert w.doc(a).state == MIRROR_ARCHIVED               # one read of the source: not found, archived


async def test_scan_marks_an_intent_doc_whose_record_is_gone():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    await w.mirror.save_doc(replace(w.doc(a), pending_op="create", pending_name="a.mp4.md",
                                    pending_parent_id=w.root.root_item_id, pending_at=w.clock))
    await w.repo.delete_records(USER, [a])                 # no notice: the record carried no digest of ours
    await w.run()
    assert w.doc(a).gone_pending and w.doc(a).gone_why == UNKNOWN


async def test_scan_waits_while_enumerating():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    await w.repo.delete_records(USER, [a])
    await w.run(epoch_idle=False)
    assert not w.doc(a).gone_pending
    await w.run(round_complete=False)
    assert not w.doc(a).gone_pending
    await w.run()
    assert w.doc(a).gone_pending


async def test_scan_pages_rotate(monkeypatch):
    from src.services import drive_transcript_mirror as mod
    monkeypatch.setattr(mod, "MIRROR_SCAN_PAGE", 2)
    w = MirrorWorld()
    ids = await mirrored(w, "a.mp4", "b.mp4", "c.mp4", "d.mp4", "e.mp4")
    await w.mirror.save_root(replace(w.root, scan_cursor=""))
    await w.repo.delete_records(USER, ids)
    marked = []
    for _ in range(3):
        await w.run(ops=0)
        marked.append(sum(1 for i in ids if w.doc(i).gone_pending))
    assert marked == [2, 4, 5] and w.root.scan_cursor == ""


async def test_detached_rechecked_weekly():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    await w.delete_source(a, why=UNSTORED)                 # still on the drive, no longer stored
    w.advance(2 * HOUR)
    await w.run()
    assert w.doc(a).state == MIRROR_DETACHED and w.doc(a).checked_at == w.clock

    def source_reads():
        return [c for c in w.drive.calls if c[0] == "get_change" and c[1]["item_id"] == a]

    w.advance(3 * DAY)
    w.reset_calls()
    await w.run()
    assert source_reads() == []
    w.advance(5 * DAY)
    w.reset_calls()
    await w.run()
    assert len(source_reads()) == 1
    assert w.doc(a).state == MIRROR_DETACHED and w.doc(a).checked_at == w.clock and not w.doc(a).gone_pending
    w.drive.remove(a)
    w.advance(WEEK + HOUR)
    w.reset_calls()
    await w.run()
    assert len(source_reads()) == 1 and w.doc(a).gone_pending and w.doc(a).gone_why == GONE
    w.advance(2 * HOUR)
    await w.run()
    assert w.doc(a).state == MIRROR_ARCHIVED


async def test_detached_recheck_failure_backs_off():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    await w.delete_source(a, why=UNSTORED)
    w.advance(2 * HOUR)
    await w.run()
    w.drive.fail("get_change", ValueError("Graph 503"), lambda args: args["item_id"] == a)
    w.advance(WEEK + HOUR)
    counters = await w.run()
    assert counters["mirror_charged"] >= 1 and w.doc(a).attempts == 1 and w.doc(a).retry_at > w.clock
    w.reset_calls()
    await w.run()                                          # backing off: no read
    assert not [c for c in w.drive.calls if c[0] == "get_change" and c[1]["item_id"] == a]


# -- carries: a document that cannot be read never stalls the queue or a mark ----------------------------------------

async def test_unreadable_document_on_a_charge_holds_the_record(monkeypatch):
    from src.services import drive_transcript_mirror as mod
    monkeypatch.setattr(mod, "QUEUE_PAGE", 1)
    w = MirrorWorld()
    await mirrored(w, "x.mp4")
    bad = await w.recording("bad.mp4", day="2026-10-05")
    good = await w.recording("good.mp4", day="2026-10-01")
    bad_key = f"{USER}:{KIND_FILE}:{bad}"
    real = w.mirror.get_docs

    async def get_docs(user_id, keys):
        if bad_key in keys:
            raise ValueError("a document that cannot be decoded")
        return await real(user_id, keys)

    monkeypatch.setattr(w.mirror, "get_docs", get_docs)
    counters = await w.run()
    assert counters["mirror_charged"] >= 1
    assert not w.rec(bad).mirror_due and w.rec(bad).mirror_retry_at is not None      # held, off the queue
    assert w.mirror_path(good) == "good.mp4.md"           # the head no longer blocks the older record


async def test_mark_meeting_another_drives_document_is_held():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    key = archive_folder_key(USER, DRIVE, "", ARCHIVE_NAME)
    other = MirrorDoc(USER, "drive-2", KIND_ARCHIVE, key.rsplit(":", 1)[1], mirror_item_id="elsewhere")
    await w.mirror.save_doc(other)                         # the key the archive needs holds another drive's document
    await w.delete_source(a)
    w.advance(2 * HOUR)
    counters = await w.run()
    assert counters["mirror_other_drive"] == 1 and w.mirror.docs[key] == other
    assert w.doc(a).mark_ready_at > w.clock and w.doc(a).attempts == 1               # held, not ready every tick
    w.reset_calls()
    counters = await w.run()
    assert not counters.get("mirror_other_drive") and w.drive.writes() == []


# -- the property: the audit never touches what the bot did not create ------------------------------------------------

@pytest.mark.parametrize("seed", range(10))
async def test_audit_never_touches_an_item_it_did_not_create(seed):
    """Random trees, the owner's edits, deletions, renames and moves of the bot's items, foreign items inside the
    mirror and in its archive, and weekly audits: no write ever targets a planted item, every create and every moved
    item's destination is under the root, and no run counts an unexpected error."""
    rnd = random.Random(seed)
    w = MirrorWorld()
    folders = [AREA]
    files = []
    for step in range(28):
        roll = rnd.random()
        if roll < 0.12:
            folders.append(await w.folder(rnd.choice(["A", "B", "Archive", "c"]), rnd.choice(folders)))
        elif roll < 0.35 or not files:
            files.append(await w.recording(rnd.choice(["x", "a.mp4", "b.mp4", "c"]), rnd.choice(folders)))
        elif roll < 0.45:
            f = rnd.choice(files)
            if w.rec(f) is not None:
                await w.delete_source(f, why=rnd.choice([GONE, UNSTORED, UNKNOWN]))
        elif w.root is not None:
            root_id = w.root.root_item_id
            ours = [d.mirror_item_id for d in w.mirror.docs.values()
                    if d.mirror_item_id in w.drive.nodes and d.kind == KIND_FILE]
            places = [root_id] + [d.mirror_item_id for d in w.mirror.docs.values()
                                  if d.kind != KIND_FILE and d.mirror_item_id in w.drive.nodes
                                  and w.drive.under(d.mirror_item_id, root_id)]
            act = rnd.choice(["edit", "delete", "rename", "move", "move_out", "foreign", "foreign_folder"])
            if act in ("edit", "delete", "rename", "move", "move_out") and ours:
                node = w.drive.nodes[rnd.choice(ours)]
                if act == "edit":
                    node.data, node.ctag = b"by hand", f"u{seed}-{step}"
                elif act == "delete":
                    w.drive.remove(node.item_id)
                elif act == "rename":
                    node.name = f"hand {step}.md"
                elif act == "move":
                    node.parent_id = rnd.choice(places)
                else:
                    node.parent_id = AREA
            elif act == "foreign":
                w.drive.add_file(rnd.choice(places), rnd.choice(["a.mp4.md", "notes.txt", "x.md"]), b"owner")
            elif act == "foreign_folder":
                w.drive.add_folder(rnd.choice(places), rnd.choice(["Mine", "x.md"]))
        w.advance(rnd.choice([HOUR, DAY, 4 * DAY, WEEK]))
        try:
            counters = await w.run(ops=rnd.choice([5, 40]), reads=rnd.choice([3, 10]))
        except Crash:
            continue
        assert not counters.get("mirror_errors"), counters
    assert w.root is not None
    root_id = w.root.root_item_id
    writes = w.drive.writes()
    assert [c for c in writes if c[0] in ("replace_content", "move", "delete") and c[1]["item_id"] in w.drive.planted] \
        == []
    for method, chains, created in w.drive.write_parents:
        if method == "create_folder" and chains == ((AREA,),):
            assert created == root_id
            continue
        if method in ("upload", "create_folder"):
            assert all(root_id in chain for chain in chains), method
        if method == "move" and len(chains) == 2:
            assert root_id in chains[1], "a move whose destination is outside the root"
    assert not [c for c in writes if c[0] in ("upload", "create_folder") and c[1]["parent_id"] in w.drive.planted]
