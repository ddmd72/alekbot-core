"""drive_accuracy (USER_DRIVE_INDEXING_RFC §26.4, D35, Δ61): the formula, §26.4's worked examples pinned (88, 94, 100,
0), the weights table pinned to ACCURACY_VERSION, the source share from the capture (and `extracted_by`), reading
points, no score on photographs or cards with nothing model-written, and the score never in the card text."""
from datetime import datetime, timezone

import pytest

from src.domain import drive_accuracy as acc
from src.domain.drive_card import card_hash, card_text
from src.domain.drive_enrichment import DriveProfile, Grade, GradedField, GradedValue
from src.domain.drive_index import (
    ACCURACY_VERSION, CURRENT_VERSIONS, DocumentBrief, DriveChunk, DriveIndexRecord, TriageVerdict,
)

IN, AS, NOT = Grade.IN_TEXT.value, Grade.IN_TEXT_AS.value, Grade.NOT_IN_TEXT.value
# ACCURACY_VERSION 1 is unreleased: Δ85's `digital_model_read` row re-pins it (without the row: f0fb92a4…)
# ACCURACY_VERSION 2: the grader recognises more printed date forms (grades change, weights table does not) -> same digest
WEIGHTS_PINS = {1: "925598b63241851381294e996eefcb893ec1a7f5b1774ca00319df4ffa75369b",
                2: "925598b63241851381294e996eefcb893ec1a7f5b1774ca00319df4ffa75369b"}      # ACCURACY_VERSION -> weights_digest(); a changed weight is a version bump


def gv(field, grade, value="v"):
    return GradedValue(field=field.value, value=value, grade=grade)


def twenty(date_grade):
    """§26.4's example: one date (weight 3) and nineteen values weighing 2 on average (here: 19 entities, weight 2)."""
    return [gv(GradedField.DATE, date_grade, "d")] + [gv(GradedField.ENTITY, IN, f"e{i}") for i in range(19)]


def rec(**kw):
    base = dict(user_id="u", item_id="i", name="f.pdf", kind="pdf", indexed_generation=1)
    base.update(kw)
    return DriveIndexRecord(**base)


class TestWorkedExamples:
    def test_a_scan_with_one_date_missing_is_88(self):
        assert acc.accuracy(twenty(NOT), source=0.05, points=0) == 88

    def test_the_same_date_only_written_differently_is_94(self):
        assert acc.accuracy(twenty(AS), source=0.05, points=0) == 94

    def test_native_text_with_every_value_in_its_text_is_100(self):
        assert acc.accuracy(twenty(IN), source=0.0, points=0) == 100

    def test_a_card_whose_only_value_is_not_in_the_text_is_0(self):
        assert acc.accuracy([gv(GradedField.VALUE, NOT)], source=0.0, points=0) == 0

    def test_no_graded_value_means_no_miss(self):
        assert acc.verification_miss([]) == 0.0 and acc.accuracy([], source=0.0, points=0) == 100

    def test_the_score_is_clamped(self):
        assert acc.accuracy([], source=0.0, points=500) == 0
        assert acc.accuracy([], source=-1.0, points=0) == 100


class TestWeights:
    def test_the_table(self):
        assert acc.FIELD_WEIGHTS == {"date": 3, "value": 3, "number": 3, "entity": 2, "topic_native": 0}
        assert acc.GRADE_MISS == {IN: 0.0, AS: 0.1, NOT: 1.0}
        assert acc.SOURCE_SHARE == {"native": 0.0, "digital": 0.0, "screenshot": 0.03, "scan": 0.05,
                                    "document_photo": 0.08, "model_unknown": 0.05,
                                    "digital_model_read": 0.03}   # Δ85
        assert acc.READING_POINTS == {"pages_refused": 5, "read_by_fallback": 3, "truncated": 2}

    def test_the_weights_digest_is_pinned_to_the_version(self):
        assert ACCURACY_VERSION == 2
        # a change to any weight changes this digest: bump ACCURACY_VERSION and pin the new digest beside the old one
        assert WEIGHTS_PINS.get(ACCURACY_VERSION) == acc.weights_digest(), acc.weights_digest()

    def test_the_digest_covers_every_table(self, monkeypatch):
        before = acc.weights_digest()
        for name in ("FIELD_WEIGHTS", "GRADE_MISS", "SOURCE_SHARE", "READING_POINTS"):
            monkeypatch.setattr(acc, name, {**getattr(acc, name), "extra": 1})
            assert acc.weights_digest() != before, name
            monkeypatch.undo()

    def test_the_version_is_a_current_version_under_accuracy(self):
        assert CURRENT_VERSIONS["accuracy"] == ACCURACY_VERSION and acc.ACCURACY_VERSION == ACCURACY_VERSION

    def test_a_field_this_version_does_not_know_weighs_nothing(self):
        assert acc.verification_miss([GradedValue("future", "x", NOT), gv(GradedField.DATE, IN)]) == 0.0


