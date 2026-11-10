"""The scorer's features (fact profile RFC §5.3, OD-14): each feature's value on hand-built snapshot rows of the invented
universe. Weights play no part here."""
import math
from dataclasses import replace
from datetime import date, timedelta

from src.domain.fact_search_rank import (
    AGE_DAYS, DOMAIN_FURTHER, DOMAIN_PRIMARY, FEATURES, NEAR_DAYS, PEOPLE_CARD_SPELLING, PEOPLE_NAME,
    PEOPLE_RELATION, PLACE_COUNTRY, PLACE_ELSEWHERE, PLACE_HIT, PLACE_NEIGHBOUR, CardRef, HomePlace, QueryPlaceKeys,
    ScoreInputs, card_ref, feature_values,
)
from src.domain.fact_search_snapshot import FactSearchSnapshot, name_skeleton
from src.domain.person_cards import PersonCard
from tests.support.fact_search_kit import TODAY, fact, person, point, query

SPRINGFIELD, SHELBYVILLE, PORTO, COIMBRA = "pl:9100001", "pl:9100002", "pl:9100003", "pl:9100004"
PORTUGAL, ITALY = "co:portugal", "co:italy"
# Springfield as the atlas reaches it: its own key, then its neighbouring town's.
AT_SPRINGFIELD = QueryPlaceKeys(keys=(SPRINGFIELD, SHELBYVILLE), country=PORTUGAL, lp="lp:springfield")
AT_COIMBRA = QueryPlaceKeys(keys=(COIMBRA,), country=PORTUGAL, lp="lp:coimbra")
IN_PORTUGAL = QueryPlaceKeys(keys=(PORTUGAL,), country=PORTUGAL, lp="lp:portugal")
HOME = HomePlace(keys=frozenset({SPRINGFIELD, SHELBYVILLE}), lp="lp:springfield")


def values(f, q, inputs=ScoreInputs()):
    return feature_values(q, FactSearchSnapshot.from_facts([f]).rows[f.id], inputs)


def place(fact_keys, *places):
    q = query(places=[("Coimbra", "Portugal")] * len(places))
    return values(fact("f", places=fact_keys), q, ScoreInputs(places=tuple(places)))["place"]


def test_every_feature_is_present_and_nothing_else():
    assert set(values(fact("car", "Car service 2026-11-05"), query())) == set(FEATURES)


# ------------------------------------------------------------------------------------------------ similarities

def test_the_similarities_read_the_inputs_only_when_the_query_has_that_text():
    f = fact("coimbra", "Weekend in Coimbra with Clara Mar 6-8")
    inputs = ScoreInputs(sim_text={"coimbra": 0.5}, sim_label={"coimbra": 0.25}, sim_message={"coimbra": 0.75})
    full = replace(query(subject="weekend trip", kind="trip"), message="where did we go with Clara in March")
    got = values(f, full, inputs)
    assert (got["sim_text"], got["sim_label"], got["sim_message"]) == (0.5, 0.25, 0.75)
    bare = values(f, query(domains=["travel"]), inputs)
    assert (bare["sim_text"], bare["sim_label"], bare["sim_message"]) == (0.0, 0.0, 0.0)


def test_a_similarity_missing_from_the_inputs_reads_zero():
    got = values(fact("knee", "Knee surgery 2026-10-05"), replace(query(subject="knee", kind="surgery"), message="m"))
    assert (got["sim_text"], got["sim_label"], got["sim_message"]) == (0.0, 0.0, 0.0)


# ------------------------------------------------------------------------------------------------ place

def test_the_named_city_held_is_a_hit():
    assert place([COIMBRA, PORTUGAL], AT_COIMBRA) == PLACE_HIT


def test_the_owner_confirmed_local_key_is_a_hit_even_when_the_atlas_has_no_keys():
    local_only = QueryPlaceKeys(keys=(), country=None, lp="lp:springfield")
    assert place(["lp:springfield"], local_only) == PLACE_HIT
    assert place([COIMBRA], local_only) == 0.0


def test_a_country_alone_is_a_hit_when_held_and_nothing_else():
    assert place([COIMBRA, PORTUGAL], IN_PORTUGAL) == PLACE_HIT
    assert place([ITALY, "pl:100"], IN_PORTUGAL) == 0.0     # another country's city is not graded down


def test_the_neighbouring_town_the_same_country_and_another_place_grade_down_in_that_order():
    assert place([SHELBYVILLE], AT_SPRINGFIELD) == PLACE_NEIGHBOUR
    assert place([PORTO, PORTUGAL], AT_SPRINGFIELD) == PLACE_COUNTRY
    assert place([PORTO], AT_SPRINGFIELD) == PLACE_ELSEWHERE
    assert place(["lp:braga"], AT_SPRINGFIELD) == PLACE_ELSEWHERE
    assert place([ITALY], AT_SPRINGFIELD) == 0.0
    assert place([], AT_SPRINGFIELD) == 0.0
    assert PLACE_HIT > PLACE_NEIGHBOUR > PLACE_COUNTRY > 0 > PLACE_ELSEWHERE


