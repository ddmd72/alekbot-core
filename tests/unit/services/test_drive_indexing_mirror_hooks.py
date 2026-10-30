"""The tick's hooks for the transcript mirror (USER_DRIVE_INDEXING_RFC §27.9.2, §27.9.4, §27.9.8, §27.9.9; plan Task 20B;
Δ184, Δ187, Δ188, Δ203, Δ205, Δ208): the roots read once at the tick's start and kept opaque, `mirror_plan` in `_commit`,
the why of every delete and the durable marks written before the record deletes. The real service on the in-memory
repositories; the mirror's own writes (a stamped record, a ledger document, a root document) are set directly, as the
mirror step (Task 21) would leave them. Synthetic items only."""
from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import replace
from datetime import timedelta
from typing import Any, List

import pytest

from src.domain import drive_index_planner as planner
from src.domain.drive_index import (
    DriveChange, DriveIndexRecord, DriveIndexStoreUnavailable, Priority, RecordState, Scope,
)
from src.domain.drive_index_params import ACCESS_LOSS_TTL, MIRROR_ARCHIVE_GRACE
from src.domain.drive_mirror import GONE, KIND_FOLDER, UNKNOWN, UNSTORED
from src.domain.drive_mirror_plan import mirror_digest, mirror_plan
from src.domain.user_drive import DriveConnection, DriveItemNotFoundError
from src.services import drive_indexing_service as svc_mod

from .drive_content_world import admitted, prose  # noqa: F401  (fixture)
from .drive_mirror_tick_world import DRIVE, MirrorWorld

pytestmark = pytest.mark.usefixtures("admitted")

MD_NAMES = ("Interview.mp4.md", "Call.m4a.md", "_README.md")


@pytest.fixture
def world(tmp_path):
    def make(**kw: Any) -> MirrorWorld:
        return MirrorWorld(tmp_path, **kw)
    return make


def _no_missing_why(w: MirrorWorld) -> None:
    assert [c for c in w.commits if c[1] is None] == [], "a _commit with deletes passed no why"


async def _with_root(world, **kw):
    """An onboarded user with a mirror root holding an active tree, an archive and a read-me, plus one ordinary doc."""
    w = world(**kw)
    root = w.add_root()
    videos = w.drive.add("Videos", parent=root, folder=True)
    archive = w.drive.add("Archive", parent=root, folder=True)
    day = w.drive.add("2026-10-21", parent=archive, folder=True)
    mds = [w.drive.add_file("_README.md", b"managed", parent=root),
           w.drive.add_file("Interview.mp4.md", prose(3).encode(), parent=videos),
           w.drive.add_file("Call.m4a.md", prose(3).encode(), parent=day)]
    doc = w.add_doc("notes.md", prose(5))
    await w.settle()
    return w, root, (videos, archive, day), mds, doc


# -- the roots and classification (§27.9.2, §27.9.8, Δ205) -------------------------------------------------------------

async def test_files_under_root_get_no_record_and_folders_no_card(world):
    w, root, folders, mds, doc = await _with_root(world)
    rec = w.rec(root)
    assert rec is not None and rec.scope is Scope.INSIDE_OPAQUE and not rec.has_card
    for md in mds:
        assert w.rec(md) is None
    for folder in folders:
        f = w.rec(folder)
        assert f is not None and f.scope is Scope.INSIDE_OPAQUE and not f.has_card
    assert set(w.card_texts()) == {doc}
    assert w.rec(doc).state is RecordState.INDEXED


def _purge_rebuild(w):
    w.repo.states["u"] = replace(w.repo.states["u"], purging=True, purge_keep_transcripts=True)


