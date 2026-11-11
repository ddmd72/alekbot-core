"""List mode of the fact search (list mode RFC §3, §5): an `all` phrase that names a structural key returns the set its
keys define (OR within a key, AND across keys, a time window counts as a key), with its own intimate gate, ordered by
closeness or newest first under `latest`, with no kNN list, no home list, no cap and the duplicate pass with no limit;
an `all` phrase without a key runs `top`; under `top` the capped domain and home lists are ordered by closeness.
Examples from the invented universe."""
from datetime import date

from src.domain.fact_search_lists import (CUT, DOMAIN_CAP, HOME_CAP, candidate_lists, is_set_phrase, run_search,
                                          set_keys, unresolved_keys)
from src.domain.fact_search_rank import HomePlace
from src.domain.fact_search_snapshot import FactSearchSnapshot
from tests.support.fact_search_kit import fact, person, point, query, search

D = date.fromisoformat
SPRINGFIELD, COIMBRA, PORTO = "pl:9100001", "pl:9100004", "pl:9100005"
CLARA = person("Clara Moreira", "partner")
HANNA = person("Hanna Doe", "sister")


def run(facts, q, *, sim=None, message_sim=None, semantic=None, place_keys=frozenset(), home=None,
        exclude=frozenset(), duplicate=None):
    """One `run_search` with the message similarity and the home the kit's `search` does not pass."""
    return run_search(FactSearchSnapshot.from_facts(list(facts)), q, semantic_ids=semantic, label_ids=None,
                      similarity=sim or {}, query_place_keys=place_keys,
                      is_duplicate=duplicate or (lambda a, b: False), message_similarity=message_sim, home=home,
                      exclude=exclude)


# ------------------------------------------------------------------------------------------------ which phrases

def test_the_keys_of_a_phrase_in_their_fixed_order_and_latest_is_none():
    q = query(intent="period", start=D("2026-03-01"), end=D("2026-03-31"), people=["Clara"], places=[("Coimbra", "")],
              kinds=["trip"], domains=["travel"], scope="all")
    assert set_keys(q, {COIMBRA}) == ("domains", "kinds", "people", "places", "time")
    assert set_keys(query(subject="Skoda Fabia", intent="latest", scope="all"), set()) == ()
    assert set_keys(query(domains=["", ""], kinds=[""], scope="all"), set()) == ()


def test_a_named_place_that_reaches_no_key_is_no_key_and_is_reported_unresolved():
    q = query(domains=["travel"], places=[("Atlantis", "")], scope="all")
    assert set_keys(q, set()) == ("domains",) and unresolved_keys(q, set()) == ("place",)
    assert set_keys(q, {COIMBRA}) == ("domains", "places") and unresolved_keys(q, {COIMBRA}) == ()
    assert unresolved_keys(query(domains=["travel"], scope="all"), set()) == ()


def test_only_an_all_phrase_with_a_key_is_a_set_phrase():
    assert is_set_phrase(query(people=["Clara"], scope="all"), set())
    assert not is_set_phrase(query(people=["Clara"]), set())
    assert not is_set_phrase(query(subject="Clara", scope="all"), set())
    assert is_set_phrase(query(places=[("Coimbra", "")], scope="all"), {COIMBRA})
    assert not is_set_phrase(query(places=[("Atlantis", "")], scope="all"), set())


# ------------------------------------------------------------------------------------------------ membership

def test_two_domains_give_the_facts_carrying_either_with_no_both_first_grouping():
    facts = [fact("lift", "Leg day at the Iron Temple gym", domains=("sport",)),
             fact("both", "Morning run before work", domains=("sport", "lifestyle")),
             fact("sleep", "Daily walks in the park", domains=("lifestyle",)),
             fact("car", "Skoda Fabia oil change", domains=("transport",))]
    out = search(facts, query(subject="gym habits", domains=["sport", "lifestyle"], scope="all"),
                 sim={"lift": 0.9, "sleep": 0.5, "both": 0.1})
    assert out.mode == "all" and out.keys == ("domains",)
    assert out.kept == ["lift", "sleep", "both"]


def test_different_keys_are_joined_by_and():
    facts = [fact("fabia", "Bought the 2015 Skoda Fabia", domains=("transport",), kinds=["ownership"]),
             fact("bus", "Took the bus to Coimbra", domains=("transport",), kinds=["trip"]),
             fact("flat", "Bought the washer for 12 Oak Street", domains=("home",), kinds=["ownership"])]
    out = search(facts, query(subject="Skoda Fabia", domains=["transport"], kinds=["ownership"], scope="all"))
    assert out.kept == ["fabia"] and out.keys == ("domains", "kinds")


