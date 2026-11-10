"""The facts the open points skip without a word (fact profile RFC §4.7): a place printed with a blank city, and a fact
with no lineage whose people or dates cannot be keyed. Built on the cases of `test_profile_open_points`. Invented cast."""
from src.domain.fact_profile import ProfileReviewItem, UnresolvedPlace
from src.domain.profile_open_points import PointFact
from tests.unit.domain.test_profile_open_points import T0, fact, points


def lineage_less(text: str, items) -> PointFact:
    return PointFact("f9", "", text, T0, (), tuple(ProfileReviewItem(f, m) for f, m in items))


def test_a_place_printed_with_a_blank_city_is_no_point_while_its_neighbour_is():
    blank = PointFact("f1", "lin1", "Alex has a trip to Rome", T0, (UnresolvedPlace("  ", "Italy"),
                                                                     UnresolvedPlace("Rome", "")), ())

    found = points([blank])

    assert [p.point.printed for p in found] == ["Rome"]


def test_a_fact_with_a_blank_city_only_gives_no_point():
    blank = PointFact("f1", "lin1", "Alex has a trip to Rome", T0, (UnresolvedPlace("", "Italy"),), ())

    assert points([blank]) == []


def test_a_flagged_person_of_a_fact_without_a_lineage_is_no_point_while_one_with_a_lineage_is():
    text = "The daughter goes to Example School"
    items = [("people", "Jamie")]

    assert points([lineage_less(text, items)]) == []
    assert [p.point.printed for p in points([fact(1, text, items=items)])] == ["Jamie"]


def test_a_date_of_a_fact_without_a_lineage_is_no_point_while_one_with_a_lineage_is():
    text = "Alex starts at Acme on 2026-10-01"
    items = [("time_points", "on 2026-10-01")]

    assert points([lineage_less(text, items)]) == []
    assert [p.point.printed for p in points([fact(1, text, items=items)])] == ["on 2026-10-01"]
