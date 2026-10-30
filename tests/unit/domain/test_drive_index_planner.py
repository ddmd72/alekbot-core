"""Planner decisions (USER_DRIVE_INDEXING_RFC §3.6, §5.2, §7). Pure: no Firestore, no Graph, no clock."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_card import card_hash
from src.domain.drive_index import (
    GEO_TABLE_VERSION, POLICY_VERSION, ContentClass, DriveChange, DriveIndexRecord, DriveSyncState, EpochPhase,
    FolderProbe, Priority, Reason, RecordState, Scope,
)
from src.domain.drive_index_planner import (
    AccessAction, PlanContext, access_action, capped, change_from_record, classify, is_stale, needs_probe,
    parent_scope, plan_observation, schedule, stale_versions, with_card, with_retry,
)
from src.domain.drive_index_params import ACCESS_LOSS_TTL, STALE_ALERT_AFTER
from src.domain.user_drive import DriveConnection

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
ROOT = "root"


def ctx(epoch=1, phase=EpochPhase.IDLE, place_of=lambda lat, lon: "Rome (Roma), Italy"):
    return PlanContext(user_id="u", root_item_id=ROOT, epoch=epoch, phase=phase, now=NOW, place_of=place_of)


def ch(item_id, name, parent=ROOT, folder=False, **kw):
    return DriveChange(item_id=item_id, name=name, parent_id=parent, is_folder=folder, **kw)


def folder_rec(item_id, name, scope=Scope.NORMAL, reason=Reason.FOLDER, parent=ROOT, **kw):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id=item_id, name=name, parent_id=parent, is_folder=True,
                            kind="folder", content_class=ContentClass.FOLDER, scope=scope,
                            state=RecordState.METADATA_ONLY, reason=reason, policy_version=POLICY_VERSION, **kw)


def probe(other=0, docs=0, media=0, marker=False):
    counts = {ContentClass.OTHER: other, ContentClass.DOCUMENT: docs, ContentClass.MEDIA: media,
              ContentClass.FOLDER: 0}
    return FolderProbe(marker_found=marker, class_counts=counts, sampled=other + docs + media)


def observe(change, existing=None, parent=None, pr=None, c=None, previous_parent=None):
    return plan_observation(change, existing, parent, pr, c or ctx(), previous_parent)


# --- classify ---------------------------------------------------------------------------------

class TestClassify:
    def test_unknown_parent_is_unresolved(self):
        cls = classify(ch("a", "x.pdf", parent="p"), None, None, None)
        assert (cls.scope, cls.state) == (Scope.UNRESOLVED, RecordState.UNRESOLVED)

    def test_file_inside_opaque_gets_no_record(self):
        assert classify(ch("a", "x.py", parent="p"), Scope.OPAQUE_ROOT, None, None).stored is False
        assert classify(ch("a", "x.py", parent="p"), Scope.INSIDE_OPAQUE, None, None).stored is False

    def test_marker_file_inside_opaque_is_kept_without_card(self):
        cls = classify(ch("a", "pyvenv.cfg", parent="p"), Scope.OPAQUE_ROOT, None, None)
        assert cls.stored is True and cls.scope is Scope.INSIDE_OPAQUE

    def test_folder_inside_opaque_is_lightweight(self):
        cls = classify(ch("a", "src", parent="p", folder=True), Scope.OPAQUE_ROOT, None, None)
        assert (cls.scope, cls.reason) == (Scope.INSIDE_OPAQUE, Reason.FOLDER)

    def test_dot_folder_is_opaque_root(self):
        cls = classify(ch("a", ".obsidian", folder=True), Scope.NORMAL, None, None)
        assert (cls.scope, cls.reason) == (Scope.OPAQUE_ROOT, Reason.DOT_NAME)

    def test_marker_in_probe_makes_repository(self):
        cls = classify(ch("a", "site", folder=True, child_count=900), Scope.NORMAL, probe(other=10, marker=True), None)
        assert (cls.scope, cls.reason) == (Scope.OPAQUE_ROOT, Reason.OPAQUE_ROOT)

    def test_dense_mostly_other(self):
        cls = classify(ch("a", "mirror", folder=True, child_count=700), Scope.NORMAL, probe(other=200), None)
        assert (cls.scope, cls.reason, cls.density_count) == (Scope.OPAQUE_ROOT, Reason.DENSE_TREE, 700)

    @pytest.mark.parametrize("pr", [probe(docs=200), probe(media=200)])
    def test_many_documents_or_photos_stay_normal(self, pr):
        cls = classify(ch("a", "big", folder=True, child_count=1000), Scope.NORMAL, pr, None)
        assert (cls.scope, cls.reason, cls.density_count) == (Scope.NORMAL, Reason.FOLDER, 1000)

    def test_big_folder_without_probe_waits(self):
        cls = classify(ch("a", "big", folder=True, child_count=900), Scope.NORMAL, None, None)
        assert cls.probe_pending is True and cls.scope is Scope.UNRESOLVED

    def test_density_decision_reused_within_hysteresis(self):
        prev = folder_rec("a", "mirror", scope=Scope.OPAQUE_ROOT, reason=Reason.DENSE_TREE, probed_child_count=700)
        change = ch("a", "mirror", folder=True, child_count=900)
        assert needs_probe(change, Scope.NORMAL, prev) is False
        assert classify(change, Scope.NORMAL, None, prev).reason is Reason.DENSE_TREE
        assert needs_probe(ch("a", "mirror", folder=True, child_count=1400), Scope.NORMAL, prev) is True

    def test_files(self):
        assert classify(ch("a", "id_rsa"), Scope.NORMAL, None, None).reason is Reason.SECRET_NAME
        cls = classify(ch("a", "Договор.doc"), Scope.NORMAL, None, None)   # .doc: no extractor, not admitted
        assert (cls.state, cls.reason, cls.kind) == (RecordState.METADATA_ONLY, Reason.KIND_NOT_ADMITTED, "doc")

    def test_root_is_an_implicit_normal_parent(self):
        assert parent_scope(ROOT, None, ROOT) is Scope.NORMAL
        assert parent_scope("p", None, ROOT) is None


# --- observation ---------------------------------------------------------------------------------

class TestObservation:
    def test_root_item_is_never_a_record(self):
        assert observe(ch(ROOT, "Alek-bot", parent="x", folder=True)).upsert is None

    def test_first_sight_file_gets_first_seen_card_work(self):
        plan = observe(ch("a", "Scan_0012.doc", content_version="c1", modified_at=NOW))   # not admitted
        rec = plan.upsert
        assert rec.state is RecordState.METADATA_ONLY and rec.reason is Reason.KIND_NOT_ADMITTED
        assert (rec.priority, rec.due_at) == (Priority.FIRST_SEEN, NOW)
        assert "w:scan" in rec.name_tokens and rec.seen_epoch == 1

    def test_tombstone_of_known_file_deletes(self):
        existing = observe(ch("a", "x.pdf")).upsert
        assert observe(DriveChange(item_id="a", deleted=True), existing).delete_item_id == "a"

    def test_tombstone_of_unknown_item_is_ignored(self):
        plan = observe(DriveChange(item_id="zz", deleted=True))
        assert plan.upsert is None and plan.delete_item_id is None

    def test_tombstone_of_folder_marks_deleting_and_due(self):
        existing = folder_rec("f", "Contracts")
        rec = observe(DriveChange(item_id="f", deleted=True), existing).upsert
        assert rec.state is RecordState.DELETING and rec.priority == Priority.CASCADE

    def test_reemitted_unchanged_item_writes_nothing_when_idle(self):
        change = ch("a", "x.pdf", content_version="c1", modified_at=NOW)
        stored = with_card(observe(change).upsert, "h", "space", NOW)
        stored = replace(stored, card_hash=card_hash(stored))
        plan = observe(change, stored)
        assert plan.upsert is None and plan.delete_item_id is None

    def test_reemitted_unchanged_item_only_stamps_epoch_when_enumerating(self):
        change = ch("a", "x.pdf", content_version="c1", modified_at=NOW)
        stored = observe(change).upsert
        plan = observe(change, stored, c=ctx(epoch=2, phase=EpochPhase.ENUMERATING))
        assert plan.upsert == replace(stored, seen_epoch=2)

    def test_rename_schedules_an_edit_card(self):
        stored = observe(ch("a", "old.pdf")).upsert
        stored = replace(with_card(stored, card_hash(stored), "space", NOW), card_hash=card_hash(stored))
        rec = observe(ch("a", "new.pdf"), stored).upsert
        assert rec.priority == Priority.EDIT and rec.has_card is True

    def test_folder_rename_touches_only_the_folder(self):
        stored = observe(ch("f", "Old", folder=True)).upsert
        plan = observe(ch("f", "New", folder=True), stored)
        assert plan.upsert.rescope_pending is False and plan.parent_updates == ()

    def test_marker_child_makes_parent_a_repository_and_hides_itself(self):
        parent = folder_rec("p", "site")
        plan = observe(ch("g", ".git", parent="p", folder=True), None, parent)
        assert plan.parent_updates[0].scope is Scope.OPAQUE_ROOT and plan.parent_updates[0].reason is Reason.OPAQUE_ROOT
        # its card text changes too ("site | repository"), so the card edit sets the priority
        assert plan.parent_updates[0].rescope_pending is True and plan.parent_updates[0].priority == Priority.EDIT
        assert plan.upsert.scope is Scope.INSIDE_OPAQUE

    def test_marker_deleted_reopens_parent_and_reobserves(self):
        parent = folder_rec("p", "site", scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT)
        git = folder_rec("g", ".git", scope=Scope.INSIDE_OPAQUE, parent="p")
        plan = observe(DriveChange(item_id="g", deleted=True), git, previous_parent=parent)
        reopened = plan.parent_updates[0]
        assert reopened.scope is Scope.NORMAL and reopened.reobserve_pending and reopened.priority == Priority.EDIT

    def test_marker_moved_away_reopens_old_parent(self):
        old = folder_rec("p", "site", scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT)
        cfg = folder_rec("g", "pyvenv.cfg", scope=Scope.INSIDE_OPAQUE, parent="p")
        plan = observe(ch("g", "pyvenv.cfg", parent=ROOT, folder=True), cfg, None, previous_parent=old)
        assert plan.parent_updates[0].item_id == "p" and plan.parent_updates[0].scope is Scope.NORMAL
        assert plan.parent_updates[0].reobserve_pending                 # the old folder re-opens ...
        assert plan.upsert.reobserve_pending and plan.upsert.scope is Scope.NORMAL   # ... and the moved one left opaque territory

    def test_marker_moved_between_folders_updates_both(self):
        old = folder_rec("p", "site", scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT)
        new = folder_rec("q", "other")
        cfg = folder_rec("g", "pyvenv.cfg", scope=Scope.INSIDE_OPAQUE, parent="p")
        plan = observe(ch("g", "pyvenv.cfg", parent="q", folder=True), cfg, new, previous_parent=old)
        assert [(u.item_id, u.scope) for u in plan.parent_updates] == [("p", Scope.NORMAL), ("q", Scope.OPAQUE_ROOT)]
        assert plan.parent_updates[0].reobserve_pending and not plan.parent_updates[1].reobserve_pending

    def test_marker_renamed_reopens_and_child_is_classified_under_the_reopened_folder(self):
        old = folder_rec("p", "site", scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT)
        cfg = DriveIndexRecord(user_id="u", connection_id="u", item_id="g", name="pyvenv.cfg", parent_id="p", scope=Scope.NORMAL)
        plan = observe(ch("g", "notes.md", parent="p"), cfg, old, previous_parent=old)
        assert plan.parent_updates[0].scope is Scope.NORMAL and plan.upsert.scope is Scope.NORMAL

    def test_folder_leaving_opaque_rescopes_and_reobserves(self):
        stored = folder_rec("f", ".hidden", scope=Scope.OPAQUE_ROOT, reason=Reason.DOT_NAME)
        plan = observe(ch("f", "visible", folder=True), stored)
        assert plan.upsert.scope is Scope.NORMAL and plan.upsert.rescope_pending and plan.upsert.reobserve_pending

    def test_revived_deleting_folder_reobserves(self):
        stored = replace(folder_rec("f", "Contracts"), state=RecordState.DELETING)
        plan = observe(ch("f", "Contracts", folder=True), stored)
        assert plan.upsert.state is RecordState.METADATA_ONLY and plan.upsert.reobserve_pending

    def test_card_cap_survives_reobservation(self):
        stored = capped(observe(ch("a", "x.py")).upsert, NOW)
        rec = observe(ch("a", "x2.py"), stored).upsert
        assert rec.card_capped and rec.reason is Reason.KIND_NOT_ADMITTED and rec.due_at is None

    def test_place_resolved_once_and_kept(self):
        calls = []

        def place_of(lat, lon):
            calls.append((lat, lon))
            return "Rome (Roma), Italy"

        photo = ch("a", "IMG.HEIC", facets={"location": {"latitude": 41.9, "longitude": 12.5}})
        stored = observe(photo, c=ctx(place_of=place_of)).upsert
        # stamped with the current geo table (§25.4.8: v2); the constant itself is pinned in test_place_forms_v2
        assert stored.place_name == "Rome (Roma), Italy" and stored.geo_table_version == GEO_TABLE_VERSION
        observe(replace(photo, name="IMG2.HEIC"), stored, c=ctx(place_of=place_of))
        assert len(calls) == 1

    def test_file_inside_repository_is_dropped_if_stored(self):
        parent = folder_rec("p", "site", scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT)
        stored = observe(ch("a", "main.py", parent="p"), None, folder_rec("p", "site")).upsert
        assert observe(ch("a", "main.py", parent="p"), stored, parent).delete_item_id == "a"

    def test_change_from_record_round_trips(self):
        change = ch("a", "x.pdf", size_bytes=5, content_version="c", modified_at=NOW, fs_modified_at=NOW,
                    facets={"hashes": {"quickXorHash": "q"}})
        rec = observe(change).upsert
        assert change_from_record(rec) == change


# --- scheduling, retries, versions -------------------------------------------------------------

class TestScheduling:
    def test_no_work_clears_due(self):
        rec = observe(ch("a", "x.py")).upsert   # no content work for code
        done = with_card(rec, card_hash(rec), "space", NOW)
        assert done.due_at is None and done.priority is None and done.has_card

    def test_cascade_outranks_card(self):
        rec = replace(observe(ch("f", "F", folder=True)).upsert, rescope_pending=True)
        assert schedule(rec, NOW, first_seen=True).priority == Priority.CASCADE

    def test_card_dropped_when_ineligible(self):
        rec = replace(folder_rec("f", "F", scope=Scope.INSIDE_OPAQUE), has_card=True, card_hash="h")
        assert schedule(rec, NOW, first_seen=False).priority == Priority.CASCADE

    def test_retry_backs_off_in_retry_class(self):
        rec = with_retry(observe(ch("a", "x.pdf")).upsert, NOW)
        assert rec.attempts == 1 and rec.priority == Priority.RETRY and rec.due_at == NOW + timedelta(minutes=10)

    def test_stale_versions(self):
        rec = observe(ch("a", "x.pdf")).upsert
        assert stale_versions(rec) is False
        assert stale_versions(replace(rec, policy_version=0)) is True
        assert stale_versions(replace(rec, has_card=True, card_version=0)) is True


# --- gate and staleness -------------------------------------------------------------------------

class TestAccess:
    ON = DriveConnection(connected=True, drive_id="d1")

    @pytest.mark.parametrize("enabled,state,conn,expected", [
        (False, None, ON, AccessAction.NOOP_DISABLED),
        (False, DriveSyncState(user_id="u", connection_id="u"), ON, AccessAction.PURGE_OPT_OUT),
        (True, None, DriveConnection(connected=False), AccessAction.NOOP_DISABLED),
        (True, DriveSyncState(user_id="u", connection_id="u"), DriveConnection(connected=False), AccessAction.RECORD_LOSS),
        (True, DriveSyncState(user_id="u", connection_id="u"), DriveConnection(connected=True, needs_reconnect=True), AccessAction.RECORD_LOSS),
        (True, DriveSyncState(user_id="u", connection_id="u", access_lost_at=NOW - ACCESS_LOSS_TTL), DriveConnection(connected=False),
         AccessAction.PURGE_EXPIRED),
        (True, DriveSyncState(user_id="u", connection_id="u", drive_id="d0"), ON, AccessAction.PURGE_DRIVE_SWITCH),
        (True, DriveSyncState(user_id="u", connection_id="u", drive_id="d1", access_lost_at=NOW - timedelta(days=3)), ON,
         AccessAction.RESUME_AFTER_REGAIN),
        (True, DriveSyncState(user_id="u", connection_id="u", drive_id="d1", access_lost_at=NOW - ACCESS_LOSS_TTL), ON,
         AccessAction.PURGE_THEN_RESTART),
        (True, DriveSyncState(user_id="u", connection_id="u", drive_id="d1"), ON, AccessAction.PROCEED),
        (True, None, ON, AccessAction.PROCEED),
        (True, DriveSyncState(user_id="u", connection_id="u", drive_id=""), ON, AccessAction.PROCEED),
    ])
    def test_access_action(self, enabled, state, conn, expected):
        assert access_action(enabled, state, conn, NOW) is expected


class TestStaleness:
    def test_fresh_user_with_no_baseline_is_not_stale(self):
        assert is_stale(DriveSyncState(user_id="u", connection_id="u"), None, NOW) is False

    def test_onboarding_that_never_succeeded_goes_stale(self):
        st = DriveSyncState(user_id="u", connection_id="u", created_at=NOW - STALE_ALERT_AFTER - timedelta(seconds=1))
        assert is_stale(st, None, NOW) is True

    def test_no_clean_tick_for_too_long(self):
        st = DriveSyncState(user_id="u", connection_id="u", last_success_at=NOW - STALE_ALERT_AFTER - timedelta(seconds=1))
        assert is_stale(st, None, NOW) is True

    def test_overdue_edit(self):
        st = DriveSyncState(user_id="u", connection_id="u", last_success_at=NOW)
        assert is_stale(st, NOW - STALE_ALERT_AFTER - timedelta(seconds=1), NOW) is True
        assert is_stale(st, NOW - timedelta(minutes=5), NOW) is False

    def test_access_lost_never_stale(self):
        assert is_stale(DriveSyncState(user_id="u", connection_id="u", access_lost_at=NOW), None, NOW) is False


class TestReviewARegressions:
    def test_card_version_bump_reschedules_an_unchanged_record(self, monkeypatch):
        from src.domain import drive_card, drive_index_planner as planner
        rec = observe(ch("a", "x.py")).upsert   # a kind with no content work, so only the card can be due
        stored = with_card(rec, card_hash(rec), "space", NOW)
        monkeypatch.setattr(drive_card, "CARD_VERSION", drive_card.CARD_VERSION + 1)
        monkeypatch.setattr(planner, "CARD_VERSION", planner.CARD_VERSION + 1)
        plan = observe(ch("a", "x.py"), stored)
        assert plan.upsert is not None and plan.upsert.due_at == NOW

    def test_reobserve_is_class_two(self):
        rec = replace(folder_rec("f", "F"), reobserve_pending=True, has_card=True)
        rec = replace(rec, card_hash=card_hash(rec))
        assert schedule(rec, NOW, first_seen=False).priority == Priority.FIRST_SEEN

    def test_capped_leaves_a_deleting_record_alone(self):
        dying = replace(folder_rec("f", "F"), state=RecordState.DELETING)
        assert capped(dying, NOW) is dying

    def test_a_started_purge_always_finishes(self):
        st = DriveSyncState(user_id="u", connection_id="u", purging=True, drive_id="d1")
        assert access_action(True, st, DriveConnection(connected=True, drive_id="d1"), NOW) is AccessAction.PURGE_THEN_RESTART
        assert access_action(False, st, DriveConnection(connected=True, drive_id="d1"), NOW) is AccessAction.PURGE_OPT_OUT


class TestPlanDeltas16And17:
    def test_node_modules_outside_a_repository_is_opaque(self):
        cls = classify(ch("n", "node_modules", folder=True), Scope.NORMAL, None, None)
        assert (cls.scope, cls.reason) == (Scope.OPAQUE_ROOT, Reason.OPAQUE_ROOT)

    def test_renaming_node_modules_away_reopens(self):
        stored = folder_rec("n", "node_modules", scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT)
        plan = observe(ch("n", "libs", folder=True), stored)
        assert plan.upsert.scope is Scope.NORMAL and plan.upsert.reobserve_pending

    def test_deleting_folder_drops_its_card(self):
        stored = replace(folder_rec("f", "Contracts"), has_card=True, card_hash="h")
        rec = observe(DriveChange(item_id="f", deleted=True), stored).upsert
        assert rec.state is RecordState.DELETING and rec.has_card is False and rec.card_hash is None
