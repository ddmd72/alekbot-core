"""FactWriteContext and the reported_date rules of every write (Facts S3, plan §3.2, §3.5)."""

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.fact_write_context import (
    NO_READABLE_SOURCE_NOTE,
    FactWriteContext,
    as_utc,
    clamp_to_span,
    kept_reported_date_note,
    reported_date_for_merge,
    reported_date_for_new,
    reported_date_for_update,
    span_of,
)
from tests.support.fact_write import ctx

UTC = timezone.utc
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
FIRST = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
LAST = datetime(2026, 10, 22, 17, 30, tzinfo=UTC)
SPAN = (FIRST, LAST)
STAGE1 = ctx("stage1", span=SPAN)
STAGE2A = ctx("stage2a", span=SPAN)


# --- the context ------------------------------------------------------------------------------------------------------


def test_a_naive_span_is_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        FactWriteContext("stage1", "UTC", (datetime(2026, 10, 21), LAST))


def test_a_backwards_span_is_refused():
    with pytest.raises(ValueError, match="earlier to the later"):
        FactWriteContext("stage1", "UTC", (LAST, FIRST))


@pytest.mark.parametrize("zone", ["", "Not/AZone", "/etc/passwd"])
def test_an_unknown_zone_is_refused(zone):
    with pytest.raises(ValueError, match="unknown timezone"):
        FactWriteContext("stage1", zone, None)


def test_a_context_is_frozen():
    with pytest.raises(AttributeError):
        STAGE1.model_dates = False  # type: ignore[misc]


# --- as_utc, span_of, clamp ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        1_792_000_000.0,
        "2026-10-14T17:46:40",
        "2026-10-14T17:46:40Z",
        "2026-10-14T19:46:40+02:00",
        datetime(2026, 10, 14, 17, 46, 40),
        datetime(2026, 10, 14, 19, 46, 40, tzinfo=timezone(timedelta(hours=2))),
    ],
)
def test_as_utc_reads_epoch_naive_and_aware_values_as_the_same_utc_moment(value):
    moment = as_utc(value)
    assert moment.tzinfo is not None and moment.utcoffset() == timedelta(0)
    assert moment == datetime.fromtimestamp(1_792_000_000.0, tz=UTC)


def test_a_naive_email_date_is_utc_not_host_local():
    assert as_utc(datetime(2026, 10, 22, 12, 0)) == datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    assert as_utc("2026-10-22T12:00:00") == datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def test_as_utc_refuses_a_string_that_is_not_iso():
    with pytest.raises(ValueError):
        as_utc("INVALID")


def test_span_of_takes_the_earliest_and_latest_naive_and_aware_alike():
    times = [datetime(2026, 10, 22, 12, 0), datetime(2026, 10, 20, 9, 0, tzinfo=UTC), datetime(2026, 10, 21, 23, 0)]
    assert span_of(times) == (datetime(2026, 10, 20, 9, 0, tzinfo=UTC), datetime(2026, 10, 22, 12, 0, tzinfo=UTC))
    assert span_of([]) is None


def test_clamp_pulls_into_the_span_and_never_past_now():
    assert clamp_to_span(FIRST - timedelta(days=9), SPAN, NOW) == FIRST
    assert clamp_to_span(LAST + timedelta(days=9), SPAN, NOW) == LAST
    inside = FIRST + timedelta(hours=5)
    assert clamp_to_span(inside, SPAN, NOW) == inside
    assert clamp_to_span(NOW + timedelta(days=3), None, NOW) == NOW
    assert clamp_to_span(NOW + timedelta(days=3), (FIRST, NOW + timedelta(days=9)), NOW) == NOW


# --- create ----------------------------------------------------------------------------------------------------------


def test_create_in_a_conversation_pass_clamps_the_model_value():
    got, note = reported_date_for_new(STAGE1, FIRST - timedelta(days=30), [], NOW)
    assert (got, note) == (FIRST, [])
    inside = FIRST + timedelta(hours=3)
    assert reported_date_for_new(STAGE1, inside, [], NOW) == (inside, [])


def test_create_in_a_conversation_pass_without_a_value_gets_the_span_end_else_now():
    assert reported_date_for_new(STAGE1, None, [], NOW) == (LAST, [])
    assert reported_date_for_new(ctx("stage1", span=None), None, [], NOW) == (NOW, [])


def test_the_span_end_is_never_after_now():
    future = (FIRST, NOW + timedelta(days=2))
    assert reported_date_for_new(ctx(span=future), None, [], NOW)[0] == NOW


def test_create_in_a_cleanup_pass_ignores_the_model_value_and_says_so():
    got, note = reported_date_for_new(STAGE2A, FIRST + timedelta(hours=1), [], NOW)
    assert got == LAST
    assert note == ["reported_date is kept by the system in this pass (2026-10-22); expiry horizons count from it"]


def test_create_in_a_cleanup_pass_takes_the_latest_derived_source_date():
    older, newer = datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 6, 1, 12, tzinfo=UTC)
    assert reported_date_for_new(STAGE2A, None, [older, newer], NOW, derived_named=2) == (newer, [])
    got, note = reported_date_for_new(STAGE2A, NOW, [older], NOW, derived_named=1)
    assert got == older and note == [kept_reported_date_note(older, "Europe/Paris")]


