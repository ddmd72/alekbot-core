"""Candidate generation of the scored search (fact profile RFC §5.3, OD-14): the lists generate candidates and none is
cut before scoring except the named caps; the message, domain, people and home lists of `candidate_lists`; the intimate
gate over all of them."""
from dataclasses import replace

from src.domain.fact_search_lists import DEPTH_KNN, DOMAIN_CAP, HOME_CAP, candidate_lists
from src.domain.fact_search_snapshot import FactSearchSnapshot
from tests.support.fact_search_kit import fact, person, point, query

SPRINGFIELD, COIMBRA = "pl:9100001", "pl:9100004"
HOME_KEYS = frozenset({SPRINGFIELD})


def lists(facts, q, *, semantic=None, labels=None, message=None, sim=None, place_keys=frozenset(),
          home_keys=frozenset()):
    return candidate_lists(FactSearchSnapshot.from_facts(list(facts)), q, semantic_ids=semantic, label_ids=labels,
                           message_ids=message, similarity=sim or {}, query_place_keys=place_keys, home_keys=home_keys)


def many(n, **kw):
    return [fact(f"f{i:03d}", f"Clara Moreira fact {i}", **kw) for i in range(n)]


def test_a_knn_list_of_full_depth_is_kept_whole():
    facts = many(DEPTH_KNN)
    got, _ = lists(facts, query(subject="Clara"), semantic=[f.id for f in facts], labels=[f.id for f in facts])
    assert len(got["semantic"]) == DEPTH_KNN and len(got["labels"]) == DEPTH_KNN


def test_the_key_lists_are_not_cut():
    n = 120
    got, _ = lists(many(n, kinds=["appointment"], people=[person("Clara Moreira", "partner")],
                        points=[point("2026-11-05")]),
                   query(intent="past", kinds=["appointment"], people=["Clara"]))
    assert len(got["kinds"]) == n and len(got["people"]) == n and len(got["time"]) == n


def test_one_domain_lists_every_fact_carrying_it_up_to_the_cap():
    got, _ = lists(many(DOMAIN_CAP + 10, domains=("family",)), query(domains=["family"]))
    assert len(got["domain"]) == DOMAIN_CAP


def test_two_domains_list_the_facts_carrying_both_first_then_either_up_to_the_cap():
    both = [fact(f"b{i}", domains=("family", "travel")) for i in range(3)]
    either = many(DOMAIN_CAP, domains=("family",)) + [fact("t0", domains=("travel",))]
    got, _ = lists(both + either, query(domains=["travel", "family"]))
    assert len(got["domain"]) == DOMAIN_CAP
    assert set(got["domain"][:3]) == {"b0", "b1", "b2"} and not set(got["domain"][3:]) & {"b0", "b1", "b2"}


def test_two_domains_without_any_fact_carrying_both_list_either():
    got, _ = lists([fact("a", domains=("family",)), fact("b", domains=("travel",))], query(domains=["family", "travel"]))
    assert sorted(got["domain"]) == ["a", "b"]


def test_the_people_list_is_not_narrowed_by_the_domains():
    home = fact("home", domains=("family",), people=[person("Clara Moreira", "partner")])
    work = fact("work", domains=("work",), people=[person("Clara Moreira", "partner")])
    got, _ = lists([home, work], query(people=["Clara"], domains=["family"]))
    assert sorted(got["people"]) == ["home", "work"]


def test_the_home_list_holds_the_facts_at_home_when_the_query_names_no_place_up_to_the_cap():
    at_home = many(HOME_CAP + 5, places=[SPRINGFIELD])
    away = fact("coimbra", "Weekend in Coimbra with Clara Mar 6-8", places=[COIMBRA])
    got, _ = lists(at_home + [away], query(), home_keys=HOME_KEYS)
    assert len(got["home"]) == HOME_CAP and "coimbra" not in got["home"]


def test_no_home_list_with_a_query_place_or_without_a_home():
    facts = [fact("lunch", "Christmas lunch at Maria's", places=[SPRINGFIELD])]
    named, _ = lists(facts, query(places=[("Coimbra", "")]), place_keys=frozenset({COIMBRA}), home_keys=HOME_KEYS)
    assert "home" not in named
    homeless, _ = lists(facts, query())
    assert "home" not in homeless


def test_the_message_list_is_present_only_when_message_ids_are_given():
    facts = many(3)
    got, _ = lists(facts, replace(query(), message="what is new with Clara"), message=["f002", "f000"])
    assert got["message"] == ["f002", "f000"]
    none, _ = lists(facts, query(subject="Clara"), semantic=["f000"])
    assert "message" not in none


def test_the_intimate_gate_runs_over_the_message_and_home_lists():
    private = fact("private", sensitivity="intimate", places=[SPRINGFIELD])
    plain = fact("plain", places=[SPRINGFIELD])
    got, dropped = lists([private, plain], replace(query(), message="m"), message=["private", "plain"],
                         home_keys=HOME_KEYS)
    assert got["message"] == ["plain"] and got["home"] == ["plain"] and dropped == 2


def test_the_latest_time_list_pools_the_key_lists_before_the_message_and_home_lists():
    seen = fact("seen", "Car service 2026-11-05", points=[point("2026-11-05")])
    msg = fact("msg", "Dentist check-up 2026-10-12", points=[point("2026-10-12")])
    home = fact("home", "Christmas lunch at Maria's", points=[point("2025-12-25")], places=[SPRINGFIELD])
    got, _ = lists([seen, msg, home], replace(query(intent="latest", subject="car"), message="m"),
                   semantic=["seen"], message=["msg"], home_keys=HOME_KEYS)
    assert got["time"] == ["seen"] and got["message"] == ["msg"] and got["home"] == ["home"]
    assert list(got) == ["semantic", "time", "message", "home"]

