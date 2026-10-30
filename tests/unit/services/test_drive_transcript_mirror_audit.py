"""DriveTranscriptMirror: the audit and the rotating scan (USER_DRIVE_INDEXING_RFC §27.9.9, §27.9.6; Δ117, Δ201; plan
Task 21B). Synthetic items only, on the in-memory repositories and a scripted drive."""
from __future__ import annotations

import logging
import random
from dataclasses import replace
from datetime import timedelta

import pytest

from src.domain.drive_index import DriveIndexRecord, Reason, RecordState, Scope
from src.domain.drive_index_params import MIRROR_AUDIT_DAYS
from src.domain.drive_mirror import (
    ARCHIVE_NAME, GONE, KIND_ARCHIVE, KIND_FILE, LONG_PATHS_NAME, MIRROR_ACTIVE, MIRROR_ARCHIVED, MIRROR_DETACHED,
    README_NAME, ROOT_NAME, UNKNOWN, UNSTORED, MirrorDoc, MirrorRoot, archive_folder_key,
)
from src.domain.drive_mirror_names import is_root_name
from src.domain.drive_mirror_plan import audit_due, cleared_item, lost_root_reset
from src.domain.drive_mirror_render import readme_bytes, render_archived
from src.domain.drive_transcript import StoredTranscript

from .drive_mirror_fakes import (
    AREA, Crash, DRIVE_IDENTITY, EN, MirrorWorld, SCOPE, START, USER, identity, mirror_key, stored,
)
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
    root = MirrorRoot(USER, DRIVE_IDENTITY, "r", ROOT_NAME, created_at=START)
    assert not audit_due(root, START + WEEK - HOUR) and audit_due(root, START + WEEK)
    done = replace(root, audited_at=START + 10 * DAY)
    assert not audit_due(done, START + 16 * DAY) and audit_due(done, START + 17 * DAY)
    assert audit_due(MirrorRoot(USER, DRIVE_IDENTITY, "r", ROOT_NAME), START)


def test_root_names_recognised():
    assert all(is_root_name(n) for n in ("_Transcripts", "_transcripts (alek)", "_Transcripts (Alek 2)",
                                         "_Transcripts (Alek 12)"))
    assert not any(is_root_name(n) for n in ("Transcripts", "_Transcripts copy", "_Transcripts (Robin)", "x"))


def test_lost_root_reset_drops_the_archive_and_clears_the_rest():
    doc = MirrorDoc(USER, DRIVE_IDENTITY, KIND_FILE, "s", mirror_item_id="m", mirror_parent_id="p", mirror_name="a.md",
                    mirror_ctag="c", rendered_hash="h", pending_op="create", pending_name="a.md", attempts=3,
                    gone_pending=True, gone_why=GONE)
    new = lost_root_reset(doc)
    assert new is not None and new == replace(cleared_item(doc), attempts=0)
    assert not (new.mirror_item_id or new.mirror_parent_id or new.rendered_hash or new.pending_op)
    assert new.gone_pending                                # the mark stays: the mark step decides the source
    assert lost_root_reset(replace(doc, state=MIRROR_ARCHIVED)) is None
    assert lost_root_reset(MirrorDoc(USER, DRIVE_IDENTITY, KIND_ARCHIVE, "k", mirror_item_id="m")) is None
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
    day_key = archive_folder_key(USER, DRIVE_IDENTITY, day, "")
    keys = [w.doc(i).content_key for i in (a, b, c)]
    w.drive.remove(w.mirror.docs[day_key].mirror_item_id)  # the owner deletes the whole day
    w.reset_calls()
    await audit(w)
    assert all(w.doc(i) is None for i in (a, b, c))
    assert day_key not in w.mirror.docs
    assert archive_folder_key(USER, DRIVE_IDENTITY, day, "Videos") not in w.mirror.docs
    assert archive_folder_key(USER, DRIVE_IDENTITY, "", ARCHIVE_NAME) in w.mirror.docs      # the archive itself stays
    assert not [k for k in keys if await w.mirror.pinned(USER, k)]
    assert w.drive.writes() == []


