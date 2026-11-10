"""The owner's answers to profile questions (fact profile RFC §4.7, OD-13): one document per account, read whole by the
profile step and applied by code (`fact_profile_derive.apply_answers`), never by a model.

- A *place* answer is account-wide, keyed by the folded city and country as the profile names them
  (`place_point_key`): `atlas` (the row the owner picked or named, by geonameid), `local` (a real place the atlas lacks,
  city and country as printed or typed) or `unknown`.
- A *person* answer belongs to one fact lineage and one folded mention (`person_point_key`, OD-13 a): `entry` (a
  confirmed list entry's name), `not_listed` or `unknown`.
- A *date* answer belongs to one fact lineage (`date_point_key`, OD-13 c) and the mention it was asked about: `date`
  (a point in the forms the derivation reads from the profiler: `start`, optional `end`, `precision`) or `unknown`.

`unknown` ("don't remember") is stored so the point is never asked again; it changes no profile and is never in the
answers digest. The first answer for a key wins (the store's `record`).

`PlaceAnswer`, `PersonAnswer`, `DateAnswer` and `ProfileAnswers` are one stored shape, so they share this module on
purpose.
"""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple, Union

from .fact_profile_answer import ProfileAnswer
from .place_names import PlaceNames, fold

PlaceResolution = Literal["atlas", "local", "unknown"]
PersonResolution = Literal["entry", "not_listed", "unknown"]
DateResolution = Literal["date", "unknown"]
PLACE_RESOLUTIONS: Tuple[str, ...] = ("atlas", "local", "unknown")
PERSON_RESOLUTIONS: Tuple[str, ...] = ("entry", "not_listed", "unknown")
DATE_RESOLUTIONS: Tuple[str, ...] = ("date", "unknown")
DATE_PRECISIONS: Tuple[str, ...] = ("day", "month", "year")
_DIGEST_CHARS = 16


def place_point_key(city: str, country: str) -> str:
    """The key of a place across the account; callers pass `names.canonical_country(country) or country`."""
    return "place:" + fold(city) + "|" + fold(country)


def person_point_key(lineage_id: str, mention: str) -> str:
    """The key of a person mention of one fact lineage."""
    return "person:" + lineage_id + ":" + fold(mention)


def date_point_key(lineage_id: str) -> str:
    """The key of a date question of one fact lineage."""
    return "date:" + lineage_id


@dataclass(frozen=True, kw_only=True)
class PlaceAnswer:
    key: str
    city: str
    country: str
    resolution: PlaceResolution
    geonameid: str = ""
    question_id: str
    answered_at: datetime

    @property
    def effective(self) -> bool:
        return self.resolution in ("atlas", "local")


@dataclass(frozen=True, kw_only=True)
class PersonAnswer:
    key: str
    lineage_id: str
    mention: str
    resolution: PersonResolution
    name: str = ""
    question_id: str
    answered_at: datetime

    @property
    def effective(self) -> bool:
        return self.resolution in ("entry", "not_listed")


@dataclass(frozen=True, kw_only=True)
class DateAnswer:
    key: str
    lineage_id: str
    mention: str
    resolution: DateResolution
    start: str = ""
    end: str = ""
    precision: str = ""
    question_id: str
    answered_at: datetime

    @property
    def effective(self) -> bool:
        return self.resolution == "date"


OwnerAnswer = Union[PlaceAnswer, PersonAnswer, DateAnswer]


@dataclass(frozen=True)
class ProfileAnswers:
    """Every answer of one account by key."""
    places: Mapping[str, PlaceAnswer]
    people: Mapping[str, PersonAnswer]
    dates: Mapping[str, DateAnswer] = field(default_factory=dict)

    def has(self, key: str) -> bool:
        return key in self.places or key in self.people or key in self.dates

    def with_answer(self, answer: OwnerAnswer) -> "ProfileAnswers":
        """These answers plus `answer`; ValueError when its key already has one (the first answer wins)."""
        if self.has(answer.key):
            raise ValueError(f"the key {answer.key!r} already has an answer")
        if isinstance(answer, PlaceAnswer):
            return ProfileAnswers({**self.places, answer.key: answer}, self.people, self.dates)
        if isinstance(answer, PersonAnswer):
            return ProfileAnswers(self.places, {**self.people, answer.key: answer}, self.dates)
        return ProfileAnswers(self.places, self.people, {**self.dates, answer.key: answer})


