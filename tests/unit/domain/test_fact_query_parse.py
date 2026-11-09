"""parse_slots turns the query step's JSON into a FactQuery; code decides what survives (fact profile RFC §5.1)."""
from datetime import date

import pytest

from src.domain.fact_domains import Domain, FACT_DOMAINS
from src.domain.fact_profile_answer import PROFILE_KINDS
from src.domain.fact_query import FactQuery, QueryPlace, QueryTime
from src.domain.fact_query_parse import QUERY_DOMAINS, parse_slots, semantic_only_query
from src.domain.fact_search_lists import window

TODAY = date(2026, 11, 9)
MARIIA_UK = "Марія Доу"      # the Ukrainian transliteration of Maria Doe (invented universe)


def _slots(**over):
    base = {
        "subject": "", "kind": "", "people": [], "places": [],
        "time": {"intent": "none", "from": "", "to": ""},
        "domains": [], "domain_link": "one_matter", "kinds": [], "scope": "top",
    }
    base.update(over)
    return base


def _parse(**over):
    return parse_slots(_slots(**over), today=TODAY, path="router")


def test_query_domains_are_the_registry_keys_with_a_query_row():
    assert Domain.AGENT_DIRECTIVES not in QUERY_DOMAINS
    assert QUERY_DOMAINS == tuple(e.key for e in FACT_DOMAINS if e.key != Domain.AGENT_DIRECTIVES)
    assert len(QUERY_DOMAINS) == 26


def test_a_valid_payload_becomes_the_query():
    parsed = _parse(
        subject="Clara's passport expiry", kind="identity document, renewal", people=["Clara"],
        places=[{"name": "Rome", "country": "Italy"}],
        time={"intent": "future", "from": "2026-11-09", "to": "2026-12-31"},
        domains=["biography", "family"], kinds=["document", "identifier"], scope="all",
    )
    assert parsed.query == FactQuery(
        today=TODAY, subject="Clara's passport expiry", kind="identity document, renewal", people=("Clara",),
        places=(QueryPlace(name="Rome", country="Italy"),),
        time=QueryTime(intent="future", start=date(2026, 11, 9), end=date(2026, 12, 31)),
        domains=("biography", "family"), kinds=("document", "identifier"), scope="all", path="router",
    )
    assert parsed.refused == () and parsed.dropped == ()


@pytest.mark.parametrize("raw", [None, [], "text", 5, [{"subject": "x"}]])
def test_not_a_mapping_is_invalid_output(raw):
    with pytest.raises(ValueError):
        parse_slots(raw, today=TODAY, path="router")


def test_an_empty_object_is_the_empty_top_query():
    parsed = parse_slots({}, today=TODAY, path="fm")
    assert parsed.query == FactQuery(today=TODAY, path="fm")
    assert parsed.dropped == () and parsed.refused == ()


def test_subject_and_kind_are_strings_only_and_stripped():
    assert _parse(subject="  car insurance  ", kind=" insurance ").query.subject == "car insurance"
    assert _parse(subject="  car insurance  ", kind=" insurance ").query.kind == "insurance"
    query = _parse(subject=["a"], kind=3).query
    assert (query.subject, query.kind) == ("", "")


def test_people_are_stripped_and_empty_ones_dropped():
    assert _parse(people=[" Maria ", "", "  ", "Clara"]).query.people == ("Maria", "Clara")
    assert _parse(people=[1, None, "Tom"]).query.people == ("Tom",)
    assert _parse(people="Maria").query.people == ()


def test_places_are_kept_when_they_have_a_name_or_a_country():
    parsed = _parse(places=[
        {"name": "Rome", "country": ""}, {"name": "", "country": "Italy"}, {"name": "", "country": ""},
        {"name": " Porto ", "country": " Portugal "}, "Rome", {"name": 4, "country": None},
    ])
    assert parsed.query.places == (
        QueryPlace("Rome", ""), QueryPlace("", "Italy"), QueryPlace("Porto", "Portugal"),
    )


@pytest.mark.parametrize("intent", ["none", "future", "past", "period", "latest"])
def test_the_five_intents_pass(intent):
    assert _parse(time={"intent": intent, "from": "", "to": ""}).query.time.intent == intent


@pytest.mark.parametrize("intent", ["soon", "", None, 3, "FUTURE"])
def test_an_unknown_intent_is_none(intent):
    assert _parse(time={"intent": intent, "from": "", "to": ""}).query.time.intent == "none"


def test_a_missing_or_malformed_time_is_none():
    assert _parse(time="tomorrow").query.time == QueryTime()
    assert parse_slots({"time": None}, today=TODAY, path="x").query.time == QueryTime()


def test_dates_read_the_first_ten_characters():
    time = _parse(time={"intent": "period", "from": "2025-08-10T00:00:00Z", "to": "2025-08-14"}).query.time
    assert (time.start, time.end) == (date(2025, 8, 10), date(2025, 8, 14))


def test_an_invalid_date_is_dropped_and_named():
    parsed = _parse(time={"intent": "period", "from": "August", "to": "2025-13-40"})
    assert (parsed.query.time.start, parsed.query.time.end) == (None, None)
    assert parsed.dropped == ("from", "to")


def test_an_empty_date_is_not_a_drop():
    parsed = _parse(time={"intent": "latest", "from": "", "to": ""})
    assert parsed.dropped == ()


