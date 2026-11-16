"""A scan the paid reader read feeds the card exactly as text-layer text does (email index v2, slice 5): the text
reaches the card input, and a value printed only in the scan grades as printed. Invented mail only."""
from src.domain.email_attachment import AttachmentRead, EmailAttachment
from src.domain.email_card_answer import parse_mail_answer
from src.domain.email_card_build import build_card
from src.domain.email_card_input import build_card_input
from src.domain.email_card_source import card_attachments_of
from tests.unit.domain.email_card_fixtures import NONCE, PEOPLE, answer_json, mail

SCAN_TEXT = "Laboratorio Clinico Exemplo. Report LAB-2025-03-0417. All values within range."
BODY = "Please find the report attached."


def lab(read: AttachmentRead, text=None) -> EmailAttachment:
    return EmailAttachment(name="LAB-2025-03-0417.pdf", kind="pdf", size=40_000, read=read, text=text,
                           head=text or "", reader_pages=3, reader_usd=0.002)


def test_card_attachments_of_gives_the_reader_text_and_the_stored_outcome():
    cards, texts = card_attachments_of([lab(AttachmentRead.READ_BY_READER, SCAN_TEXT)])
    assert texts == (SCAN_TEXT,) and cards[0].read == "read_by_reader"
    assert not hasattr(cards[0], "reader_pages")      # the reader fields are never stored on the card


def test_a_scan_that_was_not_read_gives_no_text():
    _, texts = card_attachments_of([lab(AttachmentRead.SCAN_NOT_READ)])
    assert texts == ("",)


def test_the_input_lists_the_file_as_read_with_its_text_below():
    cards, texts = card_attachments_of([lab(AttachmentRead.READ_BY_READER, SCAN_TEXT)])
    card_input = build_card_input(mail(), BODY, list(cards), list(texts), direct=True, nonce=NONCE)
    assert "read, text below" in card_input.text and SCAN_TEXT in card_input.text
    assert SCAN_TEXT in card_input.evidence.chunks and card_input.attachments_included == 1


def _grade(attachment: EmailAttachment):
    cards, texts = card_attachments_of([attachment])
    card_input = build_card_input(mail(), BODY, list(cards), list(texts), direct=True, nonce=NONCE)
    answer = answer_json(key_values=[{"label": "booking_reference", "printed": "LAB-2025-03-0417",
                                      "canonical": "LAB2025030417"}])
    return build_card(parse_mail_answer(answer), mail(), list(cards), card_input.evidence, PEOPLE)


def test_a_value_printed_only_in_the_scan_grades_as_printed():
    keys = set(_grade(lab(AttachmentRead.READ_BY_READER, SCAN_TEXT)).about_keys)
    assert "v:lab2025030417" in keys and "va:lab2025030417" not in keys


def test_the_same_value_with_the_scan_unread_is_not_printed():
    keys = set(_grade(lab(AttachmentRead.SCAN_NOT_READ)).about_keys)
    assert "va:lab2025030417" in keys and "v:lab2025030417" not in keys
