"""The mail-source helpers of the card writer (slice 3): code fields, the owner's zone, address comparison for
"the owner is a direct recipient", and the attachment list with the text the input builder reads. Invented cast."""
from datetime import datetime, timezone

import pytest

from src.domain.email_attachment import SKIP_IMAGE, AttachmentRead as SliceTwoRead, EmailAttachment
from src.domain.email_card import AttachmentRead as CoreRead
from src.domain.email_card_source import (
    GMAIL_DOMAINS, address_key, card_attachments_of, is_direct_recipient, mail_fields_of, owner_zone,
)

SENT = datetime(2025, 6, 2, 23, 30)   # naive UTC, as the Gmail adapter returns it


def test_mail_fields_split_the_sender_and_make_the_time_aware():
    mail = mail_fields_of("msg_rome01", '"Albergo   Immaginario" <Booking@Albergo.Example>', "RM-55120", SENT, "Europe/Lisbon")
    assert (mail.from_name, mail.from_address, mail.from_domain) == ("Albergo Immaginario", "Booking@Albergo.Example", "albergo.example")
    assert mail.sent_at == datetime(2025, 6, 2, 23, 30, tzinfo=timezone.utc) and mail.sent_at.tzinfo is not None
    assert mail.sent_day.isoformat() == "2025-06-03"   # 00:30 in Lisbon (summer time): the day is the owner's


def test_a_sender_without_a_display_name_or_a_domain():
    plain = mail_fields_of("m", "noreply@airline.example", "s", SENT, "UTC")
    assert (plain.from_name, plain.from_domain) == ("", "airline.example")
    assert mail_fields_of("m", "not an address", "s", SENT, "UTC").from_domain == ""


def test_an_aware_time_is_kept():
    aware = datetime(2025, 6, 2, 9, 0, tzinfo=timezone.utc)
    assert mail_fields_of("m", "a@example.com", "s", aware, "UTC").sent_at == aware


@pytest.mark.parametrize("raw, expected", [("Europe/Lisbon", "Europe/Lisbon"), ("Not/AZone", "UTC"), ("", "UTC"),
                                           (None, "UTC"), (" Europe/Berlin ", "Europe/Berlin")])
def test_owner_zone(raw, expected):
    assert owner_zone(raw) == expected


def test_address_key_folds_case_and_plus_tags():
    assert address_key("Alex.Doe+news@Example.com") == "alex.doe@example.com"
    assert address_key("no at sign") == ""


def test_the_dotless_rule_applies_only_to_the_listed_domains():
    other = frozenset({"example.com"})
    assert address_key("A.Lex+news@Example.com", dotless_domains=other) == address_key("alex@example.com", dotless_domains=other)
    assert address_key("A.Lex@example.com") == "a.lex@example.com"          # example.com is not a gmail domain
    assert address_key("a.l.e.x@googlemail.com") == "alex@gmail.com"
    assert GMAIL_DOMAINS == frozenset({"gmail.com", "googlemail.com"})


def test_is_direct_recipient():
    assert is_direct_recipient(["alex@example.com", "clara@example.com"], ["ALEX@example.com"])
    assert is_direct_recipient(["clara@example.com", "Alex+bills@example.com"], ["alex@example.com"])   # a Cc match too
    assert not is_direct_recipient(["clara@example.com"], ["alex@example.com"])                         # a list-only mail
    assert not is_direct_recipient(["clara@example.com"], [""])                                         # no owner address
    assert not is_direct_recipient([], ["alex@example.com"])


def _att(name, kind, read, **kw):
    return EmailAttachment(name=name, kind=kind, size=10, read=read, **kw)


def test_attachments_give_the_stored_list_and_the_text_to_read():
    atts = [
        _att("RM-55120.pdf", "pdf", SliceTwoRead.TEXT, text="FULL TEXT", head="HEAD", tail="TAIL"),
        _att("budget_2026.xlsx", "xlsx", SliceTwoRead.TEXT, text="ALL ROWS", head="HEADER ROWS"),
        _att("policy.pdf", "pdf", SliceTwoRead.LOCKED),
        _att("scan.pdf", "pdf", SliceTwoRead.SCAN_NOT_READ),
        _att("photo.jpg", "jpg", SliceTwoRead.SKIPPED, reason=SKIP_IMAGE),
    ]
    cards, texts = card_attachments_of(atts)
    assert [(c.name, c.kind, c.size, c.read) for c in cards] == [
        ("RM-55120.pdf", "pdf", 10, "text"), ("budget_2026.xlsx", "xlsx", 10, "text"), ("policy.pdf", "pdf", 10, "locked"),
        ("scan.pdf", "pdf", 10, "scan_not_read"), ("photo.jpg", "jpg", 10, "skipped:image")]
    assert texts == ("FULL TEXT", "HEADER ROWS", "", "", "")


def test_every_outcome_of_the_attachment_service_is_a_value_the_card_stores():
    core = {r.value for r in CoreRead}
    for read in SliceTwoRead:
        outcome = "skipped:x" if read is SliceTwoRead.SKIPPED else read.value
        assert outcome in core or outcome.startswith("skipped:")
