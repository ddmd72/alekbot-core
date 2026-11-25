"""The memory inbox (MCP_MEMORY_SUBMISSIONS_RFC): what an outside assistant sent about the owner, and the candidates the
daily triage made of it for the owner to check in the Cabinet.

One collection holds two kinds of record under one `status` field (§6.1); the values never overlap, so a status names
its kind:

    submission:  received -> triaged | discarded
    candidate:   candidate -> confirmed -> consolidated;  candidate -> rejected | expired

A record's stored form is `to_record()` (no id; the id is the document id) and `from_record(id, raw)` reads it back,
raising ValueError for a document that is not a record of that kind. Every datetime is aware UTC. `purge_after` is the
field of the collection's Firestore TTL policy (§6.3, `purge_after_of`): set when a record closes, never on an open one.
No field is named `expires_at` (root CLAUDE.md TTL trap). The per-user state document (§6.2) is `InboxState`.

The numbers below are the RFC's proposals (§5.4, §6.3, §7, §9, §10); the owner confirms them at the plan's review.

One module holds every record, enum and limit of the inbox on purpose (a waiver of the one-public-class rule): they are
one frozen vocabulary that the store, the MCP tool, triage, the final pass, the rhythm and the Cabinet all build against.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Final, FrozenSet, List, Mapping, Optional, Tuple, Union

from .fact_words import word_count
from .local_time import local_date
from .user import UserBotConfig


class Basis(str, Enum):
    """How the outside assistant knows what it sent: the user said it, a document the user showed says it, or the
    assistant inferred it (the least trusted; also every missing or unrecognised value at the tool)."""

    SAID = "said"
    DOCUMENT = "document"
    INFERRED = "inferred"


class SubmissionStatus(str, Enum):
    RECEIVED = "received"
    TRIAGED = "triaged"        # cited by at least one candidate
    DISCARDED = "discarded"    # a triage reason, or `undecided` after TRIAGE_MAX_OFFERS


class CandidateStatus(str, Enum):
    CANDIDATE = "candidate"          # waiting for the owner
    CONFIRMED = "confirmed"          # the owner said "True"; waiting for the final pass
    CONSOLIDATED = "consolidated"    # the final pass ended over it (or gave up on it)
    REJECTED = "rejected"            # the owner said "Not true"
    EXPIRED = "expired"              # ANSWER_WINDOW passed unanswered


class DiscardReason(str, Enum):
    """Why triage discarded a submission (§7.4); for counts only. `undecided` is code's own: a submission no pass
    decided after TRIAGE_MAX_OFFERS offers."""

    KNOWN = "known"
    WAITING = "waiting"
    ANSWERED_BEFORE = "answered_before"
    NOT_ABOUT_USER = "not_about_user"
    TRIVIAL = "trivial"
    UNCLEAR = "unclear"
    UNDECIDED = "undecided"


class RelationKind(str, Enum):
    """A candidate's relation to stored memory; every kind but `new` names a current fact of the account."""

    NEW = "new"
    REFINES = "refines"
    CONTRADICTS = "contradicts"
    DUPLICATES = "duplicates"


class FinalOutcomeKind(str, Enum):
    DONE = "done"          # a final pass ended over it
    GAVE_UP = "gave_up"    # FINAL_MAX_ATTEMPTS passes failed over it


class AnswerResult(str, Enum):
    """The answer to the owner's "True" / "Not true" on a candidate."""

    DONE = "done"
    NOT_FOUND = "not_found"        # no such candidate
    OTHER_USER = "other_user"      # another user's candidate
    NOT_WAITING = "not_waiting"    # answered before, or expired
    INVALID = "invalid"            # the owner's fields are refused (the service's check; the store never answers it)


# ------------------------------------------------------------------------------------------------ limits (§5.4, §7, §9, §10)

