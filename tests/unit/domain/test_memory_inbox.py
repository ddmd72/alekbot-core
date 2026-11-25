"""The memory inbox's domain (MCP_MEMORY_SUBMISSIONS_RFC §5.4, §6, §10.2): retention, the owner's field checks, the
strict basis, the owner-local day, the triage interval and the stored form of every record."""
from dataclasses import fields as fields_of
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.memory_inbox import (
    Basis,
    Candidate,
    CandidateFields,
    CandidateStatus,
    DiscardReason,
    DraftClass,
    FinalOutcomeKind,
    InboxState,
    Relation,
    RelationKind,
    SourceCopy,
    Submission,
    SubmissionStatus,
    check_owner_fields,
    owner_day,
    parse_basis_strict,
    purge_after_of,
    triage_interval,
)
from src.domain.user import UserBotConfig

UTC = timezone.utc
AT = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)


def fields(text: str = "Clara booked the Coimbra weekend", basis: Basis = Basis.SAID) -> CandidateFields:
    return CandidateFields(text=text, happens_when="March 6-8", said_when="", basis=basis)


def cfg(days: int) -> UserBotConfig:
    return UserBotConfig(memory_inbox_triage_interval_days=days)


def test_purge_after_follows_the_retention_table():
    assert purge_after_of(SubmissionStatus.TRIAGED, AT) == AT + timedelta(days=30)
    assert purge_after_of(SubmissionStatus.DISCARDED, AT) == AT + timedelta(days=30)
    assert purge_after_of(CandidateStatus.EXPIRED, AT) == AT + timedelta(days=30)
    assert purge_after_of(CandidateStatus.REJECTED, AT) == AT + timedelta(days=180)
    assert purge_after_of(CandidateStatus.CONSOLIDATED, AT) == AT + timedelta(days=180)
    for still_open in (SubmissionStatus.RECEIVED, CandidateStatus.CANDIDATE, CandidateStatus.CONFIRMED):
        assert purge_after_of(still_open, AT) is None


def test_owner_fields_are_refused_empty_or_over_sixty_words():
    assert check_owner_fields(fields(text="  ")) == "empty_text"
    assert check_owner_fields(fields(text=" ".join(["word"] * 61))) == "too_long"
    assert check_owner_fields(fields(text=" ".join(["word"] * 60))) is None


def test_strict_basis_takes_exactly_the_three_values():
    assert [parse_basis_strict(v) for v in ("said", "document", "inferred", "heard", None)] == \
           [Basis.SAID, Basis.DOCUMENT, Basis.INFERRED, None, None]
    assert parse_basis_strict("Said") is None and parse_basis_strict(Basis.SAID) is Basis.SAID


def test_the_owner_day_is_the_local_date():                       # Review Focus 4
    assert owner_day(datetime(2026, 11, 25, 11, 30, tzinfo=UTC), "Pacific/Auckland") == "2026-11-26"
    assert owner_day(datetime(2026, 11, 25, 11, 30, tzinfo=UTC), "Europe/Madrid") == "2026-11-25"


def test_the_triage_interval_is_user_then_account_then_one():
    assert (triage_interval(None, None), triage_interval(cfg(3), cfg(2)), triage_interval(cfg(0), cfg(2))) == (1, 3, 2)
    assert triage_interval(UserBotConfig(), cfg(0)) == 1


def test_no_record_field_is_named_expires_at():
    assert "expires_at" not in {f.name for f in fields_of(Submission)} | {f.name for f in fields_of(Candidate)}
    assert "expires_at" not in {f.name for f in fields_of(InboxState)}


SUBMISSION = Submission(
    submission_id="s1", user_id="u1", account_id="a1", status=SubmissionStatus.DISCARDED, received_at=AT,
    client_id="c1", client_name="Desk assistant", information="Alex plays tennis on Sundays",
    basis=Basis.INFERRED, said_when="last week", happens_when="every Sunday", cut=frozenset({"information"}),
    triage_offers=1, triaged_at=AT + timedelta(days=1), discard_reason=DiscardReason.TRIVIAL,
    purge_after=AT + timedelta(days=31),
)
CANDIDATE = Candidate(
    candidate_id="k1", user_id="u1", account_id="a1", status=CandidateStatus.CONSOLIDATED, created_at=AT,
    answer_by=AT + timedelta(days=30), language="en", source_ids=("s1", "s2"),
    sources=(
        SourceCopy(submission_id="s1", received_at=AT - timedelta(hours=2), client_name="Desk assistant",
                   information="Clara booked the Coimbra weekend", basis=Basis.SAID, said_when="",
                   happens_when="March 6-8"),
        SourceCopy(submission_id="s2", received_at=AT - timedelta(hours=1), client_name="Desk assistant",
                   information="Alex goes with Clara", basis=Basis.DOCUMENT, said_when="yesterday", happens_when=""),
    ),
    text="Clara booked the Coimbra weekend, March 6-8", happens_when="March 6-8, 2027", said_when="late November 2026",
    basis=Basis.SAID,
    draft=DraftClass(directive=False, temporal_class="event", context_priority="medium",
                     expiry={"kind": "date", "value": "2027-03-08"}),
    relation=Relation(kind=RelationKind.REFINES, fact_id="f1", fact_text="Alex plans a trip to Coimbra"),
    proposed=CandidateFields(text="Clara booked Coimbra", happens_when="March", said_when="", basis=Basis.INFERRED),
    edited=True, confirmed_at=AT + timedelta(days=2), rejected_at=None, final_attempts=2,
    consolidated_at=AT + timedelta(days=3), outcome=FinalOutcomeKind.GAVE_UP, purge_after=AT + timedelta(days=183),
)
STATE = InboxState(
    user_id="u1", last_triage_at=AT, last_triage_failed_at=AT - timedelta(days=1), final_due_at=AT + timedelta(hours=1),
    final_attempts=2, last_final_at=AT - timedelta(hours=5), received_day=("2026-11-25", 7),
)


@pytest.mark.parametrize("record, read", [
    (SUBMISSION, lambda raw: Submission.from_record("s1", raw)),
    (CANDIDATE, lambda raw: Candidate.from_record("k1", raw)),
    (STATE, lambda raw: InboxState.from_record("u1", raw)),
], ids=["submission", "candidate", "state"])
def test_every_field_round_trips(record, read):
    every_field_set = [f.name for f in fields_of(record) if getattr(record, f.name) in (None, (), frozenset(), 0)]
    assert every_field_set in ([], ["rejected_at"])     # the case sets every field (a candidate is never both)
    assert read(record.to_record()) == record


def test_a_document_of_the_other_kind_is_not_read():
    with pytest.raises(ValueError):
        Submission.from_record("k1", CANDIDATE.to_record())
    with pytest.raises(ValueError):
        Candidate.from_record("s1", SUBMISSION.to_record())


def test_a_candidate_is_received_when_its_newest_source_arrived():
    assert CANDIDATE.received == AT - timedelta(hours=1)
    assert CANDIDATE.fields == CandidateFields(text=CANDIDATE.text, happens_when="March 6-8, 2027",
                                               said_when="late November 2026", basis=Basis.SAID)
