"""The bot's `_Transcripts` folder is opaque to the index (USER_DRIVE_INDEXING_RFC §27.9.2, §27.9.8; Δ184, Δ205):
`PlanContext.mirror_root_ids`, the union of every root document's current and previous ids, classifies the folder
`inside_opaque / metadata_only / folder`; nothing under it gets a card or a content record. Pure planner tests."""
from dataclasses import replace
from datetime import datetime, timezone

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_card import card_eligible
from src.domain.drive_index import (
    POLICY_VERSION, ContentClass, DriveChange, DriveIndexRecord, EpochPhase, Reason, RecordState, Scope,
)
from src.domain.drive_index_planner import PlanContext, mirror_root_classification, parent_scope, plan_observation

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
ROOT = "approot"
MIRROR = "m-root"


def ctx(*roots: str) -> PlanContext:
    return PlanContext(scope=ConnectionScope(user_id="u", connection_id="u"), root_item_id=ROOT, epoch=1, phase=EpochPhase.IDLE, now=NOW,
                       place_of=lambda lat, lon: None, mirror_root_ids=frozenset(roots))


def ch(item_id: str, name: str, parent: str = ROOT, folder: bool = False, **kw) -> DriveChange:
    return DriveChange(item_id=item_id, name=name, parent_id=parent, is_folder=folder,
                       content_version="" if folder else "c1", **kw)


def folder_rec(item_id: str, name: str, scope: Scope = Scope.NORMAL, parent: str = ROOT, **kw) -> DriveIndexRecord:
    return DriveIndexRecord(user_id="u", connection_id="u", item_id=item_id, name=name, parent_id=parent, is_folder=True, kind="folder",
                            content_class=ContentClass.FOLDER, scope=scope, state=RecordState.METADATA_ONLY,
                            reason=Reason.FOLDER, policy_version=POLICY_VERSION, **kw)


def test_default_context_has_no_mirror_roots():
    c = PlanContext(scope=ConnectionScope(user_id="u", connection_id="u"), root_item_id=ROOT, epoch=1, phase=EpochPhase.IDLE, now=NOW,
                    place_of=lambda lat, lon: None)
    assert c.mirror_root_ids == frozenset()


def test_root_id_classifies_inside_opaque():
    plan = plan_observation(ch(MIRROR, "_Transcripts", folder=True), None, None, None, ctx(MIRROR))
    rec = plan.upsert
    assert rec is not None and plan.delete_item_id is None
    assert (rec.scope, rec.state, rec.reason) == (Scope.INSIDE_OPAQUE, RecordState.METADATA_ONLY, Reason.FOLDER)
    assert not card_eligible(rec) and not rec.has_card and not rec.readme_pending
    cls = mirror_root_classification(ch(MIRROR, "_Transcripts", folder=True))
    assert cls.stored and cls.scope is Scope.INSIDE_OPAQUE and cls.reason is Reason.FOLDER


def test_the_same_folder_without_the_id_is_an_ordinary_folder():
    rec = plan_observation(ch(MIRROR, "_Transcripts", folder=True), None, None, None, ctx()).upsert
    assert rec is not None and rec.scope is Scope.NORMAL


def test_previous_root_id_too():
    """A previous root (§27.9.9) stays opaque: the tick passes the union, so a restored old root matches."""
    c = ctx("new-root", "old-root")
    for item in ("new-root", "old-root"):
        rec = plan_observation(ch(item, "_Transcripts (Alek)", folder=True), None, None, None, c).upsert
        assert rec is not None and rec.scope is Scope.INSIDE_OPAQUE


def test_another_drives_root_id_harmless():
    """An id from another drive's root document matches no item of this drive: ordinary items plan as without it."""
    for change in (ch("f1", "Videos", folder=True), ch("a1", "memo.m4a"), ch("d1", "notes.md")):
        assert plan_observation(change, None, None, None, ctx("other-drive-root")) == \
            plan_observation(change, None, None, None, ctx())


def test_root_renamed_or_moved_stays_opaque():
    """The id is the identity (§27.9.1): a renamed root, or one moved under an ordinary folder, stays opaque."""
    parent = folder_rec("p", "Work")
    for change, par in ((ch(MIRROR, "My transcripts", folder=True), None),
                        (ch(MIRROR, "_Transcripts", parent="p", folder=True), parent)):
        rec = plan_observation(change, None, par, None, ctx(MIRROR)).upsert
        assert rec is not None and rec.scope is Scope.INSIDE_OPAQUE