def test_domains_with_kinds_are_ignored_and_a_kind_match_under_another_domain_stays():
    facts = [fact("fabia", "Bought the 2015 Skoda Fabia", domains=("transport",), kinds=["ownership"]),
             fact("washer", "Bought the washer for 12 Oak Street", domains=("home",), kinds=["ownership"]),
             fact("bus", "Took the bus to Coimbra", domains=("transport",), kinds=["trip"])]
    out = search(facts, query(domains=["transport"], kinds=["ownership"], scope="all"))
    assert sorted(out.kept) == ["fabia", "washer"] and out.keys == ("kinds",)


def test_a_phrase_with_only_domains_still_filters_by_domain():
    facts = [fact("fabia", "Bought the 2015 Skoda Fabia", domains=("transport",), kinds=["ownership"]),
             fact("washer", "Bought the washer for 12 Oak Street", domains=("home",), kinds=["ownership"])]
    out = search(facts, query(domains=["transport"], scope="all"))
    assert out.kept == ["fabia"] and out.keys == ("domains",)


def test_domains_with_people_are_ignored():
    facts = [fact("sister", "Hanna lives in Porto", domains=("family",), people=[HANNA]),
             fact("lease", "Hanna signed the Porto lease", domains=("home",), people=[HANNA]),
             fact("car", "Skoda Fabia oil change", domains=("transport",))]
    out = search(facts, query(people=["Hanna"], domains=["family"], scope="all"))
    assert sorted(out.kept) == ["lease", "sister"] and out.keys == ("people",)


def test_people_match_by_name_or_relation():
    facts = [fact("named", "Dinner with Clara", people=[person("Clara", "")]),
             fact("related", "Birthday gift for my partner: a cookbook", people=[person("", "partner")]),
             fact("hanna", "Hanna rents a flat in Porto", people=[HANNA])]
    out = search(facts, query(people=["Clara", "my partner"], scope="all"))
    assert sorted(out.kept) == ["named", "related"]


def test_a_named_place_matches_by_the_place_keys():
    facts = [fact("coimbra", "Weekend in Coimbra", places=[COIMBRA]), fact("porto", "Visit Hanna in Porto",
                                                                           places=[PORTO])]
    out = search(facts, query(places=[("Coimbra", "")], scope="all"), place_keys=frozenset({COIMBRA}))
    assert out.kept == ["coimbra"] and out.keys == ("places",)


def test_an_unresolved_place_is_ignored_for_membership():
    facts = [fact("coimbra", "Weekend in Coimbra", domains=("travel",), places=[COIMBRA]),
             fact("porto", "Visit Hanna in Porto", domains=("travel",), places=[PORTO]),
             fact("gym", "Iron Temple gym", domains=("sport",))]
    out = search(facts, query(domains=["travel"], places=[("Atlantis", "")], scope="all"))
    assert out.mode == "all" and out.keys == ("domains",) and sorted(out.kept) == ["coimbra", "porto"]


def test_an_unresolved_place_alone_falls_back_to_top():
    facts = [fact("coimbra", "Weekend in Coimbra", places=[COIMBRA])]
    out = search(facts, query(subject="Atlantis trip", places=[("Atlantis", "")], scope="all"), semantic=["coimbra"])
    assert out.mode == "top" and out.kept == ["coimbra"]


def test_a_time_window_alone_defines_a_slice_with_a_recurring_occasion_by_month_and_day():
    facts = [fact("trip", "Weekend in Coimbra", points=[point("2026-03-07")]),
             fact("birthday", "Peter's birthday", points=[point("1948-03-02")], labels=["birthday"]),
             fact("later", "Skoda Fabia service", points=[point("2026-11-05")]),
             fact("undated", "Trains at the Iron Temple gym")]
    out = search(facts, query(intent="period", start=D("2026-03-01"), end=D("2026-03-31"), scope="all"))
    assert out.keys == ("time",) and sorted(out.kept) == ["birthday", "trip"]


def test_a_window_is_one_more_and_condition():
    facts = [fact("march", "Weekend in Coimbra", domains=("travel",), points=[point("2026-03-07")]),
             fact("may", "Porto with Hanna", domains=("travel",), points=[point("2026-05-02")]),
             fact("gym", "Iron Temple gym pass renewed", domains=("sport",), points=[point("2026-03-10")])]
    out = search(facts, query(domains=["travel"], intent="period", start=D("2026-03-01"), end=D("2026-03-31"),
                              scope="all"))
    assert out.kept == ["march"]


# ------------------------------------------------------------------------------------------------ no nearness, no caps

def test_no_knn_list_takes_part_and_the_set_is_the_only_list():
    facts = [fact("fabia", "Skoda Fabia oil change", domains=("transport",)),
             fact("near", "Skoda Fabia brochure", domains=("shopping",))]
    out = search(facts, query(subject="Skoda Fabia", domains=["transport"], scope="all"), semantic=["near", "fabia"],
                 labels=["near"])
    assert out.lists == {"set": ["fabia"]} and out.kept == ["fabia"]