EMPTY_ANSWERS = ProfileAnswers({}, {}, {})


# ── Stored shape ────────────────────────────────────────────────────────────

def _text(raw: Mapping[str, Any], name: str, what: str) -> str:
    value = raw.get(name, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{what} {name} must be a string")
    return value


def _common(raw: Any, what: str, prefix: str, resolutions: Tuple[str, ...]) -> Tuple[str, str, str, datetime]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{what} must be a mapping")
    key = _text(raw, "key", what)
    if not key:
        raise ValueError(f"{what} has an empty key")
    if not key.startswith(prefix):
        raise ValueError(f"{what} key must start with {prefix!r}")
    resolution = _text(raw, "resolution", what)
    if resolution not in resolutions:
        raise ValueError(f"{what} resolution {resolution!r} is not one of {resolutions}")
    answered_at = raw.get("answered_at")
    if not isinstance(answered_at, datetime):
        raise ValueError(f"{what} answered_at must be a timestamp")
    return key, resolution, _text(raw, "question_id", what), answered_at


def _place(raw: Any, what: str) -> PlaceAnswer:
    key, resolution, question_id, answered_at = _common(raw, what, "place:", PLACE_RESOLUTIONS)
    city, country, geonameid = _text(raw, "city", what), _text(raw, "country", what), _text(raw, "geonameid", what)
    if resolution == "atlas" and not geonameid:
        raise ValueError(f"{what} is an atlas answer without a geonameid")
    if resolution == "local" and not fold(city):
        raise ValueError(f"{what} is a local answer without a city")
    return PlaceAnswer(key=key, city=city, country=country, resolution=resolution,  # type: ignore[arg-type]
                       geonameid=geonameid, question_id=question_id, answered_at=answered_at)


def _person(raw: Any, what: str) -> PersonAnswer:
    key, resolution, question_id, answered_at = _common(raw, what, "person:", PERSON_RESOLUTIONS)
    name = _text(raw, "name", what)
    if resolution == "entry" and not name.strip():
        raise ValueError(f"{what} is an entry answer without a name")
    return PersonAnswer(key=key, lineage_id=_text(raw, "lineage_id", what), mention=_text(raw, "mention", what),
                        resolution=resolution, name=name,  # type: ignore[arg-type]
                        question_id=question_id, answered_at=answered_at)


def _date(raw: Any, what: str) -> DateAnswer:
    key, resolution, question_id, answered_at = _common(raw, what, "date:", DATE_RESOLUTIONS)
    start, end, precision = _text(raw, "start", what), _text(raw, "end", what), _text(raw, "precision", what)
    if resolution == "date" and (not start or precision not in DATE_PRECISIONS):
        raise ValueError(f"{what} is a date answer without a start or a precision")
    return DateAnswer(key=key, lineage_id=_text(raw, "lineage_id", what), mention=_text(raw, "mention", what),
                      resolution=resolution, start=start, end=end, precision=precision,  # type: ignore[arg-type]
                      question_id=question_id, answered_at=answered_at)


def _items(data: Mapping[str, Any], name: str) -> List[Any]:
    raw = data.get(name)
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{name} must be a list")
    return list(raw)


def parse_profile_answers(data: Mapping[str, Any]) -> ProfileAnswers:
    """The answers from their stored shape `{places: [...], people: [...], dates: [...]}`; ValueError on any broken
    rule (an empty key or one of another kind, a key twice, an unknown resolution, an atlas answer without a geonameid,
    a local one without a city, an entry without a name, a date without a start or precision, a missing time)."""
    if not isinstance(data, Mapping):
        raise ValueError("the profile answers must be a mapping")
    answers = EMPTY_ANSWERS
    parsers = (("places", _place), ("people", _person), ("dates", _date))
    for name, parse in parsers:
        for i, raw in enumerate(_items(data, name)):
            answer: OwnerAnswer = parse(raw, f"{name} {i + 1}")
            answers = answers.with_answer(answer)
    return answers


def profile_answers_document(answers: ProfileAnswers) -> Dict[str, Any]:
    """`{places, people, dates}` in the shape `parse_profile_answers` reads; the adapter adds account and time."""
    return {
        "places": [{"key": a.key, "city": a.city, "country": a.country, "resolution": a.resolution,
                    "geonameid": a.geonameid, "question_id": a.question_id, "answered_at": a.answered_at}
                   for a in answers.places.values()],
        "people": [{"key": a.key, "lineage_id": a.lineage_id, "mention": a.mention, "resolution": a.resolution,
                    "name": a.name, "question_id": a.question_id, "answered_at": a.answered_at}
                   for a in answers.people.values()],
        "dates": [{"key": a.key, "lineage_id": a.lineage_id, "mention": a.mention, "resolution": a.resolution,
                   "start": a.start, "end": a.end, "precision": a.precision, "question_id": a.question_id,
                   "answered_at": a.answered_at}
                  for a in answers.dates.values()],
    }


# ── The answers one fact names ──────────────────────────────────────────────

def named_answers(answers: ProfileAnswers, answer: ProfileAnswer, lineage_id: str,
                  names: PlaceNames) -> List[OwnerAnswer]:
    """The effective answers this fact's model answer names: a place answered `atlas`/`local` that it gives, a person
    mention answered `entry`/`not_listed` and a date answered `date` that its review items name (a `confident` answer
    keeps no review items, so it names no person or date). Each answer once, place first."""
    found: Dict[str, OwnerAnswer] = {}
    for place in answer.places:
        key = place_point_key(place.city, names.canonical_country(place.country) or place.country)
        hit = answers.places.get(key)
        if hit is not None and hit.effective:
            found.setdefault(key, hit)
    if lineage_id and answer.quality == "needs_review":
        for item in answer.review_items:
            if item.field == "people":
                key = person_point_key(lineage_id, item.mention)
                person = answers.people.get(key)
                if person is not None and person.effective:
                    found.setdefault(key, person)
            elif item.field == "time_points":
                dated = answers.dates.get(date_point_key(lineage_id))
                if dated is not None and dated.effective and fold(dated.mention) == fold(item.mention):
                    found.setdefault(dated.key, dated)
    return list(found.values())


def _digest_entry(answer: OwnerAnswer) -> Dict[str, str]:
    if isinstance(answer, PlaceAnswer):
        return {"key": answer.key, "resolution": answer.resolution, "geonameid": answer.geonameid,
                "city": answer.city, "country": answer.country}
    if isinstance(answer, PersonAnswer):
        return {"key": answer.key, "resolution": answer.resolution, "name": answer.name}
    return {"key": answer.key, "resolution": answer.resolution, "mention": answer.mention, "start": answer.start,
            "end": answer.end, "precision": answer.precision}


def answers_digest(answers: ProfileAnswers, answer: Optional[ProfileAnswer], lineage_id: str,
                   names: PlaceNames) -> str:
    """The part of `profile_derivation` that follows the owner's answers (fact profile RFC §4.2): `""` when this fact's
    answer names no effective answer, else the first 16 hex of sha256 over the sorted canonical JSON of what each named
    answer applies (key, resolution and its value). An answer elsewhere, or an `unknown`, leaves the string as it was."""
    if answer is None:
        return ""
    entries = [_digest_entry(a) for a in named_answers(answers, answer, lineage_id, names)]
    if not entries:
        return ""
    entries.sort(key=lambda e: e["key"])
    canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
