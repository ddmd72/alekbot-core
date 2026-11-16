"""The header form of a mail with no readable card (email index v2 §6.7 as changed by slice 9b): exactly email_id,
date, from and subject, plus the attachment names each `read: false` when the record names files; the date is the sent
moment in the owner's zone with its offset (a naive stored date is UTC); a stored v1 fact, tags, category, metadata or
snippet never leaves."""
from datetime import datetime

import pytest

from src.domain.email import IndexedEmail
from src.domain.email_memory_candidate import HEADER_CANDIDATE_KEYS, header_candidate
from tests.support.clock_instants import BERLIN, transition

INDEXED = datetime(2026, 4, 1, 8, 0)


def _policy_mail(**over) -> IndexedEmail:
    fields = dict(email_id="msg_policy05", user_id="user-1", account_id="acct-1", source="gmail",
                  subject="Your policy MP-2026-118204", from_address="Example Mutual Insurance <policy@mutual.example>",
                  email_date=transition(BERLIN, "spring", "T+1m").utc.replace(tzinfo=None),
                  attachments=["policy_MP-2026-118204.pdf"], indexed_at=INDEXED)
    fields.update(over)
    return IndexedEmail(**fields)


def test_the_keys_are_exactly_the_headers_and_the_unread_attachments():
    got = header_candidate(_policy_mail(), BERLIN)

    assert list(got) == list(HEADER_CANDIDATE_KEYS) == ["email_id", "date", "from", "subject", "attachments"]
    assert got == {
        "email_id": "msg_policy05",
        "date": "2026-03-29T03:01:00+02:00",
        "from": "Example Mutual Insurance <policy@mutual.example>",
        "subject": "Your policy MP-2026-118204",
        "attachments": [{"name": "policy_MP-2026-118204.pdf", "read": False}],
    }


@pytest.mark.parametrize("label, expected", [("T-1m", "2026-03-29T01:59:00+01:00"),
                                             ("T+1m", "2026-03-29T03:01:00+02:00")])
def test_the_date_carries_the_owners_offset_on_both_sides_of_the_shift(label, expected):
    for stored in (transition(BERLIN, "spring", label).utc, transition(BERLIN, "spring", label).utc.replace(tzinfo=None)):
        assert header_candidate(_policy_mail(email_date=stored), BERLIN)["date"] == expected


def test_a_mail_with_no_attachment_names_has_no_attachments_key():
    assert "attachments" not in header_candidate(_policy_mail(attachments=[]), BERLIN)


def test_an_old_record_with_the_v1_fields_sends_none_of_them():
    old = _policy_mail(text="User renewed the car insurance policy.", tags=["insurance"], category="finance",
                       metadata={"snippet": "Dear Alex Doe, your renewal is attached.", "labels": ["INBOX"]})

    got = header_candidate(old, BERLIN)

    for key in ("fact", "text", "category", "tags", "metadata", "snippet", "labels"):
        assert key not in got
    assert "renewed" not in str(got) and "Dear Alex" not in str(got)
