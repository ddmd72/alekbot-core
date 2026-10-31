"""Cross-cutting fix round, pure parts (USER_DRIVE_INDEXING_RFC §26; deltas Δ89, Δ90, Δ91, A5, B-minor 3 and 4).

Δ89 native topics weigh 0; Δ90 scene-label hits do not make a word common; Δ91 an ID printed with separators is also
keyed unspaced (never an amount, a date or an unconfirmed value); A5 a gated state is not stale; B-minor 3 a file
entering metadata_only without a read loses its capture; B-minor 4 a changed capture on an indexed text image sends it
to the re-scoring pass."""
from dataclasses import replace
from datetime import datetime, timezone

from src.domain import drive_accuracy as acc
from src.domain.drive_enrichment import DriveProfile, Grade, GradedField, GradedValue, KeyValue
from src.domain.drive_index import (
    ACCURACY_VERSION, EXTRACTION_VERSION, DriveIndexRecord, DriveSyncState, RecordState, TriageVerdict,
)
from src.domain.drive_index_params import STALE_ALERT_AFTER
from src.domain.drive_index_planner import is_stale, triaged
from src.domain.drive_key_sources import RecordKeyInputs
from src.domain.drive_keys import about_keys_of, query_concepts
from src.domain.drive_tier_pool import ConceptHits, document_frequency, document_frequency_common
from src.domain.drive_tiers import Allowance, FileKeys, allowances, classify
from src.domain.drive_match import MatchTier
from src.domain.drive_enrichment_calls import TriageResultV2

IN, AS, NOT = Grade.IN_TEXT.value, Grade.IN_TEXT_AS.value, Grade.NOT_IN_TEXT.value
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def gv(field, grade, value="v"):
    return GradedValue(field=field.value, value=value, grade=grade)


class TestTopicsWeighNothing:     # Δ89
    def test_a_native_topic_is_graded_but_moves_nothing(self):
        assert acc.FIELD_WEIGHTS["topic_native"] == 0
        graded = [gv(GradedField.TOPIC_NATIVE, NOT, "t1"), gv(GradedField.TOPIC_NATIVE, NOT, "t2")]
        assert acc.verification_miss(graded) == 0.0
        assert acc.accuracy(graded, source=0.0, points=0) == 100

    def test_a_missing_topic_does_not_dilute_or_raise_the_miss_of_the_other_values(self):
        values = [gv(GradedField.DATE, NOT, "d"), gv(GradedField.ENTITY, IN, "e")]
        with_topics = values + [gv(GradedField.TOPIC_NATIVE, NOT, f"t{i}") for i in range(5)]
        assert acc.verification_miss(with_topics) == acc.verification_miss(values) == 3 / 5

    def test_a_card_whose_every_value_is_in_its_text_scores_100_whatever_its_topics(self):
        graded = [gv(GradedField.DATE, IN), gv(GradedField.TOPIC_NATIVE, NOT, "t")]
        assert acc.accuracy(graded, source=0.0, points=0) == 100


def _hits(doc_ids=(), scene_ids=()):
    h = ConceptHits()
    for i in doc_ids:
        h.add_about(i, {"e:travel"})
    for i in scene_ids:
        h.add_about(i, {"sl:travel"})
    return h


class TestSceneLabelsAreNotFrequency:     # Δ90
    def test_photos_found_only_by_a_scene_label_do_not_count_for_the_common_test(self):
        h = _hits(doc_ids=["d1", "d2"], scene_ids=[f"p{i}" for i in range(60)])
        assert len(h.files()) == 62 and h.document_files() == {"d1", "d2"}
        assert document_frequency_common([h]) == [2]
        assert document_frequency([h])[0][0] == 62       # the absence test and the card count keep every hit

    def test_a_word_held_only_by_scene_labels_is_present_not_absent_and_not_common(self):
        h = _hits(scene_ids=[f"p{i}" for i in range(60)])
        assert document_frequency_common([h]) == [1]
        assert document_frequency_common([ConceptHits()]) == [0]

    def test_a_photo_that_also_holds_the_word_elsewhere_still_counts(self):
        h = _hits(scene_ids=["p1"])
        h.add_about("p1", {"sl:travel", "e:travel"})     # added again with another key: now a document hit too
        h.scene.discard("p1")
        assert h.document_files() == {"p1"}

    def test_travel_insurance_still_requires_travel_in_a_photo_heavy_drive(self):
        concepts = list(query_concepts("travel insurance").concepts)
        travel = _hits(doc_ids=["d1"], scene_ids=[f"p{i}" for i in range(60)])
        insurance = ConceptHits()
        for i in ("d1", "d2"):
            insurance.add_about(i, {"e:insurance"})
        carded = 100
        before = allowances(concepts, dict(zip((c.word for c in concepts), document_frequency([travel, insurance])[0])),
                            carded)
        after = allowances(concepts, dict(zip((c.word for c in concepts), document_frequency_common([travel, insurance]))),
                           carded)
        assert "travel" in before.common            # the defect: sixty labelled photos made it missable
        assert after.common == frozenset()

    def test_a_cyrillic_document_word_stays_required_too(self):
        concepts = list(query_concepts("договор квартира").concepts)
        flat = _hits(doc_ids=["d1"], scene_ids=[f"p{i}" for i in range(80)])
        contract = ConceptHits()
        contract.add_about("d1", {"e:договор"})
        dfs = document_frequency_common([contract, flat]) if concepts[0].word == "договор" else \
            document_frequency_common([flat, contract])
        assert allowances(concepts, dict(zip((c.word for c in concepts), dfs)), 100).common == frozenset()


