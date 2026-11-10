"""The fact search engine (fact profile RFC §5.3), pure over an account snapshot: the candidate lists a query asks for,
the intimate gate, the scored order (`fact_search_rank.py`) and the near-duplicate pass.

The lists only generate candidates; none is cut before scoring except where a cap is named (`DOMAIN_CAP`, `HOME_CAP`,
the kNN depth). The key lists are built in one fixed order (semantic, labels, domain, time, kinds, places, people,
then the `latest` time list pooled over those), then the message, domain, people and home lists of
`candidate_lists`. The kNN lists arrive from the store as ids; every other list is set membership over the snapshot,
ordered on its whole candidate set (the order feeds the caps and the log, not the ranking).
"""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import (TYPE_CHECKING, AbstractSet, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set,
                    Tuple)

from .drive_key_forms import primary as form_primary
from .drive_key_forms import skeleton_key, token_spans
from .fact_query import SCOPE_ALL, FactQuery
from .fact_search_snapshot import FactSearchSnapshot, Interval, SnapshotRow, name_skeleton

if TYPE_CHECKING:
    from .fact_search_rank import CardRef, HomePlace, QueryPlaceKeys

# kNN depth of the semantic, labels and message lists; caps of the domain and home lists; the cut after scoring.
DEPTH_KNN, DOMAIN_CAP, HOME_CAP, CUT = 60, 100, 60, 30
RELATION_WORDS = frozenset({
    "wife", "husband", "spouse", "son", "daughter", "mother", "father", "mom", "mum", "dad", "brother", "sister",
    "sibling", "parent", "child", "children", "kid", "partner", "girlfriend", "boyfriend", "fiance", "fiancee",
    "grandmother", "grandfather", "grandma", "grandpa", "grandson", "granddaughter", "aunt", "uncle", "cousin", "niece",
    "nephew", "stepson", "stepdaughter", "ex-wife", "ex-husband", "ex-partner", "former",
})
REL_PRIMARY = frozenset(form_primary(word) for word in RELATION_WORDS)
_FILLER_WORDS = ("my", "the", "of", "s")
_NO_GAP = 10 ** 6
_STATE_ROLES = ("state", "validity")
_EVENT_ROLES = ("event", "measurement")
_YEAR_RANGE = range(1, 10000)

Window = Tuple[date, date]


@dataclass(frozen=True)
class SearchOutcome:
    """`lists`: the candidate lists after the gate, in insertion order; `gate_dropped`: entries the gate removed;
    `uncut`: for a list a cap (`DOMAIN_CAP`, `HOME_CAP`) cut, its size before the cut;
    `fused`: the candidates (the `exclude`d ones removed) in scored order before the duplicate pass; `kept`: ids after it
    (cut to 30 for scope top); `scores`: the score of every candidate."""
    lists: Dict[str, List[str]]
    gate_dropped: int
    uncut: Dict[str, int]
    fused: List[str]
    kept: List[str]
    scores: Dict[str, float]


# ------------------------------------------------------------------------------------------------ time

def window(query: FactQuery) -> Optional[Window]:
    """The span the question aims at, by its time intent; None when it aims at none."""
    start, end, today = query.time.start, query.time.end, query.today
    intent = query.time.intent
    if intent == "future":
        first = today if start is None or start < today else start
        return (first, end if end is not None and end >= first else first + timedelta(days=365))
    if intent == "past":
        last = min(end or today, today)
        return (start if start is not None and start <= last else last - timedelta(days=365), last)
    if intent == "period":
        if start is not None and end is not None and start <= end:
            return (start, end)
        if start is not None:
            return (start, start + timedelta(days=31))
        if end is not None:
            return (end - timedelta(days=31), end)
    return None


def gap(interval: Interval, span: Window) -> int:
    """Days between `interval` and `span` (0 when they overlap)."""
    first, last = interval
    if last < span[0]:
        return (span[0] - last).days
    if first > span[1]:
        return (first - span[1]).days
    return 0


def _shift(day: date, year: int) -> date:
    try:
        return day.replace(year=year)
    except ValueError:      # Feb 29
        return day.replace(year=year, day=28)


