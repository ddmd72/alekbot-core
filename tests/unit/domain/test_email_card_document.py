"""The stored shape of an email card (slice 3): a round trip over cards the core builds, JSON-native values only, a
missing optional list reads as empty. Invented mail only (email_card_fixtures)."""
import json
from datetime import date, datetime
from typing import Any

import pytest

from src.domain.email_card import CardAttachment, CardState
from src.domain.email_card_answer import parse_mail_answer
from src.domain.email_card_build import build_card
from src.domain.email_card_document import card_document, card_from_document
from src.domain.email_card_input import build_card_input
from tests.unit.domain.email_card_fixtures import BOOKING_BODY, BOOKING_PDF, NONCE, PEOPLE, answer_json, mail

LOCKED = CardAttachment(name="MP-2026-118204.pdf", kind="pdf", size=9_000, read="locked")


def _card(body=BOOKING_BODY, attachments=(BOOKING_PDF,), texts=("Booking RM-55120",), **overrides):
    m = mail()
    inp = build_card_input(m, body, list(attachments), list(texts), direct=True, nonce=NONCE)
    return build_card(parse_mail_answer(answer_json(**overrides)), m, list(attachments), inp.evidence, PEOPLE)


CARDS = {
    "booking with a span": _card(),
    "a person linked by name": _card(
        body="Jamie Doe is invited to the parents' meeting on 3 March 2026.", attachments=(), texts=(),
        people=[{"printed": "Jamie Doe", "entry_id": "P1"}], about="person",
        dates=[{"label": "event", "date": "2026-03-03", "date_end": "", "printed": "3 March 2026"}]),
    "a locked attachment": _card(attachments=(LOCKED,), texts=("",)),
    "an empty card": _card(substance="none"),
}


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)


@pytest.mark.parametrize("name", list(CARDS))
def test_a_card_survives_the_document_and_json(name):
    card = CARDS[name]
    doc = card_document(card)
    assert card_from_document(doc) == card
    assert card_from_document(json.loads(json.dumps(doc))) == card


def test_the_document_holds_json_native_values_only():
    for card in CARDS.values():
        for value in _walk(card_document(card)):
            assert not isinstance(value, (date, datetime, tuple))


def test_the_queried_fields_are_top_level_and_the_state_is_a_string():
    doc = card_document(CARDS["booking with a span"])
    assert doc["card_state"] == "carded" and CARDS["an empty card"].state is CardState.EMPTY
    assert card_document(CARDS["an empty card"])["card_state"] == "empty"
    assert "v:rm55120" in doc["about_keys"] and doc["template_key"] and doc["card_version"] == 1
    assert doc["card"]["mail"]["email_id"] == "msg_rome01" and doc["card"]["mail"]["sent_at"].endswith("+00:00")
    assert "body" not in json.dumps(doc["card"]["mail"])


def test_a_missing_optional_list_reads_as_empty():
    doc = json.loads(json.dumps(card_document(CARDS["booking with a span"])))
    for key in ("attachments", "people", "times"):
        del doc["card"][key]
    for key in ("dates", "entities", "key_values", "topics", "graded", "label_words"):
        del doc["card"]["profile"][key]
    for key in ("about_keys", "name_keys", "people_keys", "month_keys"):
        del doc[key]
    card = card_from_document(doc)
    assert card.attachments == () and card.people == () and card.times == ()
    assert card.profile.dates == () and card.profile.graded == () and card.about_keys == ()


def test_a_missing_required_field_is_a_value_error():
    doc = card_document(CARDS["booking with a span"])
    del doc["card"]["mail"]["email_id"]
    with pytest.raises(ValueError):
        card_from_document(doc)
    with pytest.raises(ValueError):
        card_from_document({"card_state": "carded"})
