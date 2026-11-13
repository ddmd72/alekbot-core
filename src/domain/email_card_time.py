"""
Email card time — the time envelope of a card (email index v2 §2.4, card slice 1). Pure: no I/O, no clock (the sent
day is the mail's, in the owner's zone).

`sent_at` is when the mail was written; a booking's `event` is when the trip happens, a bill's `due` when it must be
paid, a policy's `valid_until` when it ends. The model labels each printed date and writes its ISO days; code reads the
printed form itself and decides what is stored:
- **issued == sent is dropped.** A cheap model relabels the mail's own send date as `issued`: an `issued` single day
  equal to the sent day is not a date of the matter (measured 2026-11-13).
- **Ranges** ("10–14 Aug 2025", "10/08 – 14/08/2025"): both ends are read from the one printed form; an end that omits
  its month or year takes the other end's.
- **Yearless dates** ("14 March", "10–14 Aug") take the year that puts them on or after the sent day (an `issued` date:
  on or before it); a range's end follows its start (into the next year when it falls before the start).
- **Weekdays** ("Friday 14 March"): a printed weekday must fall on the day read; a disagreement leaves the date
  unverified.
A date is **verified** (printed-verified: an exact time key and a time filter) when its printed form occurs in the
mail, code's reading of it agrees with the model's day and month (and year, when the form prints one) and any printed
weekday agrees. A form code cannot read is verified for a single day by the drive's date grade (`date_grade`), and never
for a range. An unverified date keeps the model's days and is automatic. When the form prints no year, code's year
replaces the model's. Month names are the drive grader's table plus the universe languages' (pt, de) read here only;
no key depends on them.

Public interface:
    time_envelope(dates, sent_day, evidence) -> (TimePoint, ...)
    profile_dates(points) -> (ProfileDate, ...)      the dates the drive grader and keyer see (starts, then ends)
    month_keys(points, sent_day) -> ("m:YYYY-MM", ...)   every verified point's first and last month, and the sent month
    when_of(points, sent_day) -> When                event, else due, else valid_until (verified), else sent
    render_when(when) -> str                         "event 10–14 Aug 2025", "sent 2 Jun 2025"
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from .drive_enrichment import Grade, ProfileDate
from .drive_grade import _GRADE_MONTH_OF, TextEvidence
from .drive_index_params import PROFILE_DATES_MAX
from .drive_match import KeyFamily
from .email_card import AnswerDate, TimePoint, When

_WHEN_ORDER = ("event", "due", "valid_until")
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# Weekday names (Monday = 0) in the product languages (en, es, fr, uk, ru) and the universe's (pt, de); a Portuguese
# "segunda-feira" is read by its first word. Generic language samples, no person.
_WEEKDAYS: Dict[str, int] = {}
for _day, _names in enumerate((
        ("monday", "mon", "lunes", "lundi", "segunda", "montag", "понеділок", "понедельник"),
        ("tuesday", "tue", "tues", "martes", "mardi", "terça", "terca", "dienstag", "вівторок", "вторник"),
        ("wednesday", "wed", "miércoles", "miercoles", "mercredi", "quarta", "mittwoch", "середа", "середу", "среда",
         "среду"),
        ("thursday", "thu", "thur", "thurs", "jueves", "jeudi", "quinta", "donnerstag", "четвер", "четверг"),
        ("friday", "fri", "viernes", "vendredi", "sexta", "freitag", "пятниця", "пятницю", "пятница", "пятницу"),
        ("saturday", "sat", "sábado", "sabado", "samedi", "samstag", "sonnabend", "субота", "суботу", "суббота",
         "субботу"),
        ("sunday", "sun", "domingo", "dimanche", "sonntag", "неділя", "неділю", "воскресенье"))):
    for _name in _names:
        _WEEKDAYS[_name] = _day
# Month names of the universe's languages (pt, de) the drive's normaliser table does not hold, merged under it, never
# over it: they only read a printed form here, no key depends on them.
_MONTHS: Dict[str, int] = dict(_GRADE_MONTH_OF)
for _month, _forms in enumerate((
        ("janeiro", "januar"), ("fevereiro", "februar"), ("março", "marco", "märz", "maerz"), ("maio",), ("junho", "juni"),
        ("julho", "juli"), (), (), ("setembro",), ("outubro", "oktober"), ("novembro",), ("dezembro", "dezember")), 1):
    for _form in _forms:
        _MONTHS.setdefault(_form, _month)
_APOSTROPHES = re.compile(r"['’ʼ]")
_CLOCK = re.compile(r"\b\d{1,2}[:h]\d{2}\b")
_TOKEN = re.compile(r"\d+|[^\W\d_]+")
# A span separator: a dash with spaces or an en/em dash, "..", or a word ("to", "a", "al", "bis", "до", ...).
_RANGE_SEP = re.compile(r"\s*[–—]\s*|\s+-\s+|\s*\.\.\s*|\s+(?:to|until|till|through|a|al|hasta|au|bis|até|до|по)\s+",
                        re.IGNORECASE)
_DAY_DASH_DAY = re.compile(r"(?<![\d./-])(\d{1,2})-(\d{1,2})(?![\d./-])")   # "10-14 Aug": a span of days


@dataclass(frozen=True)
class _Partial:
    day: Optional[int] = None
    month: Optional[int] = None
    year: Optional[int] = None
    year2: Optional[int] = None          # a two-digit year
    weekday: Optional[int] = None


def time_envelope(dates: Sequence[AnswerDate], sent_day: date, evidence: TextEvidence) -> Tuple[TimePoint, ...]:
    """The card's time points, in the answer's order, with the issued == sent day dropped (module docstring)."""
    points: List[TimePoint] = []
    for d in dates:
        if d.label == "issued" and d.end is None and d.start == sent_day:
            continue
        points.append(_point(d, sent_day, evidence))
    return tuple(points)


def profile_dates(points: Sequence[TimePoint]) -> Tuple[ProfileDate, ...]:
    """The drive profile's dates: every point's first day, then the last days of spans while the drive's
    PROFILE_DATES_MAX allows (the keyer cuts there), each (label, day) once."""
    out: Dict[Tuple[str, date], ProfileDate] = {}
    for p in points:
        out.setdefault((p.label, p.start), ProfileDate(label=p.label, iso=p.start, printed=p.printed))
    for p in points:
        if p.end != p.start and len(out) < PROFILE_DATES_MAX:
            out.setdefault((p.label, p.end), ProfileDate(label=p.label, iso=p.end, printed=p.printed))
    return tuple(out.values())[:PROFILE_DATES_MAX]


def month_keys(points: Sequence[TimePoint], sent_day: date) -> Tuple[str, ...]:
    """`m:YYYY-MM` of every verified point's first and last day and of the sent day, sorted, each once."""
    days = [sent_day, *(d for p in points if p.verified for d in (p.start, p.end))]
    return tuple(sorted({f"{KeyFamily.MONTH.value}{d.year:04d}-{d.month:02d}" for d in days}))


