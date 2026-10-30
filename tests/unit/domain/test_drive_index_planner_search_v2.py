"""Search v2 P5 — the planner's integration (USER_DRIVE_INDEXING_RFC §25.4.9, §25.2.1, §25.3, §25.4.3): place forms
with the geo v2 stamp, name keys v2, the profile's issue date first, every collapsed folder's README, the clearing
rules, `script_rendered` and the keys work in the schedule."""
from dataclasses import replace
from datetime import date, datetime, timezone

from src.domain.connection_scope import ConnectionScope
from src.domain.drive_enrichment import DriveProfile, PhotoDescription, PhotoObject, ProfileDate
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus, TriageResult
from src.domain.drive_index import (
    GEO_TABLE_VERSION, KEYS_VERSION, NAME_KEYS_VERSION, DriveChange, DriveIndexRecord, EpochPhase, Priority, Reason,
    RecordState, Scope, TriageVerdict,
)
from src.domain.drive_index_planner import (
    PlanContext, indexed, plan_observation, readme_read, schedule, triaged,
)
from src.domain.drive_keys import name_keys_v2
from src.domain.drive_media_facts import DriveMediaFacts
from src.domain.place_lookup import Place

NOW = datetime(2026, 10, 20, 8, tzinfo=timezone.utc)
ROME = Place(name="Rome (Roma), Italy", forms=("Rome", "Roma", "Рим", "Italy", "Italia"))
GPS = DriveMediaFacts(latitude=41.9, longitude=12.5, image_width=10, image_height=10)


def ctx(place_of=lambda lat, lon: ROME) -> PlanContext:
    return PlanContext(scope=ConnectionScope(user_id="u", connection_id="u"), root_item_id="root", epoch=1, phase=EpochPhase.ENUMERATING, now=NOW,
                       place_of=place_of)


def change(item_id="p1", name="IMG_1.jpg", **kw) -> DriveChange:
    base = dict(parent_id="root", content_version="c1", modified_at=NOW, fs_modified_at=NOW)
    base.update(kw)
    return DriveChange(item_id=item_id, name=name, **base)


class TestPlaceForms:
    def test_a_located_photo_gets_its_place_forms_with_the_geo_v2_stamp(self):
        rec = plan_observation(change(media=GPS), None, None, None, ctx()).upsert
        assert (rec.place_name, rec.place_forms, rec.geo_table_version) == (ROME.name, ROME.forms, GEO_TABLE_VERSION)

    def test_a_stored_place_keeps_its_forms(self):
        calls = []

        def place_of(lat, lon):
            calls.append((lat, lon))
            return ROME

        rec = plan_observation(change(media=GPS), None, None, None, ctx(place_of)).upsert
        again = plan_observation(change(media=GPS, name="renamed.jpg"), rec, None, None, ctx(place_of)).upsert
        assert again.place_forms == ROME.forms and len(calls) == 1

    def test_a_display_name_lookup_still_works(self):
        rec = plan_observation(change(media=GPS), None, None, None, ctx(lambda lat, lon: "Rome")).upsert
        assert (rec.place_name, rec.place_forms) == ("Rome", ("Rome",))

    def test_no_location_no_forms(self):
        rec = plan_observation(change(), None, None, None, ctx()).upsert
        assert (rec.place_name, rec.place_forms) == (None, ())


class TestNameKeys:
    def test_names_are_keyed_by_name_keys_v2(self):
        rec = plan_observation(change(name="Квиток Львів 2026.pdf"), None, None, None, ctx()).upsert
        assert rec.name_tokens == name_keys_v2("Квиток Львів 2026.pdf") and rec.name_keys_version == NAME_KEYS_VERSION


class TestEffectiveDate:
    def test_the_profiles_issue_date_wins_over_the_file_date_on_a_re_plan(self):
        rec = plan_observation(change(item_id="d1", name="a.pdf"), None, None, None, ctx()).upsert
        p = DriveProfile(title="", what_it_is="", language="", doc_type="invoice",
                         dates=(ProfileDate("issued", date(2025, 2, 3)),))
        rec = replace(rec, profile=p)
        again = plan_observation(change(item_id="d1", name="b.pdf"), rec, None, None, ctx()).upsert
        assert again.effective_date == datetime(2025, 2, 3, tzinfo=timezone.utc)


def _collapsed(reason=Reason.DENSE_TREE, **kw) -> DriveIndexRecord:
    base = dict(user_id="u", item_id="f1", parent_id="root", name="photos-dump", is_folder=True, kind="folder",
                scope=Scope.OPAQUE_ROOT, state=RecordState.METADATA_ONLY, reason=reason, child_count=900,
                probed_child_count=900, policy_version=2)
    base.update(kw)
    return DriveIndexRecord(**base)


