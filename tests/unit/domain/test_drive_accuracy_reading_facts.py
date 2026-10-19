"""Reading facts and the accuracy bookkeeping on the record (USER_DRIVE_INDEXING_RFC §26.4, Δ61): the planner copies
`pages_refused` and the fallback flag from the extraction outcome; `scored` writes score and version;
`accuracy_settled` is the upgrade pass's test; `regraded` moves grades and dates, nothing of the profile job."""
from dataclasses import replace
from datetime import datetime, timezone

from src.domain import drive_accuracy as acc
from src.domain.drive_enrich_plan import regraded
from src.domain.drive_enrichment import DriveProfile, Grade, GradedField, GradedValue, ProfileSource
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import ACCURACY_VERSION, DriveIndexRecord, TriageVerdict
from src.domain.drive_index_planner import demoted, indexed
from src.domain.drive_index import Reason

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)


def _rec(**kw):
    base = dict(user_id="u", item_id="i", parent_id="p", name="a.pdf", kind="pdf", indexed_generation=1,
                profile=DriveProfile(title="t", what_it_is="w", language="en", doc_type="contract",
                                     graded=(GradedValue(GradedField.DATE.value, "2031-05-07", Grade.NOT_IN_TEXT.value),)),
                profile_source=ProfileSource.MODEL.value)
    base.update(kw)
    return DriveIndexRecord(**base)


def test_the_outcome_says_whether_the_fallback_served_a_window():
    assert ExtractionOutcome(status=ExtractionStatus.TEXT, model_fallback_cost=0.002).read_by_fallback
    assert not ExtractionOutcome(status=ExtractionStatus.TEXT, model_cost=0.5).read_by_fallback


def test_indexed_copies_the_reading_facts_and_a_later_clean_read_clears_them():
    outcome = ExtractionOutcome(status=ExtractionStatus.TEXT, text="x", pages_refused=3, model_fallback_cost=0.01)
    done = indexed(_rec(), outcome=outcome, generation=2, chunk_count=1, now=NOW)
    assert (done.pages_refused, done.read_by_fallback) == (3, True)
    clean = indexed(done, outcome=replace(outcome, pages_refused=0, model_fallback_cost=0.0), generation=3,
                    chunk_count=1, now=NOW)
    assert (clean.pages_refused, clean.read_by_fallback) == (0, False)


def test_a_demoted_record_carries_no_score_and_no_reading_facts_of_an_older_read():
    before = _rec(card_accuracy=80, accuracy_version=ACCURACY_VERSION, pages_refused=2, read_by_fallback=True)
    done = demoted(before, Reason.NO_TEXT, NOW)
    assert (done.card_accuracy, done.pages_refused, done.read_by_fallback) == (None, 0, False)
    assert acc.accuracy_settled(done)   # not scorable: never holds the key


def test_scored_writes_the_score_and_the_version():
    done = acc.scored(_rec(), [])
    assert (done.card_accuracy, done.accuracy_version) == (0, ACCURACY_VERSION)   # its one value is not in the text


def test_a_photograph_and_a_folder_are_settled_without_a_score():
    assert acc.accuracy_settled(_rec(triage_verdict=TriageVerdict.PHOTO))
    assert acc.accuracy_settled(_rec(is_folder=True))
    assert not acc.accuracy_settled(_rec(accuracy_version=ACCURACY_VERSION - 1 if ACCURACY_VERSION > 1 else 0))
    assert acc.accuracy_settled(_rec(accuracy_version=ACCURACY_VERSION))


def test_regraded_moves_the_grades_and_dates_and_nothing_of_the_profile_job():
    rec = _rec(profile_version=2, profile_fingerprint="fp", profiled_content_version=1, content_version=5,
               valid_until=datetime(2031, 5, 7, tzinfo=timezone.utc))
    out = regraded(rec, replace(rec.profile, title="t2"), NOW)
    assert out.profile.title == "t2" and out.valid_until is None   # a profile with no confirmed date moves no expiry
    assert (out.profile_version, out.profile_fingerprint, out.profiled_content_version) == (2, "fp", 1)
