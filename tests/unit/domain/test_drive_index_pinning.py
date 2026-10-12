"""Pins RFC-required planner and name-key behaviour that no other test pinned (retro_domain.md F15).

Each test names the surviving mutant it kills. The code was already right; these tests keep it right.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from src.domain.drive_index import (
    GEO_TABLE_VERSION, POLICY_VERSION, ContentClass, DriveChange, DriveIndexRecord, DriveSyncState, EpochPhase,
    FolderProbe, Priority, Reason, RecordState, Scope,
)
from src.domain.drive_index_params import NAME_MIN_TRIGRAMS, STALE_ALERT_AFTER
from src.domain.drive_index_planner import (
    PlanContext, is_stale, needs_probe, plan_observation, stale_versions,
)
from src.domain.drive_name_keys import name_keys, name_match, query_name_keys

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
ROOT = "root"
MADRID = {"latitude": 40.4168, "longitude": -3.7038}
PORTO = {"latitude": 41.1496, "longitude": -8.6109}


def ctx(place_of=lambda lat, lon: None, epoch=1, phase=EpochPhase.IDLE):
    return PlanContext(user_id="u", root_item_id=ROOT, epoch=epoch, phase=phase, now=NOW, place_of=place_of)


def folder_rec(item_id, name, scope=Scope.NORMAL, reason=Reason.FOLDER, parent=ROOT, **kw):
    return DriveIndexRecord(user_id="u", item_id=item_id, name=name, parent_id=parent, is_folder=True,
                            kind="folder", content_class=ContentClass.FOLDER, scope=scope,
                            state=RecordState.METADATA_ONLY, reason=reason, policy_version=POLICY_VERSION, **kw)


def folder_change(item_id, name, child_count, parent=ROOT):
    return DriveChange(item_id=item_id, name=name, parent_id=parent, is_folder=True, child_count=child_count)


class TestStaleness:
    def test_lost_access_is_never_stale(self):
        """stale_access_lost_off — §12.1: a user who lost access gets the reconnect notice, not a stale alert."""
        state = DriveSyncState(user_id="u", created_at=NOW - timedelta(days=3),
                               last_success_at=NOW - STALE_ALERT_AFTER * 5, access_lost_at=NOW - timedelta(hours=1))
        assert is_stale(state, NOW - STALE_ALERT_AFTER * 5, NOW) is False


class TestVersionUpgrades:
    def test_geo_table_bump_marks_a_located_photo_stale(self):
        """stale_versions_geo_off — §8.5, §12.3: a new place table re-resolves place names."""
        photo = DriveIndexRecord(user_id="u", item_id="p", name="IMG_1.jpg", kind="image", scope=Scope.NORMAL,
                                 state=RecordState.METADATA_ONLY, policy_version=POLICY_VERSION,
                                 facets={"location": MADRID}, place_name="Madrid",
                                 geo_table_version=GEO_TABLE_VERSION - 1)
        assert stale_versions(photo) is True
        assert stale_versions(replace(photo, geo_table_version=GEO_TABLE_VERSION)) is False

    def test_changed_location_at_the_same_table_version_resolves_the_place_again(self):
        """place_cache_loc_off: the cached place name is reused only for the same coordinates."""
        def place_of(lat, lon):
            return "Porto" if round(lat) == 41 else "Madrid"

        first = DriveChange(item_id="p", name="IMG_1.jpg", parent_id=ROOT, size_bytes=10,
                            facets={"location": MADRID})
        stored = plan_observation(first, None, None, None, ctx(place_of)).upsert
        assert stored.place_name == "Madrid" and stored.geo_table_version == GEO_TABLE_VERSION
        moved = replace(first, facets={"location": PORTO})
        assert plan_observation(moved, stored, None, None, ctx(place_of)).upsert.place_name == "Porto"


class TestDensityDecision:
    def test_policy_bump_re_decides_density(self):
        """reuse_policy_off — §5.2: a density decision taken under an older policy is taken again."""
        dense = folder_rec("c", "cache", scope=Scope.OPAQUE_ROOT, reason=Reason.DENSE_TREE, child_count=700,
                           probed_child_count=700)
        change = folder_change("c", "cache", 700)
        assert needs_probe(change, Scope.NORMAL, dense) is False   # same policy: the decision stands
        old = replace(dense, policy_version=POLICY_VERSION - 1)
        assert needs_probe(change, Scope.NORMAL, old) is True

    def test_reopened_big_repository_waits_for_a_probe(self):
        """reopen_big_off — §5.2: density is decided before the re-opened folder's children are applied."""
        repo = folder_rec("p", "site", scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT, child_count=600)
        git = folder_rec("g", ".git", scope=Scope.INSIDE_OPAQUE, parent="p")
        plan = plan_observation(DriveChange(item_id="g", deleted=True), git, None, None, ctx(), repo)
        (reopened,) = plan.parent_updates
        assert reopened.scope is Scope.UNRESOLVED and reopened.probe_pending is True
        assert reopened.state is RecordState.UNRESOLVED

    def test_reopened_small_repository_is_normal_at_once(self):
        """The counterpart of the test above: at or below the threshold no probe is owed."""
        repo = folder_rec("p", "site", scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT, child_count=40)
        git = folder_rec("g", ".git", scope=Scope.INSIDE_OPAQUE, parent="p")
        (reopened,) = plan_observation(DriveChange(item_id="g", deleted=True), git, None, None, ctx(),
                                       repo).parent_updates
        assert reopened.scope is Scope.NORMAL and reopened.probe_pending is False

    def test_big_repository_root_is_not_probed_on_every_re_emission(self):
        """needs_probe_marker_off — cost: a marker-closed root has no density anchor, yet needs no probe."""
        unresolved = plan_observation(folder_change("p", "site", 600), None, None, None, ctx()).upsert
        assert unresolved.probe_pending is True
        probe = FolderProbe(marker_found=True, class_counts={ContentClass.OTHER: 200}, sampled=200)
        repo = plan_observation(folder_change("p", "site", 600), unresolved, None, probe, ctx(),
                                observed=False).upsert
        assert repo.scope is Scope.OPAQUE_ROOT and repo.reason is Reason.OPAQUE_ROOT
        assert repo.probed_child_count == 0
        assert needs_probe(folder_change("p", "site", 600), Scope.NORMAL, repo) is False