@pytest.mark.parametrize("purge", ["rebuild", "opt_out", "access_loss_ttl"])
async def test_root_opaque_on_the_first_enumeration_after_a_purge(world, purge):
    """No purge deletes the root document (Δ184), and the tick reads it before the first enumeration after the purge,
    so no `.md` file of the mirror is ever written as a record, not even held unresolved."""
    w, root, _, mds, doc = await _with_root(world)
    before = w.root_doc()
    mark = len(w.events)
    if purge == "rebuild":
        _purge_rebuild(w)
        await w.settle()
    elif purge == "opt_out":
        w.users.indexing = False
        await w.settle()
        assert w.repo.states.get("u") is None
        w.users.indexing = True
        await w.settle()
    else:
        w.drive.connection_state = DriveConnection(connected=False)
        await w.tick()
        w.clock.advance(ACCESS_LOSS_TTL.total_seconds() + 60)
        await w.settle()
        assert w.repo.states.get("u") is None
        w.drive.connection_state = DriveConnection(connected=True, drive_id=DRIVE)
        await w.settle()
    written = {item for name, detail in w.events[mark:] if name == "upsert_records" for item, _ in detail}
    assert not written & set(mds), "a mirror file was written as a record after the purge"
    assert w.root_doc() == before and len(w.mirror.root_docs) == 1
    assert w.rec(root).scope is Scope.INSIDE_OPAQUE
    assert w.rec(doc) is not None and w.rec(doc).state is RecordState.INDEXED
    assert all(w.rec(md) is None for md in mds)


async def test_rebuild_then_onboarding_keeps_the_root(world):
    w, root, _, _, _ = await _with_root(world)
    before = w.root_doc()
    _purge_rebuild(w)
    await w.settle()
    assert w.mirror.writes() == []   # the purge and the onboarding wrote nothing of the mirror
    assert w.root_doc() == before and list(w.mirror.root_docs) == [("u", DRIVE)]
    assert w.records_under(root) and all(w.rec(i).scope is Scope.INSIDE_OPAQUE for i in w.records_under(root))


async def test_roots_read_error_ends_the_tick(world):
    """A read error on the roots is systemic: the tick ends before any observation (never an enumeration with no root
    ids) and backs off; the next tick reads them again."""
    w = world()
    root = w.add_root()
    w.drive.add_file("Interview.mp4.md", b"x", parent=root)
    pages: List[Any] = []
    real = w.drive.get_changes

    async def counting(user_id, cursor):
        pages.append(cursor)
        return await real(user_id, cursor)
    w.drive.get_changes = counting
    w.mirror.fail_roots = DriveIndexStoreUnavailable("roots unavailable")
    result = await w.tick()
    assert result.status == "error"
    assert pages == [] and w.repo.records == {}
    assert w.repo.states["u"].systemic_failures == 1
    await w.settle()
    assert w.rec(root).scope is Scope.INSIDE_OPAQUE and len(w.repo.records) == 1


async def test_empty_drive_id_skips_the_mirror_but_classifies(world):
    """No connected drive id and none on the state: the tick's mirror has no drive id (the step of Task 21 does not
    run), yet the roots still classify; once the state carries the drive id, it is the fallback."""
    w = world()
    w.drive.connection_state = DriveConnection(connected=True, drive_id="")
    root = w.add_root()
    w.drive.add_file("Interview.mp4.md", b"x", parent=root)
    seen: List[Any] = []
    real = w.svc._start_mirror

    async def capture(*a: Any, **kw: Any):
        mirror = await real(*a, **kw)
        seen.append(mirror)
        return mirror
    w.svc._start_mirror = capture
    await w.tick()
    assert seen[0] is not None and seen[0].drive_id == "" and root in seen[0].root_ids
    assert w.rec(root).scope is Scope.INSIDE_OPAQUE and len(w.repo.records) == 1
    await w.tick()
    assert seen[-1].drive_id == DRIVE   # the sync state's drive id (from the pages)
    w.drive.connection_state = DriveConnection(connected=True, drive_id=DRIVE)
    await w.tick()
    assert seen[-1].drive_id == DRIVE


