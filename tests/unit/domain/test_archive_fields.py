"""`archive_fields` (the in-place write of an archive) and `stale_order_key` (the order of the review's stale list)."""
from datetime import datetime, timedelta, timezone

from src.domain.entities import FactState
from src.domain.fact_lifecycle import LIFECYCLE_EVENTS_CAP, IN_PLACE_FIELDS, archive_fields
from src.domain.fact_review_rules import EARLIEST_QUEUE_KEY, queue_key, stale_order_key
from tests.support.fact_kit import make_fact

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
STALE_SINCE = NOW - timedelta(days=4)
EARLIER = {"at": NOW - timedelta(days=4), "from": "current", "to": "stale", "actor": "janitor", "reason": "expired"}


def stale_fact(**over):
    return make_fact("old", state=FactState.STALE, stale_since=STALE_SINCE, lifecycle_events=[EARLIER], **over)


def test_the_archive_closes_validity_keeps_the_fact_current_in_its_lineage_and_stamps_now():
    fields = archive_fields(stale_fact(), NOW, "review", "rewritten as f-new")

    assert fields["state"] is FactState.ARCHIVED and fields["is_current"] is True
    assert fields["valid_to"] == NOW and fields["last_updated"] == NOW


def test_the_trail_gains_one_entry_from_the_stored_state_to_archived():
    fields = archive_fields(stale_fact(), NOW, "review", "rewritten as f-new")

    assert fields["lifecycle_events"] == [
        EARLIER, {"at": NOW, "from": "stale", "to": "archived", "actor": "review", "reason": "rewritten as f-new"}
    ]


def test_the_entry_names_the_state_the_fact_was_in():
    current = make_fact("cur")

    (entry,) = archive_fields(current, NOW, "review", "r")["lifecycle_events"]

    assert entry["from"] == "current" and entry["to"] == "archived"


def test_stale_since_is_not_written_and_every_key_may_be_written_in_place():
    fields = archive_fields(stale_fact(), NOW, "review", "r")

    assert "stale_since" not in fields
    assert set(fields) <= IN_PLACE_FIELDS


def test_the_trail_keeps_the_newest_entries_when_it_is_full():
    events = [{**EARLIER, "reason": f"e{n}"} for n in range(LIFECYCLE_EVENTS_CAP)]

    trail = archive_fields(make_fact("full", lifecycle_events=events), NOW, "review", "r")["lifecycle_events"]

    assert len(trail) == LIFECYCLE_EVENTS_CAP and trail[0]["reason"] == "e1" and trail[-1]["to"] == "archived"


def test_the_fact_given_is_not_changed():
    fact = stale_fact()

    archive_fields(fact, NOW, "review", "r")

    assert fact.state is FactState.STALE and fact.lifecycle_events == [EARLIER]


# ------------------------------------------------------------------------------------------------ the order

def test_the_older_stale_since_sorts_first():
    older = make_fact("a", stale_since=NOW - timedelta(days=9))
    newer = make_fact("b", stale_since=NOW - timedelta(days=1))

    assert sorted([newer, older], key=stale_order_key) == [older, newer]


def test_a_fact_without_stale_since_sorts_before_every_dated_one():
    undated = make_fact("z", stale_since=None)
    dated = make_fact("a", stale_since=datetime(1971, 1, 1, tzinfo=timezone.utc))

    assert sorted([dated, undated], key=stale_order_key) == [undated, dated]
    assert stale_order_key(undated)[0] == EARLIEST_QUEUE_KEY


def test_equal_dates_fall_back_to_the_id():
    first, second = make_fact("a", stale_since=NOW), make_fact("b", stale_since=NOW)

    assert sorted([second, first], key=stale_order_key) == [first, second]


def test_a_naive_stale_since_is_read_as_utc_and_compares_with_aware_ones():
    naive = make_fact("n", stale_since=datetime(2026, 5, 1))
    aware = make_fact("w", stale_since=datetime(2026, 5, 2, tzinfo=timezone.utc))

    assert sorted([aware, naive], key=stale_order_key) == [naive, aware]


def test_the_key_is_the_queue_key_then_the_id():
    fact = make_fact("a", stale_since=NOW)

    assert stale_order_key(fact) == (queue_key(fact), fact.id)