MAX_ITEMS_PER_CALL: Final[int] = 20
MAX_INFORMATION_CHARS: Final[int] = 1000
MAX_WHEN_CHARS: Final[int] = 200
RECEIVED_PER_DAY: Final[int] = 100          # per user per owner-local day; items past it are not stored
CANDIDATE_MAX_WORDS: Final[int] = 60        # by `word_count`; refused with its count, never cut
ANSWER_WINDOW: Final[timedelta] = timedelta(days=30)
TRIAGE_BATCH: Final[int] = 40
TRIAGE_SHOWN_CAP: Final[int] = 100
TRIAGE_PASSES: Final[int] = 3
TRIAGE_MAX_OFFERS: Final[int] = 2           # offered this many times undecided -> discarded `undecided`
FINAL_DELAY: Final[timedelta] = timedelta(hours=1)
FINAL_BACKSTOP_GRACE: Final[timedelta] = timedelta(hours=1)
FINAL_BATCH: Final[int] = 20
FINAL_MAX_ATTEMPTS: Final[int] = 3          # failed final passes over a candidate -> consolidated, outcome `gave_up`
SAID_WHEN_LOOKBACK: Final[timedelta] = timedelta(days=365)
DEFAULT_TRIAGE_INTERVAL_DAYS: Final[int] = 1

# ------------------------------------------------------------------------------------------------ retention (§6.3)

_RETENTION: Final[Mapping[Union["SubmissionStatus", "CandidateStatus"], timedelta]] = {
    SubmissionStatus.TRIAGED: timedelta(days=30),
    SubmissionStatus.DISCARDED: timedelta(days=30),
    CandidateStatus.EXPIRED: timedelta(days=30),
    CandidateStatus.REJECTED: timedelta(days=180),
    CandidateStatus.CONSOLIDATED: timedelta(days=180),
}

# The names of the owner-editable fields, as the Cabinet sends them and as they are stored.
OWNER_FIELD_NAMES: Final[Tuple[str, ...]] = ("text", "happens_when", "said_when", "basis")


def purge_after_of(status: Union[SubmissionStatus, CandidateStatus], at: datetime) -> Optional[datetime]:
    """When a record that reached `status` at `at` may be deleted by the TTL policy; None for an open status
    (`received`, `candidate`, `confirmed`), which is never purged."""
    keep = _RETENTION.get(status)
    return None if keep is None else at + keep


def check_owner_fields(fields: "CandidateFields") -> Optional[str]:
    """Why the owner's fields are refused, or None: `empty_text` (nothing but whitespace) or `too_long` (more than
    CANDIDATE_MAX_WORDS words by `word_count`). The basis is typed, so it is checked where it is parsed."""
    if not fields.text.strip():
        return "empty_text"
    if word_count(fields.text) > CANDIDATE_MAX_WORDS:
        return "too_long"
    return None


def parse_basis_strict(value: Any) -> Optional[Basis]:
    """The basis `value` names exactly (`said`, `document`, `inferred`), or None: the Cabinet's and triage's reading,
    which refuse anything else (the tool's reading is lenient and lives with the tool)."""
    if not isinstance(value, str):
        return None
    for basis in Basis:
        if value == basis.value:
            return basis
    return None


def owner_day(now: datetime, zone: str) -> str:
    """The owner-local calendar date of `now` as an ISO date (`zone` an IANA name; an unknown one is UTC)."""
    return local_date(now, zone).isoformat()


def triage_interval(config: Optional[UserBotConfig], defaults: Optional[UserBotConfig]) -> int:
    """Days between triage runs: the user's setting, else the account default, else DEFAULT_TRIAGE_INTERVAL_DAYS. A value
    below 1 is no setting (as the review's interval)."""
    for source in (config, defaults):
        days = source.memory_inbox_triage_interval_days if source is not None else None
        if days is not None and days >= 1:
            return days
    return DEFAULT_TRIAGE_INTERVAL_DAYS


# ------------------------------------------------------------------------------------------------ value objects


@dataclass(frozen=True)
class SubmittingClient:
    """The registered MCP client a submission came from: its id and its (normalised) name."""

    client_id: str
    client_name: str


@dataclass(frozen=True)
class SubmissionDraft:
    """One normalised item of a tool call, before it is stored; `cut` names the fields cut at their char cap."""

    information: str
    basis: Basis
    said_when: str
    happens_when: str
    cut: FrozenSet[str] = frozenset()