@pytest.mark.parametrize("switch,flag", [(False, True), (True, False), (False, False)])
async def test_classification_not_gated_by_the_switches(world, switch, flag):
    w, root, folders, mds, _ = await _with_root(world, switch=switch, flag=flag)
    assert w.rec(root).scope is Scope.INSIDE_OPAQUE
    assert all(w.rec(md) is None for md in mds)
    assert "roots" in w.mirror.calls


async def test_previous_root_stays_opaque(world):
    w = world()
    old = w.drive.add("_Transcripts", folder=True)
    w.add_root("_Transcripts (Alek)", previous=(old,))
    w.drive.add_file("Old.mp4.md", b"x", parent=old)
    await w.settle()
    assert w.rec(old).scope is Scope.INSIDE_OPAQUE and len(w.records_under(old)) == 0


async def test_another_drives_root_document_harmless(world):
    """A dormant root document of another drive (a drive switch) adds its ids to the union; no item here matches."""
    w = world()
    w.add_root(drive="old-drive", root_item_id="old-root-id")
    doc = w.add_doc("notes.md", prose(5))
    folder = w.drive.add("Videos", folder=True)
    await w.settle()
    assert w.rec(doc).state is RecordState.INDEXED and w.rec(folder).scope is Scope.NORMAL


async def test_one_roots_query_per_tick_and_nothing_else_without_a_root(world):
    w = world()
    doc = w.add_doc("notes.md", prose(5))
    folder = w.drive.add("Folder", folder=True)
    kid = w.add_doc("kid.md", prose(4), parent=folder)
    await w.settle()
    w.mirrored(doc)   # even a record that looks mirrored: no root, no mark
    w.drive.delete(doc)
    w.drive.delete(folder)
    await w.settle()
    assert w.rec(doc) is None and w.rec(kid) is None
    ran = [r for r in w.results if r.status in ("done", "error", "throttled", "access_lost")]
    assert set(w.mirror.calls) == {"roots"}
    assert w.mirror.calls.count("roots") == len(ran)


async def test_no_mirror_repository_no_mirror_call(world):
    w = world(wired=False)
    doc = w.add_doc("notes.md", prose(5))
    await w.settle()
    w.drive.delete(doc)
    await w.settle()
    assert w.mirror.calls == []


async def test_a_large_root_is_never_probed(world, monkeypatch):
    """The root is opaque whatever its density: no density probe of it (one item call saved)."""
    w = world()
    root = w.add_root()
    w.drive.items[root] = replace(w.drive.items[root], child_count=5_000)
    w.drive.log.append(w.drive.items[root])
    await w.settle()
    assert root not in w.drive.children_calls
    assert w.rec(root).scope is Scope.INSIDE_OPAQUE and not w.rec(root).probe_pending


# -- the mirror plan in _commit (§27.9.4) -------------------------------------------------------------------------------

async def test_commit_runs_the_mirror_plan(world):
    w = world()
    w.add_root()
    audio = w.add_recording()
    doc = w.add_doc("notes.md", prose(5))
    await w.to_current(audio)
    rec = w.rec(audio)
    assert rec.transcript_state == "current" and rec.mirror_due and rec.mirrored_digest == ""
    assert not w.rec(doc).mirror_due   # not a recording with a current transcript
    w.mirrored(audio)   # the mirror wrote the file and stamped the record
    w.drive.rename(audio, "memo renamed.m4a")
    await w.settle()
    rec = w.rec(audio)
    assert rec.name == "memo renamed.m4a" and rec.mirror_due   # the rename changed the digest: due again
    w.repo.records[("u", audio)] = replace(rec, mirrored_digest=mirror_digest(rec), mirror_due=False)
    w.drive.reemit(audio)
    await w.settle()
    assert not w.rec(audio).mirror_due


async def test_mirror_plan_runs_without_a_mirror_repository(world):
    """The plan is pure and runs on every write (§27.9.4): the flag is set whether or not the mirror is wired."""
    w = world(wired=False)
    audio = w.add_recording()
    await w.to_current(audio)
    assert w.rec(audio).mirror_due