def gap_recurring(interval: Interval, span: Window) -> int:
    """A recurring occasion matches by month and day whatever the year: a span of up to 31 days is projected into every
    year the window touches and its neighbours."""
    first, last = interval
    if (last - first).days > 31:
        return gap(interval, span)
    best = gap(interval, span)
    for year in range(span[0].year - 1, span[1].year + 2):
        if year not in _YEAR_RANGE:
            continue
        shifted = _shift(first, year)
        best = min(best, gap((shifted, shifted + (last - first)), span))
    return best


# ------------------------------------------------------------------------------------------------ people

def person_targets(people: Sequence[str]) -> Tuple[List[FrozenSet[str]], Set[str]]:
    """What the people slots look for: the name skeletons (a name, or the non-relation words of a relation phrase) and
    the relation words (primary forms)."""
    targets: List[FrozenSet[str]] = []
    relations: Set[str] = set()
    for person in people:
        tokens = [token for token, _, _ in token_spans(person)]
        found = {form_primary(token) for token in tokens} & REL_PRIMARY
        if found:
            relations |= found
            rest = frozenset(
                sk for token in tokens
                if form_primary(token) not in REL_PRIMARY and token not in _FILLER_WORDS
                and (sk := skeleton_key(token)))
            if rest:
                targets.append(rest)
        else:
            skeleton = name_skeleton(person)
            if skeleton:
                targets.append(skeleton)
    return targets, relations


# ------------------------------------------------------------------------------------------------ one search