@dataclass(frozen=True)
class CandidateFields:
    """The four fields of a candidate the owner may edit."""

    text: str
    happens_when: str
    said_when: str
    basis: Basis

    def to_record(self) -> Dict[str, Any]:
        return {"text": self.text, "happens_when": self.happens_when, "said_when": self.said_when,
                "basis": self.basis.value}

    @classmethod
    def from_record(cls, raw: Mapping[str, Any]) -> "CandidateFields":
        return cls(text=_text(raw, "text"), happens_when=_text(raw, "happens_when"), said_when=_text(raw, "said_when"),
                   basis=_enum(Basis, raw, "basis"))


@dataclass(frozen=True)
class DraftClass:
    """Triage's advisory classification, as the consolidator sets it on `create_fact` (§7.4); `expiry` is the stored
    form `parse_expiry` reads."""

    directive: bool
    temporal_class: str
    context_priority: str
    expiry: Mapping[str, Any]

    def to_record(self) -> Dict[str, Any]:
        return {"directive": self.directive, "temporal_class": self.temporal_class,
                "context_priority": self.context_priority, "expiry": dict(self.expiry)}

    @classmethod
    def from_record(cls, raw: Mapping[str, Any]) -> "DraftClass":
        expiry = raw.get("expiry")
        if not isinstance(expiry, Mapping):
            raise ValueError("draft.expiry is not a map")
        directive = raw.get("directive")
        if not isinstance(directive, bool):
            raise ValueError("draft.directive is not a bool")
        return cls(directive=directive, temporal_class=_text(raw, "temporal_class"),
                   context_priority=_text(raw, "context_priority"), expiry=dict(expiry))


@dataclass(frozen=True)
class Relation:
    """A candidate's relation to memory: `fact_id` and `fact_text` (the stored text, read by code) for every kind but
    `new`, where both are None."""

    kind: RelationKind
    fact_id: Optional[str]
    fact_text: Optional[str]

    def to_record(self) -> Dict[str, Any]:
        return {"kind": self.kind.value, "fact_id": self.fact_id, "fact_text": self.fact_text}

    @classmethod
    def from_record(cls, raw: Mapping[str, Any]) -> "Relation":
        return cls(kind=_enum(RelationKind, raw, "kind"), fact_id=_optional_text(raw, "fact_id"),
                   fact_text=_optional_text(raw, "fact_text"))


@dataclass(frozen=True)
class SourceCopy:
    """A candidate's copy of one cited submission, so the card shows the raw text after the submission is purged."""

    submission_id: str
    received_at: datetime
    client_name: str
    information: str
    basis: Basis
    said_when: str
    happens_when: str

    def to_record(self) -> Dict[str, Any]:
        return {"submission_id": self.submission_id, "received_at": self.received_at, "client_name": self.client_name,
                "information": self.information, "basis": self.basis.value, "said_when": self.said_when,
                "happens_when": self.happens_when}

    @classmethod
    def from_record(cls, raw: Mapping[str, Any]) -> "SourceCopy":
        return cls(submission_id=_text(raw, "submission_id"), received_at=_time(raw, "received_at"),
                   client_name=_text(raw, "client_name"), information=_text(raw, "information"),
                   basis=_enum(Basis, raw, "basis"), said_when=_text(raw, "said_when"),
                   happens_when=_text(raw, "happens_when"))


# ------------------------------------------------------------------------------------------------ records