async def test_folder_with_a_mirror_counterpart_due_on_rename(world):
    w = world()
    w.add_root()
    folder = w.drive.add("Videos", folder=True)
    await w.settle()
    assert not w.rec(folder).mirror_due   # no counterpart yet: never due
    w.mirrored(folder)
    w.drive.rename(folder, "Films")
    await w.settle()
    assert w.rec(folder).mirror_due


# -- deletion notices and marks (§27.9.4, Δ188, Δ208) --------------------------------------------------------------------

async def _mirrored_doc(world, **kw):
    w = world(**kw)
    w.add_root()
    doc = w.add_doc("notes.md", prose(5))
    await w.settle()
    w.mirrored(doc)
    return w, doc


async def test_tombstone_marks_gone(world):
    w, doc = await _mirrored_doc(world)
    w.drive.delete(doc)
    now = w.clock.now
    await w.settle()
    assert w.rec(doc) is None
    mark = w.doc(doc)
    assert mark.gone_pending and mark.gone_why == GONE
    assert now <= mark.gone_at and mark.mark_ready_at == mark.gone_at + MIRROR_ARCHIVE_GRACE
    assert ("mark_gone", ((doc, GONE),), DRIVE) in w.mirror.marks
    _no_missing_why(w)


async def test_marks_written_before_the_record_deletes(world):
    w, doc = await _mirrored_doc(world)
    w.drive.delete(doc)
    start = len(w.events)
    await w.settle()
    names = [(n, d) for n, d in w.events[start:]]
    mark_at = next(i for i, (n, d) in enumerate(names) if n == "mark_gone" and (doc, GONE) in d)
    delete_at = next(i for i, (n, d) in enumerate(names) if n == "delete_records" and doc in d)
    assert mark_at < delete_at


async def test_a_failed_mark_deletes_nothing(world):
    """The marks are written before the record deletes behind the same fence: a mark that fails leaves the record (the
    page is applied again), never a deleted record without its mark."""
    w, doc = await _mirrored_doc(world)
    real = w.mirror.mark_gone
    fails = {"n": 1}

    async def failing(*a: Any, **kw: Any):
        if fails["n"]:
            fails["n"] -= 1
            raise DriveIndexStoreUnavailable("ledger unavailable")
        return await real(*a, **kw)
    w.mirror.mark_gone = failing
    w.drive.delete(doc)
    result = await w.tick()
    assert result.status == "error" and w.rec(doc) is not None
    await w.settle()
    assert w.rec(doc) is None and w.doc(doc).gone_pending and w.doc(doc).gone_why == GONE


async def test_not_stored_delete_marks_unstored(world):
    """A file moved into a dot-folder is no longer stored but still on the drive: `unstored`."""
    w, doc = await _mirrored_doc(world)
    hidden = w.drive.add(".private", folder=True)
    await w.settle()
    w.drive.move(doc, hidden)
    await w.settle()
    assert w.rec(doc) is None
    assert w.doc(doc).gone_pending and w.doc(doc).gone_why == UNSTORED
    _no_missing_why(w)


async def test_cascade_marks_gone(world):
    """A folder's tombstone alone: the cascade deletes its files (`gone`), then the folder (`gone`, its folder document
    marked too)."""
    w = world()
    w.add_root()
    folder = w.drive.add("Videos", folder=True)
    doc = w.add_doc("notes.md", prose(5), parent=folder)
    await w.settle()
    w.mirrored(doc)
    w.mirrored(folder)
    w.tombstone_only(folder)
    await w.settle()
    assert w.rec(doc) is None and w.rec(folder) is None
    assert w.doc(doc).gone_why == GONE and w.doc(folder, KIND_FOLDER).gone_why == GONE
    assert not any(c.item_id == doc and c.deleted for c in w.drive.log), "the file had no tombstone of its own"
    _no_missing_why(w)


