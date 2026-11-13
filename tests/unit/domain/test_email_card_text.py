"""The card's embedding text and Smart's result line (email index v2 §2.5; card slice 1). Invented mail only."""
from datetime import datetime, timezone

from src.domain.drive_match import SourceClass
from src.domain.email_card import CardAttachment
from src.domain.email_card_answer import parse_mail_answer
from src.domain.email_card_build import build_card
from src.domain.email_card_input import build_card_input
from src.domain.email_card_params import RESULT_LINE_CHARS_MAX
from src.domain.email_card_text import READING_WORDS, card_embedding_text, render_result_line
from tests.unit.domain.email_card_fixtures import BOOKING_BODY, BOOKING_PDF, NONCE, PEOPLE, answer_json, mail


def _card(body=BOOKING_BODY, attachments=(BOOKING_PDF,), texts=("Booking RM-55120",), m=None, **overrides):
    m = m or mail()
    inp = build_card_input(m, body, list(attachments), list(texts), direct=True, nonce=NONCE)
    return build_card(parse_mail_answer(answer_json(**overrides)), m, list(attachments), inp.evidence, PEOPLE)


def test_the_embedding_text_joins_type_names_sender_subject_title_summary_printed_entities_and_topics():
    card = _card(entities=[{"name": "Rome", "type": "place", "printed": "Rome", "forms": []},
                           {"name": "Prague", "type": "place", "printed": "Prague", "forms": []}])
    parts = card_embedding_text(card).split(" · ")
    assert parts[0].startswith("booking, bookings, reservation")
    assert parts[1:] == ["Albergo Immaginario albergo.example", "Prenotazione confermata RM-55120",
                         "Prenotazione confermata", "Hotel booking in Rome for 10–14 Aug 2025, 560 EUR.",
                         "Rome", "hotel booking"]                     # Prague is not printed: left out


def test_the_embedding_text_uses_the_mail_names_of_a_mail_only_type_and_skips_empty_parts():
    card = _card(doc_type="appointment", title="", topics=[], entities=[], m=mail(from_name=""))
    parts = card_embedding_text(card).split(" · ")
    assert parts[0].startswith("appointment, appointments")
    assert parts[1] == "albergo.example"
    assert "" not in parts and len(parts) == 4


def test_the_embedding_text_of_an_other_card_has_no_type_names():
    assert not card_embedding_text(_card(doc_type="other")).startswith("·")


def test_the_result_line_reads_like_the_spec():
    line = render_result_line(_card(), ["v:rm55120", "e:rome", "v:rm55120"])
    assert line == (
        '• [msg_rome01] 2025-06-02 · booking · Albergo Immaginario (albergo.example) · '
        '"Prenotazione confermata RM-55120"\n'
        '  → Hotel booking in Rome for 10–14 Aug 2025, 560 EUR. | when: event 10–14 Aug 2025 | 📎 1 read\n'
        '  matched: booking reference "RM-55120" (printed); place "Rome" (printed)')


def test_matched_reading_words_name_translation_and_automatic_sources():
    card = _card()
    line = render_result_line(card, ["ef:roma", "tw:hotel", "l:type:booking", "zz:none"])
    assert 'place "Roma" (by translation)' in line
    assert 'topic word "hotel" (automatic)' in line
    assert 'label "booking" (automatic)' in line
    assert READING_WORDS[SourceClass.VERIFIED_RECORD.value] == "printed"


def test_without_matches_attachments_or_sender_name():
    card = _card(attachments=(), texts=(), m=mail(from_name="", from_domain=""))
    line = render_result_line(card)
    assert "matched:" not in line and "📎" not in line
    assert "· booking@albergo.example ·" in line


def test_some_attachments_unread_and_no_summary():
    atts = (BOOKING_PDF, CardAttachment("scan.pdf", "pdf", 90_000, "scan_not_read"))
    line = render_result_line(_card(attachments=atts, texts=("Booking RM-55120", ""), what_it_is="", title=""))
    assert "  → when: event 10–14 Aug 2025 | 📎 1 of 2 read" in line


def test_a_card_with_no_event_shows_the_sent_day():
    card = _card(dates=[])
    assert "when: sent 2 Jun 2025" in render_result_line(card)


def test_the_line_fits_the_cap_losing_matches_then_summary_then_subject():
    long_subject = "Prenotazione confermata RM-55120 " + "con colazione e parcheggio " * 8
    card = _card(what_it_is="Hotel booking in Rome for 10–14 Aug 2025, 560 EUR, " + "with breakfast " * 20,
                 m=mail(long_subject.strip(), sent_at=datetime(2025, 6, 2, tzinfo=timezone.utc)))
    line = render_result_line(card, ["v:rm55120", "e:rome", "v:560", "e:albergo_immaginario", "tw:hotel"])
    assert len(line) <= RESULT_LINE_CHARS_MAX
    assert 'booking reference "RM-55120" (printed)' in line
    assert "…" in line


def test_a_single_long_match_is_shortened_last():
    name = "Albergo Immaginario Via Finta Sette Roma Centro Storico " * 5
    body = BOOKING_BODY + name + "\n"
    card = _card(body=body, what_it_is="Hotel booking in Rome for 10–14 Aug 2025, 560 EUR, with breakfast daily.",
                 entities=[{"name": name.strip(), "type": "organization", "printed": name.strip(), "forms": []}],
                 m=mail("Prenotazione confermata RM-55120 con colazione e parcheggio inclusi nel prezzo"))
    key = next(k for k in card.about_keys if k.startswith("e:albergo"))
    line = render_result_line(card, [key])
    assert len(line) <= RESULT_LINE_CHARS_MAX
    assert line.endswith('…') and "matched: organization" in line
