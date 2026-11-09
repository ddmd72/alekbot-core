"""Code computes a fact's domains and profile from the profiler's answer (fact profile RFC §3.3, taxonomy §5.2): the
precedence replayed on the taxonomy's `generic` boundary cases, the extras caps, `none` without a flag, directives, and
whole profiles of invented-universe facts (RFC §10.1)."""
import pytest

from src.domain.fact_domains import is_known
from src.domain.fact_profile import PROFILE_FIELDS, ProfileTimePoint
from src.domain.fact_profile_answer import ProfileAnswer
from src.domain.fact_profile_derive import (
    INTIMATE_WITHOUT_LABELS, derive_domains, derive_profile, directive_profile,
)
from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames

ABBR = {"BIO": "biography", "PPL": "people", "PET": "pets", "HLT": "health", "SPT": "sport", "FOOD": "food",
        "MED": "media", "HOB": "hobbies", "CEL": "going_out", "MEM": "memories", "LIFE": "lifestyle",
        "BEL": "beliefs", "HOME": "home", "TRN": "transport", "TECH": "tech", "CLO": "belongings", "APP": "appearance",
        "TRV": "travel", "MON": "money", "LAW": "state_law", "WRK": "work", "PRJ": "projects", "LRN": "learning",
        "ABL": "abilities", "NONE": "none"}
ATLAS = PlaceNames([PlaceRow(0.0, 0.0, "Rome", "Roma", "Italy", 2_000_000, geonameid="3169070")])
INSURANCE = ("Alex's car insurance with Example Mutual Insurance, policy MP-2026-118204, runs 01/04/2026–31/03/2027 "
             "and costs 412.00 EUR.")


def answer(**over):
    base = dict(label_words=[], time_role="timeless", time_points=[], places=[], people=[], tax_or_fine="none",
                user_identity=False, pet=False, state_or_law=False, about_person="none", subject="lifestyle",
                money_flow=False, use_areas=[], kinds=[], sensitivity="ordinary")
    base.update(over)
    return ProfileAnswer.model_validate(base)


def from_tokens(tokens):
    """The taxonomy test set's answer notation: I, P, S, r, o, m, T=tax|fine, s=SUBJ, u=[A,B], k=…, σ=…"""
    a = {}
    for tok in [t.strip() for t in tokens.split(";")]:
        if tok.startswith("T="):
            a["tax_or_fine"] = tok[2:]
        elif tok == "I":
            a["user_identity"] = True
        elif tok == "P":
            a["pet"] = True
        elif tok == "S":
            a["state_or_law"] = True
        elif tok == "r":
            a["about_person"] = "relative"
        elif tok == "o":
            a["about_person"] = "other"
        elif tok == "m":
            a["money_flow"] = True
        elif tok.startswith("s="):
            a["subject"] = ABBR[tok[2:]]
        elif tok.startswith("u=["):
            a["use_areas"] = [ABBR[x.strip()] for x in tok[3:-1].split(",")]
    return answer(**a)


# The taxonomy's boundary cases whose Origin is `generic` (§7): answers -> primary; extras.
GENERIC_CASES = [
    (6, "I; s=ABL", "biography", ["abilities"]),
    (16, "s=HOME; k=own", "home", []),
    (20, "s=HOME; m", "home", ["money"]),
    (24, "o; s=MED", "people", ["media"]),
    (26, "s=MON; m; u=[PPL]", "money", ["people"]),
    (32, "s=WRK", "work", []),
    (36, "P; s=HLT", "pets", ["health"]),
    (37, "P; s=HLT; m", "pets", ["money", "health"]),
    (38, "P; s=NONE; k=doc", "pets", []),
    (47, "S; s=TRN; k=doc", "state_law", ["transport"]),
    (48, "T=fine; S; s=TRN", "state_law", ["money", "transport"]),
    (52, "T=tax; s=MON", "money", ["state_law"]),
    (58, "s=BEL; m", "beliefs", ["money"]),
    (62, "s=TRN; m", "transport", ["money"]),
    (64, "s=TECH", "tech", []),
    (66, "s=CLO; k=pref", "belongings", []),
    (69, "s=TRV; k=pref", "travel", []),
    (74, "s=FOOD", "food", []),
    (77, "s=HLT; u=[SPT]; k=incident", "health", ["sport"]),
    (79, "s=SPT; m", "sport", ["money"]),
    (81, "s=MED", "media", []),
    (82, "s=HOB", "hobbies", []),
    (83, "s=PRJ; k=intention", "projects", []),
    (85, "s=BEL; σ=discreet", "beliefs", []),
    (87, "s=LRN", "learning", []),
    (97, "T=fine; S; s=TRN; m", "state_law", ["money", "transport"]),
    (131, "s=LIFE", "lifestyle", []),
    (139, "s=MON", "money", []),
    (140, "s=ABL", "abilities", []),
    (141, "o; s=TECH", "people", ["tech"]),
    (142, "s=LRN; k=doc", "learning", []),
    (144, "s=APP", "appearance", []),
    (145, "s=MEM", "memories", []),
    (147, "s=MON; m", "money", []),
]


