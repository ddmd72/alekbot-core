"""Whom a mail is about (email index v2 §2.3, §3.1; card slice 1): per-render handles, the printed-person check, the
derived `about`, and the people keys (printed name forms only; no `p:` keys in this slice). The invented household:
Alex Doe (owner), Jamie and Ana Doe (daughters), Clara Moreira (partner)."""
import pytest

from src.domain.drive_grade import TextEvidence
from src.domain.drive_keys import phrase_keys
from src.domain.email_card import About, AnswerPerson, CardPerson
from src.domain.email_card_people import (
    LINKED_BY_NAME, LINKED_BY_RELATION, derive_about, people_keys, person_handles, resolve_people,
)
from tests.unit.domain.email_card_fixtures import PEOPLE

SCHOOL = "Dear parents, the parents' meeting about Jamie Doe is on Tuesday 3 March 2026 at Example School."
KINDERGARTEN = "Example School (kindergarten): enrolment of Ana Doe for 2026."


def _resolve(people, about, text, roster=PEOPLE):
    return resolve_people([AnswerPerson(*p) for p in people], about, roster, TextEvidence([text]))


def test_handles_number_the_listed_people_from_one_without_the_owner():
    handles = person_handles(PEOPLE)
    assert {h: e.name for h, e in handles.items()} == {"P1": "Jamie Doe", "P2": "Ana Doe", "P3": "Clara Moreira"}
    assert person_handles(None) == {}


def test_a_printed_name_with_its_handle_is_linked_by_name():
    persons, about = _resolve([("Jamie Doe", "P1")], About.PERSON, SCHOOL)
    assert persons == (CardPerson("Jamie Doe", "Jamie Doe", LINKED_BY_NAME),)
    assert about is About.PERSON


def test_a_first_name_form_links_too():
    persons, _ = _resolve([("Ana", "P2")], About.PERSON, "Ana is enrolled at Example School for 2026.")
    assert persons == (CardPerson("Ana", "Ana Doe", LINKED_BY_NAME),)


def test_a_name_not_printed_in_the_mail_is_dropped():
    persons, about = _resolve([("Ana Doe", "P2")], About.PERSON, SCHOOL)
    assert persons == () and about is About.UNKNOWN


def test_the_printed_form_may_be_written_in_another_word_order():
    persons, _ = _resolve([("Jamie Doe", "P1")], About.PERSON, "Pupil: Doe, Jamie. Meeting on 3 March.")
    assert persons == (CardPerson("Jamie Doe", "Jamie Doe", LINKED_BY_NAME),)


def test_a_handle_that_does_not_fit_what_is_printed_keeps_only_the_printed_name():
    persons, about = _resolve([("Jamie Doe", "P2")], About.PERSON, SCHOOL)
    assert persons == (CardPerson("Jamie Doe"),)
    assert about is About.UNKNOWN                          # a printed stranger alone is unknown


def test_a_handle_off_the_list_links_nothing():
    persons, _ = _resolve([("Jamie Doe", "P9")], About.PERSON, SCHOOL)
    assert persons == (CardPerson("Jamie Doe"),)
    persons, _ = _resolve([("Jamie Doe", "P1")], About.PERSON, SCHOOL, roster=None)
    assert persons == (CardPerson("Jamie Doe"),)


def test_relation_words_that_only_one_entry_has_link_it():
    text = "Your partner is invited to the weekend in Coimbra, 6-8 March."
    persons, about = _resolve([("your partner", "P3")], About.PERSON, text)
    assert persons == (CardPerson("your partner", "Clara Moreira", LINKED_BY_RELATION),)
    assert about is About.PERSON


def test_relation_words_that_two_entries_share_do_not_link():
    text = "Your daughter has a parents' meeting on 3 March."
    persons, about = _resolve([("your daughter", "P1")], About.PERSON, text)
    assert persons == (CardPerson("your daughter"),)
    assert about is About.UNKNOWN


