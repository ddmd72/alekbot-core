"""The card's keys (email index v2 §2.2, §5.4; card slice 1): the card as the drive keyer's record, name keys, the
template key from code fields only and the id/amount folding stop. Invented mail only."""
from datetime import date

import pytest

from src.domain.drive_enrichment import DriveProfile, ProfileDate
from src.domain.drive_key_sources import KeyedRecord, RecordKeyInputs
from src.domain.drive_keys import about_keys_of, name_keys_v2
from src.domain.email_card_answer import parse_mail_answer
from src.domain.email_card_build import build_card
from src.domain.email_card_input import build_card_input
from src.domain.email_card_keys import (
    CARD_KIND, card_name_keys, card_record, fold_stop_keys, keyed_record, stops_folding, template_key,
)
from tests.unit.domain.email_card_fixtures import BOOKING_BODY, BOOKING_PDF, NONCE, PEOPLE, answer_json, mail


def _card(**overrides):
    m = mail()
    inp = build_card_input(m, BOOKING_BODY, [BOOKING_PDF], ["Booking RM-55120"], direct=True, nonce=NONCE)
    return build_card(parse_mail_answer(answer_json(**overrides)), m, [BOOKING_PDF], inp.evidence, PEOPLE)


def test_the_keyed_record_is_an_email_record_of_the_profile_with_the_sent_day():
    profile = DriveProfile("t", "w", "it", "insurance_policy",
                           dates=(ProfileDate("valid_until", date(2027, 3, 31), "31/03/2027"),))
    rec = keyed_record(profile, date(2026, 4, 1))
    assert isinstance(rec, RecordKeyInputs)
    assert (rec.kind, rec.profile, rec.effective_date, rec.valid_until) == (
        CARD_KIND, profile, date(2026, 4, 1), date(2027, 3, 31))
    assert (rec.place_forms, rec.photo_description, rec.photo_objects, rec.visible_text, rec.capture) == (
        (), "", (), "", "")
    keyed: KeyedRecord = rec
    keys = about_keys_of(keyed)
    assert "l:valid" in keys and "y:2026" in keys and "m:2026-04" in keys


def test_card_record_rebuilds_the_record_of_a_built_card():
    card = _card()
    rec = card_record(card)
    assert rec.profile == card.profile and rec.effective_date == date(2025, 6, 2)
    assert about_keys_of(rec) == card.about_keys


def test_name_keys_are_those_of_the_subject_and_the_sender_name():
    keys = card_name_keys("Prenotazione confermata RM-55120", "Albergo Immaginario")
    assert set(keys) == set(name_keys_v2("Prenotazione confermata RM-55120")) | set(name_keys_v2("Albergo Immaginario"))
    assert list(keys) == sorted(keys)
    assert card_name_keys("Invoice FA-0137", " ") == tuple(sorted(name_keys_v2("Invoice FA-0137")))


@pytest.mark.parametrize("subject_a, subject_b", [
    ("Your statement for June 2025", "Your statement for July 2025"),
    ("Receipt No. 0931", "Receipt No. 0412"),
    ("Re: Policy MP-2026-118204 renewal", "Fwd: RE: Policy MP-2026-118204 renewal"),
    ("Grade notice 14/03/2025", "Grade notice 01.04.2026"),
    ("Factura de septiembre", "Factura de octubre"),
])
def test_template_mail_of_one_sender_shares_its_key(subject_a, subject_b):
    assert template_key("bank.example", subject_a) == template_key("Bank.example ", subject_b)


def test_the_template_key_masks_numbers_dates_and_months_and_keeps_the_domain():
    assert template_key("albergo.example", "Prenotazione confermata RM-55120") == \
        "albergo.example|prenotazione confermata rm-#"
    assert template_key("bank.example", "Your statement for June 2025") == "bank.example|your statement for #"
    assert template_key("bank.example", "Hello") != template_key("school.example", "Hello")
    assert template_key("bank.example", "Balance alert") != template_key("bank.example", "Card blocked")


def test_only_printed_ids_and_amounts_stop_folding():
    card = _card()
    stops = fold_stop_keys(card)
    assert "v:rm55120" in stops and "v:560" in stops
    assert not any(k.startswith("v:2025") for k in stops)             # a date never stops folding
    assert stops_folding(card, ["v:rm55120"])
    assert not stops_folding(card, ["e:rome", "tw:hotel", "w:albergo", "v:2025-08-10"])


def test_an_unprinted_value_never_stops_folding():
    card = _card(key_values=[{"label": "booking_reference", "printed": "RM-99999", "canonical": "RM99999"}])
    assert fold_stop_keys(card) == frozenset()
    assert not stops_folding(card, ["va:rm99999", "v:rm99999"])