def _profile(printed, grade=IN, label="reference_number"):
    return DriveProfile(title="t", what_it_is="w", language="en", doc_type="other",
                        key_values=(KeyValue(label, printed, printed),),
                        graded=(GradedValue(field=GradedField.VALUE.value, value=printed, grade=grade),))


def _keys(profile):
    return set(about_keys_of(RecordKeyInputs(kind="pdf", profile=profile)))


def _tier(query, profile):
    keys = frozenset(about_keys_of(RecordKeyInputs(kind="pdf", profile=profile)))
    f = FileKeys(item_id="f", about_keys=keys, name_keys=frozenset(), chunk_keys={}, hint_keys={})
    match = classify(f, query_concepts(query).concepts, Allowance(), {}, {})
    return match.tier if match is not None else None


class TestUnspacedIds:     # Δ91
    def test_a_printed_id_with_a_space_is_also_keyed_unspaced(self):
        keys = _keys(_profile("QX 4471902"))
        assert {"v:4471902", "v:qx4471902"} <= keys

    def test_both_spellings_of_the_query_find_the_card_in_an_exact_tier(self):
        profile = _profile("QX 4471902")
        assert _tier("QX4471902", profile) is MatchTier.ABOUT
        assert _tier("qx-4471902", profile) is MatchTier.ABOUT
        assert _tier("QX 4471902", profile) is MatchTier.ABOUT

    def test_a_hyphenated_digit_group_is_keyed_whole(self):
        assert "v:123456789" in _keys(_profile("123-456-789"))

    def test_an_amount_or_a_date_is_never_keyed_unspaced(self):
        for printed in ("1.250,00", "12 05 2024", "2024-05-12"):
            keys = {k for k in _keys(_profile(printed)) if k.startswith("v:")}
            assert "v:" + "".join(ch for ch in printed if ch.isalnum()).lower() not in keys, printed

    def test_a_value_the_text_does_not_hold_stays_automatic(self):
        keys = _keys(_profile("QX 4471902", grade=NOT))
        assert not [k for k in keys if k.startswith("v:")]
        assert "va:4471902" in keys

    def test_a_value_without_digits_gets_no_extra_form(self):
        assert {k for k in _keys(_profile("Acme Corp")) if k.startswith("v:")} == {"v:acmecorp"}


def _state(**kw):
    return DriveSyncState(user_id="u", connection_id="u", created_at=NOW - STALE_ALERT_AFTER * 3, last_success_at=NOW - STALE_ALERT_AFTER * 2,
                          **kw)


class TestGatedStateIsNotStale:     # A5
    def test_a_state_built_by_v1_awaiting_the_rebuild_is_not_stale(self):
        assert is_stale(_state(applied_versions={"policy": 2}), None, NOW) is False

    def test_a_current_state_with_the_gate_key_still_goes_stale(self):
        assert is_stale(_state(applied_versions={"policy": 2, "accuracy": 1}), None, NOW) is True

    def test_a_state_with_no_versions_at_all_still_goes_stale(self):
        assert is_stale(_state(), None, NOW) is True


def _text_image(**kw):
    base = dict(user_id="u", item_id="i", name="a.jpg", kind="image", state=RecordState.INDEXED, indexed_generation=3,
                content_version="c1", indexed_version="c1", extraction_version=EXTRACTION_VERSION, capture="scan", capture_source="automatic", accuracy_version=ACCURACY_VERSION,
                card_accuracy=90)
    base.update(kw)
    return DriveIndexRecord(**base)


class TestTriageRescoring:     # B-minor 4
    def _verdict(self, capture):
        return TriageResultV2(item_id="i", verdict=TriageVerdict.TEXT, capture=capture)

    def test_a_changed_capture_on_an_indexed_text_image_resets_the_accuracy_version(self):
        out = triaged(_text_image(capture="", capture_source="", accuracy_version=ACCURACY_VERSION),
                      self._verdict("screenshot"), NOW)
        assert out.capture == "screenshot" and out.accuracy_version == 0
        scorable = replace(out, profile=_profile("x"), profile_source="model")
        assert not acc.accuracy_settled(scorable)       # the upgrade pass picks it up

    def test_an_unchanged_capture_keeps_its_score_version(self):
        out = triaged(_text_image(capture="scan", capture_source="automatic"), self._verdict("scan"), NOW)
        assert out.capture == "scan" and out.accuracy_version == ACCURACY_VERSION

    def test_a_pending_image_is_left_alone(self):
        out = triaged(_text_image(state=RecordState.PENDING, indexed_generation=None, capture="", capture_source=""),
                      self._verdict("scan"), NOW)
        assert out.accuracy_version == ACCURACY_VERSION
