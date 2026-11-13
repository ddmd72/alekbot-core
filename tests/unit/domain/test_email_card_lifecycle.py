"""The stored lifecycle fields of an email card (slice 6): the people digest, the fields per outcome, the field set a
card write may name. Invented cast (Alex Doe, Jamie and Ana Doe, Clara Moreira; the Example School notice)."""
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.confirmed_people import parse_confirmed_people
from src.domain.email_card import CardState
from src.domain.email_card_answer import parse_mail_answer
from src.domain.email_card_build import build_card
from src.domain.email_card_input import build_card_input
from src.domain.email_card_lifecycle import (
    CARD_FIELDS, CardExpect, block_digest, gone_fields, pending_fields, people_digest, settled_fields,
)
from src.domain.email_card_params import CARD_VERSION
from tests.unit.domain.email_card_fixtures import BOOKING_BODY, NONCE, PEOPLE, answer_json, mail

NOW = datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc)
EMPTY_DIGEST = hashlib.sha256(b"[]").hexdigest()


def _card(body, people_answer, about="person", people=PEOPLE, **over):
    m = mail()
    inp = build_card_input(m, body, [], [], direct=True, nonce=NONCE)
    answer = parse_mail_answer(answer_json(people=people_answer, about=about, **over))
    return build_card(answer, m, [], inp.evidence, people)


JAMIE = _card("Dear parents, the parents' meeting about Jamie Doe is on Tuesday 3 March 2026 at Example School.",
              [{"printed": "Jamie Doe", "entry_id": "P1"}])
CLARA_UNLINKED = _card("A birthday cookbook for Clara Moreira, ordered by Alex Doe.",
                       [{"printed": "Clara Moreira", "entry_id": ""}])
BOOKING = _card(BOOKING_BODY, [], about="owner")


def _edit(people_raw, name, forms):
    raw = json.loads(json.dumps(people_raw))
    for entry in [raw["user"], *raw["people"]]:
        if entry["name"] == name:
            entry["forms"] = forms
    return parse_confirmed_people(raw)


RAW = {
    "user": {"name": "Alex Doe", "forms": ["Alex"]},
    "people": [
        {"name": "Jamie Doe", "forms": ["Jamie"], "relation": "daughter"},
        {"name": "Ana Doe", "forms": ["Ana"], "relation": "younger daughter"},
        {"name": "Clara Moreira", "forms": ["Clara"], "relation": "partner"},
    ],
}


def test_a_card_linking_jamie_depends_on_jamies_entry_only():
    assert people_digest(JAMIE, PEOPLE) != EMPTY_DIGEST
    assert people_digest(JAMIE, _edit(RAW, "Ana Doe", ["Ana", "Ана"])) == people_digest(JAMIE, PEOPLE)
    assert people_digest(JAMIE, _edit(RAW, "Jamie Doe", ["Jamie", "Джеймі"])) != people_digest(JAMIE, PEOPLE)


def test_a_printed_name_counts_its_entry_even_when_unlinked():
    unlinked = replace(CLARA_UNLINKED, people=tuple(replace(p, entry_name="", linked_by="") for p in CLARA_UNLINKED.people))
    assert people_digest(unlinked, _edit(RAW, "Clara Moreira", ["Clara", "Клара"])) != people_digest(unlinked, PEOPLE)
    assert people_digest(unlinked, _edit(RAW, "Ana Doe", ["Ana", "Ана"])) == people_digest(unlinked, PEOPLE)


def test_no_list_or_no_such_entry_is_the_digest_of_an_empty_list():
    assert people_digest(JAMIE, None) == EMPTY_DIGEST
    assert people_digest(BOOKING, PEOPLE) == EMPTY_DIGEST


def test_the_order_of_the_entries_in_the_list_does_not_matter():
    shuffled = parse_confirmed_people({"user": RAW["user"], "people": list(reversed(RAW["people"]))})
    assert people_digest(JAMIE, shuffled) == people_digest(JAMIE, PEOPLE)


def test_the_order_of_an_entrys_forms_is_data():
    assert people_digest(JAMIE, _edit(RAW, "Jamie Doe", ["Jamie", "Джеймі"])) != \
        people_digest(JAMIE, _edit(RAW, "Jamie Doe", ["Джеймі", "Jamie"]))


def test_block_digest_is_the_sha256_of_the_utf8_bytes():
    assert block_digest("Albergo Immaginario") == hashlib.sha256("Albergo Immaginario".encode()).hexdigest()


def test_a_carded_card_is_stored_with_its_lifecycle_fields():
    fields = settled_fields(JAMIE, digest="d1", model="flash-lite", now=NOW, vector_pending=True)
    assert fields["card_state"] == "carded" and fields["card_version"] == CARD_VERSION
    assert fields["people_digest"] == "d1" and fields["card_model"] == "flash-lite" and fields["carded_at"] == NOW
    assert (fields["card_failures"], fields["card_reason"], fields["card_vector_pending"]) == (0, "", True)
    assert fields["about_keys"] and "card" in fields and "template_key" in fields


def test_an_empty_card_is_settled_too():
    empty = _card(BOOKING_BODY, [], about="owner", substance="none")
    assert empty.state is CardState.EMPTY
    fields = settled_fields(empty, digest="d", model="m", now=NOW, vector_pending=False)
    assert fields["card_state"] == "empty" and fields["card_vector_pending"] is False


def test_a_pending_card_is_not_settled():
    pending = replace(JAMIE, state=CardState.PENDING)
    with pytest.raises(ValueError):
        settled_fields(pending, digest="d", model="m", now=NOW, vector_pending=False)


@pytest.mark.parametrize("reason", ["invalid", "refused", "outage", "attachments", "budget", "source", "error"])
def test_pending_fields_carry_the_reason_and_the_charged_failures(reason):
    fields = pending_fields(reason=reason, failures=1, model="m")
    assert fields == {"card_state": "pending", "card_version": CARD_VERSION, "people_digest": "", "card_model": "m",
                      "card_failures": 1, "card_reason": reason, "card_vector_pending": False}


def test_gone_fields():
    assert gone_fields() == {"card_state": "source_gone", "card_version": CARD_VERSION, "people_digest": "",
                             "card_reason": "gone", "card_failures": 0, "card_model": "",
                             "card_vector_pending": False}


def test_no_field_outside_the_card_set_is_ever_produced_and_all_but_the_date_are_json():
    produced = [settled_fields(JAMIE, digest="d", model="m", now=NOW, vector_pending=False),
                pending_fields(reason="outage", failures=0, model=""), gone_fields()]
    for fields in produced:
        assert set(fields) <= set(CARD_FIELDS)
        json.dumps({k: v for k, v in fields.items() if k != "carded_at"})


def test_absent_is_both_none():
    assert CardExpect.absent() == CardExpect(None, None)
