"""The intimate gate of the fact search (fact profile RFC §10.1): an intimate fact stays only in the labels list and its
own domain list; collecting everything about a person lifts the gate; a discreet fact is never gated."""
import pytest

from tests.support.fact_search_kit import fact, person, point, query, search
from datetime import date

D = date.fromisoformat


def intimate(fact_id="secret", **kw):
    """A neutral universe fact (the washer warranty in the Lviv flat) with the sensitivity set, as RFC §10.1 has it."""
    base = dict(domains=("home",), sensitivity="intimate", points=[point("2027-11-30")], kinds=["document"],
                places=["pl:1"], people=[person("Taras Melnyk", "")])
    return fact(fact_id, "Washer warranty WX800-55-1209 until 30/11/2027", **{**base, **kw})


def everything(**kw):
    return dict(subject="x", kind="y", domains=["home"], kinds=["document"], people=["Taras"],
                places=[("Lviv", "Ukraine")], intent="future", start=D("2027-11-01"), end=D("2027-12-31"), **kw)


def test_an_intimate_fact_stays_only_in_the_labels_and_domain_lists():
    out = search([intimate()], query(**everything()), semantic=["secret"], labels=["secret"],
                 place_keys=frozenset({"pl:1"}))
    assert {name: members for name, members in out.lists.items()} == {
        "semantic": [], "labels": ["secret"], "domain": ["secret"], "time": [], "kinds": [], "places": [], "people": []}
    assert out.gate_dropped == 5          # semantic, time, kinds, places, people
    assert out.kept == ["secret"]


@pytest.mark.parametrize("name", ["semantic", "time", "kinds", "places", "people"])
def test_each_other_list_drops_it(name):
    kw = dict(semantic=["secret"], place_keys=frozenset({"pl:1"}))
    out = search([intimate(), fact("plain", sensitivity="ordinary", domains=("work",))], query(**everything()), **kw)
    assert "secret" not in out.lists[name]


def test_collect_all_about_a_person_lifts_the_gate():
    out = search([intimate()], query(people=["Taras"], scope="all"))       # list mode RFC §3.5: one list, the set
    assert out.lists == {"set": ["secret"]} and out.gate_dropped == 0 and out.kept == ["secret"]


def test_collect_all_without_a_person_keeps_the_gate():
    out = search([intimate()], query(kinds=["document"], scope="all"))
    assert out.lists == {"set": []} and out.gate_dropped == 1 and out.kept == []


def test_a_person_without_collect_all_keeps_the_gate():
    out = search([intimate()], query(people=["Taras"]))
    assert out.lists["people"] == [] and out.gate_dropped == 1


def test_a_discreet_fact_is_never_gated():
    union = fact("union", "Alex joined the Acme employees' trade union in November 2026; union dues 12 EUR a month",
                 sensitivity="discreet", domains=("work", "money"), kinds=["status"],
                 people=[person("Alex Doe", "")], places=["pl:1"], points=[point("2026-11", precision="month")])
    out = search([union], query(**{**everything(), "kinds": ["status"], "domains": ["work"], "people": ["Alex"],
                                   "start": D("2026-11-20"), "end": D("2026-12-31")}),
                 semantic=["union"], labels=["union"], place_keys=frozenset({"pl:1"}))
    assert out.gate_dropped == 0
    for name in ("semantic", "labels", "domain", "time", "kinds", "places", "people"):
        assert out.lists[name] == ["union"], name


def test_an_unprofiled_fact_has_no_sensitivity_and_passes_the_gate():
    out = search([fact("bare", profiled=False)], query(domains=["health"]), semantic=["bare"])
    assert out.lists["semantic"] == ["bare"] and out.gate_dropped == 0