def test_unknown_domains_are_dropped_and_agent_directives_too():
    parsed = _parse(domains=["agent_directives", "weather", "health"])
    assert parsed.query.domains == ("health",)
    assert parsed.dropped == ("domains", "domains")


def test_four_domains_keep_the_first_two_known_ones():
    parsed = _parse(domains=["nonsense", "travel", "money", "health", "work"])
    assert parsed.query.domains == ("travel", "money")
    assert parsed.dropped.count("domains") == 3


def test_repeated_domains_collapse():
    assert _parse(domains=["travel", "travel", "money"]).query.domains == ("travel", "money")


def test_non_string_domains_and_kinds_are_dropped_without_error():
    parsed = _parse(domains=[{"a": 1}, ["x"], "travel"], kinds=[None, "document"])
    assert parsed.query.domains == ("travel",) and parsed.query.kinds == ("document",)


def test_kinds_outside_the_closed_set_are_dropped():
    parsed = _parse(kinds=["constraint", "hobby", "ownership"])
    assert parsed.query.kinds == ("constraint", "ownership")
    assert parsed.dropped == ("kinds",)
    assert set(_parse(kinds=list(PROFILE_KINDS)).query.kinds) == set(PROFILE_KINDS)


def test_scope_is_all_or_top():
    assert _parse(scope="all").query.scope == "all"
    for value in ("top", "everything", "", None):
        assert _parse(scope=value).query.scope == "top"


def test_domain_link_does_not_change_the_query():
    one = _parse(domains=["travel", "money"], domain_link="one_matter").query
    two = _parse(domains=["travel", "money"], domain_link="separate_topics").query
    assert one == two
    assert _parse(domains=["travel", "money"], domain_link="bogus").query == one


def test_path_and_today_are_carried():
    query = parse_slots(_slots(), today=date(2026, 1, 2), path="connector").query
    assert (query.today, query.path) == (date(2026, 1, 2), "connector")


# ---- Cyrillic refusals -------------------------------------------------------------------------------------------

def test_a_cyrillic_subject_is_emptied_and_named():
    parsed = _parse(subject=MARIIA_UK, kind="family")
    assert parsed.query.subject == "" and parsed.query.kind == "family"
    assert parsed.refused == ("subject",)


def test_a_cyrillic_kind_is_emptied_and_named():
    parsed = _parse(subject="mother", kind=MARIIA_UK)
    assert (parsed.query.subject, parsed.query.kind) == ("mother", "")
    assert parsed.refused == ("kind",)


def test_a_cyrillic_person_is_removed_and_the_rest_kept():
    parsed = _parse(people=[MARIIA_UK, "Maria Doe", MARIIA_UK])
    assert parsed.query.people == ("Maria Doe",)
    assert parsed.refused == ("people",)


def test_a_cyrillic_place_is_removed_by_either_field():
    parsed = _parse(places=[{"name": "Рим", "country": "Italy"}, {"name": "Rome", "country": "Італія"},
                            {"name": "Porto", "country": "Portugal"}])
    assert parsed.query.places == (QueryPlace("Porto", "Portugal"),)
    assert parsed.refused == ("places",)


def test_every_refused_slot_is_named_once_in_order():
    parsed = _parse(subject=MARIIA_UK, kind=MARIIA_UK, people=[MARIIA_UK, MARIIA_UK],
                    places=[{"name": "Рим", "country": ""}])
    assert parsed.refused == ("subject", "kind", "people", "places")


def test_latin_with_accents_is_not_refused():
    parsed = _parse(people=["João Exemplo"], places=[{"name": "Kraków", "country": "Poland"}])
    assert parsed.refused == () and parsed.query.people == ("João Exemplo",)


# ---- the clamp is PR 6's `window`, applied to the parsed query ----------------------------------------------------

def test_a_future_start_before_today_starts_today():
    query = _parse(time={"intent": "future", "from": "2026-01-01", "to": "2026-12-09"}).query
    assert window(query) == (TODAY, date(2026, 12, 9))


def test_a_past_end_after_today_ends_today():
    query = _parse(time={"intent": "past", "from": "2026-10-01", "to": "2027-03-01"}).query
    assert window(query) == (date(2026, 10, 1), TODAY)


def test_a_period_with_one_end_is_a_month_window():
    only_start = _parse(time={"intent": "period", "from": "2025-08-10", "to": ""}).query
    only_end = _parse(time={"intent": "period", "from": "", "to": "2025-08-14"}).query
    assert window(only_start) == (date(2025, 8, 10), date(2025, 9, 10))
    assert window(only_end) == (date(2025, 7, 14), date(2025, 8, 14))


def test_a_period_with_both_ends_is_that_span():
    query = _parse(time={"intent": "period", "from": "2025-08-10", "to": "2025-08-14"}).query
    assert window(query) == (date(2025, 8, 10), date(2025, 8, 14))


def test_an_invalid_date_leaves_the_window_to_the_clamp():
    query = _parse(time={"intent": "future", "from": "soon", "to": ""}).query
    assert window(query) == (TODAY, date(2027, 11, 9))


# ---- the degradation query ---------------------------------------------------------------------------------------

def test_the_semantic_only_query_is_the_raw_request_and_nothing_else():
    query = semantic_only_query("when does the pollen allergy flare up", today=TODAY, path="fm")
    assert query == FactQuery(today=TODAY, subject="when does the pollen allergy flare up", path="fm")
    assert query.people == () and query.places == () and query.domains == () and query.kinds == ()
    assert query.time == QueryTime() and query.scope == "top"
