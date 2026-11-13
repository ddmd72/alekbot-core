"""
Email search time — the window a mail request searches (email index v2 §5.3.3; search slice 4). Pure: no I/O, no
clock (Today is the owner's, passed in).

**Code reads absolute periods itself** (a month with or without a year, a year, a date); relative ones ("last month",
"next week") come only from the validated slots. A window **filters** only when the request's own text names a time
(`filters`): a model's time slot alone never removes a card (the slot may have misread the request), it only adds the
window's mail to the candidates. `latest` never filters.

A bare month name means the latest such month that has started by Today (asked on 2026-11-13: September is 2026-09,
December is 2025-12).

A card is **in** a window by its sent day, or by a verified time point (an unverified point is automatic and never a
time filter, §2.4); an `upcoming` window reads verified points only (what is still ahead: an event, a due date, a
validity end).

Public interface:
    TimeWindow                                       intent, start, end, filters, label
    text_names_time(concepts) -> bool
    named_window(time_concepts, today) -> Optional[TimeWindow]
    choose_window(concepts, slots, today) -> Optional[TimeWindow]
    window_months(window, today) -> ("m:YYYY-MM", ...)    oldest first
    in_window(card, window, today) -> bool
"""
from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional, Sequence, Tuple

from .drive_grade import _GRADE_MONTH_OF
from .drive_keys import TIME_WORDS
from .drive_match import Concept, KeyFamily
from .email_card import EmailCard
from .email_search_params import UPCOMING_DAYS
from .email_search_query import MailConcepts, MailQuerySlots

INTENT_PERIOD, INTENT_PAST, INTENT_UPCOMING, INTENT_LATEST = "period", "past", "upcoming", "latest"
_FORWARD_LABELS = frozenset({"event", "due", "valid_until"})
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                "November", "December")
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_KEY_MONTH = KeyFamily.MONTH.value
_FULL_DATE_KEY = re.compile(re.escape(KeyFamily.VALUE.value) + r"(\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True)
class TimeWindow:
    """The window: `intent` (period | past | upcoming | latest), its first and last day (None for `latest`), whether
    the request's own text names a time (`filters`) and how a count line names it ("September 2026", "2025",
    "14 Mar 2025", "1 Mar – 31 May 2025")."""
    intent: str
    start: Optional[date]
    end: Optional[date]
    filters: bool
    label: str


def text_names_time(concepts: MailConcepts) -> bool:
    """The request's text holds a time concept, or a removed word of the drive's time words ("last", "month")."""
    if concepts.time:
        return True
    return any(unicodedata.normalize("NFC", w).casefold() in TIME_WORDS for w in concepts.ignored)


def named_window(time_concepts: Sequence[Concept], today: date) -> Optional[TimeWindow]:
    """The window the text names by itself, `filters=True`; several time concepts make one window from the earliest
    start to the latest end (module docstring)."""
    spans: List[Tuple[date, date]] = []
    for concept in time_concepts:
        span = _concept_span(concept, today)
        if span is not None:
            spans.append(span)
    if not spans:
        return None
    start, end = min(s for s, _ in spans), max(e for _, e in spans)
    return TimeWindow(INTENT_PERIOD, start, end, True, _label(start, end))


def choose_window(concepts: MailConcepts, slots: Optional[MailQuerySlots], today: date) -> Optional[TimeWindow]:
    """`named_window` when the text names one (code beats the model on absolute periods), else the slot's time
    (module docstring), else None."""
    named = named_window(concepts.time, today)
    if named is not None:
        return named
    slot = slots.time if slots is not None else None
    if slot is None:
        return None
    filters = text_names_time(concepts)
    if slot.intent == INTENT_LATEST:
        return TimeWindow(INTENT_LATEST, None, None, False, "")
    if slot.intent == INTENT_UPCOMING:
        end = slot.end or today + timedelta(days=UPCOMING_DAYS)
        return TimeWindow(INTENT_UPCOMING, today, end, filters, _label(today, end))
    if slot.start is None and slot.end is None:
        return None
    end = slot.end or today
    start = slot.start or date(end.year, 1, 1)
    if start > end:
        return None
    return TimeWindow(slot.intent, start, end, filters, _label(start, end))


def window_months(window: TimeWindow, today: date) -> Tuple[str, ...]:
    """`m:YYYY-MM` of every month the window touches, oldest first (`upcoming`: from today's month); () for `latest`."""
    if window.start is None or window.end is None:
        return ()
    first = today if window.intent == INTENT_UPCOMING else window.start
    year, month = first.year, first.month
    out: List[str] = []
    while (year, month) <= (window.end.year, window.end.month):
        out.append(f"{_KEY_MONTH}{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return tuple(out)


def in_window(card: EmailCard, window: TimeWindow, today: date) -> bool:
    """The card's sent day or a verified point is in the window (module docstring)."""
    if window.intent == INTENT_LATEST:
        return True
    start, end = window.start or date.min, window.end or date.max
    if window.intent == INTENT_UPCOMING:
        return any(p.verified and p.label in _FORWARD_LABELS and p.end >= today and p.start <= end
                   for p in card.times)
    if start <= card.mail.sent_day <= end:
        return True
    return any(p.verified and p.start <= end and p.end >= start for p in card.times)


# -- implementation -------------------------------------------------------------------------------------------------

def _month_span(year: int, month: int) -> Tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _concept_span(concept: Concept, today: date) -> Optional[Tuple[date, date]]:
    if concept.is_number:
        for key in concept.about_keys:
            if key.startswith(_KEY_MONTH):
                y, m = key[len(_KEY_MONTH):].split("-")
                return _month_span(int(y), int(m))
        for key in concept.about_keys:
            found = _FULL_DATE_KEY.fullmatch(key)
            if found is not None:
                try:
                    day = date.fromisoformat(found.group(1))
                except ValueError:
                    return None
                return day, day
        if concept.word.isdigit() and len(concept.word) == 4:
            year = int(concept.word)
            return date(year, 1, 1), date(year, 12, 31)
        return None
    month = _GRADE_MONTH_OF.get(unicodedata.normalize("NFC", concept.word).casefold())
    if month is None:
        return None
    year = today.year if date(today.year, month, 1) <= today else today.year - 1
    return _month_span(year, month)


def _day_text(day: date, with_year: bool) -> str:
    return f"{day.day} {_MONTH_ABBR[day.month - 1]}" + (f" {day.year}" if with_year else "")


def _label(start: date, end: date) -> str:
    if start == end:
        return _day_text(start, True)
    if (start.month, start.day, end.month, end.day) == (1, 1, 12, 31) and start.year == end.year:
        return str(start.year)
    whole_months = start.day == 1 and end == _month_span(end.year, end.month)[1]
    if whole_months and (start.year, start.month) == (end.year, end.month):
        return f"{_MONTH_NAMES[start.month - 1]} {start.year}"
    if start.year == end.year:
        return f"{_day_text(start, False)} – {_day_text(end, True)}"
    return f"{_day_text(start, True)} – {_day_text(end, True)}"
