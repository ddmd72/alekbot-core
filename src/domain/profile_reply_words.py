"""The owner's words in reply to a profile question, read by code only (fact profile RFC §4.7 "Words"): pure.

A reply is short and one line. It is refused (None) when it is empty, has more than one line or more than six words; a
place or person reply is refused too when it holds a digit (a digit is an option, read before the words). Nothing here
is a model and nothing is guessed: a reply that is not one of the forms below is not read, the question stays open and
the owner is told the form.

- *Place* — `city, country` (the city before the last comma, a country the atlas knows): one atlas row for the pair is
  `atlas`; a city the atlas lacks, or has only in other countries, is `local` (the city as typed, the atlas's name of the
  country); several rows is not read. Without a comma: a country that names exactly one of the shown options picks that
  row; else a city the atlas places in one row (inside the question's country when it has one) is that row.
- *Person* — a name, or a form of a name, of exactly one shown entry.
- *Date* — a year, a year and month, or a full date as ISO values, or a range of two of them (`-`, `–`, `—` or `to`
  between); both ends of a range have one precision. Read with the derivation's own date reader
  (`fact_profile_derive.owner_time_point`), so the derivation accepts exactly what is recorded.

`PlaceReading`, `PersonReading` and the three readers are one small vocabulary, so they share this module on purpose."""
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .confirmed_people import ConfirmedPeople
from .fact_profile_derive import owner_time_point
from .place_names import PlaceNames, country_key, fold
from .profile_answers import DateAnswer
from .profile_open_point import ProfileOpenPoint

MAX_WORDS = 6
_ISO = re.compile(r"\d{4}(?:-\d{2}){0,2}")
_RANGE = re.compile(r"\s+(?:-|–|—|to)\s+|\s*[–—]\s*")
_PRECISION = {4: "year", 7: "month", 10: "day"}


@dataclass(frozen=True)
class PlaceReading:
    """`atlas` with the row's `geonameid`, or `local` with the city as typed and the atlas's name of the country."""
    resolution: str
    city: str
    country: str
    geonameid: str = ""


@dataclass(frozen=True)
class PersonReading:
    """The name of the shown list entry the reply means."""
    name: str


def _shape_ok(text: str, digits_refused: bool) -> bool:
    if not text.strip() or "\n" in text or "\r" in text:
        return False
    if digits_refused and any(ch.isdigit() for ch in text):
        return False
    return len(text.split()) <= MAX_WORDS


def read_place_reply(text: str, point: ProfileOpenPoint, names: PlaceNames) -> Optional[PlaceReading]:
    """The place `text` says, or None when it is not one of the forms read."""
    if point.field != "places" or not _shape_ok(text, True):
        return None
    text = text.strip()
    if "," in text:
        before, _, after = text.rpartition(",")
        city, country = before.strip(), after.strip()
        canonical = names.canonical_country(country) if country else None
        if not city or canonical is None:
            return None
        reason, rows = names.candidates(city, canonical)
        if reason is None and len(rows) == 1 and rows[0].geonameid:
            return PlaceReading("atlas", city, canonical, rows[0].geonameid)
        if reason in ("not_in_atlas", "not_in_country"):
            return PlaceReading("local", city, canonical)
        return None
    canonical = names.canonical_country(text)
    if canonical is not None:
        named = [option for option in point.options if country_key(canonical) in names.keys_of(option.value)]
        if len(named) == 1:
            return PlaceReading("atlas", point.city, canonical, named[0].value)
    reason, rows = names.candidates(text, point.country)
    if reason is None and len(rows) == 1 and rows[0].geonameid:
        return PlaceReading("atlas", point.city, rows[0].country, rows[0].geonameid)
    return None


def read_person_reply(text: str, point: ProfileOpenPoint, confirmed: Optional[ConfirmedPeople]) -> Optional[PersonReading]:
    """The shown entry whose name or form the folded `text` equals; None when no entry or several do."""
    if point.field != "people" or confirmed is None or not _shape_ok(text, True):
        return None
    wanted = fold(text)
    shown = {option.value for option in point.options}
    found = [entry for entry in confirmed.entries() if entry.name in shown
             and wanted in {fold(entry.name), *(fold(form) for form in entry.forms)}]
    return PersonReading(found[0].name) if len(found) == 1 else None


def read_date_reply(text: str, point: ProfileOpenPoint, question_id: str, at: datetime) -> Optional[DateAnswer]:
    """The date answer `text` gives, or None when it is no year, month, full date or range of two the derivation reads."""
    if point.field != "time_points" or not _shape_ok(text, False):
        return None
    parts = [part.strip() for part in _RANGE.split(text.strip())]
    if len(parts) > 2 or not all(_ISO.fullmatch(part) for part in parts):
        return None
    precisions = {_PRECISION.get(len(part)) for part in parts}
    if len(precisions) != 1 or None in precisions:
        return None
    answer = DateAnswer(key=point.key, lineage_id=point.lineage_id, mention=point.mention, resolution="date",
                        start=parts[0], end=parts[1] if len(parts) == 2 else "", precision=precisions.pop() or "",
                        question_id=question_id, answered_at=at)
    return answer if owner_time_point(answer) is not None else None