def when_of(points: Sequence[TimePoint], sent_day: date) -> When:
    """The display time: the first verified event, else due, else valid_until point; else the sent day."""
    for label in _WHEN_ORDER:
        for p in points:
            if p.verified and p.label == label:
                return When(label=label, start=p.start, end=p.end)
    return When(label="sent", start=sent_day, end=sent_day)


def render_when(when: When) -> str:
    """"event 14 Aug 2025", "event 10–14 Aug 2025", "event 28 Dec 2025 – 3 Jan 2026"."""
    return f"{when.label.replace('_', ' ')} {_span(when.start, when.end)}"


# -- implementation -------------------------------------------------------------------------------------------------

def _span(start: date, end: date) -> str:
    def full(d: date) -> str:
        return f"{d.day} {_MONTH_ABBR[d.month - 1]} {d.year}"
    if end == start:
        return full(start)
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day}–{full(end)}"
    if start.year == end.year:
        return f"{start.day} {_MONTH_ABBR[start.month - 1]} – {full(end)}"
    return f"{full(start)} – {full(end)}"


def _point(d: AnswerDate, sent_day: date, evidence: TextEvidence) -> TimePoint:
    end = d.end or d.start
    automatic = TimePoint(label=d.label, start=d.start, end=end, printed=d.printed, verified=False)
    if not d.printed.strip() or not evidence.occurs(d.printed):
        return automatic
    sides = _read(d.printed, d.end is not None)
    if sides is None:
        if d.end is None and evidence.date_grade(d.start, d.printed)[0] is not Grade.NOT_IN_TEXT:
            return TimePoint(label=d.label, start=d.start, end=end, printed=d.printed, verified=True)
        return automatic
    first, last = sides
    if first.month is None or first.day is None:       # `_read` returns sides with both
        raise ValueError("_read returned a side without a month or day")
    if first.year is None and first.year2 is None:
        read_start = _yearless(first.month, first.day, sent_day, d.label == "issued")
        if read_start is None or (read_start.month, read_start.day) != (d.start.month, d.start.day):
            return automatic
        read_end = _after(read_start, last) if last is not None else read_start
        if read_end is None or (read_end.month, read_end.day) != (end.month, end.day):
            return automatic
    else:
        if _full(first, d.start) != d.start or (_full(last, d.start) if last is not None else d.start) != end:
            return automatic
        read_start, read_end = d.start, end
    for part, day in ((first, read_start), (last, read_end)):
        if part is not None and part.weekday is not None and part.weekday != day.weekday():
            return automatic
    return TimePoint(label=d.label, start=read_start, end=read_end, printed=d.printed, verified=True)