class _Search:
    def __init__(self, snapshot: FactSearchSnapshot, query: FactQuery, similarity: Mapping[str, float]) -> None:
        self._rows = snapshot.rows
        self._ids = snapshot.ids
        self._query = query
        self._sim = similarity
        self._window = window(query)

    def _gap(self, row: SnapshotRow, interval: Interval, span: Window) -> int:
        return gap_recurring(interval, span) if row.recurring else gap(interval, span)

    def _similarity(self, fact_id: str) -> float:
        return float(self._sim.get(fact_id, 0.0))

    @staticmethod
    def _newest(row: SnapshotRow) -> date:
        return max(end for _, end in row.intervals) if row.intervals else row.reported

    @staticmethod
    def _now_distance(row: SnapshotRow, today: date) -> int:
        intervals = row.intervals or ((row.reported, row.reported),)
        return min(gap(interval, (today, today)) for interval in intervals)

    def order(self, candidates: Sequence[str]) -> List[str]:
        """The order inside a key list: with a window, by distance to it; `latest`, newest first; otherwise by time role,
        interleaved: state and validity newest first, event and measurement nearest today, the timeless by similarity."""
        rows, span = self._rows, self._window

        def by_meaning(fact_id: str) -> float:
            return -self._similarity(fact_id)

        if span is not None:
            return sorted(candidates, key=lambda i: (
                min((self._gap(rows[i], x, span) for x in rows[i].intervals), default=_NO_GAP), by_meaning(i)))
        if self._query.time.intent == "latest":
            return sorted(candidates, key=lambda i: (-self._newest(rows[i]).toordinal(), by_meaning(i)))
        states = sorted((i for i in candidates if rows[i].time_role in _STATE_ROLES),
                        key=lambda i: (-self._newest(rows[i]).toordinal(), by_meaning(i)))
        events = sorted((i for i in candidates if rows[i].time_role in _EVENT_ROLES),
                        key=lambda i: (self._now_distance(rows[i], self._query.today),
                                       -self._newest(rows[i]).toordinal(), by_meaning(i)))
        timeless = sorted((i for i in candidates if rows[i].time_role not in _STATE_ROLES + _EVENT_ROLES),
                          key=by_meaning)
        out: List[str] = []
        for k in range(max(len(states), len(events), len(timeless))):
            for group in (states, events, timeless):
                if k < len(group):
                    out.append(group[k])
        return out

    def person_match(self) -> List[str]:
        """Facts naming a person of the query: a relation word matches mentions whose role carries it; a name (and the
        non-relation words of a relation phrase) matches by skeleton subset, either way round."""
        targets, relations = person_targets(self._query.people)
        hits = []
        for fact_id in self._ids:
            for names, roles in self._rows[fact_id].people:
                if (roles & relations) or any(names and t and (t <= names or names <= t) for t in targets):
                    hits.append(fact_id)
                    break
        return hits

    def lists(self, semantic_ids: Optional[Sequence[str]], label_ids: Optional[Sequence[str]],
              place_keys: FrozenSet[str]) -> Dict[str, List[str]]:
        rows, query = self._rows, self._query
        lists: Dict[str, List[str]] = {}
        if semantic_ids is not None:
            lists["semantic"] = list(semantic_ids)
        if label_ids is not None:
            lists["labels"] = list(label_ids)
        domains = [d for d in query.domains if d][:2]
        people = self.person_match() if query.people else []
        if domains:
            wanted = set(domains)
            candidates = [i for i in self._ids if rows[i].domains & wanted]
            if len(domains) == 2:
                both = [i for i in candidates if wanted <= rows[i].domains]
                candidates = both or candidates
            narrowed = [i for i in people if rows[i].domains & wanted]
            if narrowed:
                people = narrowed
            lists["domain"] = self.order(candidates)
        span = self._window
        if span is not None:
            lists["time"] = self.order([
                i for i in self._ids if any(self._gap(rows[i], x, span) == 0 for x in rows[i].intervals)])
        if query.kinds:
            wanted_kinds = set(query.kinds)
            candidates = [i for i in self._ids if wanted_kinds & rows[i].kinds]
            if candidates:
                lists["kinds"] = self.order(candidates)
        if query.places and place_keys:
            candidates = [i for i in self._ids if rows[i].place_keys & place_keys]
            if candidates:
                lists["places"] = self.order(candidates)
        if people:
            lists["people"] = self.order(people)
        if query.time.intent == "latest" and lists:
            pooled = list(dict.fromkeys(i for members in lists.values() for i in members))
            dated = [i for i in pooled if rows[i].intervals]
            lists["time"] = sorted(dated, key=lambda i: (-self._newest(rows[i]).toordinal(),
                                                         -self._similarity(i)))
        return lists

    def gate(self, lists: Dict[str, List[str]]) -> Tuple[Dict[str, List[str]], int]:
        """An intimate fact stays only in the labels list and its own domain list; the exception is collecting all facts
        about a person."""
        if self._query.scope == SCOPE_ALL and self._query.people:
            return lists, 0
        kept = {name: [i for i in members if self._rows[i].sensitivity != "intimate" or name in ("labels", "domain")]
                for name, members in lists.items()}
        dropped = sum(len(lists[name]) - len(kept[name]) for name in lists)
        return kept, dropped


def candidate_lists(
    snapshot: FactSearchSnapshot,
    query: FactQuery,
    *,
    semantic_ids: Optional[Sequence[str]],
    label_ids: Optional[Sequence[str]],
    message_ids: Optional[Sequence[str]],
    similarity: Mapping[str, float],
    query_place_keys: FrozenSet[str],
    home_keys: AbstractSet[str],
) -> Tuple[Dict[str, List[str]], int, Dict[str, int]]:
    """The gated candidate lists of one query, the count the gate dropped and, per list a cap cut, its size before the
    cut.

    The key lists come whole (the `latest` time list is pooled over them before the lists below join). Then: `message`
    = `message_ids` when given; `domain` rebuilt when the query names domains (two domains: the facts carrying both,
    then the facts carrying either, each in-list order; capped at `DOMAIN_CAP`); `people` = every fact naming a queried
    person, not narrowed by the domains; `home` = the facts at the owner's home town when the query names no place
    (capped at `HOME_CAP`). The intimate gate runs over every list."""
    engine = _Search(snapshot, query, similarity)
    rows = snapshot.rows
    lists = engine.lists(semantic_ids, label_ids, query_place_keys)
    if message_ids is not None:
        lists["message"] = list(message_ids)
    uncut: Dict[str, int] = {}
    domains = [d for d in query.domains if d][:2]
    if domains:
        wanted = set(domains)
        both = [i for i in snapshot.ids if wanted <= rows[i].domains] if len(domains) == 2 else []
        in_both = set(both)
        either = [i for i in snapshot.ids if rows[i].domains & wanted and i not in in_both]
        ordered = engine.order(both) + engine.order(either)
        lists["domain"] = ordered[:DOMAIN_CAP]
        if len(ordered) > DOMAIN_CAP:
            uncut["domain"] = len(ordered)
    if query.people:
        people = engine.person_match()
        if people:
            lists["people"] = engine.order(people)
    if not query.places and home_keys:
        at_home = [i for i in snapshot.ids if rows[i].place_keys & home_keys]
        if at_home:
            ordered_home = engine.order(at_home)
            lists["home"] = ordered_home[:HOME_CAP]
            if len(ordered_home) > HOME_CAP:
                uncut["home"] = len(ordered_home)
    gated, dropped = engine.gate(lists)
    return gated, dropped, uncut