async def test_rescope_marks_unstored(world):
    """A marker child closes the folder: the rescope cascade deletes its files as not stored (`unstored`)."""
    w = world()
    w.add_root()
    folder = w.drive.add("project", folder=True)
    doc = w.add_doc("notes.md", prose(5), parent=folder)
    await w.settle()
    w.mirrored(doc)
    w.drive.add_file("pyvenv.cfg", b"home = /usr", parent=folder)
    await w.settle()
    assert w.rec(folder).scope is Scope.OPAQUE_ROOT and w.rec(doc) is None
    assert w.doc(doc).gone_why == UNSTORED
    _no_missing_why(w)


async def test_sweep_marks_gone(world):
    """An item a full enumeration no longer lists is swept (§7.5): `gone`."""
    w, doc = await _mirrored_doc(world)
    w.silent_drop(doc)
    w.drive.resync_next = True
    w.drive.add_file("later.md", prose(2).encode())   # a change after the cursor, so the delta read resyncs
    await w.settle()
    assert w.rec(doc) is None and w.doc(doc).gone_why == GONE
    _no_missing_why(w)


async def _seed_unresolved(w, item_id, parent_id, name="memo.pdf"):
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id=item_id, parent_id=parent_id, name=name, kind="pdf",
                           scope=Scope.UNRESOLVED, state=RecordState.UNRESOLVED, policy_version=planner.POLICY_VERSION,
                           seen_epoch=w.repo.states["u"].epoch, due_at=w.clock.now, priority=int(Priority.CASCADE))
    await w.repo.upsert_records([rec])


async def test_orphan_removal_marks_gone(world):
    """An unresolved file whose parent is definitely not found is removed (Δ29): `gone`."""
    w = world()
    w.add_root()
    w.add_doc("anchor.md", prose(3))
    await w.settle()
    await _seed_unresolved(w, "orphan", "ghost")
    w.mirrored("orphan")
    await w.settle()
    assert w.rec("orphan") is None and w.doc("orphan").gone_why == GONE
    _no_missing_why(w)


async def test_replans_mark_unstored(world):
    """A re-plan that deletes (`_commit_plan` from the resolve step; the upgrade pass; `_reread`) is `unstored`."""
    w = world()
    w.add_root()
    hidden = w.drive.add(".private", folder=True)
    folder = w.drive.add("Work", folder=True)
    doc = w.add_doc("old.md", prose(5), parent=folder)
    await w.settle()
    # the resolve step: a parked file whose parent is now stored opaque
    await _seed_unresolved(w, "parked", hidden)
    w.drive.items["parked"] = DriveChange(item_id="parked", name="memo.pdf", parent_id=hidden)
    w.mirrored("parked")
    await w.settle()
    assert w.rec("parked") is None and w.doc("parked").gone_why == UNSTORED
    # the upgrade pass: a stale record re-planned under a folder that is opaque by now
    w.mirrored(doc)
    w.repo.records[("u", folder)] = replace(w.rec(folder), scope=Scope.OPAQUE_ROOT,
                                            reason=planner.Reason.OPAQUE_ROOT)
    w.repo.records[("u", doc)] = replace(w.rec(doc), policy_version=2)
    state = w.repo.states["u"]
    w.repo.states["u"] = replace(state, applied_versions={**state.applied_versions, "policy": 2})
    w.clock.advance(timedelta(days=1).total_seconds())
    await w.settle()
    assert w.rec(doc) is None and w.doc(doc).gone_why == UNSTORED
    _no_missing_why(w)