def test_the_owner_named_as_a_person_is_not_kept():
    persons, about = _resolve([("Alex Doe", "")], About.OWNER, "Clínica Ribeira: Alex Doe, a sua limpeza dentária é a 20/10/2026.")
    assert persons == () and about is About.OWNER


def test_a_shared_surname_does_not_make_a_listed_person_the_owner():
    persons, _ = _resolve([("Jamie Doe", "P1")], About.PERSON, SCHOOL,
                          roster=PEOPLE.__class__(user=PEOPLE.user.__class__("Alex Doe", ("Doe",), "", True),
                                                  people=PEOPLE.people))
    assert persons == (CardPerson("Jamie Doe", "Jamie Doe", LINKED_BY_NAME),)


def test_each_entry_and_each_printed_form_is_kept_once_and_empty_forms_are_skipped():
    text = "Jamie Doe and Jamie: the parents' meeting about Jamie Doe."
    persons, _ = _resolve([("Jamie Doe", "P1"), ("Jamie", "P1"), ("jamie doe", ""), ("  ", "P2")], About.PERSON, text)
    assert persons == (CardPerson("Jamie Doe", "Jamie Doe", LINKED_BY_NAME),)


def test_a_cross_script_spelling_of_a_form_names_the_entry():
    persons, _ = _resolve([("Клара Морейра", "P3")], About.PERSON, "Клара Морейра: вихідні в Коїмбрі, 6–8 березня.")
    assert persons == (CardPerson("Клара Морейра", "Clara Moreira", LINKED_BY_NAME),)


# -- about ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("model, linked, expected", [
    (About.PERSON, 1, About.PERSON),
    (About.UNKNOWN, 1, About.PERSON),
    (About.OWNER, 1, About.HOUSEHOLD),        # the owner and a listed person as one matter
    (About.HOUSEHOLD, 1, About.HOUSEHOLD),
    (About.PERSON, 2, About.HOUSEHOLD),       # a school mail naming both daughters
    (About.OWNER, 0, About.OWNER),
    (About.HOUSEHOLD, 0, About.OWNER),        # a home matter with nobody named, addressed to the owner
    (About.PERSON, 0, About.UNKNOWN),         # `person` with no surviving person
    (About.UNKNOWN, 0, About.UNKNOWN),
])
def test_about_is_derived_from_the_linked_count_and_the_models_owner_answer(model, linked, expected):
    names = ["Jamie Doe", "Ana Doe"][:linked]
    persons = [CardPerson(n, n, LINKED_BY_NAME) for n in names] + [CardPerson("Dr. Marta Quillen")]
    assert derive_about(model, persons) is expected


def test_two_daughters_named_make_a_household_mail():
    text = "Example School: Jamie Doe and Ana Doe are enrolled for 2026."
    persons, about = _resolve([("Jamie Doe", "P1"), ("Ana Doe", "P2")], About.PERSON, text)
    assert [p.entry_name for p in persons] == ["Jamie Doe", "Ana Doe"]
    assert about is About.HOUSEHOLD


def test_a_kindergarten_notice_about_a_daughter_is_person():
    persons, about = _resolve([("Ana Doe", "P2")], About.PERSON, KINDERGARTEN)
    assert about is About.PERSON and persons[0].entry_name == "Ana Doe"


# -- people keys ----------------------------------------------------------------------------------------------------

def test_people_keys_are_the_printed_name_forms_and_never_relation_words():
    persons = [CardPerson("Jamie Doe", "Jamie Doe", LINKED_BY_NAME), CardPerson("your partner", "Clara Moreira",
                                                                                 LINKED_BY_RELATION),
               CardPerson("Dr. Marta Quillen")]
    keys = people_keys(persons)
    assert keys == tuple(dict.fromkeys([*phrase_keys("Jamie Doe"), *phrase_keys("Dr. Marta Quillen")]))
    assert keys and all(k.startswith(("e:", "ex:", "exl:")) for k in keys)
    assert not any("partner" in k for k in keys)
    assert not any(k.startswith("p:") for k in keys)        # stable entry ids are a later slice
