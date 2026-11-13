"""Email search query (email index v2 §5.1-§5.3; search slice 4): the slot validation, the concepts with the mail
words removed and split into topic and time, and the people, label and exact keys. Invented cast only."""
from datetime import date

from src.domain.drive_keys import phrase_keys
from src.domain.email_search_query import (
    MailQuerySlots, TimeSlot, exact_query_keys, label_query_keys, mail_concepts, parse_query_slots,
    people_query_keys,
)
from tests.unit.domain.email_card_fixtures import PEOPLE

TEXT = "invoice FA-0137 from the hardware shop"


def _words(concepts):
    return [c.word for c in concepts]


# -- parse_query_slots ----------------------------------------------------------------------------------------------

def test_a_full_slot_object_is_read_as_it_is():
    raw = {"subject": " lock  replacement ", "doc_types": ["invoice", "booking"], "domains": ["home"],
           "people": ["Jamie"], "time": {"intent": "period", "from": "2026-09-01", "to": "2026-09-30"},
           "exact_keys": ["FA-0137"], "scope": "all"}
    slots = parse_query_slots(raw, TEXT)
    assert slots == MailQuerySlots(subject="lock replacement", doc_types=("invoice", "booking"), domains=("home",),
                                   people=("Jamie",), time=TimeSlot("period", date(2026, 9, 1), date(2026, 9, 30)),
                                   exact_keys=("FA-0137",), scope="all")


def test_a_value_of_the_wrong_type_empties_that_slot_only():
    raw = {"subject": 5, "doc_types": "invoice", "domains": ["home"], "people": {"a": 1}, "time": "september",
           "exact_keys": 7, "scope": ["all"]}
    slots = parse_query_slots(raw, TEXT)
    assert (slots.subject, slots.doc_types, slots.people, slots.time, slots.exact_keys) == ("", (), (), None, ())
    assert slots.domains == ("home",) and slots.scope == "top"


def test_anything_but_an_object_gives_empty_slots():
    for raw in (None, "x", 3, ["a"]):
        slots = parse_query_slots(raw, TEXT)  # type: ignore[arg-type]
        assert slots.doc_types == () and slots.time is None and slots.scope == "top"


def test_doc_types_and_domains_keep_the_closed_sets_without_other():
    slots = parse_query_slots({"doc_types": ["invitation", "receipt", "other", "spaceship", 4, "receipt"],
                               "domains": ["travel", "other", "weather"]}, TEXT)
    assert slots.doc_types == ("invitation", "receipt") and slots.domains == ("travel",)


def test_people_are_non_empty_strings_at_most_six():
    slots = parse_query_slots({"people": ["Jamie", "", "  ", 3, "Ana", "Jamie", "a", "b", "c", "d", "e"]}, TEXT)
    assert slots.people == ("Jamie", "Ana", "a", "b", "c", "d")


def test_time_needs_a_known_intent_iso_days_and_an_ordered_span():
    ok = parse_query_slots({"time": {"intent": "upcoming", "from": None, "to": "2027-03-31"}}, TEXT).time
    assert ok == TimeSlot("upcoming", None, date(2027, 3, 31))
    assert parse_query_slots({"time": {"intent": "sometime"}}, TEXT).time is None
    assert parse_query_slots({"time": {"intent": "period", "from": "2026-09-30", "to": "2026-09-01"}}, TEXT).time is None
    garbled = parse_query_slots({"time": {"intent": "past", "from": "20260901", "to": "2026-13-40"}}, TEXT).time
    assert garbled == TimeSlot("past", None, None)


def test_an_exact_key_stays_only_when_the_request_text_holds_it():
    slots = parse_query_slots({"exact_keys": ["fa-0137", "  FA-0137 ", "MP-2026-118204", "RM-55120", ""]},
                              "the invoice  FA-0137 please")
    assert slots.exact_keys == ("fa-0137", "FA-0137")