@dataclass(frozen=True)
class Submission:
    """One item an outside assistant sent (§6.1). `triage_offers` counts the passes that saw it and decided nothing;
    `triaged_at` is when triage settled it (cited or discarded)."""

    submission_id: str
    user_id: str
    account_id: str
    status: SubmissionStatus
    received_at: datetime
    client_id: str
    client_name: str
    information: str
    basis: Basis
    said_when: str
    happens_when: str
    cut: FrozenSet[str] = frozenset()
    triage_offers: int = 0
    triaged_at: Optional[datetime] = None
    discard_reason: Optional[DiscardReason] = None
    purge_after: Optional[datetime] = None

    def to_record(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id, "account_id": self.account_id, "status": self.status.value,
            "received_at": self.received_at, "client_id": self.client_id, "client_name": self.client_name,
            "information": self.information, "basis": self.basis.value, "said_when": self.said_when,
            "happens_when": self.happens_when, "cut": sorted(self.cut), "triage_offers": self.triage_offers,
            "triaged_at": self.triaged_at,
            "discard_reason": None if self.discard_reason is None else self.discard_reason.value,
            **_purge(self.purge_after),
        }

    @classmethod
    def from_record(cls, submission_id: str, raw: Mapping[str, Any]) -> "Submission":
        cut = raw.get("cut") or []
        if not isinstance(cut, list) or not all(isinstance(c, str) for c in cut):
            raise ValueError("cut is not a list of field names")
        reason = raw.get("discard_reason")
        return cls(
            submission_id=submission_id, user_id=_text(raw, "user_id"), account_id=_text(raw, "account_id"),
            status=_enum(SubmissionStatus, raw, "status"), received_at=_time(raw, "received_at"),
            client_id=_text(raw, "client_id"), client_name=_text(raw, "client_name"),
            information=_text(raw, "information"), basis=_enum(Basis, raw, "basis"), said_when=_text(raw, "said_when"),
            happens_when=_text(raw, "happens_when"), cut=frozenset(cut), triage_offers=_count(raw, "triage_offers"),
            triaged_at=_optional_time(raw, "triaged_at"),
            discard_reason=None if reason is None else _enum(DiscardReason, raw, "discard_reason"),
            purge_after=_optional_time(raw, "purge_after"),
        )


@dataclass(frozen=True)
class Candidate:
    """One thought triage made of one or more submissions, for the owner to check (§6.1). `text`, `happens_when`,
    `said_when` and `basis` are what the owner confirmed (triage's until the owner edits); `proposed` keeps triage's own four
    once the owner answered, and `edited` says whether the owner's differ. `final_attempts` counts failed final passes."""

    candidate_id: str
    user_id: str
    account_id: str
    status: CandidateStatus
    created_at: datetime
    answer_by: datetime
    language: str
    source_ids: Tuple[str, ...]
    sources: Tuple[SourceCopy, ...]
    text: str
    happens_when: str
    said_when: str
    basis: Basis
    draft: DraftClass
    relation: Relation
    proposed: Optional[CandidateFields] = None
    edited: bool = False
    confirmed_at: Optional[datetime] = None
    rejected_at: Optional[datetime] = None
    final_attempts: int = 0
    consolidated_at: Optional[datetime] = None
    outcome: Optional[FinalOutcomeKind] = None
    purge_after: Optional[datetime] = None

    @property
    def fields(self) -> CandidateFields:
        """The four owner-editable fields as they stand."""
        return CandidateFields(text=self.text, happens_when=self.happens_when, said_when=self.said_when, basis=self.basis)

    @property
    def received(self) -> datetime:
        """When its newest source arrived (the final pass orders by it, the card shows it); `created_at` without one."""
        return max((s.received_at for s in self.sources), default=self.created_at)

    def to_record(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id, "account_id": self.account_id, "status": self.status.value,
            "created_at": self.created_at, "answer_by": self.answer_by, "language": self.language,
            "source_ids": list(self.source_ids), "sources": [s.to_record() for s in self.sources],
            **self.fields.to_record(), "draft": self.draft.to_record(), "relation": self.relation.to_record(),
            "proposed": None if self.proposed is None else self.proposed.to_record(), "edited": self.edited,
            "confirmed_at": self.confirmed_at, "rejected_at": self.rejected_at, "final_attempts": self.final_attempts,
            "consolidated_at": self.consolidated_at, "outcome": None if self.outcome is None else self.outcome.value,
            **_purge(self.purge_after),
        }

    @classmethod
    def from_record(cls, candidate_id: str, raw: Mapping[str, Any]) -> "Candidate":
        source_ids = raw.get("source_ids")
        sources = raw.get("sources")
        if not isinstance(source_ids, list) or not all(isinstance(s, str) for s in source_ids):
            raise ValueError("source_ids is not a list of ids")
        if not isinstance(sources, list) or not all(isinstance(s, Mapping) for s in sources):
            raise ValueError("sources is not a list of maps")
        proposed, draft, relation, outcome = raw.get("proposed"), raw.get("draft"), raw.get("relation"), raw.get("outcome")
        if not isinstance(draft, Mapping) or not isinstance(relation, Mapping):
            raise ValueError("draft or relation is not a map")
        if proposed is not None and not isinstance(proposed, Mapping):
            raise ValueError("proposed is not a map")
        edited = raw.get("edited", False)
        if not isinstance(edited, bool):
            raise ValueError("edited is not a bool")
        fields = CandidateFields.from_record(raw)
        return cls(
            candidate_id=candidate_id, user_id=_text(raw, "user_id"), account_id=_text(raw, "account_id"),
            status=_enum(CandidateStatus, raw, "status"), created_at=_time(raw, "created_at"),
            answer_by=_time(raw, "answer_by"), language=_text(raw, "language"), source_ids=tuple(source_ids),
            sources=tuple(SourceCopy.from_record(s) for s in sources), text=fields.text,
            happens_when=fields.happens_when, said_when=fields.said_when, basis=fields.basis,
            draft=DraftClass.from_record(draft), relation=Relation.from_record(relation),
            proposed=None if proposed is None else CandidateFields.from_record(proposed), edited=edited,
            confirmed_at=_optional_time(raw, "confirmed_at"), rejected_at=_optional_time(raw, "rejected_at"),
            final_attempts=_count(raw, "final_attempts"), consolidated_at=_optional_time(raw, "consolidated_at"),
            outcome=None if outcome is None else _enum(FinalOutcomeKind, raw, "outcome"),
            purge_after=_optional_time(raw, "purge_after"),
        )


