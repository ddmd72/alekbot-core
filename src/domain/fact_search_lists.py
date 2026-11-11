"""The fact search engine (fact profile RFC §5.3, list mode RFC), pure over an account snapshot. A phrase runs one of two
algorithms:

- `top` (`run_search`): the candidate lists a query asks for, the intimate gate, the scored order
  (`fact_search_rank.py`) and the near-duplicate pass, cut to `CUT`.
- the set of an `all` phrase that names a structural key (`is_set_phrase`, `run_set`): a boolean filter over the
  snapshot (OR within a key, AND across keys, a time window counts as a key), its own gate, ordered by closeness (or
  newest first under `latest`), the near-duplicate pass with no limit. No kNN list, no home list, no cap, no scorer.
  An `all` phrase without such a key runs `top` (list mode RFC §3.4); `run_search` dispatches.

The `top` lists only generate candidates; none is cut before scoring except where a cap is named (`DOMAIN_CAP`,
`HOME_CAP`, the kNN depth). The key lists are built in one fixed order (semantic, labels, domain, time, kinds, places,
people, then the `latest` time list pooled over those), then the message, domain, people and home lists of
`candidate_lists`. The kNN lists arrive from the store as ids; every other list is set membership over the snapshot,
ordered on its whole candidate set (the order feeds the caps and the log, not the ranking). The two capped lists
(domain, home) are ordered by closeness, so a cap drops the facts farthest from the phrase.

Closeness is the subject similarity, else (no subject: an empty map) the message similarity.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import (TYPE_CHECKING, AbstractSet, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set,
                    Tuple)

from .drive_key_forms import primary as form_primary
from .drive_key_forms import skeleton_key, token_spans
from .fact_query import SCOPE_ALL, SCOPE_TOP, FactQuery
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
    (cut to 30 for `top`); `scores`: the score of every candidate.
    `mode`: `top` or `all` (the algorithm that ran: an `all` phrase without a structural key ran `top`). For `all`,
    `lists` holds the one list `set` (the gated set in its order), `scores` the closeness of each member, `fused` the set
    without the `exclude`d ids, `kept` all of it after the duplicate pass, and `keys` the keys that defined the set
    (`set_keys`)."""
    lists: Dict[str, List[str]]
    gate_dropped: int
    uncut: Dict[str, int]
    fused: List[str]
    kept: List[str]
    scores: Dict[str, float]
    mode: str = SCOPE_TOP
    keys: Tuple[str, ...] = ()


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


# ------------------------------------------------------------------------------------------------ the set of an all phrase

def query_domains(query: FactQuery) -> List[str]:
    """The domains a query names: the non-blank ones, the first two."""
    return [d for d in query.domains if d][:2]


def _query_kinds(query: FactQuery) -> FrozenSet[str]:
    return frozenset(k for k in query.kinds if k)


def set_keys(query: FactQuery) -> Tuple[str, ...]:
    """The structural keys a phrase names (list mode RFC §3.1), in a fixed order: `domains`, `kinds`, `people`, `places`
    (a named place, whether or not the atlas resolves it), `time` (a `period`, `future` or `past` window). `latest` is
    no key: it only orders."""
    named = (("domains", bool(query_domains(query))), ("kinds", bool(_query_kinds(query))),
             ("people", bool(query.people)), ("places", bool(query.places)), ("time", window(query) is not None))
    return tuple(name for name, present in named if present)


def is_set_phrase(query: FactQuery) -> bool:
    """An `all` phrase that names a structural key: it returns the set its keys define (list mode RFC §3). An `all`
    phrase without one runs `top` (§3.4)."""
    return query.scope == SCOPE_ALL and bool(set_keys(query))


def closeness(similarity: Mapping[str, float], message_similarity: Optional[Mapping[str, float]]) -> Mapping[str, float]:
    """Closeness to the phrase (list mode RFC §3.2): the subject similarity, else the message similarity. The subject
    similarity is empty exactly when the phrase has no subject (or no fact has a vector)."""
    return similarity if similarity else (message_similarity or {})


# ------------------------------------------------------------------------------------------------ one search

