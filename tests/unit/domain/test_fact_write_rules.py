"""The write-semantics predicates the adapter used to decide itself, and the merge note (S4 T2d, review M2)."""

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.fact_update import parse_fact_update
from src.domain.fact_update_plan import concurrent_update_refusal, merge_source_moved_refusal, successor_refusal
from src.domain.fact_write_context import kept_reported_date_note, reported_date_for_merge
from tests.support.fact_write import ctx

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
SPAN = (NOW - timedelta(days=2), NOW - timedelta(days=1))
SOURCE = NOW - timedelta(days=10)


# --- an update that landed on another version -----------------------------------------------------------------------


@pytest.mark.parametrize("updates", [{"content": "new text"}, {"tags": ["a"]}, {"content": "x", "tags": ["a"]}])
def test_content_or_tags_on_another_version_are_refused_with_the_fact_id(updates):
    refusal = concurrent_update_refusal("fact-1", "v1", "v2", parse_fact_update(updates))
    assert refusal == "fact fact-1 changed concurrently; search again"


@pytest.mark.parametrize("updates", [{"content": "new text"}, {"tags": ["a"]}, {"state": "archived"}, {"metadata": {"a": 1}}])
def test_the_same_version_passes(updates):
    assert concurrent_update_refusal("fact-1", "v1", "v1", parse_fact_update(updates)) is None


@pytest.mark.parametrize("updates", [{"state": "archived"}, {"metadata": {"a": 1}}, {"temporal_class": "STABLE"}])
def test_a_field_that_does_not_replace_whole_passes_on_another_version(updates):
    assert concurrent_update_refusal("fact-1", "v1", "v2", parse_fact_update(updates)) is None


# --- a merge source that moved --------------------------------------------------------------------------------------


def test_merge_sources_that_resolve_elsewhere_are_refused():
    assert merge_source_moved_refusal(["a", "b"], ["a", "c"]) == "source changed; search again"
    assert merge_source_moved_refusal(["a", "b"], ["b", "a"]) == "source changed; search again"
    assert merge_source_moved_refusal(["a", "b"], ["a"]) == "source changed; search again"


def test_merge_sources_that_resolve_the_same_pass():
    assert merge_source_moved_refusal(["a", "b"], ["a", "b"]) is None
    assert merge_source_moved_refusal([], []) is None


# --- a successor that is the fact being closed ----------------------------------------------------------------------


def test_a_successor_equal_to_the_closing_fact_is_refused():
    assert successor_refusal("x", "x") == "superseded_by must name a different fact than the one being closed"


def test_a_different_successor_passes():
    assert successor_refusal("y", "x") is None


# --- reported_date_for_merge's note ---------------------------------------------------------------------------------


def test_a_pass_whose_model_dates_do_not_count_says_so_when_the_call_carried_a_date():
    reported, note = reported_date_for_merge(ctx("stage2a", span=SPAN), SPAN[0], [SOURCE], NOW)
    assert reported == SOURCE
    assert note == [kept_reported_date_note(SOURCE, "Europe/Lisbon")]


def test_a_conversation_pass_with_a_model_date_has_no_note():
    reported, note = reported_date_for_merge(ctx("stage1", span=SPAN), SPAN[0], [SOURCE], NOW)
    assert reported == SPAN[0] and note == []


def test_a_pass_whose_model_dates_do_not_count_has_no_note_without_a_model_date():
    reported, note = reported_date_for_merge(ctx("stage2a", span=SPAN), None, [SOURCE], NOW)
    assert reported == SOURCE and note == []
