"""Stable entry ids of the confirmed people list (email index v2 slice 0): assignment rules, parse rules, and the proof
that an id changes no fingerprint, people block or card digest. Invented universe only."""
import copy

import pytest

from src.domain.confirmed_people import (
    ConfirmedPeople, ConfirmedPerson, assign_entry_ids, confirmed_people_document, parse_confirmed_people,
    render_people_block,
)
from src.domain.email_card_answer import parse_mail_answer
from src.domain.email_card_build import build_card
from src.domain.email_card_input import build_card_input
from src.domain.email_card_lifecycle import people_digest
from src.domain.fact_profile_fingerprint import named_people, profile_fingerprint
from tests.unit.domain.email_card_fixtures import NONCE, answer_json, mail

RAW = {
    "user": {"name": "Alex Doe", "forms": ["Alex", "Bo"]},
    "people": [
        {"name": "Clara Moreira", "forms": ["Clara"], "relation": "partner"},
        {"name": "Jamie Doe", "forms": ["Jamie"], "relation": "daughter"},
        {"name": "Maria Doe", "forms": ["Mariia", "Mia"], "relation": "mother"},
    ],
}


def people(*entries, user=None):
    return parse_confirmed_people({"user": user or RAW["user"], "people": list(entries)})


def entry(name, forms=(), relation="relative"):
    return {"name": name, "forms": list(forms), "relation": relation}


def ids(result):
    return {p.name: p.entry_id for p in result.people}


def test_an_id_less_list_gets_ids_in_the_owners_order_and_a_counter():
    result, plan = assign_entry_ids(parse_confirmed_people(RAW), parse_confirmed_people(RAW))
    assert [p.entry_id for p in result.people] == [1, 2, 3]
    assert result.next_entry_id == 4
    assert [n for n, _ in plan.new] == ["Clara Moreira", "Jamie Doe", "Maria Doe"] and not plan.kept


def test_assignment_with_no_stored_list_starts_at_one():
    result, _ = assign_entry_ids(parse_confirmed_people(RAW), None)
    assert [p.entry_id for p in result.people] == [1, 2, 3] and result.next_entry_id == 4


def test_it_is_idempotent():
    first, _ = assign_entry_ids(parse_confirmed_people(RAW), None)
    second, plan = assign_entry_ids(first, first)
    assert ids(second) == ids(first) and second.next_entry_id == first.next_entry_id
    assert len(plan.kept) == 3 and not plan.new and not plan.retired and not plan.renamed


def test_a_reorder_keeps_the_ids():
    stored, _ = assign_entry_ids(parse_confirmed_people(RAW), None)
    reordered = people(entry("Maria Doe", ["Mariia", "Mia"], "mother"), entry("Clara Moreira", ["Clara"], "partner"),
                       entry("Jamie Doe", ["Jamie"], "daughter"))
    result, _ = assign_entry_ids(reordered, stored)
    assert ids(result) == {"Maria Doe": 3, "Clara Moreira": 1, "Jamie Doe": 2}


def test_a_rename_through_a_shared_form_keeps_the_id():
    stored, _ = assign_entry_ids(parse_confirmed_people(RAW), None)
    renamed = people(entry("Clara Moreira", ["Clara"], "partner"), entry("Jamie Doe", ["Jamie"], "daughter"),
                     entry("Maria A. Doe", ["Maria Doe", "Mariia", "Mia"], "mother"))
    result, plan = assign_entry_ids(renamed, stored)
    assert ids(result)["Maria A. Doe"] == 3
    assert plan.renamed == (("Maria Doe", "Maria A. Doe", 3),)


def test_a_removed_id_is_retired_and_never_reused():
    stored, _ = assign_entry_ids(parse_confirmed_people(RAW), None)
    without_maria = people(entry("Clara Moreira", ["Clara"], "partner"), entry("Jamie Doe", ["Jamie"], "daughter"))
    smaller, plan = assign_entry_ids(without_maria, stored)
    assert plan.retired == (("Maria Doe", 3),) and smaller.next_entry_id == 4
    later = people(entry("Clara Moreira", ["Clara"], "partner"), entry("Jamie Doe", ["Jamie"], "daughter"),
                   entry("Peter Doe", [], "father"))
    result, _ = assign_entry_ids(later, smaller)
    assert result.people[2].entry_id == 4 and result.next_entry_id == 5


