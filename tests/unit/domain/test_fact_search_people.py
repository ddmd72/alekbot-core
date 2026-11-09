"""People in the fact search (fact profile RFC §5.3): a relation word matches mentions whose role carries it, a name
matches by skeleton subset (cross-script too), and the people list is narrowed to the query's domains only when that
leaves any fact."""
from datetime import date

from tests.support.fact_search_kit import fact, person, query, search


def people_list(facts, **kw):
    return search(facts, query(**kw)).lists.get("people", [])


def test_a_relation_word_matches_the_mentions_whose_role_carries_it():
    clara = fact("clara", people=[person("Clara Moreira", "partner")])
    jamie = fact("jamie", people=[person("Jamie Doe", "daughter")])
    other = fact("other", people=[person("Tom Baker", "colleague")])
    assert people_list([clara, jamie, other], people=["partner"]) == ["clara"]
    assert people_list([clara, jamie, other], people=["my daughter"]) == ["jamie"]


def test_a_relation_phrase_also_matches_its_name_words():
    maria = fact("maria", people=[person("Maria Doe", "")])
    clara = fact("clara", people=[person("Clara Moreira", "partner")])
    assert sorted(people_list([maria, clara], people=["partner", "Maria"])) == ["clara", "maria"]
    assert people_list([maria, clara], people=["mother Maria"]) == ["maria"]


def test_a_name_matches_by_skeleton_subset_in_either_direction():
    maria = fact("maria", people=[person("Maria Doe", "mother")])
    mariia = fact("mariia", people=[person("Mariia", "")])
    other = fact("other", people=[person("Clara Moreira", "")])
    assert sorted(people_list([maria, mariia, other], people=["Mariia"])) == ["maria", "mariia"]
    assert sorted(people_list([maria, mariia, other], people=["Maria Doe"])) == ["maria", "mariia"]


def test_a_name_matches_across_scripts():
    maria = fact("maria", people=[person("Maria Doe", "")])
    other = fact("other", people=[person("Clara Moreira", "")])
    assert people_list([maria, other], people=["Марія"]) == ["maria"]


def test_a_mention_without_a_name_or_role_matches_nothing_and_blank_people_match_nothing():
    anon = fact("anon", people=[person("", "")])
    assert people_list([anon], people=["Clara"]) == []
    assert search([anon], query(people=[" "])).lists == {}


def test_no_mentions_and_an_unprofiled_fact_are_never_matched():
    assert people_list([fact("none"), fact("bare", profiled=False)], people=["Clara"]) == []


def test_the_people_list_is_narrowed_to_the_query_domains_when_that_leaves_any():
    home = fact("home", domains=("family",), people=[person("Clara Moreira", "partner")])
    work = fact("work", domains=("work",), people=[person("Clara Moreira", "partner")])
    out = search([home, work], query(people=["Clara"], domains=["family"]))
    assert out.lists["people"] == ["home"]


def test_the_people_list_keeps_every_match_when_none_is_in_the_domains():
    work = fact("work", domains=("work",), people=[person("Clara Moreira", "partner")])
    out = search([work, fact("x", domains=("family",))], query(people=["Clara"], domains=["family"]))
    assert out.lists["people"] == ["work"]


def test_without_domains_the_people_list_is_not_narrowed():
    home = fact("home", domains=("family",), people=[person("Clara Moreira", "partner")])
    work = fact("work", domains=("work",), people=[person("Clara Moreira", "partner")])
    assert sorted(people_list([home, work], people=["Clara"])) == ["home", "work"]


def test_the_people_list_is_ordered_like_a_key_list():
    old = fact("old", role="state", people=[person("Clara Moreira", "")], reported=date(2020, 1, 1))
    new = fact("new", role="state", people=[person("Clara Moreira", "")])
    assert people_list([old, new], people=["Clara"]) == ["new", "old"]