@pytest.mark.parametrize("case, tokens, primary, extras", GENERIC_CASES, ids=[f"case{c[0]}" for c in GENERIC_CASES])
def test_the_generic_boundary_cases(case, tokens, primary, extras):
    assert derive_domains(from_tokens(tokens)) == (primary, tuple(extras), False)


# ------------------------------------------------------------------------------------------------ precedence and caps

def test_tax_outranks_every_flag_and_money_flow_adds_nothing_twice():
    got = derive_domains(answer(tax_or_fine="tax", user_identity=True, state_or_law=True, money_flow=True, subject="work"))
    assert got == ("money", ("state_law", "work", "biography"), False)


def test_identity_outranks_pet_state_and_person():
    got = derive_domains(answer(user_identity=True, pet=True, state_or_law=True, about_person="relative",
                                subject="none"))
    assert got == ("biography", ("pets", "state_law"), False)


def test_person_is_primary_when_it_is_the_only_flag():
    assert derive_domains(answer(about_person="relative", subject="health"))[:2] == ("family", ("health",))


def test_topical_extras_are_capped_at_two_after_the_subject_and_flag_hits():
    got = derive_domains(answer(pet=True, about_person="other", subject="travel", use_areas=["food", "media"]))
    assert got == ("pets", ("travel", "people"), False)


def test_only_the_first_two_use_areas_count_and_repeats_of_primary_or_hard_fall_out():
    assert derive_domains(answer(subject="home", money_flow=True, use_areas=["money", "tech"])) == (
        "home", ("money", "tech"), False)
    assert derive_domains(answer(subject="home", money_flow=True, use_areas=["money", "home", "tech", "travel"])) == (
        "home", ("money",), False)


def test_a_subject_without_a_flag_is_never_its_own_extra():
    assert derive_domains(answer(subject="travel", use_areas=["travel"])) == ("travel", (), False)


def test_none_without_a_flag_reads_as_lifestyle_and_is_a_profiling_error():
    assert derive_domains(answer(subject="none")) == ("lifestyle", (), True)


def test_none_with_a_flag_adds_no_subject():
    assert derive_domains(answer(state_or_law=True, subject="none")) == ("state_law", (), False)


def test_every_derived_key_is_a_registry_key():
    for _, tokens, _, _ in GENERIC_CASES:
        primary, extras, _ = derive_domains(from_tokens(tokens))
        assert all(is_known(k) for k in (primary, *extras))


def test_a_directive_is_agent_directives_alone():
    profile = directive_profile()
    assert (profile.primary_domain, profile.domain_keys, profile.profile_answer) == (
        "agent_directives", ("agent_directives",), None)
    assert (profile.label_words, profile.time_points, profile.user_identity) == ((), (), False)


# ------------------------------------------------------------------------------------------------ whole profiles

def test_the_car_insurance_profile():
    raw = answer(label_words=["insurance", "vehicle cover", "annual premium"], time_role="validity",
                 time_points=[{"from": "2026-04-01", "to": "2027-03-31", "precision": "day",
                               "source": "01/04/2026–31/03/2027"}],
                 subject="transport", money_flow=True, kinds=["document"])
    outcome = derive_profile(raw, INSURANCE, {}, ATLAS)
    p = outcome.profile
    assert outcome.redo_reason is None
    assert (p.primary_domain, p.domain_keys, p.time_role, p.kinds, p.sensitivity) == (
        "transport", ("transport", "money"), "validity", ("document",), "ordinary")
    assert p.time_points == (ProfileTimePoint("2026-04-01", "2027-03-31", "day", "01/04/2026–31/03/2027"),)
    assert p.label_words == ("vehicle cover", "annual premium")
    assert p.profile_drops == {"label_repeats_text": 1}
    assert (p.place_keys, p.people, p.user_identity) == ((), (), False)
    assert p.profile_answer == raw.stored()


def test_the_rome_trip_profile_places_and_points():
    text = "Rome trip 10–14 Aug 2025, Albergo Immaginario, booking RM-55120."
    raw = answer(label_words=["summer holiday"], time_role="event", subject="travel",
                 time_points=[{"from": "2025-08-10", "to": "2025-08-14", "precision": "day", "source": "10–14 Aug 2025"}],
                 places=[{"city": "Rome", "country": "Italy", "source": "Rome"}], kinds=["identifier"])
    p = derive_profile(raw, text, {"booking": "RM-55120"}, ATLAS).profile
    assert (p.primary_domain, p.place_keys, len(p.time_points)) == ("travel", ("pl:3169070", "co:italy"), 1)