async def test_reread_marks_unstored(world):
    w = world()
    w.add_root()
    hidden = w.drive.add(".private", folder=True)
    await w.settle()
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="x", parent_id=hidden, name="memo.pdf", kind="pdf",
                           state=RecordState.INDEXED, scope=Scope.NORMAL, policy_version=planner.POLICY_VERSION)
    await w.repo.upsert_records([rec])
    w.mirrored("x")
    token = svc_mod._TICK_MIRROR.set(await w.svc._start_mirror("u", w.repo.states["u"], None))
    try:
        await w.svc._reread("u", w.rec("x"), w.svc._ctx("u", w.repo.states["u"], w.clock.now))
    finally:
        svc_mod._TICK_MIRROR.reset(token)
    assert w.rec("x") is None and w.doc("x").gone_why == UNSTORED
    assert w.commits[-1][1] == {"x": UNSTORED}


async def test_reupsert_in_the_same_page_drops_the_notice(world):
    """A delete followed by the same item again in one page (`_fold` → `deletes.discard`) leaves no notice."""
    w, doc = await _mirrored_doc(world)
    kept = w.drive.items[doc]
    w.drive.delete(doc)
    w.drive.items[doc] = kept
    w.drive.log.append(kept)
    await w.settle()
    assert w.rec(doc) is not None and not w.doc(doc).gone_pending
    assert not any(m[0] == "mark_gone" for m in w.mirror.marks)


async def test_inaccessible_source_is_never_marked(world):
    """A download that answers 404, and again by id: the record is marked inaccessible, never deleted (§26.2, Δ48), so no
    deletion notice."""
    w, doc = await _mirrored_doc(world)
    w.drive.edit(doc, prose(7).encode(), modified_at=w.clock.now - timedelta(hours=2))
    w.drive.download_errors[doc] = DriveItemNotFoundError(doc)
    real = w.drive.get_change

    async def gone_by_id(user_id, item_id):
        if item_id == doc:
            raise DriveItemNotFoundError(item_id)
        return await real(user_id, item_id)
    w.drive.get_change = gone_by_id
    await w.settle()
    rec = w.rec(doc)
    assert rec is not None and rec.inaccessible_since is not None
    assert not w.doc(doc).gone_pending and not any(m[0] == "mark_gone" for m in w.mirror.marks)


async def test_abandon_run_changes_no_digest_field(world):
    """`_abandon_run` writes outside `_commit` (§27.9.4): it changes no field the mirror digest reads, and its write runs
    the mirror plan, so the queue flag is the one the plan gives the run's record (never stale)."""
    w = world()
    audio = w.add_recording()
    await w.to_current(audio)
    run = replace(w.rec(audio), mirrored_digest="d" * 32, mirror_held_digest="h" * 32,
                  mirror_retry_at=w.clock.now + timedelta(hours=1), mirror_due=False,
                  attempt_started_at=w.clock.now, index_generation=3)
    await w.svc._abandon_run("u", run, None, w.clock.now)
    back = w.rec(audio)
    assert mirror_digest(back) == mirror_digest(run)
    planned = mirror_plan(run)
    assert back.mirror_due and planned.mirror_due   # a stale flag (False on a due record) is replanned
    assert (back.mirrored_digest, back.mirror_held_digest, back.mirror_retry_at, back.mirror_due) == (
        planned.mirrored_digest, planned.mirror_held_digest, planned.mirror_retry_at, planned.mirror_due)
    # a record whose flag already matches its digest keeps every mirror field as it is
    settled = replace(run, mirrored_digest=mirror_digest(run), mirror_held_digest="", mirror_retry_at=None)
    await w.svc._abandon_run("u", settled, None, w.clock.now)
    again = w.rec(audio)
    assert (again.mirrored_digest, again.mirror_held_digest, again.mirror_retry_at, again.mirror_due) == (
        settled.mirrored_digest, "", None, False)


