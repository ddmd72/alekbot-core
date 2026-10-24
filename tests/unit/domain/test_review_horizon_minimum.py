"""A review or owner horizon is at least MIN_REVIEW_HORIZON_DAYS (Facts S4 plan §3.6 "N < 2", T2 review M1), whatever the
offset of `reported_date`: rounding the stored `ttl_days` up must not let a one-day horizon through."""

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind
from src.domain.exceptions import FactBornDueError
from src.domain.fact_expiry import Expiry
from src.domain.fact_lifecycle import MIN_REVIEW_HORIZON_DAYS, horizon_from_now, resolves_too_soon, short_review_horizon
from src.domain.fact_write_context import review_horizon, reviews_stored_memory
from src.domain.pass_actor import PassActor
from tests.support.fact_write import ctx

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
REVIEW_ROWS = [a for a in PassActor if reviews_stored_memory(ctx(str(a)))]
# Offsets of reported_date before now: whole days and the non-whole ones the rounding used to let a 1-day horizon pass.
WHOLE = [timedelta(days=40), timedelta(0)]
NON_WHOLE = [timedelta(days=40, hours=5, minutes=17), timedelta(days=3, microseconds=1), timedelta(hours=23, minutes=59)]


def test_the_minimum_is_two_days():
    assert MIN_REVIEW_HORIZON_DAYS == 2


@pytest.mark.parametrize("offset", NON_WHOLE)
def test_the_rounding_alone_lets_a_one_day_horizon_through_at_a_non_whole_offset(offset):
    """The premise of M1: without the explicit minimum, born-due would accept these."""
    reported = NOW - offset
    assert not resolves_too_soon(horizon_from_now(1, reported, NOW)["expires_at"], NOW)


@pytest.mark.parametrize("actor", REVIEW_ROWS)
@pytest.mark.parametrize("offset", NON_WHOLE + WHOLE)
def test_a_one_day_horizon_is_refused_at_any_offset(actor, offset):
    with pytest.raises(FactBornDueError) as refused:
        review_horizon(Expiry(ExpiryKind.HORIZON, days=1), NOW - offset, NOW, ctx(str(actor)))
    assert str(refused.value) == str(short_review_horizon(1))


@pytest.mark.parametrize("actor", REVIEW_ROWS)
def test_the_refusal_names_the_minimum_and_the_review_way_out_only(actor):
    with pytest.raises(FactBornDueError) as refused:
        review_horizon(Expiry(ExpiryKind.HORIZON, days=1), NOW - NON_WHOLE[0], NOW, ctx(str(actor)))
    text = str(refused.value)
    assert "at least 2 days" in text and "counted from now" in text
    assert "this pass reviews stored memory: give a horizon (counted from now)" in text
    assert "DISCARD" not in text and "candidate" not in text and "history" not in text


@pytest.mark.parametrize("actor", REVIEW_ROWS)
@pytest.mark.parametrize("offset", NON_WHOLE + WHOLE + [timedelta(days=400, seconds=7)])
def test_a_two_day_horizon_passes_at_any_offset(actor, offset):
    reported = NOW - offset
    fields, note = review_horizon(Expiry(ExpiryKind.HORIZON, days=2), reported, NOW, ctx(str(actor)))
    assert note is None and fields == horizon_from_now(2, reported, NOW)
    assert fields["expires_at"] >= NOW + timedelta(days=2)
