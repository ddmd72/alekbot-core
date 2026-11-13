"""The lean card answer parse (email index v2 card slice 1): json.loads, closed sets, mail caps, ValueError for a
malformed answer (the agent's parse retry). Invented answers of the Rome booking mail."""
import json
from datetime import date

import pytest

from src.domain.email_card import About, AnswerDate, AnswerPerson, Substance
from src.domain.email_card_answer import REQUIRED_FIELDS, parse_mail_answer
from src.domain.email_card_labels import MAIL_DOC_TYPES
from src.domain.email_card_params import (
    MAIL_DATES_MAX, MAIL_ENTITIES_MAX, MAIL_ENTITY_FORMS_MAX, MAIL_KEY_VALUES_MAX, MAIL_PEOPLE_MAX, MAIL_TITLE_CHARS,
    MAIL_TOPICS_MAX, MAIL_WHAT_IT_IS_CHARS, MAIL_WORD_LIST_MAX,
)
from tests.unit.domain.email_card_fixtures import answer, answer_json


def test_a_well_formed_answer_parses_field_by_field():
    got = parse_mail_answer(answer_json(people=[{"printed": "Jamie Doe", "entry_id": "p1"}]))
    assert got.language == "it" and got.doc_type == "booking" and got.life_domains == ("travel",)
    assert got.title == "Prenotazione confermata"
    assert got.about is About.OWNER and got.substance is Substance.SOME
    assert got.people == (AnswerPerson(printed="Jamie Doe", handle="P1"),)
    assert got.dates == (AnswerDate(label="event", start=date(2025, 8, 10), end=date(2025, 8, 14),
                                    printed="10–14 Aug 2025"),)
    assert [(e.name, e.type, e.printed, e.forms) for e in got.entities] == [
        ("Rome", "place", "Rome", ("Roma",)), ("Albergo Immaginario", "organization", "Albergo Immaginario", ())]
    assert [(v.label, v.printed, v.canonical) for v in got.key_values] == [
        ("booking_reference", "RM-55120", "RM55120"), ("amount_total", "560 EUR", "560.00")]
    assert got.topics == ("hotel booking",) and got.topics_native == ("prenotazione",)
    assert got.topic_words == ("hotel", "trip", "albergo")


@pytest.mark.parametrize("raw", ["not json", "```json\n{}\n```", "[1, 2]", "\"text\""])
def test_a_reply_that_is_not_one_json_object_is_a_value_error(raw):
    with pytest.raises(ValueError):
        parse_mail_answer(raw)


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_every_required_field_missing_is_a_value_error_naming_it(field):
    data = answer()
    del data[field]
    with pytest.raises(ValueError, match=field):
        parse_mail_answer(json.dumps(data))


@pytest.mark.parametrize("overrides, field", [
    ({"title": 7}, "title"),
    ({"topics": "hotel"}, "topics"),
    ({"topic_words": ["hotel", 3]}, "topic_words"),
    ({"dates": {"label": "event"}}, "dates"),
    ({"people": ["Jamie Doe"]}, "people"),
    ({"entities": [{"name": "Rome", "type": "place", "printed": "Rome", "forms": "Roma"}]}, "forms"),
])
def test_a_field_of_the_wrong_type_is_a_value_error(overrides, field):
    with pytest.raises(ValueError, match=field):
        parse_mail_answer(answer_json(**overrides))


def test_values_outside_the_closed_sets_become_other_or_are_dropped():
    got = parse_mail_answer(answer_json(
        doc_type="hotel_voucher", life_domains=["holidays", "travel", "travel"], about="family",
        substance="maybe",
        dates=[{"label": "check_in", "date": "2025-08-10", "date_end": "", "printed": "10 Aug 2025"}],
        entities=[{"name": "Rome", "type": "city", "printed": "Rome", "forms": []}],
        key_values=[{"label": "voucher", "printed": "RM-55120", "canonical": "RM55120"}]))
    assert got.doc_type == "other"
    assert got.life_domains == ("travel",)
    assert got.about is About.UNKNOWN
    assert got.substance is Substance.SOME            # lean to keep: an unknown answer never hides a mail
    assert got.dates[0].label == "other"
    assert got.entities[0].type == "other"
    assert got.key_values[0].label == "other_id"