async def test_marks_only_for_mirrored_records_and_roots(world):
    w = world()
    w.add_root()
    mirrored = w.add_doc("a.md", prose(5))
    plain = w.add_doc("b.md", prose(5))
    await w.settle()
    w.mirrored(mirrored)
    w.mirror.docs[f"u:file:{plain}"] = replace(w.doc(mirrored), source_item_id=plain)   # a stray document: not noticed
    w.drive.delete(mirrored)
    w.drive.delete(plain)
    await w.settle()
    sent = [m for m in w.mirror.marks if m[0] == "mark_gone"]
    assert sent == [("mark_gone", ((mirrored, GONE),), DRIVE)]
    assert not w.doc(plain).gone_pending
    deleted_with_why = [c for c in w.commits if plain in c[0]]
    assert deleted_with_why and all(plain not in (c[1] or {}) for c in deleted_with_why)


async def test_mark_skips_another_drives_document(world):
    """Ledger keys carry no drive id: the notice is sent with the mirror's drive id, so a document of another drive under
    the same source id stays unmarked (carry of Task 20A)."""
    w, doc = await _mirrored_doc(world)
    w.mirror.docs[f"u:file:{doc}"] = replace(w.doc(doc), drive_id="other-drive")
    w.drive.delete(doc)
    await w.settle()
    assert ("mark_gone", ((doc, GONE),), DRIVE) in w.mirror.marks
    assert not w.doc(doc).gone_pending


async def test_root_deleting_marks_and_return_clears(world):
    """The root's record going to `deleting` sets the root-loss mark before the record is written; a recycle-bin
    restore (same id) writes it back and clears the mark (§27.9.9, Δ203)."""
    w = world()
    root = w.add_root()
    sub = w.drive.add("Videos", parent=root, folder=True)
    await w.settle()
    kept = {i: w.drive.items[i] for i in (root, sub)}
    w.drive.delete(root)
    start = len(w.events)
    await w.tick()
    assert w.root_doc().root_gone_at is not None
    events = w.events[start:]
    marked_at = next(i for i, (n, d) in enumerate(events) if n == "mark_root_gone")
    deleting_at = next(i for i, (n, d) in enumerate(events)
                       if n == "upsert_records" and (root, RecordState.DELETING.value) in d)
    assert marked_at < deleting_at
    gone_at = w.root_doc().root_gone_at
    await w.settle()   # the cascade deletes the root's record: no second mark (the first time stays)
    assert w.rec(root) is None and w.root_doc().root_gone_at == gone_at
    assert [m for m in w.mirror.marks if m[0] == "mark_root_gone"] == [("mark_root_gone", root)]
    for item in (root, sub):   # the restore: same ids
        w.drive.items[item] = kept[item]
        w.drive.log.append(kept[item])
    start = len(w.events)
    await w.settle()
    assert w.root_doc().root_gone_at is None and w.rec(root).scope is Scope.INSIDE_OPAQUE
    events = w.events[start:]
    written_at = next(i for i, (n, d) in enumerate(events) if n == "upsert_records" and any(x == root for x, _ in d))
    cleared_at = next(i for i, (n, d) in enumerate(events) if n == "clear_root_gone")
    assert written_at < cleared_at
    assert [m for m in w.mirror.marks if m[0] == "clear_root_gone"] == [("clear_root_gone", root)]
    _no_missing_why(w)


async def test_root_written_back_without_a_mark_clears_nothing(world):
    w = world()
    root = w.add_root()
    await w.settle()
    w.drive.rename(root, "My transcripts")
    await w.settle()
    assert w.rec(root).name == "My transcripts" and w.rec(root).scope is Scope.INSIDE_OPAQUE
    assert w.mirror.marks == []


async def test_root_swept_marks_the_loss(world):
    w = world()
    root = w.add_root()
    w.add_doc("anchor.md", prose(3))
    await w.settle()
    w.silent_drop(root)
    w.drive.resync_next = True
    w.drive.add_file("later.md", prose(2).encode())
    await w.settle()
    assert w.rec(root) is None and w.root_doc().root_gone_at is not None


