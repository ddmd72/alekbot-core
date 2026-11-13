"""The graded, keyed card (email index v2 §2, §4, §6.3; card slice 1): the drive grader and keyer run unchanged on the
card, the envelope grades the dates, a mail-only type keeps its name keys, people and `about` come from code, and an
empty card is recognised. Invented mail only."""
from datetime import date, datetime, timezone

import pytest

from src.domain.drive_enrichment import Grade, GradedField, is_unconfirmed
from src.domain.drive_keys import phrase_keys
from src.domain.email_card import About, CardAttachment, CardPerson, CardState, Substance
from src.domain.email_card_answer import parse_mail_answer
from src.domain.email_card_build import build_card, card_state_of
from src.domain.email_card_input import build_card_input
from src.domain.email_card_keys import card_name_keys, template_key
from src.domain.email_card_params import CARD_VERSION
from tests.unit.domain.email_card_fixtures import BOOKING_BODY, BOOKING_PDF, NONCE, PEOPLE, answer_json, mail


def _card(body=BOOKING_BODY, attachments=(BOOKING_PDF,), texts=("Booking RM-55120",), m=None, roster=PEOPLE,
          **overrides):
    m = m or mail()
    inp = build_card_input(m, body, list(attachments), list(texts), direct=True, nonce=NONCE)
    return build_card(parse_mail_answer(answer_json(**overrides)), m, list(attachments), inp.evidence, roster)


def _grade(card, gfield, value):
    return [g.grade for g in card.profile.graded if g.field == gfield.value and g.value == value]


def test_the_rome_booking_card():
    card = _card()
    assert card.state is CardState.CARDED and card.card_version == CARD_VERSION
    assert card.about is About.OWNER and card.people == () and card.substance is Substance.SOME
    assert card.mail.email_id == "msg_rome01" and card.attachments == (BOOKING_PDF,)
    assert [(t.label, t.start, t.end, t.verified) for t in card.times] == [
        ("event", date(2025, 8, 10), date(2025, 8, 14), True)]
    keys = set(card.about_keys)
    assert {"l:type:booking", "l:domain:travel", "e:rome", "v:rm55120", "v:560", "v:2025-08-10", "v:2025-08-14",
            "m:2025-08", "m:2025-06", "y:2025"} <= keys
    assert "ef:roma" in keys                            # a spelling the mail does not print: never exact
    assert {"lw:booking", "dw:travel", "tw:hotel"} <= keys   # the drive tables' names and the model's words
    assert card.month_keys == ("m:2025-06", "m:2025-08")
    assert card.template_key == template_key("albergo.example", "Prenotazione confermata RM-55120")
    assert card.name_keys == card_name_keys("Prenotazione confermata RM-55120", "Albergo Immaginario")


def test_values_dates_and_entities_the_mail_does_not_print_are_kept_apart():
    card = _card(key_values=[{"label": "booking_reference", "printed": "RM-99999", "canonical": "RM99999"}],
                 entities=[{"name": "Prague", "type": "place", "printed": "Prague", "forms": []}],
                 dates=[{"label": "due", "date": "2025-07-01", "date_end": "", "printed": "1 July 2025"}])
    keys = set(card.about_keys)
    assert "va:rm99999" in keys and "v:rm99999" not in keys
    assert "ef:prague" in keys and "e:prague" not in keys
    assert "va:2025-07-01" in keys and "ma:2025-07" in keys
    assert card.month_keys == ("m:2025-06",)


def test_the_envelope_grades_the_dates_a_span_and_a_yearless_one_held():
    body = "Your stay 10–14 Aug is confirmed. Reply by 20 June."
    card = _card(body=body, attachments=(), texts=(), dates=[
        {"label": "event", "date": "2026-08-10", "date_end": "2026-08-14", "printed": "10–14 Aug"},
        {"label": "due", "date": "2025-06-20", "date_end": "", "printed": "20 June"}])
    assert [(t.start, t.end, t.verified) for t in card.times] == [
        (date(2025, 8, 10), date(2025, 8, 14), True), (date(2025, 6, 20), date(2025, 6, 20), True)]
    assert _grade(card, GradedField.DATE, "2025-08-10") == [Grade.IN_TEXT_AS.value]
    assert not is_unconfirmed(card.profile, GradedField.DATE, "2025-08-14")
    assert {"v:2025-08-10", "v:2025-08-14", "v:2025-06-20"} <= set(card.about_keys)
    assert card.profile.dates[0].iso == date(2025, 8, 10)            # code's year, not the model's 2026


def test_a_date_the_drive_grade_holds_keeps_its_own_grade():
    card = _card(body="Check-in 2025-08-10, check-out 2025-08-14.", attachments=(), texts=(), dates=[
        {"label": "event", "date": "2025-08-10", "date_end": "", "printed": "2025-08-10"}])
    assert _grade(card, GradedField.DATE, "2025-08-10") == [Grade.IN_TEXT.value]


