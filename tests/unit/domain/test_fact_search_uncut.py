"""`SearchOutcome.uncut`: the size before the cap for a list a cap cut (domain, home); absent for an uncut list."""
from src.domain.fact_search_lists import DOMAIN_CAP
from tests.support.fact_search_kit import fact, query, search


def test_a_domain_list_cut_by_its_cap_reports_its_size_before_the_cut():
    facts = [fact(f"f{i:03d}", f"Car service {i}", domains=("vehicles",)) for i in range(DOMAIN_CAP + 12)]
    outcome = search(facts, query(domains=["vehicles"]))
    assert len(outcome.lists["domain"]) == DOMAIN_CAP
    assert outcome.uncut == {"domain": DOMAIN_CAP + 12}


def test_a_domain_list_under_its_cap_reports_nothing():
    facts = [fact(f"f{i}", f"Car service {i}", domains=("vehicles",)) for i in range(5)]
    assert search(facts, query(domains=["vehicles"])).uncut == {}


def test_keep_within_cut_keeps_the_head_and_the_lifted_ones_past_it():
    from src.domain.fact_search_lists import keep_within_cut
    assert keep_within_cut(["a", "b", "c", "d", "e"], 2, {"d"}) == ["a", "b", "d"]