def test_at_most_three_exact_keys_and_a_subject_cut_to_200_characters():
    text = "a1 b2 c3 d4"
    slots = parse_query_slots({"exact_keys": ["a1", "b2", "c3", "d4"], "subject": "x" * 500}, text)
    assert slots.exact_keys == ("a1", "b2", "c3") and len(slots.subject) == 200


# -- mail_concepts --------------------------------------------------------------------------------------------------

def test_the_school_request_has_topic_words_a_month_and_the_arrival_verb_ignored():
    found = mail_concepts("what did Example School send in March")
    assert _words(found.topic) == ["example", "school"] and _words(found.time) == ["march"]
    assert "send" in found.ignored


def test_a_ukrainian_period_request_has_one_time_concept_and_no_topic():
    found = mail_concepts("листи за вересень")
    assert found.topic == () and _words(found.time) == ["вересень"]
    assert "листи" in found.ignored


def test_a_month_with_a_year_is_one_time_concept():
    found = mail_concepts("mail from September 2026")
    assert found.topic == () and _words(found.time) == ["september 2026"]
    assert found.time[0].about_keys[0] == "m:2026-09"


def test_an_invoice_id_is_a_topic_number_concept():
    found = mail_concepts("invoice FA-0137")
    assert _words(found.topic) == ["fa-0137", "invoice"] and found.time == ()
    assert found.topic[0].is_number


def test_a_date_and_a_year_are_time_concepts():
    assert _words(mail_concepts("14.03.2025").time) == ["14.03.2025"]
    assert _words(mail_concepts("receipts 2025").time) == ["2025"]


def test_a_four_digit_id_outside_the_years_is_not_a_time():
    found = mail_concepts("receipt No. 0931")
    assert [c.word for c in found.time] == [] and "0931" in _words(found.topic)


def test_mail_words_of_every_product_language_are_removed():
    for text, word in (("emails about invoices", "emails"), ("correos enviados", "correos"),
                       ("courriels reçus", "courriels"), ("пошта прийшла", "пошта"), ("письма пришли", "письма")):
        found = mail_concepts(text)
        assert word in found.ignored and word not in _words(found.topic)
    assert _words(mail_concepts("mail inbox received").topic) == []
    assert "message" in _words(mail_concepts("message from the school").topic)


# -- people, label and exact keys -----------------------------------------------------------------------------------

def test_a_relation_value_resolves_to_every_entry_with_those_words():
    keys = people_query_keys(["my daughter"], PEOPLE)
    assert set(phrase_keys("Jamie")) <= set(keys) and set(phrase_keys("Ana Doe")) <= set(keys)
    assert not set(phrase_keys("Clara Moreira")) & set(keys)


def test_a_name_value_resolves_to_that_entry_alone():
    keys = people_query_keys(["Jamie"], PEOPLE)
    assert set(phrase_keys("Jamie Doe")) <= set(keys) and not set(phrase_keys("Ana")) & set(keys)


def test_a_name_no_entry_fits_gives_its_own_keys():
    assert people_query_keys(["Dr. Marta Quillen"], PEOPLE) == phrase_keys("Dr. Marta Quillen")
    assert people_query_keys(["Jamie"], None) == phrase_keys("Jamie")


def test_the_owner_is_never_resolved():
    assert people_query_keys(["Alex"], PEOPLE) == ()
    assert people_query_keys(["me"], PEOPLE) == ()


def test_each_people_key_comes_once():
    keys = people_query_keys(["Jamie", "my daughter"], PEOPLE)
    assert len(keys) == len(set(keys))


def test_label_keys_are_the_type_and_area_labels():
    slots = parse_query_slots({"doc_types": ["booking"], "domains": ["travel", "home"]}, "x")
    assert label_query_keys(slots) == ("l:type:booking", "l:domain:travel", "l:domain:home")
    assert label_query_keys(None) == ()


def test_exact_keys_are_the_value_keys_of_the_ids_and_amounts():
    slots = parse_query_slots({"exact_keys": ["FA-0137", "249,00"]}, "FA-0137 249,00")
    keys = exact_query_keys(slots)
    assert "v:fa0137" in keys and all(k.startswith("v:") for k in keys)
    assert exact_query_keys(None) == ()