def _read(printed: str, is_range: bool) -> Optional[Tuple[_Partial, Optional[_Partial]]]:
    """The printed form's one or two sides, each side's missing month and year taken from the other; None when code
    cannot read it (or it reads as a span for a single date, or the reverse)."""
    text = _CLOCK.sub(" ", _APOSTROPHES.sub("", unicodedata.normalize("NFC", printed)))
    text = _DAY_DASH_DAY.sub(r"\1 – \2", text)
    sides = [s for s in _RANGE_SEP.split(text) if s.strip()]
    if len(sides) not in (1, 2) or (len(sides) == 2) != is_range:
        return None
    parsed = [_side(s) for s in sides]
    first = parsed[0]
    if first is None:
        return None
    if len(parsed) == 1:
        return (first, None) if first.day is not None and first.month is not None else None
    last = parsed[1]
    if last is None:
        return None
    own_first, own_last = first, last
    first = _Partial(day=first.day, month=first.month or last.month, year=first.year or last.year,
                     year2=first.year2 or last.year2, weekday=first.weekday)
    last = _Partial(day=last.day, month=last.month or first.month, year=last.year or first.year,
                    year2=last.year2 or first.year2, weekday=last.weekday)
    if first.day is None or last.day is None or first.month is None or last.month is None:
        return None
    if (first.month, first.day) > (last.month, last.day):
        # a span over the new year ("28 Dec – 3 Jan 2026"): the end that borrowed a year moves past the start
        if own_first.year is None and own_first.year2 is None and (last.year is not None or last.year2 is not None):
            first = _shift_year(first, -1)
        elif own_last.year is None and own_last.year2 is None and (first.year is not None or first.year2 is not None):
            last = _shift_year(last, 1)
    return first, last


def _shift_year(p: _Partial, by: int) -> _Partial:
    return _Partial(day=p.day, month=p.month, year=None if p.year is None else p.year + by,
                    year2=None if p.year2 is None else (p.year2 + by) % 100, weekday=p.weekday)


def _side(text: str) -> Optional[_Partial]:
    """One side of a printed date: its day, month, year and weekday; None when it is not a date's side."""
    months: List[int] = []
    numbers: List[str] = []
    weekday: Optional[int] = None
    for token in _TOKEN.findall(text):
        if token.isdigit():
            numbers.append(token)
            continue
        word = token.casefold()
        if word in _WEEKDAYS:
            weekday = _WEEKDAYS[word]
        elif word in _MONTHS:
            months.append(_MONTHS[word])
    if len(set(months)) > 1:
        return None
    if months:
        years = [n for n in numbers if len(n) == 4]
        days = [n for n in numbers if len(n) <= 2]
        if len(years) > 1 or len(days) > 1 or len(years) + len(days) != len(numbers):
            return None
        return _valid(_Partial(day=int(days[0]) if days else None, month=months[0],
                               year=int(years[0]) if years else None, weekday=weekday))
    if not numbers or len(numbers) > 3:
        return None
    if len(numbers) == 3 and len(numbers[0]) == 4:
        y, m, d = numbers
        return _valid(_Partial(day=int(d), month=int(m), year=int(y), weekday=weekday)) if len(m) <= 2 and len(d) <= 2 \
            else None
    if len(numbers) == 2 and len(numbers[0]) <= 2 and len(numbers[1]) == 4:
        return _valid(_Partial(day=int(numbers[0]), year=int(numbers[1]), weekday=weekday))   # "14, 2025" of a span
    if any(len(n) > 2 for n in numbers[:2]):
        return None
    day = int(numbers[0])
    month = int(numbers[1]) if len(numbers) > 1 else None
    if len(numbers) == 3:
        y = numbers[2]
        if len(y) == 4:
            return _valid(_Partial(day=day, month=month, year=int(y), weekday=weekday))
        if len(y) == 2:
            return _valid(_Partial(day=day, month=month, year2=int(y), weekday=weekday))
        return None
    return _valid(_Partial(day=day, month=month, weekday=weekday))


def _valid(p: _Partial) -> Optional[_Partial]:
    if p.day is not None and not 1 <= p.day <= 31:
        return None
    if p.month is not None and not 1 <= p.month <= 12:
        return None
    return p


def _full(p: _Partial, model_day: date) -> Optional[date]:
    """A side with a year: a two-digit year takes the model's century."""
    if p.month is None or p.day is None:
        raise ValueError("a date side needs both a month and a day")
    year = p.year if p.year is not None else (model_day.year // 100) * 100 + (p.year2 or 0)
    return _date(year, p.month, p.day)


def _yearless(month: int, day: int, sent_day: date, issued: bool) -> Optional[date]:
    """The occurrence of (month, day) on or after the sent day (`issued`: on or before it), within a leap cycle."""
    years = range(sent_day.year, sent_day.year - 5, -1) if issued else range(sent_day.year, sent_day.year + 5)
    for year in years:
        found = _date(year, month, day)
        if found is not None and (found <= sent_day if issued else found >= sent_day):
            return found
    return None


def _after(start: date, p: _Partial) -> Optional[date]:
    """A yearless span's last day: in the start's year, or the next when it would fall before the start."""
    if p.month is None or p.day is None:
        raise ValueError("a date side needs both a month and a day")
    for year in (start.year, start.year + 1):
        found = _date(year, p.month, p.day)
        if found is not None and found >= start:
            return found
    return None


def _date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None