async def test_archived_edit_rewritten_and_rename_moved_back():
    """Owner ruling 2026-10-21 (Δ241), reversing D48's "the owner's edits inside the archive stand": an archived file
    is the bot's; an edit is rewritten from the pinned store entry and a rename moved back, and the pin stays."""
    w = MirrorWorld()
    a = await archived(w)
    node = item_of(w, a)
    place = (w.doc(a).mirror_parent_id, w.doc(a).mirror_name)
    node.data, node.ctag, node.name = b"my own notes", "c-owner", "kept.md"
    w.reset_calls()
    counters = await audit(w)
    assert [(c[0], c[1]["item_id"]) for c in w.drive.writes()] == [("replace_content", node.item_id),
                                                                   ("move", node.item_id)]
    doc = w.doc(a)
    stored_entry = await w.repo.get_transcript(USER, doc.content_key)
    assert node.data == render_archived(stored_entry, doc, w.last_host.texts) and b"my own notes" not in node.data
    assert (node.parent_id, node.name) == place == (doc.mirror_parent_id, doc.mirror_name)
    assert doc.state == MIRROR_ARCHIVED and doc.mirror_ctag == node.ctag != "c-owner"
    assert counters["mirror_archive_rewritten"] == 1 and counters["mirror_moved_back"] == 1
    assert not counters.get("mirror_followed") and not counters.get("mirror_edit_left")
    assert await w.mirror.pinned(USER, doc.content_key)


