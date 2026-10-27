"""The review's and the owner's date rules (Facts S4 plan §2.1, §3.6, T2): `reported_date_for_update` with `evidence_at`,
the review note, and `review_horizon`."""

from datetime import date, datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind
from src.domain.exceptions import FactBornDueError
from src.domain.fact_expiry import EXPIRY_NONE, Expiry
from src.domain.fact_lifecycle import compute_expiry, horizon_from_now
from src.domain.fact_write_context import (
    REVIEW_REPORTED_DATE_NOTE,
    kept_reported_date_note,
    reported_date_for_update,
    review_horizon,
    reviews_stored_memory,
)
from src.domain.pass_actor import PassActor
from tests.support.fact_write import ctx

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
STORED = NOW - timedelta(days=40, hours=3)
SPAN = (NOW - timedelta(days=1), NOW - timedelta(hours=2))
REVIEW_ACTORS = [a for a in PassActor if reviews_stored_memory(ctx(str(a)))]
OTHER_ACTORS = [a for a in PassActor if a not in REVIEW_ACTORS]


def test_the_review_actors_are_the_rows_that_reach_a_stale_fact():
    # P2 revised (C9): TIMEOUT takes the owner's row, so it reaches a stale fact too.
    assert REVIEW_ACTORS == [PassActor.REVIEW, PassActor.OWNER, PassActor.TIMEOUT]


# --- reported_date_for_update ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("content_change", [False, True])
def test_without_evidence_the_stored_date_stays(actor, content_change):
    assert reported_date_for_update(ctx(str(actor), span=SPAN), None, STORED, content_change, NOW) == (STORED, None)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_a_later_evidence_date_is_taken(actor):
    evidence = NOW - timedelta(days=2)
    assert reported_date_for_update(ctx(str(actor)), None, STORED, False, NOW, evidence_at=evidence) == (evidence, None)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_an_earlier_evidence_date_never_moves_the_date_back(actor):
    earlier = STORED - timedelta(days=9)
    assert reported_date_for_update(ctx(str(actor)), None, STORED, False, NOW, evidence_at=earlier) == (STORED, None)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_a_naive_evidence_date_is_read_as_utc(actor):
    naive = (NOW - timedelta(days=2)).replace(tzinfo=None)
    got, _ = reported_date_for_update(ctx(str(actor)), None, STORED, False, NOW, evidence_at=naive)
    assert got == NOW - timedelta(days=2) and got.tzinfo is not None


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("evidence_at", [None, NOW - timedelta(days=2)])
def test_a_model_date_is_never_used_and_gets_the_review_note(actor, evidence_at):
    got, note = reported_date_for_update(ctx(str(actor), span=SPAN), NOW, STORED, True, NOW, evidence_at=evidence_at)
    assert got == (evidence_at or STORED)
    assert note == REVIEW_REPORTED_DATE_NOTE


@pytest.mark.parametrize("actor", OTHER_ACTORS)
@pytest.mark.parametrize("model_value", [None, NOW - timedelta(hours=5)])
@pytest.mark.parametrize("content_change", [False, True])
def test_every_other_pass_ignores_evidence_at(actor, model_value, content_change):
    context = ctx(str(actor), span=SPAN)
    assert reported_date_for_update(
        context, model_value, STORED, content_change, NOW, evidence_at=NOW - timedelta(hours=1)
    ) == reported_date_for_update(context, model_value, STORED, content_change, NOW)


@pytest.mark.parametrize("actor", [a for a in OTHER_ACTORS if not a.model_dates])
def test_a_cleanup_pass_keeps_its_own_note(actor):
    assert reported_date_for_update(ctx(str(actor)), NOW, STORED, False, NOW) == (
        STORED, kept_reported_date_note(STORED, "Europe/Lisbon"))


def test_the_review_note_is_shaped_like_the_kept_note_and_says_horizons_count_from_now():
    assert REVIEW_REPORTED_DATE_NOTE.startswith("reported_date is kept by the system in this pass")
    assert REVIEW_REPORTED_DATE_NOTE.endswith("expiry horizons count from now")
    assert REVIEW_REPORTED_DATE_NOTE != kept_reported_date_note(STORED, "UTC")


# --- review_horizon --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("days", [2, 14, 30, 365])
@pytest.mark.parametrize("reported", [STORED, NOW - timedelta(seconds=1), NOW])
def test_a_horizon_counts_from_now_and_is_stored_from_reported(actor, days, reported):
    fields, note = review_horizon(Expiry(ExpiryKind.HORIZON, days=days), reported, NOW, ctx(str(actor)))
    assert note is None
    assert fields == horizon_from_now(days, reported, NOW)
    assert fields["expires_at"] == reported + timedelta(days=fields["ttl_days"])
    assert NOW + timedelta(days=days) <= fields["expires_at"] < NOW + timedelta(days=days + 1)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_a_horizon_after_a_later_reported_date_counts_from_that_date(actor):
    later = NOW + timedelta(days=3)  # never a real case, but the base is the later of the two
    fields, _ = review_horizon(Expiry(ExpiryKind.HORIZON, days=10), later, NOW, ctx(str(actor)))
    assert fields["expires_at"] == later + timedelta(days=10) and fields["ttl_days"] == 10


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
def test_a_future_date_and_none_are_stored_as_s3_stores_them(actor):
    context = ctx(str(actor))
    future = Expiry(ExpiryKind.DATE, on=date(2027, 7, 31))  # summer: in winter Lisbon's end of day is UTC midnight
    assert review_horizon(future, STORED, NOW, context) == (compute_expiry(future, STORED, "Europe/Lisbon"), None)
    assert review_horizon(EXPIRY_NONE, STORED, NOW, context) == (
        {"expiry_kind": ExpiryKind.NONE, "ttl_days": None, "expires_at": None}, None)


@pytest.mark.parametrize("actor", REVIEW_ACTORS)
@pytest.mark.parametrize("expiry", [
    Expiry(ExpiryKind.DATE, on=date(2026, 10, 20)),
    Expiry(ExpiryKind.DATE, on=date(2026, 10, 24)),
    Expiry(ExpiryKind.HORIZON, days=1),
])
def test_a_due_decision_is_refused_with_no_floor(actor, expiry):
    reported = NOW - timedelta(days=40)  # whole days: a one-day horizon ends exactly at now + 1 day
    with pytest.raises(FactBornDueError, match="this pass reviews stored memory: give a horizon"):
        review_horizon(expiry, reported, NOW, ctx(str(actor)))


@pytest.mark.parametrize("actor", OTHER_ACTORS)
def test_a_review_horizon_outside_the_review_or_the_owner_is_refused(actor):
    with pytest.raises(ValueError, match="belongs to the review or the owner"):
        review_horizon(Expiry(ExpiryKind.HORIZON, days=30), STORED, NOW, ctx(str(actor)))