def test_derived_dates_may_be_naive_and_are_read_as_utc():
    got, _ = reported_date_for_new(STAGE2A, None, [datetime(2026, 6, 1, 12)], NOW, derived_named=1)
    assert got == datetime(2026, 6, 1, 12, tzinfo=UTC)


def test_a_cleanup_create_with_no_readable_source_falls_back_to_the_span_end_with_a_note():
    got, note = reported_date_for_new(STAGE2A, None, [], NOW, derived_named=2)
    assert got == LAST and note == [NO_READABLE_SOURCE_NOTE]
    assert reported_date_for_new(ctx("stage2a", span=None), None, [], NOW) == (NOW, [])


def test_derived_from_is_ignored_with_a_note_in_a_conversation_pass():
    got, note = reported_date_for_new(STAGE1, None, [], NOW, derived_named=1)
    assert got == LAST and note == ["derived_from is ignored: in this pass the date comes from the conversation"]
    got, note = reported_date_for_new(STAGE1, None, [datetime(2020, 1, 1, tzinfo=UTC)], NOW)
    assert got == LAST and note == ["derived_from is ignored: in this pass the date comes from the conversation"]


# --- merge -----------------------------------------------------------------------------------------------------------


def test_merge_in_a_conversation_pass_follows_the_create_rule():
    # S3-D11: the clamped model date is never earlier than the newest source, so a source at LAST lifts it to LAST.
    # The model's date is used on a conversation pass, so there is nothing to say.
    assert reported_date_for_merge(STAGE1, FIRST - timedelta(days=5), [LAST], NOW) == (LAST, [])
    assert reported_date_for_merge(STAGE1, FIRST - timedelta(days=5), [FIRST - timedelta(days=10)], NOW) == (FIRST, [])
    assert reported_date_for_merge(STAGE1, None, [datetime(2020, 1, 1, tzinfo=UTC)], NOW) == (LAST, [])


def test_merge_in_a_cleanup_pass_takes_the_latest_source_date_and_ignores_the_model():
    a, b = datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 5, 1, tzinfo=UTC)
    assert reported_date_for_merge(STAGE2A, NOW, [a, b], NOW) == (b, [kept_reported_date_note(b, "Europe/Paris")])
    assert reported_date_for_merge(STAGE2A, NOW, [], NOW) == (LAST, [kept_reported_date_note(LAST, "Europe/Paris")])


# --- update ----------------------------------------------------------------------------------------------------------

STORED = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def test_a_content_change_in_a_conversation_pass_moves_to_the_model_value_clamped():
    later = STORED + timedelta(hours=6)
    assert reported_date_for_update(STAGE1, later, STORED, True, NOW) == (later, None)
    assert reported_date_for_update(STAGE1, LAST + timedelta(days=4), STORED, True, NOW) == (LAST, None)


def test_a_content_change_without_a_value_takes_the_span_end_when_it_is_later():
    assert reported_date_for_update(STAGE1, None, STORED, True, NOW) == (LAST, None)
    assert reported_date_for_update(STAGE1, None, LAST + timedelta(hours=1), True, NOW)[0] == LAST + timedelta(hours=1)


def test_an_update_never_moves_reported_date_back():
    assert reported_date_for_update(STAGE1, FIRST, STORED + timedelta(days=1), True, NOW)[0] == STORED + timedelta(days=1)
    assert reported_date_for_update(STAGE1, FIRST, STORED + timedelta(days=1), False, NOW)[0] == STORED + timedelta(days=1)


def test_an_in_place_update_without_a_model_date_keeps_the_stored_one_even_with_a_span():
    assert reported_date_for_update(STAGE1, None, STORED, False, NOW) == (STORED, None)


def test_a_re_mention_in_place_moves_to_the_clamped_model_date():
    got, note = reported_date_for_update(STAGE1, LAST + timedelta(days=9), STORED, False, NOW)
    assert (got, note) == (LAST, None)


def test_a_cleanup_pass_preserves_the_stored_date_and_says_the_model_date_was_not_used():
    got, note = reported_date_for_update(STAGE2A, NOW, STORED, True, NOW)
    assert got == STORED
    assert note == "reported_date is kept by the system in this pass (2026-10-21); expiry horizons count from it"
    assert reported_date_for_update(STAGE2A, None, STORED, True, NOW) == (STORED, None)


def test_a_naive_stored_date_is_read_as_utc():
    got, _ = reported_date_for_update(STAGE1, None, datetime(2026, 10, 21, 12, 0), False, NOW)
    assert got == STORED


def test_the_note_names_the_date_in_the_owners_calendar():
    late = datetime(2026, 10, 21, 22, 30, tzinfo=UTC)  # 00:30 on the 22nd in Paris
    assert "(2026-10-22)" in kept_reported_date_note(late, "Europe/Paris")
    assert "(2026-10-21)" in kept_reported_date_note(late, "UTC")