def test_root_under_an_unknown_parent_is_still_opaque_not_unresolved():
    rec = plan_observation(ch(MIRROR, "_Transcripts", parent="nowhere", folder=True), None, None, None,
                           ctx(MIRROR)).upsert
    assert rec is not None and rec.scope is Scope.INSIDE_OPAQUE and rec.state is RecordState.METADATA_ONLY


def test_files_under_root_get_no_record():
    root = plan_observation(ch(MIRROR, "_Transcripts", folder=True), None, None, None, ctx(MIRROR)).upsert
    sub = plan_observation(ch("s", "Videos", parent=MIRROR, folder=True), None, root, None, ctx(MIRROR)).upsert
    for name in ("Interview.mp4.md", "_README.md", "notes.pdf", "pyvenv.txt"):
        plan = plan_observation(ch("x", name, parent=MIRROR), None, root, None, ctx(MIRROR))
        assert plan.upsert is None and plan.delete_item_id is None, name
        plan = plan_observation(ch("y", name, parent="s"), None, sub, None, ctx(MIRROR))
        assert plan.upsert is None and plan.delete_item_id is None, name


def test_files_directly_under_the_root_get_no_record_before_its_own_record_exists():
    """The first enumeration after a purge may list a child before the root's record is stored: the root's id alone
    decides, so the child is never held unresolved (and never stored)."""
    assert parent_scope(MIRROR, None, ROOT, frozenset({MIRROR})) is Scope.INSIDE_OPAQUE
    plan = plan_observation(ch("x", "Interview.mp4.md", parent=MIRROR), None, None, None, ctx(MIRROR))
    assert plan.upsert is None and plan.delete_item_id is None
    folder = plan_observation(ch("s", "Archive", parent=MIRROR, folder=True), None, None, None, ctx(MIRROR)).upsert
    assert folder is not None and folder.scope is Scope.INSIDE_OPAQUE


def test_a_stored_file_under_the_root_is_deleted_on_its_next_observation():
    root = folder_rec(MIRROR, "_Transcripts", scope=Scope.INSIDE_OPAQUE)
    stored = DriveIndexRecord(user_id="u", connection_id="u", item_id="x", name="a.md", parent_id=MIRROR, kind="markdown")
    plan = plan_observation(ch("x", "a.md", parent=MIRROR), stored, root, None, ctx(MIRROR))
    assert plan.upsert is None and plan.delete_item_id == "x"


def test_folders_under_root_have_no_card():
    root = plan_observation(ch(MIRROR, "_Transcripts", folder=True), None, None, None, ctx(MIRROR)).upsert
    for name in ("Archive", "2026-10-21", "Long paths", "Videos"):
        rec = plan_observation(ch("s", name, parent=MIRROR, folder=True), None, root, None, ctx(MIRROR)).upsert
        assert rec is not None
        assert (rec.scope, rec.state, rec.reason) == (Scope.INSIDE_OPAQUE, RecordState.METADATA_ONLY, Reason.FOLDER)
        assert not card_eligible(rec)


def test_an_ordinary_root_record_turns_opaque_and_rescopes_its_children():
    """A root stored as an ordinary folder (indexed before its id was known, e.g. by a rollback image) becomes opaque
    and re-scopes its children, so their records go (§5.2)."""
    before = folder_rec(MIRROR, "_Transcripts", has_card=True, card_hash="h")
    rec = plan_observation(ch(MIRROR, "_Transcripts", folder=True), before, None, None, ctx(MIRROR)).upsert
    assert rec is not None and rec.scope is Scope.INSIDE_OPAQUE and rec.rescope_pending
    assert not rec.reobserve_pending


def test_a_deleted_root_goes_the_ordinary_folder_way():
    """`not deleted` only: a tombstone of the root takes the record to `deleting` as any folder (§7.6), which the tick
    reads as the root's loss (§27.9.9)."""
    before = folder_rec(MIRROR, "_Transcripts", scope=Scope.INSIDE_OPAQUE)
    plan = plan_observation(DriveChange(item_id=MIRROR, deleted=True), before, None, None, ctx(MIRROR))
    assert plan.upsert is not None and plan.upsert.state is RecordState.DELETING
    assert plan_observation(DriveChange(item_id=MIRROR, deleted=True), None, None, None, ctx(MIRROR)).upsert is None


def test_root_record_back_is_opaque_again():
    dying = replace(folder_rec(MIRROR, "_Transcripts", scope=Scope.INSIDE_OPAQUE), state=RecordState.DELETING)
    rec = plan_observation(ch(MIRROR, "_Transcripts", folder=True), dying, None, None, ctx(MIRROR)).upsert
    assert rec is not None and rec.state is RecordState.METADATA_ONLY and rec.scope is Scope.INSIDE_OPAQUE
    assert not rec.reobserve_pending   # an opaque folder's subtree is never stored