class TestSourceShare:
    @pytest.mark.parametrize("capture, share", [("scan", 0.05), ("document_photo", 0.08), ("screenshot", 0.03),
                                                ("digital", 0.03)])   # Δ85: a model's reading of a digital file
    def test_by_capture(self, capture, share):
        assert acc.source_share(rec(capture=capture, capture_source="detected", extracted_by="reader@2")) == share

    def test_a_parser_read_digital_file_is_native_text(self):
        # Δ85 splits `digital` by extractor: the parser's text layer stays 0
        assert acc.source_share(rec(capture="digital", capture_source="detected", extracted_by="parser@3")) == 0.0

    def test_a_parser_read_text_with_no_capture_is_native(self):
        assert acc.source_share(rec(extracted_by="parser@3")) == 0.0
        assert acc.source_share(rec()) == 0.0

    def test_a_model_read_file_of_unknown_capture_is_0_05(self):
        assert acc.source_share(rec(extracted_by="reader@2")) == 0.05

    def test_an_unknown_capture_value_follows_the_extractor(self):
        assert acc.source_share(rec(capture="hologram", extracted_by="reader@2")) == 0.05
        assert acc.source_share(rec(capture="hologram", extracted_by="parser@3")) == 0.0


class TestReadingPoints:
    def test_each_problem_costs_its_points_once(self):
        assert acc.reading_points(rec()) == 0
        assert acc.reading_points(rec(pages_refused=1)) == 5
        assert acc.reading_points(rec(pages_refused=7)) == 5
        assert acc.reading_points(rec(read_by_fallback=True)) == 3
        assert acc.reading_points(rec(truncated=True)) == 2
        assert acc.reading_points(rec(pages_refused=2, read_by_fallback=True, truncated=True)) == 10

    def test_they_come_off_the_score(self):
        r = rec(capture="scan", capture_source="detected", pages_refused=1, read_by_fallback=True, truncated=True)
        assert acc.card_accuracy(r, []) == 95 - 10


class TestWhoCarriesAScore:
    PROFILE = DriveProfile(title="t", what_it_is="w", language="en", doc_type="other",
                           graded=(gv(GradedField.DATE, NOT),))

    def test_a_model_profiled_file_carries_its_profiles_grades(self):
        r = rec(profile=self.PROFILE, profile_source="model")
        graded = acc.card_graded(r, [])
        assert graded == self.PROFILE.graded and acc.card_accuracy(r, graded) == 0

    def test_a_photograph_carries_none(self):
        r = rec(kind="image", triage_verdict=TriageVerdict.PHOTO, profile=self.PROFILE, profile_source="model")
        assert acc.card_graded(r, []) is None and acc.card_accuracy(r, None) is None

    def test_a_folder_a_file_with_no_readable_text_and_a_bare_record_carry_none(self):
        assert acc.card_graded(rec(is_folder=True, profile=self.PROFILE, profile_source="model"), []) is None
        assert acc.card_graded(rec(indexed_generation=None, profile=self.PROFILE, profile_source="model"), []) is None
        assert acc.card_graded(rec(), []) is None

    def test_a_template_profile_with_no_brief_carries_none(self):
        assert acc.card_graded(rec(profile=self.PROFILE, profile_source="template"), []) is None

    def test_a_brief_only_file_carries_its_briefs_grades(self):
        text = "Invoice dated 12.03.2024 total 1 250,00 EUR for Maria Doe."
        chunk = DriveChunk(user_id="u", connection_id="u", item_id="i", generation=1, index=0, text=text, char_start=0,
                           char_end=len(text), kind="pdf")
        brief = DocumentBrief(doc_date=datetime(2024, 3, 12, tzinfo=timezone.utc), amount="1 250,00 EUR",
                              parties=("Maria Doe", "Nobody Corp"))
        r = rec(brief=brief, extracted_by="reader@2", capture="scan", capture_source="detected")
        graded = acc.card_graded(r, [chunk])
        assert {(g.field, g.grade) for g in graded} == {("date", IN), ("value", IN), ("entity", IN), ("entity", NOT)}
        score = acc.card_accuracy(r, graded)
        assert score == round(100 * 0.95 * (1 - 2 / 10))      # weights 3 + 3 + 2 + 2 = 10, the one miss weighs 2

    def test_a_template_profile_with_a_brief_falls_to_the_brief(self):
        brief = DocumentBrief(amount="9.99 EUR")
        chunk = DriveChunk(user_id="u", connection_id="u", item_id="i", generation=1, index=0, text="nothing", char_start=0, char_end=7,
                           kind="pdf")
        graded = acc.card_graded(rec(profile=self.PROFILE, profile_source="template", brief=brief), [chunk])
        assert [(g.field, g.grade) for g in graded] == [("value", NOT)]


class TestScoreIsNotInTheCard:
    def test_the_score_changes_neither_card_text_nor_card_hash(self):
        profile = DriveProfile(title="t", what_it_is="w", language="en", doc_type="other")
        a = rec(profile=profile, profile_source="model", card_accuracy=10, accuracy_version=1, pages_refused=3)
        b = rec(profile=profile, profile_source="model", card_accuracy=99, accuracy_version=2, read_by_fallback=True)
        assert card_text(a) == card_text(b) and card_hash(a) == card_hash(b)
        assert "accuracy" not in card_text(a)