@dataclass(frozen=True)
class CandidateDraft:
    """A candidate triage decided to make: the submissions it cites, its four fields, its draft and its relation."""

    source_ids: Tuple[str, ...]
    fields: CandidateFields
    draft: DraftClass
    relation: Relation


@dataclass(frozen=True)
class TriageDecisions:
    """What one triage pass decided: the candidates to make, and the submissions to discard with their reason."""

    candidates: Tuple[CandidateDraft, ...] = ()
    discards: Mapping[str, DiscardReason] = field(default_factory=dict)


@dataclass(frozen=True)
class InboxState:
    """The user's inbox state document (§6.2), where the rhythm lives. `received_day` is (owner-local ISO date, items
    stored that day); `final_attempts` counts failed final runs of the current burst."""

    user_id: str
    last_triage_at: Optional[datetime] = None
    last_triage_failed_at: Optional[datetime] = None
    final_due_at: Optional[datetime] = None
    final_attempts: int = 0
    last_final_at: Optional[datetime] = None
    received_day: Optional[Tuple[str, int]] = None

    def to_record(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id, "last_triage_at": self.last_triage_at,
            "last_triage_failed_at": self.last_triage_failed_at, "final_due_at": self.final_due_at,
            "final_attempts": self.final_attempts, "last_final_at": self.last_final_at,
            "received_day": None if self.received_day is None else {"day": self.received_day[0],
                                                                     "count": self.received_day[1]},
        }

    @classmethod
    def from_record(cls, user_id: str, raw: Mapping[str, Any]) -> "InboxState":
        day = raw.get("received_day")
        received_day: Optional[Tuple[str, int]] = None
        if day is not None:
            if not isinstance(day, Mapping):
                raise ValueError("received_day is not a map")
            received_day = (_text(day, "day"), _count(day, "count"))
        return cls(
            user_id=user_id, last_triage_at=_optional_time(raw, "last_triage_at"),
            last_triage_failed_at=_optional_time(raw, "last_triage_failed_at"),
            final_due_at=_optional_time(raw, "final_due_at"), final_attempts=_count(raw, "final_attempts"),
            last_final_at=_optional_time(raw, "last_final_at"), received_day=received_day,
        )


