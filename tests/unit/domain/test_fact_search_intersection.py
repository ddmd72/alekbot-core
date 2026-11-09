"""Domain lists of the fact search (fact profile RFC §5.3): one or two domains, the union fallback, only the first two
domains, the cut, and the domain list fused as a full equal-weight list."""
from tests.support.fact_search_kit import fact, query, search


def test_one_domain_lists_every_fact_carrying_it():
    out = search([fact("a", domains=("money",)), fact("b", domains=("health", "money")), fact("c", domains=("health",))],
                 query(domains=["money"]))
    assert sorted(out.lists["domain"]) == ["a", "b"]


def test_two_domains_list_the_facts_carrying_both():
    out = search([fact("a", domains=("money",)), fact("b", domains=("health", "money")), fact("c", domains=("health",))],
                 query(domains=["health", "money"]))
    assert out.lists["domain"] == ["b"]


def test_two_domains_without_a_fact_carrying_both_fall_back_to_the_union():
    out = search([fact("a", domains=("money",)), fact("c", domains=("health",)), fact("d", domains=("work",))],
                 query(domains=["health", "money"]))
    assert sorted(out.lists["domain"]) == ["a", "c"]


def test_only_the_first_two_domains_count_and_blank_ones_are_skipped():
    facts = [fact("a", domains=("money", "health")), fact("b", domains=("money", "health", "work")),
             fact("c", domains=("money",))]
    out = search(facts, query(domains=["money", "", "health", "work"]))
    assert sorted(out.lists["domain"]) == ["a", "b"]
    assert search(facts, query(domains=["", ""])).lists == {}


def test_a_domain_list_is_present_even_when_empty_and_a_fact_without_a_profile_is_in_none():
    out = search([fact("bare", profiled=False)], query(domains=["money"]))
    assert out.lists == {"domain": []}


def test_a_key_list_is_cut_at_50_and_at_100_for_collect_all():
    facts = [fact(f"f{n:03d}", domains=("money",)) for n in range(130)]
    assert len(search(facts, query(domains=["money"])).lists["domain"]) == 50
    assert len(search(facts, query(domains=["money"], scope="all")).lists["domain"]) == 100


def test_the_cut_follows_the_order_of_the_whole_candidate_set():
    facts = [fact(f"f{n:03d}", domains=("money",)) for n in range(60)]
    sim = {"f059": 0.9}
    assert search(facts, query(domains=["money"]), sim=sim).lists["domain"][0] == "f059"


def test_the_domain_list_fuses_as_a_full_equal_weight_list():
    a, b, c = (fact(x, domains=("money",)) for x in "abc")
    out = search([a, b, c], query(domains=["money"]), semantic=["c", "b"], sim={"a": 0.9, "b": 0.5, "c": 0.1})
    # domain: a, b, c; semantic: c, b. Scores: a 1/61; b 1/62 + 1/62; c 1/63 + 1/61
    assert out.lists["domain"] == ["a", "b", "c"]
    assert out.fused == ["c", "b", "a"]


def test_a_score_tie_goes_to_the_list_that_came_first():
    a, b = fact("a", domains=("money",)), fact("b", domains=("money",))
    out = search([a, b], query(domains=["money"]), semantic=["b"], sim={"a": 0.5})
    # b: 1/61 (semantic) + 1/62; a: 1/61 (domain rank 1): b leads on score
    assert out.fused == ["b", "a"]
    lone = search([fact("x", domains=("money",)), fact("y", domains=("health",))], query(domains=["money"]),
                  semantic=["y"])
    assert lone.fused == ["y", "x"]          # equal scores: the semantic list is first in insertion order