@pytest.mark.parametrize("doc_type", MAIL_DOC_TYPES)
def test_the_mail_only_doc_types_are_kept(doc_type):
    assert parse_mail_answer(answer_json(doc_type=doc_type)).doc_type == doc_type


def test_substance_none_and_every_about_value_are_read():
    assert parse_mail_answer(answer_json(substance="none")).substance is Substance.NONE
    for about in About:
        assert parse_mail_answer(answer_json(about=about.value)).about is about


def test_a_date_that_is_not_iso_is_left_out_and_a_bad_end_leaves_one_day():
    got = parse_mail_answer(answer_json(dates=[
        {"label": "event", "date": "10/08/2025", "date_end": "", "printed": "10/08/2025"},
        {"label": "due", "date": "2025-08-01", "date_end": "soon", "printed": "1 Aug 2025"},
        {"label": "event", "date": "2025-08-14", "date_end": "2025-08-10", "printed": "14 Aug 2025"},
        {"label": "event", "date": "2025-08-10", "date_end": "2025-08-10", "printed": "10 Aug 2025"},
        {"label": "event", "date": "2025-02-30", "date_end": "", "printed": "30 Feb"},
    ]))
    assert [(d.label, d.start, d.end) for d in got.dates] == [
        ("due", date(2025, 8, 1), None), ("event", date(2025, 8, 14), None), ("event", date(2025, 8, 10), None)]


def test_lists_are_cut_at_the_mail_caps():
    many = [f"word{i}" for i in range(30)]
    got = parse_mail_answer(answer_json(
        people=[{"printed": f"Person {i}", "entry_id": ""} for i in range(10)],
        dates=[{"label": "event", "date": f"2025-08-{i + 1:02d}", "date_end": "", "printed": f"{i + 1} Aug"}
               for i in range(9)],
        entities=[{"name": f"Place {i}", "type": "place", "printed": f"Place {i}", "forms": ["a", "b", "c", "d"]}
                  for i in range(15)],
        key_values=[{"label": "reference", "printed": f"RM-{i}", "canonical": f"RM{i}"} for i in range(15)],
        topics=many, topics_native=many, topic_words=many,
        life_domains=["travel", "finance", "home", "leisure"],
        title="T" * 400, what_it_is="W" * 900))
    assert len(got.people) == MAIL_PEOPLE_MAX
    assert len(got.dates) == MAIL_DATES_MAX
    assert len(got.entities) == MAIL_ENTITIES_MAX
    assert all(len(e.forms) == MAIL_ENTITY_FORMS_MAX for e in got.entities)
    assert len(got.key_values) == MAIL_KEY_VALUES_MAX
    assert len(got.topics) == len(got.topics_native) == MAIL_TOPICS_MAX
    assert len(got.topic_words) == MAIL_WORD_LIST_MAX
    assert got.life_domains == ("travel", "finance", "home")
    assert len(got.title) == MAIL_TITLE_CHARS and len(got.what_it_is) == MAIL_WHAT_IT_IS_CHARS


def test_text_is_whitespace_collapsed_lists_deduplicated_and_null_is_empty():
    got = parse_mail_answer(answer_json(title="  Prenotazione\n confermata ", topic_words=["hotel", " hotel", "", "trip"],
                                        topics=None, what_it_is=None))
    assert got.title == "Prenotazione confermata"
    assert got.topic_words == ("hotel", "trip")
    assert got.topics == () and got.what_it_is == ""


def test_null_lists_of_objects_are_empty():
    got = parse_mail_answer(answer_json(people=None, dates=None, entities=None, key_values=None))
    assert got.people == () and got.dates == () and got.entities == () and got.key_values == ()


def test_the_language_is_cut_and_a_missing_handle_is_empty():
    got = parse_mail_answer(answer_json(language="it" * 20, people=[{"printed": "Jamie Doe", "entry_id": None}]))
    assert len(got.language) == 16
    assert got.people == (AnswerPerson(printed="Jamie Doe", handle=""),)