def test_no_home_list_joins_a_phrase_that_names_no_place():
    gym = fact("gym", "Iron Temple gym three times a week", domains=("sport",), places=[SPRINGFIELD])
    lunch = fact("lunch", "Sunday lunch at the Springfield market", domains=("food",), places=[SPRINGFIELD])
    home = HomePlace(keys=frozenset({SPRINGFIELD}), lp=None)
    out = run([gym, lunch], query(domains=["sport"], scope="all"), home=home)
    assert out.kept == ["gym"] and "home" not in out.lists
    top = run([gym, lunch], query(domains=["sport"]), home=home)
    assert "lunch" in top.lists["home"]


def test_no_cap_and_no_cut():
    facts = [fact(f"f{n:03d}", f"Iron Temple gym session {n}", domains=("sport",)) for n in range(DOMAIN_CAP + 30)]
    out = search(facts, query(domains=["sport"], scope="all"))
    assert len(out.kept) == DOMAIN_CAP + 30 and out.uncut == {}


def test_the_duplicate_pass_runs_with_no_limit_and_removes_copies_only():
    facts = [fact(f"f{n:03d}", f"Iron Temple gym session {n}", domains=("sport",)) for n in range(CUT * 2)]
    copies = {f"f{n:03d}" for n in range(1, CUT * 2, 2)}
    out = search(facts, query(domains=["sport"], scope="all"),
                 duplicate=lambda candidate, kept: candidate in copies and int(kept[1:]) == int(candidate[1:]) - 1)
    assert out.kept == [f"f{n:03d}" for n in range(0, CUT * 2, 2)]
    assert out.fused == [f"f{n:03d}" for n in range(CUT * 2)]


def test_an_excluded_fact_leaves_the_set():
    facts = [fact("a", domains=("sport",)), fact("b", domains=("sport",))]
    out = run(facts, query(domains=["sport"], scope="all"), exclude={"a"})
    assert out.kept == ["b"] and out.lists["set"] == ["a", "b"]


# ------------------------------------------------------------------------------------------------ order

def test_the_set_is_ordered_by_the_subject_closeness_and_carries_it_as_its_score():
    facts = [fact(x, domains=("transport",)) for x in ("far", "mid", "near")]
    out = run(facts, query(subject="Skoda Fabia", domains=["transport"], scope="all"),
              sim={"far": 0.1, "mid": 0.5, "near": 0.9}, message_sim={"far": 0.9, "near": 0.1})
    assert out.kept == ["near", "mid", "far"] and out.scores == {"near": 0.9, "mid": 0.5, "far": 0.1}


def test_without_a_subject_the_message_closeness_orders_the_set():
    facts = [fact(x, domains=("transport",)) for x in ("far", "mid", "near")]
    out = run(facts, query(domains=["transport"], scope="all"), message_sim={"far": 0.1, "mid": 0.5, "near": 0.9})
    assert out.kept == ["near", "mid", "far"]


def test_latest_orders_newest_point_first_with_closeness_breaking_ties():
    old = fact("old", "Skoda Fabia bought", domains=("transport",), points=[point("2015-06-01")])
    new_far = fact("new_far", "Skoda Fabia tyres", domains=("transport",), points=[point("2026-10-01")])
    new_near = fact("new_near", "Skoda Fabia service", domains=("transport",), points=[point("2026-10-01")])
    undated = fact("undated", "Skoda Fabia has tinted windows", domains=("transport",), reported=date(2026, 1, 1))
    out = run([old, new_far, new_near, undated], query(subject="Skoda Fabia", domains=["transport"], intent="latest",
                                                        scope="all"),
              sim={"old": 0.9, "new_far": 0.2, "new_near": 0.8, "undated": 0.95})
    assert out.keys == ("domains",)
    assert out.kept == ["new_near", "new_far", "undated", "old"]     # an undated fact counts by its reported date


# ------------------------------------------------------------------------------------------------ the gate

def secret(fact_id="secret", **kw):
    """A neutral universe fact flagged intimate for the gate test: only the flag is sensitive, not the text."""
    base = dict(domains=("family",), sensitivity="intimate", people=[CLARA], kinds=["gift"])
    return fact(fact_id, "Birthday gift for Clara: a cookbook", **{**base, **kw})


def test_people_named_open_the_gate():
    out = search([secret()], query(people=["Clara"], scope="all"))
    assert out.kept == ["secret"] and out.gate_dropped == 0


def test_an_intimate_fact_entering_through_a_named_domain_stays():
    out = search([secret()], query(domains=["family"], kinds=["gift"], scope="all"))
    assert out.kept == ["secret"] and out.gate_dropped == 0