# ------------------------------------------------------------------------------------------------ store answers


@dataclass(frozen=True)
class AddReceipt:
    """`stored` items written; `over_day` items not stored because the owner-local day reached RECEIVED_PER_DAY."""

    stored: int
    over_day: int


@dataclass(frozen=True)
class AppliedTriage:
    """What a triage write did: `candidates` made; submissions `triaged` (cited), `discarded` (a triage reason),
    `offered` (seen, undecided, still `received` with one more offer) and `undecided` (discarded `undecided` at
    TRIAGE_MAX_OFFERS)."""

    candidates: int
    triaged: int
    discarded: int
    offered: int
    undecided: int


@dataclass(frozen=True)
class SettledFinal:
    """What a final-pass settlement did: candidates marked `consolidated` with outcome `done`; candidates left
    `confirmed` with one more failed attempt (`retried`); the ids marked `consolidated` with outcome `gave_up`."""

    consolidated: int
    retried: int
    gave_up: Tuple[str, ...]


@dataclass(frozen=True)
class InboxView:
    """The Cabinet's list: the candidates waiting for the owner, newest first, and how many confirmed ones wait for
    the final pass."""

    candidates: Tuple[Candidate, ...]
    confirmed: int


@dataclass(frozen=True)
class Answered:
    """The service's answer to the owner; `reason` names a refused field (`empty_text`, `too_long`, `basis`, or the
    name of a field that is not text) when `result` is INVALID."""

    result: AnswerResult
    reason: Optional[str] = None


# ------------------------------------------------------------------------------------------------ stored values


def _purge(at: Optional[datetime]) -> Dict[str, Any]:
    """The TTL field of a stored record: present only on a closed one (an open record never carries it, not even null)."""
    return {} if at is None else {"purge_after": at}



def _text(raw: Mapping[str, Any], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str):
        raise ValueError(f"{name} is not a text")
    return value


def _optional_text(raw: Mapping[str, Any], name: str) -> Optional[str]:
    value = raw.get(name)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} is not a text")
    return value


def _count(raw: Mapping[str, Any], name: str) -> int:
    value = raw.get(name, 0)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} is not a count")
    return value


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _time(raw: Mapping[str, Any], name: str) -> datetime:
    value = raw.get(name)
    if not isinstance(value, datetime):
        raise ValueError(f"{name} is not a time")
    return _aware(value)


def _optional_time(raw: Mapping[str, Any], name: str) -> Optional[datetime]:
    return None if raw.get(name) is None else _time(raw, name)


def _enum(kind: Any, raw: Mapping[str, Any], name: str) -> Any:
    value = raw.get(name)
    try:
        return kind(value)
    except ValueError as exc:
        raise ValueError(f"{name} {value!r} is not a {kind.__name__}") from exc


__all__: List[str] = [
    "Basis", "SubmissionStatus", "CandidateStatus", "DiscardReason", "RelationKind", "FinalOutcomeKind", "AnswerResult",
    "MAX_ITEMS_PER_CALL", "MAX_INFORMATION_CHARS", "MAX_WHEN_CHARS", "RECEIVED_PER_DAY", "CANDIDATE_MAX_WORDS",
    "ANSWER_WINDOW", "TRIAGE_BATCH", "TRIAGE_SHOWN_CAP", "TRIAGE_PASSES", "TRIAGE_MAX_OFFERS", "FINAL_DELAY",
    "FINAL_BACKSTOP_GRACE", "FINAL_BATCH", "FINAL_MAX_ATTEMPTS", "SAID_WHEN_LOOKBACK", "DEFAULT_TRIAGE_INTERVAL_DAYS",
    "OWNER_FIELD_NAMES", "purge_after_of", "check_owner_fields", "parse_basis_strict", "owner_day", "triage_interval",
    "SubmittingClient", "SubmissionDraft", "CandidateFields", "DraftClass", "Relation", "SourceCopy", "Submission",
    "Candidate", "CandidateDraft", "TriageDecisions", "InboxState", "AddReceipt", "AppliedTriage", "SettledFinal",
    "InboxView", "Answered",
]