def test_the_best_query_place_counts():
    assert place([COIMBRA], AT_SPRINGFIELD, AT_COIMBRA) == PLACE_HIT


def test_without_a_query_place_the_place_grade_is_zero():
    assert values(fact("f", places=[COIMBRA]), query(), ScoreInputs(places=(AT_COIMBRA,)))["place"] == 0.0


# ------------------------------------------------------------------------------------------------ home

def test_home_unplaced_marks_a_placeless_fact_when_a_query_place_is_the_home_town():
    q = query(places=[("Springfield", "Portugal")])
    inputs = ScoreInputs(places=(AT_SPRINGFIELD,), home=HOME)
    assert values(fact("car", "Car service 2026-11-05"), q, inputs)["home_unplaced"] == 1.0
    assert values(fact("lunch", places=[SPRINGFIELD]), q, inputs)["home_unplaced"] == 0.0
    other = ScoreInputs(places=(AT_COIMBRA,), home=HOME)
    assert values(fact("car", "Car service 2026-11-05"), query(places=[("Coimbra", "")]), other)["home_unplaced"] == 0.0


def test_home_unplaced_also_matches_the_home_town_by_its_local_key():
    local_home = HomePlace(keys=frozenset(), lp="lp:springfield")
    inputs = ScoreInputs(places=(QueryPlaceKeys(keys=(), country=None, lp="lp:springfield"),), home=local_home)
    assert values(fact("car"), query(places=[("Springfield", "")]), inputs)["home_unplaced"] == 1.0


def test_home_implicit_marks_a_fact_at_home_when_the_query_names_no_place():
    lunch = fact("lunch", "Christmas lunch at Maria's", places=[SPRINGFIELD, PORTUGAL])
    assert values(lunch, query(), ScoreInputs(home=HOME))["home_implicit"] == 1.0
    assert values(fact("hamburg", places=["pl:9100005"]), query(), ScoreInputs(home=HOME))["home_implicit"] == 0.0
    assert values(lunch, query(), ScoreInputs())["home_implicit"] == 0.0
    with_place = ScoreInputs(places=(AT_COIMBRA,), home=HOME)
    assert values(lunch, query(places=[("Coimbra", "")]), with_place)["home_implicit"] == 0.0


# ------------------------------------------------------------------------------------------------ kind and domain

def test_kind_marks_a_requested_kind_among_the_facts_kinds():
    dentist = fact("dentist", "Dentist check-up 2026-10-12", kinds=["appointment"])
    assert values(dentist, query(kinds=["appointment", "document"]))["kind"] == 1.0
    assert values(dentist, query(kinds=["document"]))["kind"] == 0.0
    assert values(dentist, query())["kind"] == 0.0


def test_domain_is_full_for_the_primary_domain_and_half_for_a_further_one():
    knee = fact("knee", "Knee surgery 2026-10-05", domains=("health", "sport"))
    assert values(knee, query(domains=["health"]))["domain"] == DOMAIN_PRIMARY
    assert values(knee, query(domains=["sport"]))["domain"] == DOMAIN_FURTHER
    assert values(knee, query(domains=["work", "sport"]))["domain"] == DOMAIN_FURTHER
    assert values(knee, query(domains=["work"]))["domain"] == 0.0
    assert values(knee, query())["domain"] == 0.0


# ------------------------------------------------------------------------------------------------ people

MARIA_CARD = PersonCard("doe_mari", "Maria Doe", ("Maria Doe", "Maria A. Doe", "Mariia", "Mia"),
                        tuple("x:" + k for k in sorted(name_skeleton("Maria A. Doe") | name_skeleton("Mia"))),
                        "mother", 12)


def people(mentions, people_slot, cards=()):
    f = fact("f", people=mentions)
    return values(f, query(people=people_slot), ScoreInputs(cards=tuple(cards)))["people"]


def test_a_queried_name_on_a_mention_is_full_either_way_round():
    assert people([person("Clara Moreira", "partner")], ["Clara Moreira"]) == PEOPLE_NAME
    assert people([person("Clara Moreira")], ["Clara"]) == PEOPLE_NAME
    assert people([person("Clara")], ["Clara Moreira"]) == PEOPLE_NAME


def test_another_spelling_on_the_card_of_a_queried_name_is_high():
    assert people([person("Mia")], ["Maria Doe"], [card_ref(MARIA_CARD)]) == PEOPLE_CARD_SPELLING
    assert people([person("Mia")], ["Maria Doe"]) == 0.0


def test_a_queried_relation_on_the_mentions_role_or_its_cards_relation_is_medium():
    assert people([person("Hanna Doe", "sister")], ["my sister"]) == PEOPLE_RELATION
    assert people([person("Mia")], ["my mother"], [card_ref(MARIA_CARD)]) == PEOPLE_RELATION
    assert people([person("Peter Doe", "father")], ["my mother"], [card_ref(MARIA_CARD)]) == 0.0


