"""
RecurrencePort — evaluation of RFC 5545 recurrence rules (RRULE).

Port justification: the RRULE grammar is an external standard and its evaluation
needs a third-party library (`dateutil`), which `domain/` may not import. Three
layers consume it — `RemindersService` (next fire), `NotesAgent` (validate + first
fire) and the Cabinet API (display) — so the rule algebra needs one shared home
that services may depend on. Substitution is real: an RRULE evaluator is
replaceable (`dateutil` today, `icalendar`/`recurring-ical-events` tomorrow).

Rules are stored WITHOUT `DTSTART`: the anchor is always the reminder's current
`due`, so a rule is a pure pattern and the note owns its position in time.

All methods are synchronous — pure computation, no I/O.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional


class RecurrencePort(ABC):

    @abstractmethod
    def normalize(self, rule: str) -> str:
        """Validate an RRULE and return its canonical form.

        Accepts a bare rule (``FREQ=WEEKLY;BYDAY=TU,FR``) or one prefixed with
        ``RRULE:``. Returns the bare, upper-cased rule that callers persist.

        Raises ``ValueError`` with a short, LLM-readable reason when the rule is
        unusable: malformed, missing/unsupported ``FREQ``, carrying ``DTSTART``
        (the note owns the anchor), bounded by ``COUNT``/``UNTIL`` (a reminder
        ends by being deleted, not by expiring into a stale document), or
        describing a pattern that never occurs.
        """

    @abstractmethod
    def first_occurrence(
        self, rule: str, not_before: datetime, tz: str
    ) -> Optional[datetime]:
        """First occurrence at or after ``not_before`` (both UTC).

        Used at creation time to snap a proposed ``due`` onto the rule — a rule
        of "Tue and Fri" with a Wednesday ``due`` must fire Friday, not Wednesday.
        """

    @abstractmethod
    def next_occurrence(self, rule: str, after: datetime, tz: str) -> Optional[datetime]:
        """First occurrence strictly after ``after`` (both UTC) — the reschedule step.

        ``tz`` is the user's IANA zone: recurrence is evaluated on the local wall
        clock, so "every day at 09:00" stays 09:00 across a DST transition.
        Returns ``None`` when the rule yields nothing within the implementation's
        horizon — the caller must treat that as "cannot reschedule", never as now.
        """

    @abstractmethod
    def describe(self, rule: str) -> str:
        """Short human phrase for a rule ("every 2 weeks on Tue, Fri").

        User-facing only (transparency messages, Cabinet). Falls back to the raw
        rule for shapes it cannot phrase — never raises. LLM-facing surfaces pass
        the rule itself; models read RRULE natively and must be able to edit it.
        """
