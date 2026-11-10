"""The confirmed people list in the fingerprint (fact profile RFC §4.2, §4.5): the model reads the whole list, the
fingerprint holds only the entries the fact names, so a list edit makes only the facts naming the edited person stale."""
from src.domain.confirmed_people import ConfirmedPeople, parse_confirmed_people, render_people_block
from src.domain.entities import FactEntity, FactType
from src.domain.fact_profile_fingerprint import (
    DERIVE_VERSION, ProfileStatus, build_profiler_user_message, classify_profile, named_people, profile_derivation,
    profile_fingerprint,
)

RAW = {
    "user": {"name": "Alex Doe", "forms": ["Alex", "Bo"]},
    "people": [
        {"name": "Clara Moreira", "forms": ["Clara"], "relation": "partner"},
        {"name": "Ana Doe", "forms": [], "relation": "younger daughter"},
    ],
}
ANA = "The user has a daughter, Ana."
CLARA = "Weekend in Coimbra with Clara."
NOBODY = "The Iron Temple gym closes on Sunday."
DERIVATION = profile_derivation("reg", "atlas", False, 2026)


def household(**edit) -> ConfirmedPeople:
    data = {"user": dict(RAW["user"]), "people": [dict(p) for p in RAW["people"]]}
    for index, change in edit.items():
        data["people"][int(index[1:])].update(change)
    return parse_confirmed_people(data)


def fact(text, metadata=None, **over):
    return FactEntity(account_id="acc", created_by_user_id="usr", lineage_id="lin", text=text, type=FactType.EVENT,
                      metadata=metadata or {}, **over)


def profiled(text, people, metadata=None):
    named = named_people(people, text, metadata)
    return fact(text, metadata, profile_fingerprint=profile_fingerprint(text, metadata, named),
                profile_derivation=DERIVATION)


def status(profiled_fact, people):
    named = named_people(people, profiled_fact.text, profiled_fact.metadata)
    return classify_profile(profiled_fact, DERIVATION, named)


def test_without_people_the_message_is_the_two_argument_message_byte_for_byte():
    assert build_profiler_user_message(ANA, {"k": "v"}, ()) == build_profiler_user_message(ANA, {"k": "v"})
    assert build_profiler_user_message(ANA, None) == f"Fact:\n{ANA}"


def test_the_whole_list_block_precedes_the_fact():
    people = household()
    message = build_profiler_user_message(ANA, None, people.entries())
    assert message == render_people_block(people.entries()) + f"Fact:\n{ANA}"
    assert message.index("Confirmed people") < message.index("Fact:")


def test_a_fact_naming_nobody_keeps_the_bytes_it_had_before_the_list_existed():
    people = household()
    assert named_people(people, NOBODY, None) == ()
    assert profile_fingerprint(NOBODY, None, named_people(people, NOBODY, None)) == profile_fingerprint(NOBODY, None)


def test_the_fingerprint_does_not_hold_the_whole_list():
    people = household()
    named = named_people(people, ANA, None)
    assert [e.name for e in named] == ["Ana Doe"]
    assert profile_fingerprint(ANA, None, named) != profile_fingerprint(ANA, None)


def test_editing_ana_makes_a_fact_naming_ana_stale_and_a_fact_naming_clara_fresh():
    before = household()
    ana_fact, clara_fact = profiled(ANA, before), profiled(CLARA, before)
    after = household(p1={"relation": "daughter"})

    assert status(ana_fact, before) is ProfileStatus.FRESH
    assert status(ana_fact, after) is ProfileStatus.STALE_INPUT
    assert status(clara_fact, after) is ProfileStatus.FRESH


def test_a_relation_word_in_the_fact_ties_it_to_the_entry():
    before = household()
    daughter_fact = profiled("The younger daughter goes to Example School.", before)
    after = household(p1={"forms": ["Ana D."]})
    assert status(daughter_fact, after) is ProfileStatus.STALE_INPUT


def test_adding_an_entry_the_fact_does_not_name_keeps_it_fresh_and_adding_one_it_names_makes_it_stale():
    before = household()
    clara_fact = profiled("Contacts: Clara Moreira, Tom Baker.", before)
    data = {"user": RAW["user"], "people": [*RAW["people"], {"name": "Hanna Doe", "forms": [], "relation": "sister"}]}
    assert status(clara_fact, parse_confirmed_people(data)) is ProfileStatus.FRESH

    data = {"user": RAW["user"], "people": [*RAW["people"], {"name": "Tom Baker", "forms": ["Tom"],
                                                              "relation": "colleague"}]}
    assert status(clara_fact, parse_confirmed_people(data)) is ProfileStatus.STALE_INPUT


def test_the_first_list_write_re_profiles_only_the_facts_naming_a_listed_person():
    nobody_fact = profiled(NOBODY, None)
    ana_fact = profiled(ANA, None)
    people = household()
    assert status(nobody_fact, people) is ProfileStatus.FRESH
    assert status(ana_fact, people) is ProfileStatus.STALE_INPUT


def test_a_listed_name_in_a_metadata_value_counts():
    people = household()
    assert [e.name for e in named_people(people, "Dentist check-up on 2026-10-12", {"patient": "Bo"})] == ["Alex Doe"]


def test_the_derivation_ends_with_the_recording_year_and_a_year_change_is_stale_derivation():
    assert DERIVE_VERSION == "3"
    assert profile_derivation("reg", "atlas", False, 2026).endswith("|y2026")
    assert profile_derivation("reg", "atlas", False) == f"{DERIVE_VERSION}|reg|atlas|0"
    stored = profiled(ANA, None)
    moved = stored.model_copy(update={"profile_derivation": profile_derivation("reg", "atlas", False, 2025)})
    assert classify_profile(moved, DERIVATION, ()) is ProfileStatus.STALE_DERIVATION