def test_otherwise_the_gate_drops_an_intimate_fact():
    plain = fact("plain", "Clara likes the Coimbra trips", kinds=["gift"])
    out = search([secret(), plain], query(kinds=["gift"], scope="all"))
    assert out.kept == ["plain"] and out.gate_dropped == 1 and out.lists["set"] == ["plain"]


def test_an_intimate_fact_under_an_ignored_domain_stays_out():
    plain = fact("plain", "Clara likes the Coimbra trips", domains=("family",), kinds=["gift"])
    out = search([secret(), plain], query(domains=["family"], kinds=["gift"], scope="all"))
    assert out.kept == ["plain"] and out.gate_dropped == 1 and out.keys == ("kinds",)


# ------------------------------------------------------------------------------------------------ fallback

def test_an_all_phrase_without_a_key_runs_top_with_the_net_the_scorer_and_the_cut():
    facts = [fact(f"f{n:03d}", f"Skoda Fabia note {n}", domains=("transport",)) for n in range(CUT + 10)]
    out = search(facts, query(subject="Skoda Fabia", scope="all"), semantic=[f.id for f in facts])
    assert out.mode == "top" and out.keys == ()
    assert "semantic" in out.lists and len(out.fused) == CUT + 10 and len(out.kept) == CUT


def test_latest_alone_is_no_key():
    facts = [fact("f", "Skoda Fabia service", points=[point("2026-10-01")])]
    out = search(facts, query(subject="Skoda Fabia", intent="latest", scope="all"), semantic=["f"])
    assert out.mode == "top"


# ------------------------------------------------------------------------------------------------ top: capped lists

def lists(facts, q, *, sim=None, message_sim=None, home_keys=frozenset()):
    return candidate_lists(FactSearchSnapshot.from_facts(list(facts)), q, semantic_ids=None, label_ids=None,
                           message_ids=None, similarity=sim or {}, query_place_keys=frozenset(), home_keys=home_keys,
                           message_similarity=message_sim)[0]


def test_the_top_domain_list_is_ordered_by_closeness_not_recency():
    new = fact("new", domains=("transport",), role="state", reported=date(2026, 11, 1))
    old = fact("old", domains=("transport",), role="state", reported=date(2016, 1, 1))
    got = lists([new, old], query(subject="Skoda Fabia", domains=["transport"]), sim={"new": 0.1, "old": 0.9})
    assert got["domain"] == ["old", "new"]


def test_two_domains_keep_the_both_group_first_each_group_by_closeness():
    facts = [fact("b_far", domains=("sport", "lifestyle")), fact("b_near", domains=("sport", "lifestyle")),
             fact("e_far", domains=("sport",)), fact("e_near", domains=("lifestyle",))]
    got = lists(facts, query(subject="gym", domains=["sport", "lifestyle"]),
                sim={"b_far": 0.1, "b_near": 0.5, "e_far": 0.2, "e_near": 0.9})
    assert got["domain"] == ["b_near", "b_far", "e_near", "e_far"]


def test_the_domain_cap_drops_the_farthest_facts_whatever_their_age():
    facts = [fact(f"f{n:03d}", domains=("transport",), role="state", reported=date(2026, 1, 1))
             for n in range(DOMAIN_CAP + 10)]
    oldest = fact("oldest", domains=("transport",), role="state", reported=date(2010, 1, 1))
    sim = {f.id: 0.5 for f in facts} | {"oldest": 0.9} | {f"f{n:03d}": 0.1 for n in range(10)}
    got = lists([*facts, oldest], query(subject="Skoda Fabia", domains=["transport"]), sim=sim)
    assert got["domain"][0] == "oldest" and len(got["domain"]) == DOMAIN_CAP
    assert not {f"f{n:03d}" for n in range(10)} & set(got["domain"])


def test_the_home_list_is_ordered_by_closeness_and_its_cap_drops_the_farthest():
    facts = [fact(f"h{n:03d}", places=[SPRINGFIELD], role="state", reported=date(2026, 11, 1))
             for n in range(HOME_CAP + 5)]
    close = {f.id: 0.5 for f in facts} | {f"h{n:03d}": 0.1 for n in range(5)} | {"h059": 0.9}
    got = lists(facts, query(subject="Springfield market"), sim=close, home_keys=frozenset({SPRINGFIELD}))
    assert got["home"][0] == "h059" and len(got["home"]) == HOME_CAP
    assert not {f"h{n:03d}" for n in range(5)} & set(got["home"])


def test_without_a_subject_the_capped_lists_use_the_message_closeness():
    facts = [fact("a", domains=("transport",), places=[SPRINGFIELD]), fact("b", domains=("transport",),
                                                                          places=[SPRINGFIELD])]
    got = lists(facts, query(domains=["transport"]), message_sim={"a": 0.1, "b": 0.9},
                home_keys=frozenset({SPRINGFIELD}))
    assert got["domain"] == ["b", "a"] and got["home"] == ["b", "a"]