def test_a_removed_person_added_again_gets_a_new_id():
    stored, _ = assign_entry_ids(people(entry("Hanna Doe", ["Hanna"], "sister"), entry("Peter Doe", [], "father")), None)
    without, _ = assign_entry_ids(people(entry("Peter Doe", [], "father")), stored)
    back, _ = assign_entry_ids(people(entry("Peter Doe", [], "father"), entry("Hanna Doe", ["Hanna"], "sister")), without)
    assert ids(back) == {"Peter Doe": 2, "Hanna Doe": 3}


def test_a_form_shared_by_two_stored_people_gives_a_new_id():
    stored, _ = assign_entry_ids(people(entry("Maria Doe", ["Doe"], "mother"), entry("Peter Doe", ["Doe"], "father")),
                                 None)
    result, plan = assign_entry_ids(people(entry("Maria D.", ["Doe"], "mother")), stored)
    assert result.people[0].entry_id == 3 and not plan.renamed


def test_two_new_people_sharing_a_stored_form_get_new_ids():
    stored, _ = assign_entry_ids(people(entry("Maria Doe", ["Mia"], "mother")), None)
    result, plan = assign_entry_ids(people(entry("Maria A. Doe", ["Mia"], "mother"),
                                           entry("Maria D.", ["Mia"], "mother")), stored)
    assert [p.entry_id for p in result.people] == [2, 3] and not plan.renamed and plan.retired == (("Maria Doe", 1),)


def test_incoming_ids_are_ignored_and_the_user_never_has_one():
    stored, _ = assign_entry_ids(parse_confirmed_people(RAW), None)
    forged = ConfirmedPeople(ConfirmedPerson("Alex Doe", ("Alex", "Bo"), "", True, entry_id=9),
                             tuple(ConfirmedPerson(p.name, p.forms, p.relation, entry_id=40 + i)
                                   for i, p in enumerate(stored.people)), next_entry_id=99)
    result, _ = assign_entry_ids(forged, stored)
    assert ids(result) == ids(stored) and result.user.entry_id is None and result.next_entry_id == 4


@pytest.mark.parametrize("bad", [True, 0, -1, "2", 1.5])
def test_parse_rejects_a_bad_id(bad):
    raw = copy.deepcopy(RAW)
    raw["people"][0]["entry_id"] = bad
    with pytest.raises(ValueError):
        parse_confirmed_people(raw)


def test_parse_rejects_a_duplicate_id_and_an_id_at_or_above_the_counter():
    raw = copy.deepcopy(RAW)
    raw["people"][0]["entry_id"] = raw["people"][1]["entry_id"] = 1
    raw["next_entry_id"] = 5
    with pytest.raises(ValueError):
        parse_confirmed_people(raw)
    raw = copy.deepcopy(RAW)
    raw["people"][0]["entry_id"] = 3
    raw["next_entry_id"] = 3
    with pytest.raises(ValueError):
        parse_confirmed_people(raw)


def test_the_document_round_trips_and_omits_unset_ids():
    assert "next_entry_id" not in confirmed_people_document(parse_confirmed_people(RAW))
    assert all("entry_id" not in p for p in confirmed_people_document(parse_confirmed_people(RAW))["people"])
    assigned, _ = assign_entry_ids(parse_confirmed_people(RAW), None)
    document = confirmed_people_document(assigned)
    assert document["next_entry_id"] == 4 and "entry_id" not in document["user"]
    back = parse_confirmed_people(document)
    assert ids(back) == ids(assigned) and back.next_entry_id == 4


def test_equality_ignores_ids():
    assigned, _ = assign_entry_ids(parse_confirmed_people(RAW), None)
    assert assigned == parse_confirmed_people(RAW)


def test_ids_change_no_block_fingerprint_or_card_digest():
    bare = parse_confirmed_people(RAW)
    assigned, _ = assign_entry_ids(bare, None)
    assert render_people_block(assigned.entries()) == render_people_block(bare.entries())
    text = "Jamie Doe has a parents' meeting; Mia called."
    assert profile_fingerprint(text, None, named_people(assigned, text, None)) == \
        profile_fingerprint(text, None, named_people(bare, text, None))

    def card(list_):
        m = mail()
        inp = build_card_input(m, "Dear parents, the meeting about Jamie Doe is on Tuesday 3 March 2026.", [], [],
                               direct=True, nonce=NONCE)
        answer = parse_mail_answer(answer_json(people=[{"printed": "Jamie Doe", "entry_id": "P1"}], about="person"))
        return build_card(answer, m, [], inp.evidence, list_)

    assert people_digest(card(assigned), assigned) == people_digest(card(bare), bare)