def test_the_best_mention_counts_and_a_query_without_people_reads_zero():
    mentions = [person("Peter Doe", "father"), person("Clara Moreira", "partner")]
    assert people(mentions, ["Clara Moreira"]) == PEOPLE_NAME
    assert values(fact("f", people=mentions), query())["people"] == 0.0
    assert PEOPLE_NAME > PEOPLE_CARD_SPELLING > PEOPLE_RELATION > 0


def test_a_card_reads_its_name_keys_without_the_skeleton_prefix_and_its_relation_in_primary_form():
    ref = card_ref(MARIA_CARD)
    assert ref == CardRef(names=name_skeleton("Maria A. Doe") | name_skeleton("Mia"), relation="mother")
    assert card_ref(replace(MARIA_CARD, relation=None)).relation is None


# ------------------------------------------------------------------------------------------------ time role

def test_the_time_role_is_one_hot_and_anything_unknown_is_timeless():
    for role in ("event", "state", "validity", "measurement"):
        got = values(fact("f", role=role), query())
        assert [n for n in FEATURES if n.startswith("role_") and got[n]] == [f"role_{role}"]
    for role in (None, "timeless", "sometimes"):
        got = values(fact("f", role=role), query())
        assert [n for n in FEATURES if n.startswith("role_") and got[n]] == ["role_timeless"]


# ------------------------------------------------------------------------------------------------ time

def day(d: date) -> str:
    return d.isoformat()


def time_of(f, q):
    got = values(f, q)
    return got["t_overlap"], got["t_near"], got["no_time"], got["event_wrong_side"]


def test_a_point_inside_the_window_overlaps():
    car = fact("car", "Car service 2026-11-05", role="event", points=[point("2026-11-05")])
    assert time_of(car, query(intent="past"))[:3] == (1.0, 0.0, 0.0)


def test_without_a_window_the_target_is_today():
    today = fact("f", role="event", points=[point(day(TODAY))])
    assert time_of(today, query())[0] == 1.0


def test_closeness_counts_either_side_of_the_target():
    before = fact("b", role="state", points=[point(day(TODAY - timedelta(days=15)))])
    after = fact("a", role="state", points=[point(day(TODAY + timedelta(days=15)))])
    near = math.exp(-15 / NEAR_DAYS)
    assert time_of(before, query())[:3] == (0.0, near, 0.0)
    assert time_of(after, query())[:3] == (0.0, near, 0.0)


def test_the_nearest_point_counts():
    f = fact("f", role="event", points=[point(day(TODAY - timedelta(days=60))), point(day(TODAY + timedelta(days=3)))])
    assert time_of(f, query())[1] == math.exp(-3 / NEAR_DAYS)


def test_a_fact_without_a_time_point_is_marked():
    assert time_of(fact("lunch", "Christmas lunch at Maria's"), query(intent="future")) == (0.0, 0.0, 1.0, 0.0)


def test_an_event_wholly_on_the_wrong_side_of_a_future_or_past_intent_is_marked():
    surgery = fact("knee", "Knee surgery 2026-10-05", role="event", points=[point("2026-10-05")])
    assert time_of(surgery, query(intent="future"))[3] == 1.0
    assert time_of(surgery, query(intent="past"))[3] == 0.0
    assert time_of(surgery, query())[3] == 0.0
    later = fact("conf", role="event", points=[point(day(TODAY + timedelta(days=20)))])
    assert time_of(later, query(intent="past"))[3] == 1.0
    assert time_of(later, query(intent="future"))[3] == 0.0
    state = fact("state", role="state", points=[point("2026-10-05")])
    assert time_of(state, query(intent="future"))[3] == 0.0


def test_a_recurring_occasion_is_projected_only_under_a_window_intent():
    birthday = fact("bday", "Clara's birthday is on 07-14", role="event", labels=["birthday"],
                    points=[point("1982-07-14")])
    assert time_of(birthday, query(intent="future"))[0] == 1.0      # next July lies inside the year ahead
    plain = fact("bday", "Clara's birthday is on 07-14", role="event", points=[point("1982-07-14")])
    assert time_of(plain, query(intent="future"))[0] == 0.0
    for intent in ("none", "latest"):
        overlap, near, _, _ = time_of(birthday, query(intent=intent))
        assert overlap == 0.0 and near < math.exp(-365 / NEAR_DAYS)


def test_age_decays_with_the_days_since_reported_and_never_exceeds_one():
    year_ago = fact("f", reported=TODAY - timedelta(days=365))
    assert values(year_ago, query())["age"] == math.exp(-365 / AGE_DAYS)
    assert values(fact("f", reported=TODAY + timedelta(days=3)), query())["age"] == 1.0