async def test_archived_moved_inside_the_mirror_moved_back():
    """Owner ruling 2026-10-21 (Δ241): an archived file moved elsewhere inside the mirror goes back to its archive place
    (one PATCH, nothing rewritten: its content was not edited); the document stays archived and pinned."""
    w = MirrorWorld()
    a = await archived(w)
    node = item_of(w, a)
    place = (w.doc(a).mirror_parent_id, w.doc(a).mirror_name)
    node.parent_id = w.root.root_item_id                   # out of the archive, still in the mirror
    w.reset_calls()
    counters = await audit(w)
    assert [(c[0], c[1]["item_id"], c[1]["new_parent_id"]) for c in w.drive.writes()] == [
        ("move", node.item_id, place[0])]
    doc = w.doc(a)
    assert doc.state == MIRROR_ARCHIVED and (node.parent_id, node.name) == place == (doc.mirror_parent_id,
                                                                                     doc.mirror_name)
    assert counters["mirror_moved_back"] == 1 and not counters.get("mirror_followed")
    assert await w.mirror.pinned(USER, doc.content_key)


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
    key = archive_folder_key(USER, DRIVE_IDENTITY, "", LONG_PATHS_NAME)
    lp = w.drive._make_folder(w.root.root_item_id, LONG_PATHS_NAME)
    await w.mirror.save_doc(Doc(USER, DRIVE_IDENTITY, KIND_ARCHIVE, key.rsplit(":", 1)[1], source_name=LONG_PATHS_NAME,
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
    await w.repo.delete_records(SCOPE, [a])
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
    await w.repo.delete_records(SCOPE, [a])                 # no notice: the record carried no digest of ours
    await w.run()
    assert w.doc(a).gone_pending and w.doc(a).gone_why == UNKNOWN


async def test_scan_waits_while_enumerating():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    await w.repo.delete_records(SCOPE, [a])
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
    await w.repo.delete_records(SCOPE, ids)
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
    bad_key = mirror_key(KIND_FILE, bad)
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
    key = archive_folder_key(USER, DRIVE_IDENTITY, "", ARCHIVE_NAME)
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

class Guard:
    """Call-time checks on every Graph write of the step (I2): no planted item is written; nothing is written to, or
    moved out of, an item under a previous root; every create, destination and rewritten file is under the current
    root (RFC §27.12; an item absent from the drive is allowed, its call answers 404 and writes nothing); no archived
    document's item is rewritten, except by the audit back to the render of its pinned store entry; the audit issues
    no Graph delete, moves an archived item only back to its ledger place, and rewrites nothing but the read-me and
    edited archived files (its other repairs go through step 4; owner ruling 2026-10-21, Δ241). A violation raises inside the step, where `_guard` counts it as
    `mirror_errors`; `violations` keeps the message."""

    def __init__(self, w: MirrorWorld) -> None:
        self.w = w
        self.in_audit = False
        self.writes = 0
        self.violations = []
        drive = w.drive
        for name in ("upload", "create_folder", "replace_content", "move", "delete"):
            setattr(drive, name, self._wrap(name, getattr(drive, name)))
        audit_page = w.service._audit_page

        async def audited(ctx):
            self.in_audit = True
            try:
                await audit_page(ctx)
            finally:
                self.in_audit = False

        w.service._audit_page = audited

    def _wrap(self, name, real):
        async def call(user_id, *args, **kw):
            try:
                self.check(name, args, kw)
            except AssertionError as exc:
                self.violations.append(f"{name}: {exc}")
                raise
            self.writes += 1
            return await real(user_id, *args, **kw)
        return call

    def _chain(self, item_id):
        return set(self.w.drive.chain(item_id))

    def _placeable(self, folder_id, current, prev):
        """Under the current root; or absent from the drive (the call answers 404, nothing is written). Never under a
        previous root."""
        chain = self._chain(folder_id)
        if chain & prev:
            return False
        return current in chain or not chain

    def _archived_render(self, doc, data):
        stored_entry = self.w.repo.transcripts.get((USER, doc.content_key))
        if stored_entry is None and doc.transcript_stub:
            stored_entry = StoredTranscript(doc.content_key, None, stub=doc.transcript_stub)
        return stored_entry is not None and data == render_archived(stored_entry, doc, EN)

    def _to_ledger_place(self, doc, item, args, kw):
        node = self.w.drive.nodes.get(item)
        new_parent = kw.get("new_parent_id") if "new_parent_id" in kw else (args[1] if len(args) > 1 else None)
        new_name = kw.get("new_name") if "new_name" in kw else (args[2] if len(args) > 2 else None)
        parent = new_parent or (node.parent_id if node else "")
        name = new_name or (node.name if node else "")
        return (parent, name) == (doc.mirror_parent_id, doc.mirror_name)

    def check(self, name, args, kw):
        w = self.w
        root = w.root
        prev = set(root.previous_root_ids) if root is not None else set()
        current = root.root_item_id if root is not None and root.root_item_id not in prev else None
        if name in ("upload", "create_folder"):
            parent = args[0]
            if parent == AREA:
                assert name == "create_folder" and is_root_name(args[1]), "a create in the App Folder"
                return
            assert parent not in w.drive.planted, "a create inside a planted folder"
            assert self._placeable(parent, current, prev), "a create outside the root"
            return
        item = args[0]
        assert item not in w.drive.planted, f"{name} of a planted item"
        assert not self._chain(item) & prev, f"{name} of an item under a previous root"
        archived = [d for d in w.mirror.docs.values() if d.mirror_item_id == item and d.state == MIRROR_ARCHIVED]
        # Δ243: a detached file keeps its pinned store entry and is rewritten and moved back like an archived one
        pinned_files = archived + [d for d in w.mirror.docs.values() if d.mirror_item_id == item
                                   and d.state == MIRROR_DETACHED and d.kind == KIND_FILE]
        if name == "replace_content":
            # owner ruling 2026-10-21 (Δ241, Δ243): the one rewrite of an archived or detached file allowed is the
            # audit's, of its edit, back to the render of its pinned store entry
            assert not pinned_files or (self.in_audit and self._archived_render(pinned_files[0], args[1])), \
                "an archived file rewritten"
            assert self._placeable(item, current, prev), "a file rewritten outside the root"
            assert not self.in_audit or item == root.readme_item_id or pinned_files, "the audit rewrote a file itself"
        if name == "move":
            # Δ241, Δ243: the audit moves an archived or detached file only back to its own ledger place
            assert not (self.in_audit and pinned_files) or self._to_ledger_place(pinned_files[0], item, args, kw), \
                "the audit moved an archived file"
        if name == "delete":
            assert not self.in_audit, "a Graph delete from the audit"
            assert self._placeable(item, current, prev), "a delete outside the root"
        if name == "move":
            new_parent = kw.get("new_parent_id") if "new_parent_id" in kw else (args[1] if len(args) > 1 else None)
            if new_parent is not None:
                assert self._placeable(new_parent, current, prev), "a move to outside the root"
            else:
                node = w.drive.nodes.get(item)
                assert self._placeable(node.parent_id if node else "", current, prev), "a rename outside the root"


async def observe_mirror_items(w: MirrorWorld) -> None:
    """What the tick's observation leaves in the index before the mirror step (§27.9.8): every folder of the bot's
    ledger has a record (`inside_opaque` under the root, `normal` once the owner moved it out of it); a file of the bot's
    under the root has none, and one the owner moved out of it has an ordinary file's record."""
    if w.root is None:
        return
    root_id = w.root.root_item_id
    gone = []
    for doc in list(w.mirror.docs.values()):
        node = w.drive.nodes.get(doc.mirror_item_id) if doc.mirror_item_id else None
        if node is None:
            continue
        inside = w.drive.under(node.parent_id, root_id)
        if doc.kind != KIND_FILE:
            await w.repo.upsert_records([DriveIndexRecord(
                USER, node.item_id, connection_id=USER, identity=identity(node.item_id), parent_id=node.parent_id, name=node.name, is_folder=True,
                state=RecordState.METADATA_ONLY, reason=Reason.FOLDER,
                scope=Scope.INSIDE_OPAQUE if inside else Scope.NORMAL)])
        elif not inside:
            await w.repo.upsert_records([DriveIndexRecord(
                USER, node.item_id, connection_id=USER, identity=identity(node.item_id), parent_id=node.parent_id, name=node.name, state=RecordState.METADATA_ONLY,
                scope=Scope.NORMAL)])
        else:
            gone.append(node.item_id)
    if gone:
        await w.repo.delete_records(SCOPE, gone)


@pytest.mark.parametrize("seed", range(12))
async def test_audit_never_touches_an_item_it_did_not_create(seed):
    """Random trees; the owner's edits, deletions, renames and moves of the bot's files and folders (also out of the
    mirror); foreign items inside the mirror and in its archive; an owner's `_README.md`; the root moved out and back
    (within the grace, or after a confirmed loss: then a previous root restored); weekly audits under random budgets.
    `Guard` checks every write at call time; no run counts an unexpected error."""
    rnd = random.Random(seed)
    w = MirrorWorld()
    guard = Guard(w)
    folders = [AREA]
    files = []
    root_out = None
    readme_size = len(readme_bytes(EN))
    seen_lost = 0
    ever_roots = set()
    cut_items = {}                                         # root id -> the ids under it when it last left
    for step in range(32):
        roll = rnd.random()
        root = w.root
        if roll < 0.10:
            folders.append(await w.folder(rnd.choice(["A", "B", "Archive", "c"]), rnd.choice(folders)))
        elif roll < 0.30 or not files:
            files.append(await w.recording(rnd.choice(["x", "a.mp4", "b.mp4", "c"]), rnd.choice(folders)))
        elif roll < 0.38:
            f = rnd.choice(files)
            if w.rec(f) is not None:
                await w.delete_source(f, why=rnd.choice([GONE, UNSTORED, UNKNOWN]))
        elif roll < 0.44 and root is not None:
            if root_out is None:                           # the root moved out of the App Folder
                rid = root.root_item_id
                root_out = (rid, [n for n in list(w.drive.nodes.values()) if w.drive.under(n.item_id, rid)])
                cut_items[rid] = {n.item_id for n in root_out[1]}
                for node in root_out[1]:
                    del w.drive.nodes[node.item_id]
                await w.mirror.mark_root_gone(USER, rid, w.clock)
                await w.repo.delete_records(SCOPE, [rid])
            else:                                          # ... and back (a previous root by now, maybe)
                for node in root_out[1]:
                    w.drive.nodes[node.item_id] = node
                if w.root.root_item_id == root_out[0] and rnd.random() < 0.5:
                    await w.mirror.clear_root_gone(USER, root_out[0])      # the tick saw it; else a read clears it
                root_out = None
        elif root is not None and root_out is None:
            root_id = root.root_item_id
            ours = [d.mirror_item_id for d in w.mirror.docs.values()
                    if d.mirror_item_id in w.drive.nodes and d.kind == KIND_FILE]
            mine_folders = [d.mirror_item_id for d in w.mirror.docs.values()
                            if d.kind != KIND_FILE and d.mirror_item_id in w.drive.nodes]
            places = [root_id] + [f for f in mine_folders if w.drive.under(f, root_id)]
            act = rnd.choice(["edit", "delete", "rename", "move", "move_out", "foreign", "foreign_folder",
                              "folder_delete", "folder_move", "readme"])
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
            elif act in ("folder_delete", "folder_move") and len(places) > 1:
                folder = w.drive.nodes[rnd.choice(places[1:])]
                if act == "folder_delete":
                    w.drive.remove(folder.item_id)
                else:
                    target = rnd.choice([AREA] + [p for p in places if not w.drive.under(p, folder.item_id)])
                    folder.parent_id = target
            elif act == "foreign":
                w.drive.add_file(rnd.choice(places), rnd.choice(["a.mp4.md", "notes.txt", "x.md"]), b"owner")
            elif act == "foreign_folder":
                w.drive.add_folder(rnd.choice(places), rnd.choice(["Mine", "x.md"]))
            elif act == "readme":                          # the owner replaces the read-me with his own, same size
                if root.readme_item_id in w.drive.nodes:
                    w.drive.remove(root.readme_item_id)
                if w.drive.child(root_id, README_NAME) is None:
                    w.drive.add_file(root_id, README_NAME, b"o" * readme_size)
        w.advance(rnd.choice([HOUR, HOUR, 4 * HOUR]) if root_out is not None else rnd.choice([HOUR, DAY, 4 * DAY, WEEK]))
        await observe_mirror_items(w)
        try:
            counters = await w.run(ops=rnd.choice([5, 40]), reads=rnd.choice([3, 10]))
        except Crash:
            continue
        seen_lost += counters.get("mirror_root_lost", 0)
        assert not guard.violations, guard.violations
        if w.root is not None:
            ever_roots.add(w.root.root_item_id)
            gone_roots = ever_roots - {w.root.root_item_id}
            assert gone_roots <= set(w.root.previous_root_ids), "a replaced root that is not a previous root"
            if gone_roots or w.root.root_item_id in w.root.previous_root_ids:
                lost = set().union(*(cut_items.get(r, set()) for r in gone_roots | {w.root.root_item_id}
                                     if r in w.root.previous_root_ids))
                stale = {i for d in w.mirror.docs.values() for i in (d.mirror_item_id, d.mirror_parent_id)} & lost
                if w.root.root_item_id not in w.root.previous_root_ids:          # the reset is complete
                    assert not stale, "a ledger document still points under a previous root"
        assert not counters.get("mirror_errors"), counters
    assert w.root is not None and guard.writes


# -- fix round 1 ------------------------------------------------------------------------------------------------------

async def test_readme_name_held_by_an_owner_file_never_written():
    """I1: an owner's `_README.md` of the read-me's very size, created long after the root, is never adopted: the
    bot adopts a file at that name only after an intent of its own (`readme_pending_at`)."""
    w = MirrorWorld()
    await mirrored(w, "a.mp4")
    w.drive.remove(w.root.readme_item_id)                  # the owner deletes the bot's read-me ...
    w.advance(3 * DAY)
    owner = w.drive.add_file(w.root.root_item_id, README_NAME, b"x" * len(readme_bytes(EN)))   # ... and writes his
    w.reset_calls()
    await audit(w)
    assert w.root.readme_item_id == "" and w.root.readme_pending_at is None
    node = w.drive.nodes[owner]
    node.data, node.ctag = b"y" * len(readme_bytes(EN)), "c-owner"                              # and edits it later
    for _ in range(2):
        await audit(w)
    assert owner not in touched_ids(w) and w.drive.nodes[owner].data == b"y" * len(readme_bytes(EN))
    assert not [c for c in w.drive.writes() if c[1].get("name") == README_NAME]


async def test_readme_intent_written_before_the_upload_and_cleared():
    w = MirrorWorld()
    await w.recording("a.mp4")
    real = w.mirror.save_root

    async def save_root(root):
        w.events.append(("root saved", root))
        await real(root)

    w.mirror.save_root = save_root
    await w.run()
    upload = w.events.index(("drive.upload", README_NAME))
    saved_before = [e[1] for e in w.events[:upload] if e[0] == "root saved"]
    assert saved_before and saved_before[-1].readme_pending_at == START          # the intent, right before the upload
    assert w.root.readme_item_id and w.root.readme_pending_at is None


async def test_readme_case_only_rename_kept():
    w = MirrorWorld()
    await mirrored(w, "a.mp4")
    readme = w.root.readme_item_id
    w.drive.nodes[readme].name = "_readme.MD"              # a case-only rename by the owner
    w.reset_calls()
    for _ in range(2):
        await audit(w)
    assert w.root.readme_item_id == readme and w.drive.writes() == []


async def test_a_doc_stamped_in_the_tick_that_ends_a_cycle_is_still_checked_next_cycle():
    """Minor 1: the clock is fixed per tick, so a document stamped in the tick that ends a cycle carries
    `audit_seen_at == audited_at`; the next cycle must still confirm it when no listing shows it."""
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    await audit(w)
    assert w.doc(a).audit_seen_at == w.root.audited_at
    w.drive.remove(w.doc(a).mirror_item_id)
    counters = await audit(w)
    assert counters["mirror_recreate_due"] == 1


async def test_blocked_move_back_of_an_active_folder_makes_its_record_due():
    w = MirrorWorld()
    box = await w.folder("Box")
    (a,) = await mirrored(w, "a.mp4", parent=box)
    folder = w.drive.nodes[w.folder_doc(box).mirror_item_id]
    folder.parent_id = AREA                                # the owner moves the mirror folder out ...
    w.drive.add_folder(w.root.root_item_id, "Box")         # ... and puts his own folder at its name
    counters = await audit(w)
    assert counters["mirror_move_back_blocked"] == 1 and w.rec(box).mirror_due
    await w.run()
    assert w.drive.under(folder.item_id, w.root.root_item_id)              # back under the root, suffixed
    assert w.mirror_path(a).startswith("Box~") and w.mirror_path(a).endswith("/a.mp4.md")


@pytest.mark.parametrize("answer", ["found", "error"])
async def test_folder_listing_404_confirmed_before_a_discard(answer):
    """Minor 5: a listing that answers 404 discards nothing until one read of the folder answers not found; a found
    folder or a failed read leaves it (and its archived documents and pins) for the next cycle."""
    from src.domain.user_drive import DriveItemNotFoundError
    w = MirrorWorld()
    a = await archived(w)
    day = w.doc(a).mirror_parent_id
    key = w.doc(a).content_key
    w.drive.fail("list_children_changes", DriveItemNotFoundError("listing"), lambda args: args["item_id"] == day)
    if answer == "error":
        w.drive.fail("get_change", ValueError("Graph 503"), lambda args: args["item_id"] == day)
    counters = await audit(w)
    assert w.doc(a) is not None and w.doc(a).state == MIRROR_ARCHIVED and await w.mirror.pinned(USER, key)
    assert not counters.get("mirror_audit_folder_gone")
    if answer == "found":
        assert counters["mirror_folder_404_unconfirmed"] == 1
    else:
        assert counters["mirror_folder_read_failed"] == 1


# -- fix round 2 ------------------------------------------------------------------------------------------------------

def folder_record(w: MirrorWorld, item_id: str, parent: str, scope: Scope, name: str = "Box"):
    """The index record the tick's observation leaves for a folder of the mirror (§27.9.8)."""
    return w.repo.upsert_records([DriveIndexRecord(USER, item_id, connection_id=USER, identity=identity(item_id), parent_id=parent, name=name, is_folder=True,
                                                   state=RecordState.METADATA_ONLY, reason=Reason.FOLDER, scope=scope)])


def writes_outside(w: MirrorWorld, root_id: str):
    """Creates into, and move destinations or rewrites of, anything whose chain (at the call) misses the root."""
    out = []
    for method, chains, created in w.drive.write_parents:
        if method == "create_folder" and chains == ((AREA,),):
            continue                                       # the root's own creation
        if method in ("upload", "create_folder", "replace_content") and not all(root_id in c for c in chains):
            out.append(method)
        if method == "move" and len(chains) == 2 and root_id not in chains[1]:
            out.append(method)
        if method == "move" and len(chains) == 1 and root_id not in chains[0]:
            out.append(method)                             # a rename in place: where the item is at the call
    return out


async def test_moved_out_mirror_folder_gets_no_write_and_is_moved_back():
    """Answer 3 (RFC §27.12): a mirror folder the owner moved out of `_Transcripts` receives no write; one read of it
    confirms it is outside, and it is moved back before step 4 writes into it."""
    w = MirrorWorld()
    box = await w.folder("Box")
    (a,) = await mirrored(w, "a.mp4", parent=box)
    mirror_box = w.folder_doc(box).mirror_item_id
    w.drive.nodes[mirror_box].parent_id = AREA             # moved out by the owner ...
    await folder_record(w, mirror_box, AREA, Scope.NORMAL)  # ... and observed so by the tick before the step
    b = await w.recording("b.mp4", box)
    w.reset_calls()
    counters = await w.run()
    assert counters["mirror_outside_found"] == 1 and counters["mirror_moved_back"] == 1
    assert [c for c in w.drive.calls if c[0] == "get_change" and c[1]["item_id"] == mirror_box]
    assert writes_outside(w, w.root.root_item_id) == []
    assert w.drive.nodes[mirror_box].parent_id == w.root.root_item_id
    assert w.mirror_path(b) == "Box/b.mp4.md" and w.mirror_path(a) == "Box/a.mp4.md"


async def test_moved_out_folder_whose_way_back_is_blocked_waits_uncharged():
    w = MirrorWorld()
    box = await w.folder("Box")
    await mirrored(w, "a.mp4", parent=box)
    mirror_box = w.folder_doc(box).mirror_item_id
    w.drive.nodes[mirror_box].parent_id = AREA
    await folder_record(w, mirror_box, AREA, Scope.NORMAL)
    w.drive.add_folder(w.root.root_item_id, "Box")         # the owner's folder now holds the name
    b = await w.recording("b.mp4", box)
    w.reset_calls()
    counters = await w.run()
    assert writes_outside(w, w.root.root_item_id) == []
    assert not counters.get("mirror_charged") and not counters.get("mirror_errors")
    assert w.rec(b).mirror_due or w.mirror_path(b) is not None


async def test_file_moved_out_is_moved_back_before_it_is_rewritten():
    w = MirrorWorld()
    (a,) = await mirrored(w, "a.mp4")
    item = w.doc(a).mirror_item_id
    w.drive.nodes[item].parent_id = AREA                   # the owner takes our file out of the mirror
    await w.repo.upsert_records([DriveIndexRecord(USER, item, connection_id=USER, identity=identity(item), parent_id=AREA, name="a.mp4.md",
                                                  state=RecordState.METADATA_ONLY, scope=Scope.NORMAL)])
    await w.repo.put_transcript(USER, stored(w.rec(a).transcript_key, "a new version"))
    await w.update(a, transcript_version=2)
    w.reset_calls()
    await w.run()
    assert writes_outside(w, w.root.root_item_id) == []
    assert w.drive.nodes[item].parent_id == w.root.root_item_id and "a new version" in w.content(a)


async def test_stale_record_with_an_inside_read_writes_normally():
    w = MirrorWorld()
    box = await w.folder("Box")
    await mirrored(w, "a.mp4", parent=box)
    mirror_box = w.folder_doc(box).mirror_item_id
    await folder_record(w, mirror_box, AREA, Scope.NORMAL)  # a stale record: the folder is inside on the drive
    b = await w.recording("b.mp4", box)
    w.reset_calls()
    counters = await w.run()
    assert counters["mirror_outside_stale"] == 1 and not counters.get("mirror_moved_back")
    assert len([c for c in w.drive.calls if c[0] == "get_change" and c[1]["item_id"] == mirror_box]) == 1
    assert w.mirror_path(b) == "Box/b.mp4.md"
    assert [c[0] for c in w.drive.writes()] == ["upload"]


async def test_missing_link_does_not_stall():
    w = MirrorWorld()
    box = await w.folder("Box")
    inner = await w.folder("Inner", box)
    await mirrored(w, "a.mp4", parent=inner)
    mirror_inner = w.folder_doc(inner).mirror_item_id
    # the inner folder's record points at a parent the index has no record of; the outer one has none at all
    await folder_record(w, mirror_inner, "not-observed-yet", Scope.INSIDE_OPAQUE, name="Inner")
    b = await w.recording("b.mp4", inner)
    w.reset_calls()
    await w.run()
    assert w.mirror_path(b) == "Box/Inner/b.mp4.md"
    assert not [c for c in w.drive.calls if c[0] == "get_change"]


async def test_folder_404_confirmed_with_one_read_a_tick():
    """A 404 saves its own cursor, so a tick with one read spends it on the confirming read, never on listing the
    folder again."""
    w = MirrorWorld()
    a = await archived(w)
    day = w.doc(a).mirror_parent_id
    key = archive_folder_key(USER, DRIVE_IDENTITY, w.doc(a).archive_day, "")
    from src.domain.user_drive import DriveItemNotFoundError
    for method in ("list_children_changes", "get_change"):   # gone by its own reads; still listed by its parent
        w.drive.fail(method, DriveItemNotFoundError("gone"), lambda args: args["item_id"] == day, times=99)
    w.advance(WEEK + HOUR)
    for _ in range(30):
        await w.run(reads=1)
        if w.doc(a) is None:
            break
    assert w.doc(a) is None and key not in w.mirror.docs
    assert len([c for c in w.drive.calls if c[0] == "list_children_changes" and c[1]["item_id"] == day]) == 1


async def test_foreign_readme_read_only_at_the_audit_cadence():
    w = MirrorWorld()
    await mirrored(w, "a.mp4")
    w.drive.remove(w.root.readme_item_id)
    owner = w.drive.add_file(w.root.root_item_id, README_NAME, b"mine")
    await audit(w)                                         # forgets the missing read-me, finds the owner's file
    assert w.root.readme_blocked_at == w.clock and w.root.readme_item_id == ""

    def readme_reads():
        return [c for c in w.drive.calls if c[0] == "child_by_name" and c[1]["name"] == README_NAME]

    for _ in range(5):
        w.advance(DAY)
        w.reset_calls()
        await w.run()
        assert readme_reads() == []                        # a normal tick costs no read for it
    w.drive.remove(owner)                                  # the owner gives the name back
    w.advance(2 * DAY + HOUR)
    w.reset_calls()
    await w.run()
    assert len(readme_reads()) == 1 and w.root.readme_item_id and w.root.readme_blocked_at is None
    assert owner not in touched_ids(w)


async def test_sibling_gone_keeps_the_deletion_notice():
    """A sibling whose file is gone is made due with `due_again`, not an empty digest, so a later delete of its record
    still sends the mirror its notice (the tick keys notices on `mirrored_digest`)."""
    from src.domain.drive_mirror_plan import REDO_DIGEST
    w = MirrorWorld()
    (x,) = await mirrored(w, "X")
    w.drive.remove(w.doc(x).mirror_item_id)                # our X.md is gone ...
    folder = await w.folder("X.md")                        # ... and a folder of its name makes it yield
    await w.recording("in.mp4", folder)
    w.reset_calls()
    await w.run(ops=2)
    rec = w.rec(x)
    assert rec.mirrored_digest == REDO_DIGEST and rec.mirror_due
    await w.run()
    assert w.mirror_path(x) is not None and w.rec(x).mirrored_digest not in ("", REDO_DIGEST)


async def test_move_into_a_moved_out_folder_goes_only_after_it_is_back():
    w = MirrorWorld()
    box = await w.folder("Box")
    (b,) = await mirrored(w, "b.mp4", parent=box)
    (a,) = await mirrored(w, "a.mp4")
    mirror_box = w.folder_doc(box).mirror_item_id
    w.drive.nodes[mirror_box].parent_id = AREA             # the owner moves the mirror folder out
    await folder_record(w, mirror_box, AREA, Scope.NORMAL)
    await w.update(a, parent_id=box)                       # and the source of a.mp4 into its source folder
    w.reset_calls()
    await w.run()
    assert writes_outside(w, w.root.root_item_id) == []
    assert w.mirror_path(a) == "Box/a.mp4.md" and w.drive.nodes[mirror_box].parent_id == w.root.root_item_id