class TestWorkClass:
    def test_card_work_of_a_stored_record_without_a_card_is_first_seen(self):
        """first_seen_hascard — §7.8: a record that never got its card is class 2, not an edit (class 0)."""
        stored = folder_rec("f", "Taxes", child_count=3)
        assert stored.has_card is False
        renamed = plan_observation(folder_change("f", "Taxes 2026", 3), stored, None, None, ctx()).upsert
        assert renamed.priority == int(Priority.FIRST_SEEN)

    def test_card_work_of_a_carded_record_is_an_edit(self):
        """The counterpart: once carded, a rename is an edit (class 0)."""
        stored = folder_rec("f", "Taxes", child_count=3, has_card=True, card_hash="old")
        renamed = plan_observation(folder_change("f", "Taxes 2026", 3), stored, None, None, ctx()).upsert
        assert renamed.priority == int(Priority.EDIT)


class TestNameMatchFloor:
    def test_exactly_the_trigram_floor_qualifies(self):
        """nk_floor_le — §10.1: NAME_MIN_TRIGRAMS shared trigrams, and no whole token, is a match."""
        grams = ("t:abc", "t:bcd", "t:cde")
        assert len(grams) == NAME_MIN_TRIGRAMS
        assert name_match(grams + ("w:abcde",), grams + ("w:xyz",)) == NAME_MIN_TRIGRAMS
        assert name_match(grams[:2] + ("w:abcde",), grams[:2] + ("w:xyz",)) is None

    def test_a_typo_matches_through_the_transliteration_trigrams(self):
        """nk_translit_grams_off: contarto/contrato share 2 Latin and 2 Cyrillic trigrams; only together they pass."""
        record = name_keys("contrato.docx")
        query = query_name_keys("contarto")
        shared = set(record) & set(query)
        assert not any(k.startswith("w:") for k in shared)
        assert name_match(record, query) == 4
