"""decide_expiry: the one place an expiry decision becomes stored fields (S4 T2d, hexagonal review M1)."""

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind
from src.domain.exceptions import FactBornDueError
from src.domain.fact_expiry import parse_expiry
from src.domain.fact_write_context import FactWriteContext, decide_expiry
from src.domain.pass_actor import PassActor
from src.domain.refusal_wording import RefusalWording
from tests.support.fact_write import EXPIRY_NONE, horizon, on

PARIS = "Europe/Paris"
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)  # 11:00 in Paris
REPORTED = NOW - timedelta(hours=2)
ALL_PASSES = list(PassActor)
FLOORING = [actor for actor in PassActor if actor.floors_due_horizon]
NOT_FLOORING = [actor for actor in PassActor if not actor.floors_due_horizon]


def decide(expiry, actor, reported=REPORTED):
    context = FactWriteContext.for_pass(actor, PARIS)
    return decide_expiry(parse_expiry(expiry), reported, NOW, context)


def test_the_table_has_passes_on_both_sides_of_the_floor():
    assert FLOORING and NOT_FLOORING


@pytest.mark.parametrize("actor", ALL_PASSES)
def test_a_date_ahead_is_kept_to_the_end_of_that_day_in_the_owners_zone(actor):
    fields, note = decide(on("2026-10-30"), actor)
    assert fields["expiry_kind"] is ExpiryKind.DATE and fields["ttl_days"] is None
    assert fields["expires_at"] == datetime(2026, 10, 30, 23, 0, tzinfo=timezone.utc)  # midnight on the 31st in Paris
    assert note is None


@pytest.mark.parametrize("actor", ALL_PASSES)
def test_a_horizon_ahead_counts_from_the_reported_date(actor):
    fields, note = decide(horizon(14), actor)
    assert (fields["expiry_kind"], fields["ttl_days"]) == (ExpiryKind.HORIZON, 14)
    assert fields["expires_at"] == REPORTED + timedelta(days=14)
    assert note is None


@pytest.mark.parametrize("actor", FLOORING)
def test_a_due_horizon_on_a_flooring_pass_is_floored_to_two_days_from_now_with_a_note(actor):
    fields, note = decide(horizon(1), actor)
    assert fields["expiry_kind"] is ExpiryKind.HORIZON and fields["ttl_days"] == 1
    assert fields["expires_at"] == NOW + timedelta(days=2)
    assert note is not None and "this horizon had already passed" in note and "2026-10-25" in note


# The text is the row's (S4 T2): Stage 1 names the horizon it was given; the review's names when it ends and its way out.
DUE_HORIZON_TEXT = {
    RefusalWording.STAGE1: "a horizon of 1 days ends",
    RefusalWording.REVIEW: "this horizon ends 2026-10-24, too soon: this pass reviews stored memory",
}


@pytest.mark.parametrize("actor", NOT_FLOORING)
def test_a_due_horizon_on_a_pass_that_does_not_floor_is_refused(actor):
    with pytest.raises(FactBornDueError, match=DUE_HORIZON_TEXT[actor.refusal_wording]):
        decide(horizon(1), actor)


@pytest.mark.parametrize("actor", ALL_PASSES)
def test_a_past_date_is_refused_on_every_pass(actor):
    with pytest.raises(FactBornDueError, match="the date 2026-10-20 is past"):
        decide(on("2026-10-20"), actor)


@pytest.mark.parametrize("actor", ALL_PASSES)
def test_none_gives_null_fields(actor):
    fields, note = decide(EXPIRY_NONE, actor)
    assert (fields["expiry_kind"], fields["ttl_days"], fields["expires_at"]) == (ExpiryKind.NONE, None, None)
    assert note is None