def test_an_unverified_point_is_not_in_text_even_where_a_verified_one_shares_its_day():
    body = "Meeting Friday 3 March 2026; reminder for 3 March 2026."
    card = _card(body=body, attachments=(), texts=(), m=mail(sent_at=datetime(2026, 2, 20, tzinfo=timezone.utc)),
                 dates=[{"label": "event", "date": "2026-03-03", "date_end": "", "printed": "Friday 3 March 2026"},
                        {"label": "due", "date": "2026-03-03", "date_end": "", "printed": "3 March 2026"}])
    assert [t.verified for t in card.times] == [False, True]
    assert _grade(card, GradedField.DATE, "2026-03-03") != [Grade.NOT_IN_TEXT.value]
    card = _card(body="Meeting Friday 3 March 2026.", attachments=(), texts=(),
                 m=mail(sent_at=datetime(2026, 2, 20, tzinfo=timezone.utc)),
                 dates=[{"label": "event", "date": "2026-03-03", "date_end": "", "printed": "Friday 3 March 2026"}])
    assert _grade(card, GradedField.DATE, "2026-03-03") == [Grade.NOT_IN_TEXT.value]
    assert "va:2026-03-03" in card.about_keys


def test_the_issued_send_date_is_not_a_date_of_the_card():
    card = _card(dates=[{"label": "issued", "date": "2025-06-02", "date_end": "", "printed": "02/06/2025"}],
                 body=BOOKING_BODY + "Data: 02/06/2025\n")
    assert card.times == () and card.profile.dates == ()


@pytest.mark.parametrize("doc_type", ["order_update", "appointment", "account_notice", "message", "invitation"])
def test_a_mail_only_type_is_kept_and_keyed_with_its_mail_names(doc_type):
    card = _card(doc_type=doc_type)
    assert card.profile.doc_type == doc_type
    assert f"l:type:{doc_type}" in card.about_keys
    assert card.profile.label_words and any(k.startswith("lw:") for k in card.about_keys)


def test_a_drive_type_gets_its_names_from_the_drive_table_not_twice():
    card = _card(doc_type="invoice")
    assert card.profile.label_words == ()
    assert "lw:invoice" in card.about_keys


def test_people_and_about_come_from_code():
    body = "Dear parents, the parents' meeting about Jamie Doe is on Tuesday 3 March 2026 at Example School."
    card = _card(body=body, attachments=(), texts=(), about="owner", doc_type="appointment",
                 people=[{"printed": "Jamie Doe", "entry_id": "P1"}, {"printed": "Ana Doe", "entry_id": "P2"}],
                 m=mail("Parents' meeting", from_name="Example School", from_address="office@school.example",
                        from_domain="school.example", sent_at=datetime(2026, 2, 20, tzinfo=timezone.utc)))
    assert card.people == (CardPerson("Jamie Doe", "Jamie Doe", "name"),)          # Ana is not printed
    assert card.about is About.HOUSEHOLD
    assert card.people_keys == phrase_keys("Jamie Doe")


def test_no_people_list_keeps_printed_people_unlinked():
    body = "Dear parents, the parents' meeting about Jamie Doe is on 3 March 2026."
    m = mail()
    inp = build_card_input(m, body, [], [], direct=True, nonce=NONCE)
    answer = parse_mail_answer(answer_json(about="person", people=[{"printed": "Jamie Doe", "entry_id": "P1"}]))
    card = build_card(answer, m, [], inp.evidence, None)
    assert card.people == (CardPerson("Jamie Doe"),) and card.about is About.UNKNOWN


# -- empty ----------------------------------------------------------------------------------------------------------

def test_substance_none_is_empty():
    assert _card(substance="none").state is CardState.EMPTY


def test_a_newsletter_or_other_card_printing_nothing_is_empty():
    nothing = dict(entities=[], key_values=[], dates=[])
    assert _card(doc_type="newsletter", **nothing).state is CardState.EMPTY
    assert _card(doc_type="other", **nothing).state is CardState.EMPTY
    unprinted = dict(entities=[{"name": "Prague", "type": "place", "printed": "Prague", "forms": []}],
                     key_values=[], dates=[])
    assert _card(doc_type="newsletter", **unprinted).state is CardState.EMPTY


def test_a_newsletter_printing_something_or_another_type_printing_nothing_is_carded():
    assert _card(doc_type="newsletter", key_values=[], dates=[]).state is CardState.CARDED     # prints Rome
    assert _card(doc_type="booking", entities=[], key_values=[], dates=[]).state is CardState.CARDED


def test_card_state_of_reads_the_profile_grades():
    card = _card(doc_type="newsletter", entities=[], key_values=[])           # the printed span holds
    assert card_state_of(Substance.SOME, card.profile) is CardState.CARDED
    assert card_state_of(Substance.NONE, card.profile) is CardState.EMPTY


def test_an_attachment_listed_unread_is_on_the_card_without_text():
    scan = CardAttachment("lab.pdf", "pdf", 90_000, "scan_not_read")
    card = _card(attachments=(BOOKING_PDF, scan), texts=("Booking RM-55120", ""))
    assert card.attachments == (BOOKING_PDF, scan)
    assert not hasattr(card.attachments[0], "text")


def test_a_verified_point_first_keeps_its_grade_over_a_later_unverified_one():
    card = _card(body="Meeting 3 March 2026; also Friday 3 March 2026.", attachments=(), texts=(),
                 m=mail(sent_at=datetime(2026, 2, 20, tzinfo=timezone.utc)),
                 dates=[{"label": "event", "date": "2026-03-03", "date_end": "", "printed": "3 March 2026"},
                        {"label": "due", "date": "2026-03-03", "date_end": "", "printed": "Friday 3 March 2026"}])
    assert [t.verified for t in card.times] == [True, False]
    assert _grade(card, GradedField.DATE, "2026-03-03") != [Grade.NOT_IN_TEXT.value]
