"""S3-D11: a merge's `reported_date` is never earlier than its newest source ("when the owner last stated it" never
moves back), on every pass. `reported_date_for_merge` is pure, so the rows run on the real function."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.fact_write_context import reported_date_for_merge
from tests.support.fact_write import ctx

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
SPAN = (NOW - timedelta(days=80), NOW - timedelta(days=79))      # a late Stage 1 batch about August
FRESH = NOW - timedelta(days=4)                                   # a source restated after the batch's span
OLD = NOW - timedelta(days=100)


def test_a_late_stage1_batch_cannot_date_a_merge_before_a_fresher_source():
    merged = reported_date_for_merge(ctx("stage1", span=SPAN), SPAN[1], [OLD, FRESH], NOW)
    assert merged == FRESH


def test_a_late_stage1_batch_without_a_model_date_takes_the_fresher_source_over_the_span_end():
    assert reported_date_for_merge(ctx("stage1", span=SPAN), None, [OLD, FRESH], NOW) == FRESH


def test_an_email_pass_cannot_date_a_merge_before_a_fresher_source():
    email = ctx("email", span=(NOW - timedelta(days=300), NOW - timedelta(days=299)))
    merged = reported_date_for_merge(email, NOW - timedelta(days=299), [FRESH], NOW)
    assert merged == FRESH


def test_a_model_date_inside_the_span_but_before_a_source_is_lifted_to_the_source():
    span = (NOW - timedelta(days=30), NOW - timedelta(days=5))
    inside = NOW - timedelta(days=20)
    source = NOW - timedelta(days=10)
    assert reported_date_for_merge(ctx("stage1", span=span), inside, [source], NOW) == source


def test_a_model_date_after_every_source_is_kept():
    span = (NOW - timedelta(days=30), NOW - timedelta(days=5))
    model = NOW - timedelta(days=7)
    sources = [NOW - timedelta(days=25), NOW - timedelta(days=12)]
    assert reported_date_for_merge(ctx("stage1", span=span), model, sources, NOW) == model


def test_a_model_date_after_the_span_is_still_clamped_before_the_comparison():
    span = (NOW - timedelta(days=30), NOW - timedelta(days=5))
    merged = reported_date_for_merge(ctx("stage1", span=span), NOW, [NOW - timedelta(days=12)], NOW)
    assert merged == span[1]


def test_no_source_dates_leave_the_old_conversation_rule():
    span = (NOW - timedelta(days=30), NOW - timedelta(days=5))
    assert reported_date_for_merge(ctx("stage1", span=span), None, [], NOW) == span[1]
    assert reported_date_for_merge(ctx("stage1", span=None), None, [], NOW) == NOW


@pytest.mark.parametrize("actor", ["stage2a", "stage2b", "cluster", "backstop"])
def test_a_cleanup_merge_still_takes_exactly_the_newest_source_date(actor):
    cleanup = ctx(actor, span=SPAN, model_dates=False)
    assert reported_date_for_merge(cleanup, SPAN[0], [OLD, FRESH], NOW) == FRESH