class _Search:
    def __init__(self, snapshot: FactSearchSnapshot, query: FactQuery, similarity: Mapping[str, float],
                 close: Optional[Mapping[str, float]] = None) -> None:
        self._rows = snapshot.rows
        self._ids = snapshot.ids
        self._query = query
        self._sim = similarity
        self._close = similarity if close is None else close
        self._window = window(query)

    def closest(self, candidates: Sequence[str]) -> List[str]:
        """`candidates` by closeness to the phrase, nearest first (ties keep their order)."""
        return sorted(candidates, key=lambda i: -float(self._close.get(i, 0.0)))

    def set_members(self, place_keys: FrozenSet[str]) -> List[str]:
        """The facts every key of the phrase matches (list mode RFC §3.1), in snapshot order: OR within a key, AND across
        keys. People match as `person_match`; a named place by the fact's place keys meeting `place_keys` (none: no fact
        matches); a window by a point inside it (a recurring occasion by month and day)."""
        rows, query, span = self._rows, self._query, self._window
        domains, kinds = frozenset(query_domains(query)), _query_kinds(query)
        people = frozenset(self.person_match()) if query.people else None

        def member(fact_id: str) -> bool:
            row = rows[fact_id]
            return ((not domains or bool(row.domains & domains))
                    and (not kinds or bool(row.kinds & kinds))
                    and (people is None or fact_id in people)
                    and (not query.places or bool(row.place_keys & place_keys))
                    and (span is None or any(self._gap(row, x, span) == 0 for x in row.intervals)))

        return [i for i in self._ids if member(i)]

    def set_gate(self, members: Sequence[str]) -> Tuple[List[str], int]:
        """The intimate gate of the set (list mode RFC §3.5): an intimate fact stays when the phrase names people, or when
        it carries a domain the phrase names; otherwise it is dropped."""
        if self._query.people:
            return list(members), 0
        domains = frozenset(query_domains(self._query))
        kept = [i for i in members if self._rows[i].sensitivity != "intimate" or self._rows[i].domains & domains]
        return kept, len(members) - len(kept)

    def set_order(self, members: Sequence[str]) -> List[str]:
        """The order of the set (list mode RFC §3.2): by closeness; under `latest`, newest point first, closeness breaking
        ties."""
        if self._query.time.intent == "latest":
            return sorted(members, key=lambda i: (-self._newest(self._rows[i]).toordinal(),
                                                  -float(self._close.get(i, 0.0))))
        return self.closest(members)

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
    message_similarity: Optional[Mapping[str, float]] = None,
) -> Tuple[Dict[str, List[str]], int, Dict[str, int]]:
    """The gated candidate lists of one `top` query, the count the gate dropped and, per list a cap cut, its size before
    the cut.

    The key lists come whole (the `latest` time list is pooled over them before the lists below join). Then: `message`
    = `message_ids` when given; `domain` rebuilt when the query names domains (two domains: the facts carrying both,
    then the facts carrying either, each group by closeness; capped at `DOMAIN_CAP`); `people` = every fact naming a
    queried person, not narrowed by the domains; `home` = the facts at the owner's home town when the query names no
    place (by closeness, capped at `HOME_CAP`). Closeness: `closeness(similarity, message_similarity)`. The intimate
    gate runs over every list."""
    engine = _Search(snapshot, query, similarity, closeness(similarity, message_similarity))
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
        ordered = engine.closest(both) + engine.closest(either)
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
            ordered_home = engine.closest(at_home)
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


def keep_within_cut(order: Sequence[str], cut: int, lifted: AbstractSet[str]) -> List[str]:
    """The facts of `order` at a position within `cut`, plus the ones in `lifted` (the own matches of the phrases whose
    scope lifts the cut) wherever they stand; every other fact past `cut` is dropped."""
    return [fact_id for position, fact_id in enumerate(order) if position < cut or fact_id in lifted]


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
    each candidate against every fact already kept. Candidates are ordered by score, ties by id.

    A set phrase (`is_set_phrase`) goes to `run_set`, which reads only `similarity`, `message_similarity`,
    `query_place_keys`, `is_duplicate` and `exclude`; any other `all` phrase runs `top` here, cut to `CUT`."""
    if is_set_phrase(query):
        return run_set(snapshot, query, similarity=similarity, message_similarity=message_similarity,
                       query_place_keys=query_place_keys, is_duplicate=is_duplicate, exclude=exclude)
    # Imported here: the scorer reads this module's time and people rules, so a module-level import would be a cycle.
    from .fact_search_rank import WEIGHTS, ScoreInputs, score_rows

    lists, dropped, uncut = candidate_lists(
        snapshot, query, semantic_ids=semantic_ids, label_ids=label_ids, message_ids=message_ids,
        similarity=similarity, query_place_keys=query_place_keys,
        home_keys=home.keys if home is not None else frozenset(), message_similarity=message_similarity,
    )
    candidates = [i for i in dict.fromkeys(i for members in lists.values() for i in members) if i not in exclude]
    inputs = ScoreInputs(sim_text=similarity, sim_label=label_similarity or {}, sim_message=message_similarity or {},
                         places=tuple(query_places), home=home, cards=tuple(cards))
    scores = score_rows(query, [snapshot.rows[i] for i in candidates], inputs, WEIGHTS if weights is None else weights)
    fused = sorted(candidates, key=lambda i: (-scores[i], i))
    kept = keep_distinct(fused, is_duplicate, CUT)
    return SearchOutcome(lists=lists, gate_dropped=dropped, uncut=uncut, fused=fused, kept=kept, scores=scores)


def run_set(
    snapshot: FactSearchSnapshot,
    query: FactQuery,
    *,
    similarity: Mapping[str, float],
    query_place_keys: FrozenSet[str],
    is_duplicate: Callable[[str, str], bool],
    message_similarity: Optional[Mapping[str, float]] = None,
    exclude: AbstractSet[str] = frozenset(),
) -> SearchOutcome:
    """The set of an `all` phrase (list mode RFC §3): every fact its structural keys match (`_Search.set_members`), the
    gate of §3.5, ordered by closeness or, under `latest`, newest first (§3.2), then the duplicate pass with no limit.
    Arguments as `run_search`; the phrase must name a key (`set_keys`)."""
    close = closeness(similarity, message_similarity)
    engine = _Search(snapshot, query, similarity, close)
    members, dropped = engine.set_gate(engine.set_members(query_place_keys))
    ordered = engine.set_order(members)
    fused = [i for i in ordered if i not in exclude]
    kept = keep_distinct(fused, is_duplicate, None)
    return SearchOutcome(lists={"set": ordered}, gate_dropped=dropped, uncut={}, fused=fused, kept=kept,
                         scores={i: float(close.get(i, 0.0)) for i in fused}, mode=SCOPE_ALL, keys=set_keys(query))
