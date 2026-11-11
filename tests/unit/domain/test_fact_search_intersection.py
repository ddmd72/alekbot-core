"""Domain lists of the fact search (fact profile RFC §5.3, OD-14): one or two domains (the facts carrying both first,
then the facts carrying either), only the first two domains, the cap, and the domain list as a source of candidates
whose in-list order does not rank them."""
from datetime import date

from src.domain.fact_search_lists import run_search
from src.domain.fact_search_snapshot import FactSearchSnapshot
from tests.support.fact_search_kit import fact, query, search


def scored(facts, q, *, semantic, sim, weights, label_sim=None):
    """One search with injected weights, so the order does not depend on the fitted ones."""
    return run_search(FactSearchSnapshot.from_facts(list(facts)), q, semantic_ids=semantic, label_ids=None,
                      similarity=sim, query_place_keys=frozenset(), is_duplicate=lambda a, b: False, weights=weights,
                      label_similarity=label_sim)


def test_one_domain_lists_every_fact_carrying_it():
    out = search([fact("a", domains=("money",)), fact("b", domains=("health", "money")), fact("c", domains=("health",))],
                 query(domains=["money"]))
    assert sorted(out.lists["domain"]) == ["a", "b"]


def test_two_domains_list_the_facts_carrying_both_first_then_the_facts_carrying_either():
    out = search([fact("a", domains=("money",)), fact("b", domains=("health", "money")), fact("c", domains=("health",))],
                 query(domains=["health", "money"]))
    assert out.lists["domain"][:1] == ["b"] and sorted(out.lists["domain"][1:]) == ["a", "c"]


def test_two_domains_without_a_fact_carrying_both_fall_back_to_the_union():
    out = search([fact("a", domains=("money",)), fact("c", domains=("health",)), fact("d", domains=("work",))],
                 query(domains=["health", "money"]))
    assert sorted(out.lists["domain"]) == ["a", "c"]


def test_only_the_first_two_domains_count_and_blank_ones_are_skipped():
    facts = [fact("a", domains=("money", "health")), fact("b", domains=("money", "health", "work")),
             fact("c", domains=("money",)), fact("d", domains=("work",))]
    out = search(facts, query(domains=["money", "", "health", "work"]))
    # both = money and health (not work); then either; a fact only in work is in no list
    assert sorted(out.lists["domain"][:2]) == ["a", "b"] and out.lists["domain"][2:] == ["c"]
    assert search(facts, query(domains=["", ""])).lists == {}


def test_a_domain_list_is_present_even_when_empty_and_a_fact_without_a_profile_is_in_none():
    out = search([fact("bare", profiled=False)], query(domains=["money"]))
    assert out.lists == {"domain": []}


def test_a_domain_list_is_capped_at_100_and_the_result_is_cut_at_30_only_after_scoring():
    facts = [fact(f"f{n:03d}", domains=("money",)) for n in range(130)]
    top = search(facts, query(domains=["money"]))
    assert len(top.lists["domain"]) == 100 and len(top.kept) == 30
    collect = search(facts, query(domains=["money"], scope="all"))       # list mode RFC §3.1: a filter key, no cap
    assert list(collect.lists) == ["set"] and len(collect.lists["set"]) == 130 and len(collect.kept) == 130


def test_the_cut_follows_the_order_of_the_whole_candidate_set():
    facts = [fact(f"f{n:03d}", domains=("money",)) for n in range(60)]
    sim = {"f059": 0.9}
    assert search(facts, query(domains=["money"]), sim=sim).lists["domain"][0] == "f059"


def test_every_domain_list_member_is_a_candidate_and_its_in_list_order_does_not_rank():
    a, b, c = (fact(x, domains=("money",), role="state", reported=day)
               for x, day in (("a", date(2026, 11, 1)), ("b", date(2024, 1, 1)), ("c", date(2020, 1, 1))))
    out = scored([a, b, c], query(subject="money", kind="savings", domains=["money"]), semantic=["c", "b"],
                 sim={"a": 0.9, "b": 0.5, "c": 0.1}, label_sim={"a": 0.1, "b": 0.5, "c": 0.9},
                 weights={"sim_label": 1.0})
    # domain (by closeness to the subject, list mode RFC §5): a, b, c; semantic: c, b; a comes from the domain list
    # alone; the score reads only the label similarity, so the fused order is the reverse of the in-list order
    assert out.lists["domain"] == ["a", "b", "c"]
    assert out.fused == ["c", "b", "a"]


def test_a_score_tie_goes_to_the_smaller_id_whichever_list_came_first():
    lone = scored([fact("x", domains=("money",)), fact("y", domains=("health",))], query(domains=["money"]),
                  semantic=["y"], sim={}, weights={})
    assert list(lone.lists) == ["semantic", "domain"] and lone.scores["x"] == lone.scores["y"]
    assert lone.fused == ["x", "y"]          # equal scores: the id decides, not the list order
