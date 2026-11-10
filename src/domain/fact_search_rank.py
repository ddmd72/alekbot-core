"""The fact search's scorer (fact profile RFC §5.3, OD-14): one linear score per candidate fact, and the fusion of the
scored lists of several phrases.

Public interface:
    QueryPlaceKeys(keys, country, lp)                one query place as the atlas reaches it
    HomePlace(keys, lp)                              the owner's home town (place_names.resolve_home)
    CardRef(names, relation); card_ref(card)         a people card as the score reads it
    ScoreInputs(sim_text, sim_label, sim_message, places, home, cards)
    FEATURES, WEIGHTS
    feature_values(query, row, inputs) -> Dict[str, float]
    score(query, row, inputs, weights=WEIGHTS) -> float
    score_rows(query, rows, inputs, weights) -> Dict[str, float]
    PRIORITY_WEIGHTS, MULTI_PHRASE_BONUS
    fuse_phrases(phrases, anchors) -> List[str]

The score is Σ weight × feature over FEATURES. Its unit is the log-odds of "this fact ranks above that one": the
weights were fitted offline on a local calibration set by a pairwise logistic model with no intercept, so a difference
of scores carries meaning and a score's absolute value does not. A feature is computed from the query and the fact's
stored fields only; a similarity missing from the inputs reads 0.0.

The input value types share this module on purpose: they exist only to carry the scorer's inputs. Pure: no I/O.
"""
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (TYPE_CHECKING, AbstractSet, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set,
                    Tuple)

from .drive_key_forms import primary as form_primary
from .fact_query import FactQuery
from .fact_search_lists import gap_recurring, person_targets, window
from .fact_search_snapshot import SnapshotRow

if TYPE_CHECKING:
    from .person_cards import PersonCard


@dataclass(frozen=True)
class QueryPlaceKeys:
    """One query place: `keys` as `PlaceNames.query_keys` returns them (a city's own `pl:` key first, or a country's
    `co:` key), `country` the city's `co:` key (None when unknown), `lp` its owner-confirmed local key (None without a
    city name)."""
    keys: Tuple[str, ...]
    country: Optional[str]
    lp: Optional[str]


@dataclass(frozen=True)
class HomePlace:
    """The owner's home town: the `pl:` keys of the city (its own and its large neighbour's; empty when the atlas does
    not resolve it) and its local key (None when none)."""
    keys: FrozenSet[str]
    lp: Optional[str]


@dataclass(frozen=True)
class CardRef:
    """A people card as the score reads it: its name skeletons and its relation word (primary form, None when none)."""
    names: FrozenSet[str]
    relation: Optional[str]


def card_ref(card: "PersonCard") -> CardRef:
    """The card's `name_keys` with the `x:` prefix stripped, and the primary form of its relation."""
    names = frozenset(key[2:] if key.startswith("x:") else key for key in card.name_keys)
    return CardRef(names=names, relation=form_primary(card.relation) if card.relation else None)


@dataclass(frozen=True)
class ScoreInputs:
    """What the score reads besides the query and the row: the subject, kind and message similarity per fact id, the
    query places (one per `query.places` entry), the owner's home town and the account's people cards."""
    sim_text: Mapping[str, float] = field(default_factory=dict)
    sim_label: Mapping[str, float] = field(default_factory=dict)
    sim_message: Mapping[str, float] = field(default_factory=dict)
    places: Tuple[QueryPlaceKeys, ...] = ()
    home: Optional[HomePlace] = None
    cards: Tuple[CardRef, ...] = ()


# ------------------------------------------------------------------------------------------------ features

FEATURES = (
    "sim_text", "sim_label", "sim_message", "place", "home_unplaced", "home_implicit", "kind", "domain", "people",
    "role_event", "role_state", "role_validity", "role_measurement", "role_timeless",
    "t_overlap", "t_near", "no_time", "event_wrong_side", "age",
)
_ROLES = ("event", "state", "validity", "measurement")      # any other time role reads as timeless
_WINDOW_INTENTS = ("future", "past", "period")              # intents under which a recurring occasion is projected
NEAR_DAYS = 30.0                                            # closeness decay of a time point outside the target
AGE_DAYS = 365.0                                            # decay of the report's age

