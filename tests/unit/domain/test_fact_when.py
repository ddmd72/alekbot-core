"""when_line: the time points of a fact as one line, each at its stored precision."""
from src.domain.entities import FactEntity, FactType
from src.domain.fact_when import when_line


def _fact(points):
    return FactEntity(account_id="acc", created_by_user_id="usr", lineage_id="lin",
                      text="Car service for the Skoda Fabia", type=FactType.EVENT, time_points=points)


def test_no_points_gives_none():
    assert when_line(_fact(None)) is None
    assert when_line(_fact([])) is None


def test_a_single_day():
    assert when_line(_fact([{"from": "2026-11-05", "to": "2026-11-05", "precision": "day"}])) == "when: 2026-11-05"


def test_a_range_prints_from_and_to():
    assert when_line(_fact([{"from": "2026-03-06", "to": "2026-03-08"}])) == "when: 2026-03-06–2026-03-08"


def test_a_missing_to_prints_the_from_alone():
    assert when_line(_fact([{"from": "2026-12"}])) == "when: 2026-12"


def test_several_points_are_joined():
    points = [{"from": "2026-12-01"}, {"from": "2026-12-09"}]
    assert when_line(_fact(points)) == "when: 2026-12-01; 2026-12-09"


def test_a_point_without_from_is_skipped():
    assert when_line(_fact([{"to": "2026-12-09"}])) is None