class TestCollapsedReadme:
    def test_a_dot_folder_reads_its_readme_on_first_sight(self, monkeypatch):
        from src.domain import drive_index_policy as pol
        from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS
        monkeypatch.setattr(pol, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
        rec = plan_observation(change(item_id="f1", name=".config", is_folder=True, content_version=""), None, None,
                               None, ctx()).upsert
        assert rec.scope is Scope.OPAQUE_ROOT and rec.readme_pending

    def test_a_readme_change_inside_any_collapsed_folder_re_reads_it(self, monkeypatch):
        from src.domain import drive_index_policy as pol
        from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS
        monkeypatch.setattr(pol, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
        folder = _collapsed(readme_version="c1", readme_item_id="r1")
        plan = plan_observation(change(item_id="r1", name="README.md", parent_id="f1", content_version="c2"), None,
                                folder, None, ctx())
        (update,) = plan.parent_updates
        assert update.item_id == "f1" and update.readme_pending

    def test_a_readme_read_stores_the_excerpt(self):
        rec = readme_read(_collapsed(readme_pending=True), "r1", "c3", "# Photos\n\nHoliday dumps.", NOW,
                          child_names=("a.jpg", "b.jpg"))
        assert not rec.readme_pending and rec.readme_excerpt.startswith("# Photos")
        assert "a.jpg" in rec.readme_excerpt and rec.readme_excerpt_version == "c3" and rec.lead_text == ""

    def test_a_folder_that_stops_being_collapsed_loses_its_excerpt(self):
        folder = _collapsed(reason=Reason.DOT_NAME, name=".old", readme_excerpt="# x", readme_excerpt_version="c1")
        rec = plan_observation(change(item_id="f1", name="old", is_folder=True, content_version="", child_count=3),
                               folder, None, None, ctx()).upsert
        assert rec.scope is Scope.NORMAL and (rec.readme_excerpt, rec.readme_excerpt_version) == ("", "")


def _doc(**kw) -> DriveIndexRecord:
    base = dict(user_id="u", item_id="d1", parent_id="root", name="page.html", kind="html", scope=Scope.NORMAL,
                state=RecordState.PENDING, content_version="c1", index_generation=1, modified_at=NOW,
                fs_modified_at=NOW, effective_date=NOW)
    base.update(kw)
    return DriveIndexRecord(**base)


class TestIndexed:
    def test_indexed_copies_script_rendered_text_chars_and_the_keys_stamp(self):
        outcome = ExtractionOutcome(status=ExtractionStatus.TEXT, text="x" * 300, extracted_by="parser@2",
                                    script_rendered=True)
        rec = indexed(_doc(), outcome=outcome, generation=1, chunk_count=1, now=NOW, keys_capped=True,
                      profile_fingerprint="fp")
        assert (rec.script_rendered, rec.text_chars, rec.keys_version, rec.keys_capped) == (True, 300, KEYS_VERSION,
                                                                                            True)
        assert rec.profile_input_fingerprint == "fp" and rec.lead_text == ""   # v2: no lead text in the card

    def test_a_new_content_version_restarts_the_jobs_attempts(self):
        before = _doc(profile_attempts=2, passages_attempts=1, indexed_version="c0", content_version="c1")
        outcome = ExtractionOutcome(status=ExtractionStatus.TEXT, text="hello", extracted_by="parser@2")
        rec = indexed(before, outcome=outcome, generation=1, chunk_count=1, now=NOW)
        assert (rec.profile_attempts, rec.passages_attempts) == (0, 0)


class TestTriaged:
    def test_a_photo_verdict_writes_the_photo_fields(self):
        photo = PhotoDescription(description="a square at night", objects=(PhotoObject("fountain", ("fontana",)),),
                                 scene="city square", visible_text="BAR 24")
        rec = triaged(_doc(name="p.jpg", kind="image"), TriageResult(item_id="d1", verdict=TriageVerdict.PHOTO),
                      NOW, photo=photo)
        assert (rec.photo_description, rec.photo_scene, rec.visible_text) == ("a square at night", "city square",
                                                                              "BAR 24")
        assert rec.photo_objects == photo.objects and rec.caption == ""

    def test_a_text_verdict_clears_the_photo_fields(self):
        before = _doc(name="p.jpg", kind="image", photo_description="old", photo_scene="s", visible_text="v",
                      photo_objects=(PhotoObject("x"),))
        rec = triaged(before, TriageResult(item_id="d1", verdict=TriageVerdict.TEXT), NOW)
        assert (rec.photo_description, rec.photo_scene, rec.visible_text, rec.photo_objects) == ("", "", "", ())

    def test_a_kind_leaving_triage_clears_the_photo_fields(self):
        photo = _doc(item_id="p1", name="p.jpg", kind="image", state=RecordState.METADATA_ONLY,
                     reason=Reason.PHOTOGRAPH, triage_verdict=TriageVerdict.PHOTO, photo_description="a cat",
                     index_generation=0)
        rec = plan_observation(change(item_id="p1", name="p.txt"), photo, None, None, ctx()).upsert
        assert (rec.photo_description, rec.photo_objects) == ("", ())


class TestKeysInTheSchedule:
    def test_keys_work_is_class_3(self):
        from src.domain.drive_card import card_hash
        rec = _doc(state=RecordState.INDEXED, indexed_generation=1, indexed_version="c1", extraction_version=3,
                   keys_pending=True)
        rec = schedule(replace(rec, card_hash=card_hash(rec), has_card=True), NOW, first_seen=False)
        assert (rec.due_at, rec.priority) == (NOW, int(Priority.MAINTENANCE))