# Place grades: the named city, its owner-confirmed local key, or the country named alone; the large neighbour; the
# same country; another place.
PLACE_HIT, PLACE_NEIGHBOUR, PLACE_COUNTRY, PLACE_ELSEWHERE = 1.0, 0.8, 0.3, -0.3
# People grades: a queried name; another spelling on that name's card; a queried relation (the mention's role or its
# card's relation).
PEOPLE_NAME, PEOPLE_CARD_SPELLING, PEOPLE_RELATION = 1.0, 0.9, 0.6
# Domain grades: the primary domain queried; only a further domain queried.
DOMAIN_PRIMARY, DOMAIN_FURTHER = 1.0, 0.5

# ------------------------------------------------------------------------------------------------ weights (fitted)

W_SIM_TEXT = 13.838694             # cosine of the subject to the fact text
W_SIM_LABEL = 10.521030            # cosine of the kind to the fact's label vector
W_SIM_MESSAGE = 18.387885          # cosine of the whole message to the fact text
W_PLACE = 1.641424                 # place grade against the query places
W_HOME_UNPLACED = 0.307404         # a query place is the home town and the fact has no place
W_HOME_IMPLICIT = 0.753186         # no query place and the fact is at the home town
W_KIND = 1.305054                  # a requested kind among the fact's kinds
W_DOMAIN = 0.589142                # domain grade (primary full, further half)
W_PEOPLE = 6.296621                # people grade (name, card spelling, relation)
W_ROLE_EVENT = -0.453935           # time role event
W_ROLE_STATE = 0.312296            # time role state
W_ROLE_VALIDITY = -0.265602        # time role validity
W_ROLE_MEASUREMENT = -0.451789     # time role measurement
W_ROLE_TIMELESS = 1.108526         # any other time role (timeless or none)
W_T_OVERLAP = 0.782602             # a time point overlaps the target (the intent's window, else today)
W_T_NEAR = 0.120493                # exp(-days / 30) from the nearest point outside the target, either side
W_NO_TIME = -0.049335              # the fact has no time point
W_EVENT_WRONG_SIDE = -0.613423     # an event wholly before today under `future`, or wholly after it under `past`
W_AGE = 1.245545                   # exp(-days since reported / 365)

WEIGHTS: Mapping[str, float] = MappingProxyType({
    "sim_text": W_SIM_TEXT, "sim_label": W_SIM_LABEL, "sim_message": W_SIM_MESSAGE, "place": W_PLACE,
    "home_unplaced": W_HOME_UNPLACED, "home_implicit": W_HOME_IMPLICIT, "kind": W_KIND, "domain": W_DOMAIN,
    "people": W_PEOPLE, "role_event": W_ROLE_EVENT, "role_state": W_ROLE_STATE, "role_validity": W_ROLE_VALIDITY,
    "role_measurement": W_ROLE_MEASUREMENT, "role_timeless": W_ROLE_TIMELESS, "t_overlap": W_T_OVERLAP,
    "t_near": W_T_NEAR, "no_time": W_NO_TIME, "event_wrong_side": W_EVENT_WRONG_SIDE, "age": W_AGE,
})


def place_grade(place: QueryPlaceKeys, fact_keys: AbstractSet[str]) -> float:
    """How well a fact's place keys meet one query place."""
    if place.lp and place.lp in fact_keys:
        return PLACE_HIT
    if not place.keys:
        return 0.0
    first = place.keys[0]
    if first.startswith("co:"):
        return PLACE_HIT if first in fact_keys else 0.0
    if first in fact_keys:
        return PLACE_HIT
    if any(key in fact_keys for key in place.keys[1:]):
        return PLACE_NEIGHBOUR
    if place.country and place.country in fact_keys:
        return PLACE_COUNTRY
    if any(key.startswith(("pl:", "lp:")) for key in fact_keys):
        return PLACE_ELSEWHERE
    return 0.0