def keep_distinct(order: Sequence[str], is_duplicate: Callable[[str, str], bool], limit: Optional[int]) -> List[str]:
    """The near-duplicate pass: walk `order`, keep a fact unless `is_duplicate(fact, kept)` holds for one already kept;
    stop at `limit` kept (None = no cut)."""
    kept: List[str] = []
    for fact_id in order:
        if limit is not None and len(kept) >= limit:
            break
        if not any(is_duplicate(fact_id, other) for other in kept):
            kept.append(fact_id)
    return kept


def run_search(
    snapshot: FactSearchSnapshot,
    query: FactQuery,
    *,
    semantic_ids: Optional[Sequence[str]],
    label_ids: Optional[Sequence[str]],
    similarity: Mapping[str, float],
    query_place_keys: FrozenSet[str],
    is_duplicate: Callable[[str, str], bool],
    message_ids: Optional[Sequence[str]] = None,
    label_similarity: Optional[Mapping[str, float]] = None,
    message_similarity: Optional[Mapping[str, float]] = None,
    query_places: Sequence["QueryPlaceKeys"] = (),
    home: Optional["HomePlace"] = None,
    cards: Sequence["CardRef"] = (),
    exclude: AbstractSet[str] = frozenset(),
    weights: Optional[Mapping[str, float]] = None,
) -> SearchOutcome:
    """One search over `snapshot`. `semantic_ids`/`label_ids`/`message_ids` are the kNN lists already cut to depth and
    limited to the snapshot (None = the query has no such text, so no list); `similarity`, `label_similarity` and
    `message_similarity` are the subject, kind and message similarity per fact id (a missing id reads 0.0);
    `query_place_keys` are the place keys the query's places reach and `query_places` the same per place;
    `home` the owner's resolved home town; `cards` the account's people cards; `exclude` ids that never become
    candidates; `weights` override the fitted ones (None = `WEIGHTS`). `is_duplicate(candidate, kept)` is asked for
    each candidate against every fact already kept. Candidates are ordered by score, ties by id."""
    # Imported here: the scorer reads this module's time and people rules, so a module-level import would be a cycle.
    from .fact_search_rank import WEIGHTS, ScoreInputs, score_rows

    lists, dropped, uncut = candidate_lists(
        snapshot, query, semantic_ids=semantic_ids, label_ids=label_ids, message_ids=message_ids,
        similarity=similarity, query_place_keys=query_place_keys,
        home_keys=home.keys if home is not None else frozenset(),
    )
    candidates = [i for i in dict.fromkeys(i for members in lists.values() for i in members) if i not in exclude]
    inputs = ScoreInputs(sim_text=similarity, sim_label=label_similarity or {}, sim_message=message_similarity or {},
                         places=tuple(query_places), home=home, cards=tuple(cards))
    scores = score_rows(query, [snapshot.rows[i] for i in candidates], inputs, WEIGHTS if weights is None else weights)
    fused = sorted(candidates, key=lambda i: (-scores[i], i))
    kept = keep_distinct(fused, is_duplicate, None if query.scope == SCOPE_ALL else CUT)
    return SearchOutcome(lists=lists, gate_dropped=dropped, uncut=uncut, fused=fused, kept=kept, scores=scores)
