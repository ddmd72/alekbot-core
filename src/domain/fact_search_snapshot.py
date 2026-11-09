"""The account snapshot the fact search runs over (fact profile RFC §5.2): per current fact, the data the search lists
read, derived once from the stored metadata and profile. Rows are sorted by fact id, so every list that orders by a
tie is deterministic. An unprofiled fact (profile fields None) is a row with empty sets.

`SnapshotRow` and `FactSearchSnapshot` are one value, so they share this module on purpose.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple

from .drive_key_forms import primary as form_primary
from .drive_key_forms import skeleton_key, token_spans
from .entities import FactEntity

# A label phrase containing one of these marks an occasion that comes back every year (RFC OD-7).
RECURRING_WORDS = ("birthday", "anniversary")

Interval = Tuple[date, date]


def name_skeleton(name: str) -> FrozenSet[str]:
    """The skeleton of every word of a name (cross-script: "Maria" and "Марія" share one)."""
    return frozenset(sk for token, _, _ in token_spans(name) if (sk := skeleton_key(token)))


def time_interval(point: Mapping[str, Any]) -> Interval:
    """A stored time point as (first day, last day) at its precision; ValueError when it is malformed."""
    precision = point.get("precision")
    start = str(point.get("from") or "")
    end = str(point.get("to") or "") or start

    def low(value: str) -> date:
        if precision == "day":
            return date.fromisoformat(value)
        if precision == "month":
            return date.fromisoformat(value + "-01")
        return date(int(value[:4]), 1, 1)

    def high(value: str) -> date:
        if precision == "day":
            return date.fromisoformat(value)
        if precision == "month":
            year, month = int(value[:4]), int(value[5:7])
            return date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
        return date(int(value[:4]), 12, 31)

    first = low(start)
    return (first, max(first, high(end)))


@dataclass(frozen=True)
class SnapshotRow:
    id: str
    text: str
    domains: FrozenSet[str]
    intervals: Tuple[Interval, ...]
    reported: date
    people: Tuple[Tuple[FrozenSet[str], FrozenSet[str]], ...]   # per mention: (name skeletons, role primary forms)
    recurring: bool
    place_keys: FrozenSet[str]
    kinds: FrozenSet[str]
    sensitivity: Optional[str]
    time_role: Optional[str]


def _row(fact: FactEntity) -> SnapshotRow:
    intervals = []
    for point in fact.time_points or []:
        try:
            intervals.append(time_interval(point))
        except ValueError:
            continue
    people = tuple(
        (name_skeleton(entry.get("name") or ""),
         frozenset(form_primary(token) for token, _, _ in token_spans(entry.get("role") or "")))
        for entry in fact.people or []
    )
    recurring = any(word in phrase for phrase in fact.label_words or [] for word in RECURRING_WORDS)
    return SnapshotRow(
        id=fact.id, text=fact.text, domains=frozenset(fact.domain_keys or ()), intervals=tuple(intervals),
        reported=fact.reported_date.date(), people=people, recurring=recurring,
        place_keys=frozenset(fact.place_keys or ()), kinds=frozenset(fact.kinds or ()),
        sensitivity=fact.sensitivity, time_role=fact.time_role,
    )


class FactSearchSnapshot:
    """The current facts of one account, as search rows (sorted by id) beside the entities they came from."""

    def __init__(self, facts: Sequence[FactEntity]) -> None:
        ordered = sorted(facts, key=lambda fact: fact.id)
        self.entities: Dict[str, FactEntity] = {fact.id: fact for fact in ordered}
        self.rows: Dict[str, SnapshotRow] = {fact.id: _row(fact) for fact in ordered}
        self.ids: Tuple[str, ...] = tuple(self.entities)

    @classmethod
    def from_facts(cls, facts: Sequence[FactEntity]) -> "FactSearchSnapshot":
        return cls(facts)

    def __len__(self) -> int:
        return len(self.ids)