def test_the_pollen_allergy_is_a_constraint_in_health():
    raw = answer(label_words=["hay fever"], time_role="state", subject="health", kinds=["constraint"])
    p = derive_profile(raw, "Alex has a pollen allergy.", None, ATLAS).profile
    assert (p.primary_domain, p.kinds, p.label_words) == ("health", ("constraint",), ("hay fever",))


def test_the_trade_union_membership_is_discreet_work_with_money():
    raw = answer(label_words=["labour union"], time_role="state", subject="work", money_flow=True,
                 sensitivity="discreet")
    p = derive_profile(raw, "Alex joined the Acme employees' trade union in November 2026; dues 12 EUR a month.",
                       None, ATLAS).profile
    assert (p.primary_domain, p.domain_keys, p.sensitivity) == ("work", ("work", "money"), "discreet")


def test_clara_moreiras_birth_date_is_identity_of_a_relative():
    raw = answer(label_words=["birthday"], time_role="timeless", about_person="relative", subject="none",
                 time_points=[{"from": "1982-07-14", "to": "", "precision": "day", "source": "14 July 1982"}],
                 people=[{"name": "Clara Moreira", "role": "partner", "source": "Clara Moreira"}])
    p = derive_profile(raw, "Clara Moreira was born on 14 July 1982.", None, ATLAS).profile
    assert (p.primary_domain, p.domain_keys) == ("family", ("family",))
    assert [(x.name, x.role) for x in p.people] == [("Clara Moreira", "partner")]
    assert p.time_points[0].start == "1982-07-14"


def test_none_without_a_flag_is_stored_as_lifestyle_and_counted():
    p = derive_profile(answer(subject="none", label_words=["daily rhythm"]), "Alex does morning yoga for 30 minutes.",
                       None, ATLAS).profile
    assert p.primary_domain == "lifestyle" and p.profile_drops == {"subject_none_without_flag": 1}


def test_use_areas_over_the_cap_are_counted():
    raw = answer(subject="home", use_areas=["tech", "travel", "food", "media"])
    p = derive_profile(raw, "Washer warranty WX800-55-1209, valid until 30/11/2027.", None, ATLAS).profile
    assert p.domain_keys == ("home", "tech", "travel") and p.profile_drops == {"use_areas_over_cap": 2}


def test_an_intimate_answer_without_a_surviving_label_needs_a_redo():
    raw = answer(subject="health", sensitivity="intimate", label_words=["allergy"])
    outcome = derive_profile(raw, "Alex has a pollen allergy.", None, ATLAS)
    assert outcome.redo_reason == INTIMATE_WITHOUT_LABELS
    assert outcome.profile.profile_drops == {"label_repeats_text": 1, INTIMATE_WITHOUT_LABELS: 1}


def test_an_intimate_answer_with_a_label_is_stored():
    raw = answer(subject="health", sensitivity="intimate", label_words=["hay fever"])
    assert derive_profile(raw, "Alex has a pollen allergy.", None, ATLAS).redo_reason is None


def test_the_stored_profile_is_plain_json_values():
    raw = answer(label_words=["summer holiday"], time_role="event", subject="travel",
                 time_points=[{"from": "2025-08-10", "to": "2025-08-14", "precision": "day", "source": "10–14 Aug 2025"}],
                 places=[{"city": "Rome", "country": "Italy", "source": "Rome"},
                         {"city": "Coimbra", "country": "Portugal", "source": "Rome"}],
                 people=[{"name": "Clara Moreira", "role": "Partner", "source": "Clara Moreira"}])
    stored = derive_profile(raw, "Rome trip 10–14 Aug 2025, Albergo Immaginario.", None, ATLAS).profile.stored()
    assert stored["time_points"] == [{"from": "2025-08-10", "to": "2025-08-14", "precision": "day",
                                      "source": "10–14 Aug 2025"}]
    assert stored["places_unresolved"] == [{"city": "Coimbra", "country": "Portugal"}]
    assert stored["people"] == []      # the universe's Rome trip names no companion: the answer's person is not printed
    assert stored["domain_keys"] == ["travel"]
    assert stored["profile_drops"] == {"place_unresolved:unknown_country": 1, "person_not_printed": 1}
    birth = answer(subject="none", about_person="relative",
                   people=[{"name": "Clara Moreira", "role": "Partner", "source": "Clara Moreira"}])
    (person,) = derive_profile(birth, "Clara Moreira was born on 14 July 1982.", None, ATLAS).profile.stored()["people"]
    assert (person["name"], person["role"]) == ("Clara Moreira", "partner")
    assert isinstance(person["name_keys"], list) and isinstance(person["role_keys"], list) and person["role_keys"]


def test_a_stored_profile_names_only_profile_fields():
    p = derive_profile(answer(subject="food", label_words=["tea time"]), "Maria likes tea.", None, ATLAS).profile
    assert set(p.stored()) <= PROFILE_FIELDS
    assert set(directive_profile().stored()) <= PROFILE_FIELDS