def _signed_gap(row: SnapshotRow, query: FactQuery) -> Optional[int]:
    """Signed days from the fact's nearest time point to the target (the intent's window, else today): negative = the
    fact lies before it, 0 = overlap, positive = after it; None when the fact has no time point. A recurring occasion
    is projected by month and day under a window intent."""
    if not row.intervals:
        return None
    span = window(query) or (query.today, query.today)
    best: Optional[int] = None
    for first, last in row.intervals:
        if row.recurring and query.time.intent in _WINDOW_INTENTS:
            distance = gap_recurring((first, last), span)
            signed = 0 if distance == 0 else (-distance if last < span[0] else distance)
        elif last < span[0]:
            signed = -(span[0] - last).days
        elif first > span[1]:
            signed = (first - span[1]).days
        else:
            signed = 0
        if best is None or abs(signed) < abs(best):
            best = signed
    return best


class _Scorer:
    """The per-query part of the features, computed once for every row of one search."""

    def __init__(self, query: FactQuery, inputs: ScoreInputs) -> None:
        self._query = query
        self._inputs = inputs
        self._domains = [d for d in query.domains if d][:2]
        self._kinds = frozenset(query.kinds)
        home = inputs.home
        self._home_keys: FrozenSet[str] = home.keys if home is not None else frozenset()
        home_lp = home.lp if home is not None else None
        self._slot_home = any(
            (bool(place.keys) and place.keys[0] in self._home_keys) or (place.lp is not None and place.lp == home_lp)
            for place in inputs.places)
        self._targets, self._relations = person_targets(query.people)
        self._card_name = [card.names for card in inputs.cards if any(t and t <= card.names for t in self._targets)]
        self._card_rel = [card.names for card in inputs.cards if card.relation and card.relation in self._relations]

    def values(self, row: SnapshotRow) -> Dict[str, float]:
        query, inputs = self._query, self._inputs
        out = dict.fromkeys(FEATURES, 0.0)
        if query.subject:
            out["sim_text"] = float(inputs.sim_text.get(row.id, 0.0))
        if query.kind:
            out["sim_label"] = float(inputs.sim_label.get(row.id, 0.0))
        if query.message:
            out["sim_message"] = float(inputs.sim_message.get(row.id, 0.0))
        if query.places:
            out["place"] = max((place_grade(place, row.place_keys) for place in inputs.places), default=0.0)
            if self._slot_home and not row.place_keys:
                out["home_unplaced"] = 1.0
        elif self._home_keys and row.place_keys & self._home_keys:
            out["home_implicit"] = 1.0
        if self._kinds and self._kinds & row.kinds:
            out["kind"] = 1.0
        if self._domains:
            if row.primary_domain in self._domains:
                out["domain"] = DOMAIN_PRIMARY
            elif row.domains & set(self._domains):
                out["domain"] = DOMAIN_FURTHER
        if query.people:
            out["people"] = self._people(row)
        self._time(row, out)
        return out

    def _people(self, row: SnapshotRow) -> float:
        best = 0.0
        for names, roles in row.people:
            if names and any(t and (t <= names or names <= t) for t in self._targets):
                return PEOPLE_NAME
            if names and any(names <= card for card in self._card_name):
                best = max(best, PEOPLE_CARD_SPELLING)
            if roles & self._relations:
                best = max(best, PEOPLE_RELATION)
            if names and any(names <= card for card in self._card_rel):
                best = max(best, PEOPLE_RELATION)
        return best

    def _time(self, row: SnapshotRow, out: Dict[str, float]) -> None:
        query, today = self._query, self._query.today
        role = row.time_role if row.time_role in _ROLES else "timeless"
        out[f"role_{role}"] = 1.0
        signed = _signed_gap(row, query)
        if signed is None:
            out["no_time"] = 1.0
        elif signed == 0:
            out["t_overlap"] = 1.0
        else:
            out["t_near"] = math.exp(-abs(signed) / NEAR_DAYS)
        if signed is not None and role == "event":
            first = min(start for start, _ in row.intervals)
            last = max(end for _, end in row.intervals)
            intent = query.time.intent
            if (intent == "future" and last < today) or (intent == "past" and first > today):
                out["event_wrong_side"] = 1.0
        out["age"] = math.exp(-max(0, (today - row.reported).days) / AGE_DAYS)


