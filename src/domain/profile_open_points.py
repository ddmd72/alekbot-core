"""The open points of the account's fact profiles that a profile question may ask (fact profile RFC §4.7): pure.

`open_points(view, answers, confirmed, names)` reads the current facts as the profile step left them and returns one
`OpenPoint` per thing the owner can settle, in the order the questions are created:

- *place* — an unresolved place of a fact's profile, one point per folded city and country across the account (the
  facts that print it are counted; the quoted fact is the newest). The reason and the candidate rows are read from the
  atlas now (`PlaceNames.candidates`), not stored on the fact. A place the atlas now resolves, or names nothing, is no
  point.
- *person* — a `people` review item whose mention fits at least one confirmed list entry; one point per fact lineage and
  folded mention (two facts printing the same words may mean two people).
- *date* — the first `time_points` review item of a fact lineage; one point per lineage (a date belongs to one fact).

A key that already has an answer (`unknown` included) is no point. The order is the most facts first, then the newest
quoted fact, then the key.

`PointFact` and `OpenPoint` are the input and the output of one function, so they share this module on purpose."""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from .confirmed_people import ConfirmedPeople, ConfirmedPerson, people_named_in_texts
from .fact_profile import ProfileReviewItem, UnresolvedPlace
from .interactive_option import MAX_LABEL_CHARS
from .place_lookup import PlaceRow
from .place_names import PlaceNames
from .profile_answers import ProfileAnswers, date_point_key, person_point_key, place_point_key
from .profile_open_point import MAX_PICKS, OpenPointOption, ProfileOpenPoint

_ELLIPSIS = "…"


@dataclass(frozen=True)
class PointFact:
    """One current fact as the open points read it: its id and lineage, its text and creation time, and what its profile
    keeps open."""
    fact_id: str
    lineage_id: str
    text: str
    created_at: datetime
    places_unresolved: Tuple[UnresolvedPlace, ...] = ()
    review_items: Tuple[ProfileReviewItem, ...] = ()


@dataclass(frozen=True)
class OpenPoint:
    """`point` is what the question carries; `fact_id` and `quoted_text` the newest fact naming it; `facts` how many
    current facts name it; `newest` that fact's creation time."""
    point: ProfileOpenPoint
    fact_id: str
    quoted_text: str
    facts: int
    newest: datetime


def _label(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_LABEL_CHARS else text[:MAX_LABEL_CHARS - 1] + _ELLIPSIS


def _place_options(rows: Sequence[PlaceRow]) -> Tuple[OpenPointOption, ...]:
    """The first three rows as options, heaviest first, labelled `name, country`. Two equal labels take the large
    neighbour (`name, country (near X)`) when the row has one; labels still equal keep the heavier row only."""
    shown = list(rows[:MAX_PICKS])
    labels = [f"{row.name_en}, {row.country}" for row in shown]
    for index, row in enumerate(shown):
        if labels.count(f"{row.name_en}, {row.country}") > 1 and row.large is not None:
            labels[index] = f"{row.name_en}, {row.country} (near {row.large.name_en})"
    options: List[OpenPointOption] = []
    seen = set()
    for row, label in zip(shown, labels):
        if label in seen or not row.geonameid:
            continue
        seen.add(label)
        options.append(OpenPointOption(id=f"pick{len(options) + 1}", label=_label(label), value=row.geonameid))
    return tuple(options)


def _person_options(entries: Sequence[ConfirmedPerson]) -> Tuple[OpenPointOption, ...]:
    return tuple(
        OpenPointOption(id=f"pick{n}", label=_label(entry.name if entry.is_user else f"{entry.name} — {entry.relation}"),
                        value=entry.name)
        for n, entry in enumerate(entries[:MAX_PICKS], start=1))


def _newest(facts: Sequence[PointFact]) -> PointFact:
    """The newest fact; a tie in time is settled by the smaller id."""
    return min(facts, key=lambda fact: (-fact.created_at.timestamp(), fact.fact_id))


def _place_points(view: Sequence[PointFact], answers: ProfileAnswers, names: PlaceNames) -> List[OpenPoint]:
    by_key: Dict[str, List[Tuple[PointFact, UnresolvedPlace]]] = {}
    for fact in view:
        taken = set()
        for place in fact.places_unresolved:
            if not place.city.strip():
                continue
            key = place_point_key(place.city, names.canonical_country(place.country) or place.country)
            if key in taken or answers.has(key):
                continue
            taken.add(key)
            by_key.setdefault(key, []).append((fact, place))
    points: List[OpenPoint] = []
    for key, named in by_key.items():
        newest = _newest([fact for fact, _ in named])
        place = next(place for fact, place in named if fact is newest)
        reason, rows = names.candidates(place.city, place.country)
        if reason is None or reason == "empty":
            continue
        printed = f"{place.city}, {place.country}" if place.country.strip() else place.city
        points.append(OpenPoint(
            ProfileOpenPoint(key=key, field="places", printed=printed, city=place.city, country=place.country,
                             reason=reason, options=_place_options(rows)),
            newest.fact_id, newest.text, len(named), newest.created_at))
    return points


def _person_points(view: Sequence[PointFact], answers: ProfileAnswers,
                   confirmed: Optional[ConfirmedPeople]) -> List[OpenPoint]:
    points: List[OpenPoint] = []
    for fact in view:
        if not fact.lineage_id:
            continue
        taken = set()
        for item in fact.review_items:
            if item.field != "people" or not item.mention.strip():
                continue
            key = person_point_key(fact.lineage_id, item.mention)
            if key in taken or answers.has(key):
                continue
            taken.add(key)
            entries = people_named_in_texts(confirmed, [item.mention])
            if not entries:
                continue
            points.append(OpenPoint(
                ProfileOpenPoint(key=key, field="people", printed=item.mention, lineage_id=fact.lineage_id,
                                 mention=item.mention, options=_person_options(entries)),
                fact.fact_id, fact.text, 1, fact.created_at))
    return points


def _date_points(view: Sequence[PointFact], answers: ProfileAnswers) -> List[OpenPoint]:
    points: List[OpenPoint] = []
    for fact in view:
        if not fact.lineage_id or answers.has(date_point_key(fact.lineage_id)):
            continue
        item = next((i for i in fact.review_items if i.field == "time_points" and i.mention.strip()), None)
        if item is None:
            continue
        points.append(OpenPoint(
            ProfileOpenPoint(key=date_point_key(fact.lineage_id), field="time_points", printed=item.mention,
                             lineage_id=fact.lineage_id, mention=item.mention),
            fact.fact_id, fact.text, 1, fact.created_at))
    return points


def open_points(view: Sequence[PointFact], answers: ProfileAnswers, confirmed: Optional[ConfirmedPeople],
                names: PlaceNames) -> List[OpenPoint]:
    """The points the owner may be asked, most facts first, then the newest, then the key. A fact lineage seen twice is
    one point (keys are unique): the first point of a key stands."""
    points = [*_place_points(view, answers, names), *_person_points(view, answers, confirmed),
              *_date_points(view, answers)]
    unique: Dict[str, OpenPoint] = {}
    for point in points:
        unique.setdefault(point.point.key, point)
    return sorted(unique.values(), key=lambda p: (-p.facts, -p.newest.timestamp(), p.point.key))