async def test_previous_root_loss_triggers_nothing(world):
    w = world()
    old = w.drive.add("_Transcripts", folder=True)
    w.add_root("_Transcripts (Alek)", previous=(old,))
    await w.settle()
    w.drive.delete(old)
    await w.settle()
    assert w.rec(old) is None
    assert w.mirror.marks == [] and w.root_doc().root_gone_at is None


async def test_unknown_why_when_a_caller_passes_none(world):
    """`_commit` with deletes and no `gone` (no caller does: see the static check below) marks them `unknown`."""
    w, doc = await _mirrored_doc(world)
    token = svc_mod._TICK_MIRROR.set(await w.svc._start_mirror("u", w.repo.states["u"], None))
    try:
        await svc_mod.DriveIndexingService._commit(w.svc, "u", [], {doc})
    finally:
        svc_mod._TICK_MIRROR.reset(token)
    assert w.doc(doc).gone_why == UNKNOWN and w.rec(doc) is None


# -- every delete site passes a why (§27.9.4; the callers enumerated in the task report) ---------------------------------

_DELETE_SITES = {"_apply", "_sweep", "_cascade_step", "_remove_orphan", "_rescope_step", "_commit_plan", "_reread",
                 "_upgrade_page"}


def _functions():
    tree = ast.parse(textwrap.dedent(inspect.getsource(svc_mod)))
    return [fn for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _calls(fn, attr: str):
    return [node for node in ast.walk(fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr]


def _deletes_arg(call: ast.Call):
    """The `deletes` argument of a `_commit` call, positional (third) or by keyword; None when it passes none."""
    if len(call.args) >= 3:
        return call.args[2]
    return next((k.value for k in call.keywords if k.arg == "deletes"), None)


def _commit_calls():
    for fn in _functions():
        for node in _calls(fn, "_commit"):
            yield fn.name, node


def _literal_empty(node: ast.expr) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "set"
            and not node.args)


@pytest.mark.slow
def test_every_delete_site_passes_a_why():
    """Static: every `self._commit(...)` whose deletes are not a literal `set()` passes `gone=`, and the sites are exactly
    the enumerated ones (a new delete site fails here until it is given its why); `_commit_plan` always names its
    source record."""
    sites = set()
    for name, call in _commit_calls():
        deletes = _deletes_arg(call)
        assert deletes is not None or any(k.arg is None for k in call.keywords), f"{name}: a _commit with no deletes?"
        if deletes is not None and _literal_empty(deletes):
            continue
        sites.add(name)
        assert any(k.arg == "gone" for k in call.keywords), f"{name} deletes without a why"
    assert sites == _DELETE_SITES
    # the record deletes happen inside `_commit` only, so no delete can bypass the marks
    deleters = {fn.name for fn in _functions() for call in _calls(fn, "delete_records")}
    assert deleters == {"_commit"}
    plan_calls = [n for n in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(svc_mod))))
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "_commit_plan"]
    assert plan_calls and all(len(c.args) == 3 for c in plan_calls)


async def test_every_delete_at_run_time_carries_a_why(world):
    """The same, observed: a tombstone, a cascade, a rescope and a sweep in one run; every `_commit` that deleted passed
    `gone`."""
    w = world()
    w.add_root()
    folder = w.drive.add("project", folder=True)
    a = w.add_doc("a.md", prose(4), parent=folder)
    b = w.add_doc("b.md", prose(4))
    c = w.add_doc("c.md", prose(4))
    other = w.drive.add("Other", folder=True)
    d = w.add_doc("d.md", prose(4), parent=other)
    await w.settle()
    w.drive.add_file("pyvenv.cfg", b"home", parent=folder)
    w.drive.delete(b)
    w.tombstone_only(other)
    await w.settle()
    w.silent_drop(c)
    w.drive.resync_next = True
    w.drive.add_file("later.md", prose(2).encode())
    await w.settle()
    assert all(w.rec(i) is None for i in (a, b, c, d))
    assert w.commits and all(gone is not None for _, gone in w.commits)
