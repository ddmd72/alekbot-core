"""The fact search snapshot (fact profile RFC §5.2): the derived row of each current fact, rows sorted by id."""
from datetime import date

import pytest

from src.domain.fact_search_snapshot import FactSearchSnapshot, time_interval
from tests.support.fact_search_kit import fact, person, point

D = date.fromisoformat


@pytest.mark.parametrize("raw, expected", [
    (point("2026-04-01", "2027-03-31"), (D("2026-04-01"), D("2027-03-31"))),
    (point("2025-08-10"), (D("2025-08-10"), D("2025-08-10"))),
    (point("2025-08", precision="month"), (D("2025-08-01"), D("2025-08-31"))),
    (point("2025-12", precision="month"), (D("2025-12-01"), D("2025-12-31"))),
    (point("2025-02", "2025-04", precision="month"), (D("2025-02-01"), D("2025-04-30"))),
    (point("2025", precision="year"), (D("2025-01-01"), D("2025-12-31"))),
    (point("2025-09-01", "2025-08-01"), (D("2025-09-01"), D("2025-09-01"))),     # an end before the start collapses
])
def test_a_time_point_is_an_interval_at_its_precision(raw, expected):
    assert time_interval(raw) == expected


@pytest.mark.parametrize("raw", [point("not-a-date"), point(""), point("2025-13", precision="month"),
                                 point("20x5", precision="year"), {"precision": "day"}])
def test_a_malformed_point_raises_value_error(raw):
    with pytest.raises(ValueError):
        time_interval(raw)


def test_rows_are_sorted_by_id_and_a_malformed_point_is_skipped():
    snap = FactSearchSnapshot.from_facts([
        fact("b", points=[point("bad"), point("2025-08-10", "2025-08-14")]), fact("a")])
    assert snap.ids == ("a", "b") and list(snap.entities) == ["a", "b"] and len(snap) == 2
    assert snap.rows["b"].intervals == ((D("2025-08-10"), D("2025-08-14")),)
    assert snap.rows["a"].intervals == ()


def test_a_row_carries_the_derived_sets():
    row = FactSearchSnapshot.from_facts([fact(
        "x", "Maria Doe's birthday", domains=("family", "identity"), labels=["mother's birthday"],
        places=["pl:1", "co:italy"], kinds=["document"], sensitivity="ordinary", role="event", reported=D("2026-03-02"),
        people=[person("Maria Doe", "mother"), person("", "mother"), person("Peter Doe", "")])]).rows["x"]
    assert row.domains == {"family", "identity"} and row.place_keys == {"pl:1", "co:italy"}
    assert row.kinds == {"document"} and row.sensitivity == "ordinary" and row.time_role == "event"
    assert row.reported == D("2026-03-02") and row.recurring and row.text == "Maria Doe's birthday"
    assert len(row.people) == 3 and row.people[1][0] == frozenset() and len(row.people[0][0]) == 2
    assert row.people[0][1] and row.people[2][1] == frozenset()


def test_an_unprofiled_fact_is_a_row_with_empty_sets():
    row = FactSearchSnapshot.from_facts([fact("bare", profiled=False)]).rows["bare"]
    assert (row.domains, row.intervals, row.people, row.place_keys, row.kinds) == (
        frozenset(), (), (), frozenset(), frozenset())
    assert (row.sensitivity, row.time_role, row.recurring) == (None, None, False)


def test_recurring_needs_a_label_phrase_containing_the_word():
    assert FactSearchSnapshot.from_facts([fact("a", labels=["wedding anniversary"])]).rows["a"].recurring
    assert not FactSearchSnapshot.from_facts([fact("b", labels=["wedding", "party"])]).rows["b"].recurring
