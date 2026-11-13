"""
Email card — the value types of one indexed mail's card (email index v2, card slice 1). Pure data.

This module holds several public classes on purpose: they are one value shape (the model's parsed answer, the mail's
code fields and the graded card built from both), read together by every card module, as `drive_enrichment` holds
the drive profile's. They are frozen dataclasses like `DriveProfile`, whose shapes they reuse.

A card is a drive profile plus mail fields. The model writes the lean answer (`MailAnswer`); code adds the mail's
own fields (`MailFields`, `CardAttachment`), grades every value against the mail as built for the model, reads the
time envelope (`TimePoint`), keeps only the people the mail prints (`CardPerson`), derives `about`, and keys the whole
(`EmailCard`). Bodies and document text are never part of a card.

Public interface:
    CardState      pending | carded | empty | abandoned | source_gone   (v2 only; the v1 `state` is never touched)
    About          owner | household | person | unknown                 (derived by code, never a search filter)
    Substance      some | none                                          (the model's; `none` makes the card empty)
    AttachmentRead the read outcomes of a listed attachment; `skipped(reason)` -> "skipped:<reason>"
    MailFields     the mail's code fields (Gmail): id, sender, subject, sent time, the owner's zone; `.sent_day`
    CardAttachment a listed attachment: name, kind sniffed from the bytes, size, read outcome; `.read_ok`
    AnswerPerson, AnswerDate, MailAnswer   the model's lean answer, parsed and validated (`email_card_answer`)
    TimePoint      one date of the time envelope: label, first and last day, printed form, printed-verified
    When           the display time of a card: label, first and last day
    CardPerson     a person the card keeps: the printed form and the confirmed entry it links to ("" when none)
    EmailCard      the graded card with its keys
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from .drive_enrichment import DriveProfile, KeyValue, ProfileEntity


class CardState(str, Enum):
    """Where a stored mail's card stands (v2 only). `empty` and `source_gone` are hidden from v2 search, never
    deleted; `abandoned` after three charged failures on one input. v2 search reads `carded` only."""
    PENDING = "pending"
    CARDED = "carded"
    EMPTY = "empty"
    ABANDONED = "abandoned"
    SOURCE_GONE = "source_gone"


class About(str, Enum):
    """Whose matter the mail is. The model answers it; code derives the stored value (`email_card_people`)."""
    OWNER = "owner"
    HOUSEHOLD = "household"
    PERSON = "person"
    UNKNOWN = "unknown"


class Substance(str, Enum):
    """`none`: nothing in the mail the owner could ask about later (the card is stored `empty`)."""
    SOME = "some"
    NONE = "none"


class AttachmentRead(str, Enum):
    """The read outcome of a listed attachment. A skipped one is `skipped:<reason>` (`skipped`)."""
    TEXT = "text"                        # the text layer was read
    READ_BY_READER = "read_by_reader"    # a scan read by the paid reader
    SCAN_NOT_READ = "scan_not_read"
    LOCKED = "locked"                    # password-protected: no retry, no failure count

    @staticmethod
    def skipped(reason: str) -> str:
        return f"skipped:{reason}"


_READ_OK = frozenset({AttachmentRead.TEXT.value, AttachmentRead.READ_BY_READER.value})


@dataclass(frozen=True)
class MailFields:
    """The mail's code fields. `sent_at` is timezone-aware; `zone` is the owner's IANA zone, in which the sent day,
    yearless dates and the display are read."""
    email_id: str
    from_name: str
    from_address: str
    from_domain: str
    subject: str
    sent_at: datetime
    zone: str

    @property
    def sent_day(self) -> date:
        """The day the mail was sent, in the owner's zone."""
        return self.sent_at.astimezone(ZoneInfo(self.zone)).date()


@dataclass(frozen=True)
class CardAttachment:
    """A listed attachment. `kind` is sniffed from the bytes, never the declared MIME; `read` an `AttachmentRead`
    value or `skipped:<reason>`. Its text is never part of the card."""
    name: str
    kind: str
    size: int
    read: str

    @property
    def read_ok(self) -> bool:
        """The attachment's text went into the extraction input."""
        return self.read in _READ_OK


@dataclass(frozen=True)
class AnswerPerson:
    """A person the model names: `printed` (a name as printed, or the relation words when no name is) and `handle`,
    the `P<n>` of the one list entry that fits ("" when none or several fit)."""
    printed: str
    handle: str


@dataclass(frozen=True)
class AnswerDate:
    """A printed date with its role: `start` (a span's first day), `end` (a span's last day; None for one day)."""
    label: str
    start: date
    end: Optional[date]
    printed: str


@dataclass(frozen=True)
class MailAnswer:
    """The model's lean card answer, parsed: enums inside their sets, lists inside the mail caps. Nothing here is
    verified yet."""
    language: str
    doc_type: str
    life_domains: Tuple[str, ...]
    title: str
    what_it_is: str
    people: Tuple[AnswerPerson, ...]
    about: About
    dates: Tuple[AnswerDate, ...]
    entities: Tuple[ProfileEntity, ...]
    key_values: Tuple[KeyValue, ...]
    topics: Tuple[str, ...]
    topics_native: Tuple[str, ...]
    topic_words: Tuple[str, ...]
    substance: Substance


@dataclass(frozen=True)
class TimePoint:
    """One date of the time envelope (§2.4). `end == start` for a single day. `verified`: the printed form is in the
    mail, reads as these days and its weekday (when printed) agrees; an unverified point is automatic and never a
    time filter."""
    label: str
    start: date
    end: date
    printed: str
    verified: bool


@dataclass(frozen=True)
class When:
    """The display time of a card: the event, else the due date, else the validity end, else the sent day."""
    label: str
    start: date
    end: date


@dataclass(frozen=True)
class CardPerson:
    """A person the card keeps. `printed` is in the mail; `entry_name` is the confirmed entry it links to ("" for a
    printed person the list does not resolve); `linked_by` is "name" (a name form printed) or "relation" (printed
    relation words that only this entry has), "" when unlinked."""
    printed: str
    entry_name: str = ""
    linked_by: str = ""


@dataclass(frozen=True)
class EmailCard:
    """The graded card with its keys. `profile` is the drive profile the drive grader and keyer ran on, with the
    card's own `doc_type` (a mail-only type included) and the mail-only type's names as label words."""
    mail: MailFields
    attachments: Tuple[CardAttachment, ...]
    profile: DriveProfile
    people: Tuple[CardPerson, ...]
    about: About
    substance: Substance
    times: Tuple[TimePoint, ...]
    state: CardState
    about_keys: Tuple[str, ...]
    name_keys: Tuple[str, ...]
    people_keys: Tuple[str, ...]
    month_keys: Tuple[str, ...]
    template_key: str
    card_version: int
