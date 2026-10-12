"""The card cap as a flag (USER_DRIVE_INDEXING_RFC §5.4): `card_capped` withholds the card and nothing else; the
state and reason stay what the structure says (§3.4, §5.2)."""
from dataclasses import replace
from datetime import datetime, timezone

from src.domain.drive_card import card_eligible, card_hash
from src.domain.drive_index import Priority, Reason, RecordState, Scope
from src.domain.drive_index_planner import capped, plan_observation, released

from .test_drive_index_planner import ROOT, ch, ctx, folder_rec, observe

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


class TestCapped:
    def test_capping_keeps_state_and_reason_and_drops_the_card(self):
        repo = replace(folder_rec("r", "proj", scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT),
                       has_card=True, card_hash="h")
        rec = capped(repo, NOW)
        assert (rec.scope, rec.state, rec.reason) == (Scope.OPAQUE_ROOT, RecordState.METADATA_ONLY,
                                                      Reason.OPAQUE_ROOT)
        assert rec.card_capped and not rec.has_card and rec.card_hash is None
        assert not card_eligible(rec) and rec.due_at is None

    def test_a_deleting_record_is_left_alone(self):
        dying = replace(folder_rec("f", "old"), state=RecordState.DELETING)
        assert capped(dying, NOW) is dying


class TestReobservation:
    def test_the_flag_survives_a_rename_and_no_card_is_due(self):
        stored = capped(observe(ch("a", "x.py")).upsert, NOW)
        rec = observe(ch("a", "x2.py"), stored).upsert
        assert rec.card_capped and rec.reason is Reason.KIND_NOT_ADMITTED and rec.due_at is None

    def test_a_capped_repository_is_still_opaque_when_re_emitted(self):
        stored = capped(folder_rec("r", "proj", scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT,
                                   child_count=900), NOW)
        plan = observe(ch("r", "proj", folder=True, child_count=900), stored)
        rec = plan.upsert or stored
        assert rec.scope is Scope.OPAQUE_ROOT and rec.reason is Reason.OPAQUE_ROOT and rec.card_capped
        assert not rec.probe_pending and not rec.reobserve_pending and not rec.rescope_pending

    def test_a_record_that_leaves_card_eligibility_drops_the_flag(self):
        stored = capped(folder_rec("f", "sub", parent="p"), NOW)
        parent = folder_rec("p", "proj", scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT)
        rec = plan_observation(ch("f", "sub", parent="p", folder=True), stored, parent, None, ctx()).upsert
        assert rec.scope is Scope.INSIDE_OPAQUE and not rec.card_capped


class TestReleased:
    def test_release_clears_the_flag_and_makes_the_card_due(self):
        rec = released(capped(observe(ch("a", "x.py")).upsert, NOW), NOW)
        assert not rec.card_capped and card_eligible(rec) and rec.card_hash != card_hash(rec)
        assert rec.due_at == NOW and rec.priority is not None and rec.priority <= int(Priority.FIRST_SEEN)
        assert rec.reason is Reason.KIND_NOT_ADMITTED and rec.parent_id == ROOT