def feature_values(query: FactQuery, row: SnapshotRow, inputs: ScoreInputs) -> Dict[str, float]:
    """Every feature of FEATURES for one candidate."""
    return _Scorer(query, inputs).values(row)


def _weighted(values: Mapping[str, float], weights: Mapping[str, float]) -> float:
    return sum(weights.get(name, 0.0) * values[name] for name in FEATURES)


def score(query: FactQuery, row: SnapshotRow, inputs: ScoreInputs, weights: Mapping[str, float] = WEIGHTS) -> float:
    """The candidate's score (a weight missing from `weights` reads 0.0)."""
    return _weighted(feature_values(query, row, inputs), weights)


def score_rows(query: FactQuery, rows: Iterable[SnapshotRow], inputs: ScoreInputs,
               weights: Mapping[str, float]) -> Dict[str, float]:
    """`score` of every row by id, with the per-query part computed once."""
    scorer = _Scorer(query, inputs)
    return {row.id: _weighted(scorer.values(row), weights) for row in rows}


# ------------------------------------------------------------------------------------------------ several phrases

PRIORITY_WEIGHTS = (1.0, 0.7, 0.5, 0.35)      # weight of a phrase by its position in the caller's priority order
MULTI_PHRASE_BONUS = 0.0                      # per extra phrase that found a fact: nothing to fit it on, so none


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def _phrase_value(position: int, fact_score: float, anchor: float) -> float:
    """A fact's value in one phrase (RFC §5.3 "Several phrases"): the phrase's priority weight times σ(score − anchor),
    where the anchor is the phrase's own cut line — the score of its 30th candidate after the duplicate pass, or of its
    last when it keeps fewer. A fact the phrase ranks at its line reads half the phrase's weight. σ of the raw score
    would not do: the score has no intercept, so it saturates near 1 for every candidate."""
    return PRIORITY_WEIGHTS[position] * _sigmoid(fact_score - anchor)


def fuse_phrases(phrases: Sequence[Sequence[Tuple[str, float]]], anchors: Sequence[float]) -> List[str]:
    """The fused order of up to four phrases (priority order). Each inner sequence is one phrase's `(fact_id, score)`
    in its score order, `anchors[i]` that phrase's cut line. A fact's value is the maximum over the phrases holding it
    of `_phrase_value` (plus MULTI_PHRASE_BONUS per further phrase); ordered by value, ties by id. ValueError on more
    phrases than PRIORITY_WEIGHTS or an anchor count that differs."""
    if len(phrases) > len(PRIORITY_WEIGHTS):
        raise ValueError(f"at most {len(PRIORITY_WEIGHTS)} phrases, got {len(phrases)}")
    if len(anchors) != len(phrases):
        raise ValueError(f"{len(phrases)} phrases need as many anchors, got {len(anchors)}")
    best: Dict[str, float] = {}
    found: Dict[str, Set[int]] = {}
    for position, (phrase, anchor) in enumerate(zip(phrases, anchors)):
        for fact_id, fact_score in phrase:
            value = _phrase_value(position, fact_score, anchor)
            if fact_id not in best or value > best[fact_id]:
                best[fact_id] = value
            found.setdefault(fact_id, set()).add(position)
    fused = {fact_id: value + MULTI_PHRASE_BONUS * (len(found[fact_id]) - 1) for fact_id, value in best.items()}
    return sorted(fused, key=lambda fact_id: (-fused[fact_id], fact_id))

