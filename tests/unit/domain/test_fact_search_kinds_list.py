"""The kinds list of the fact search (fact profile RFC §5.3): stored kinds across every domain; no list when no fact
carries them."""
from datetime import date

from tests.support.fact_search_kit import fact, point, query, search

D = date.fromisoformat


def passports():
    alex = fact("alex", "Alex's passport expires on 2031-03-04", domains=("identity",), kinds=["document"],
                role="validity", points=[point("2031-03-04")])
    clara = fact("clara", "Clara's passport is valid until 2026-12-09", domains=("family",), kinds=["document"],
                 role="validity", points=[point("2026-12-09")])
    pollen = fact("pollen", "Alex has a pollen allergy", domains=("health",), kinds=["constraint"])
    return [alex, clara, pollen]


def test_both_passports_are_listed_across_their_domains():
    out = search(passports(), query(kinds=["document"]))
    assert sorted(out.lists["kinds"]) == ["alex", "clara"] and "domain" not in out.lists


def test_a_future_window_orders_the_kinds_list_by_distance():
    out = search(passports(), query(kinds=["document"], intent="future"))
    assert out.lists["kinds"] == ["clara", "alex"]
    assert out.lists["time"] == ["clara"]


def test_without_a_window_validity_facts_come_newest_first():
    assert search(passports(), query(kinds=["document"])).lists["kinds"] == ["alex", "clara"]


def test_a_kind_nobody_carries_gives_no_list():
    assert "kinds" not in search(passports(), query(kinds=["intention"])).lists


def test_any_of_several_kinds_matches_and_no_kinds_asks_for_no_list():
    out = search(passports(), query(kinds=["constraint", "document"]))
    assert sorted(out.lists["kinds"]) == ["alex", "clara", "pollen"]
    assert "kinds" not in search(passports(), query()).lists


def test_the_kinds_list_sits_after_domain_and_time_in_insertion_order():
    out = search(passports(), query(kinds=["document"], domains=["family"], intent="future"), semantic=["alex"],
                 labels=["alex"])
    assert list(out.lists) == ["semantic", "labels", "domain", "time", "kinds"]
