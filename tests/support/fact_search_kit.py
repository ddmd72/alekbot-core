"""Builders for the fact search tests: profiled facts, queries, and one call of the pure engine (`run_search`)."""
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from src.domain.entities import FactEntity, FactType
from src.domain.fact_query import FactQuery, QueryPlace, QueryTime
from src.domain.fact_search_lists import SearchOutcome, run_search
from src.domain.fact_search_snapshot import FactSearchSnapshot

TODAY = date(2026, 11, 9)
ACC = "acc-1"


def point(start: str, end: str = "", precision: str = "day") -> Dict[str, str]:
    return {"from": start, "to": end, "precision": precision, "source": start}


def person(name: str = "", role: str = "") -> Dict[str, Any]:
    return {"name": name, "role": role, "name_keys": [], "role_keys": []}


def fact(fact_id: str, text: str = "", *, domains: Sequence[str] = ("health",), points: Sequence[Dict[str, str]] = (),
         role: Optional[str] = None, labels: Sequence[str] = (), people: Sequence[Dict[str, Any]] = (),
         places: Sequence[str] = (), kinds: Sequence[str] = (), sensitivity: Optional[str] = "ordinary",
         reported: date = date(2026, 1, 1), profiled: bool = True) -> FactEntity:
    """A current fact; `profiled=False` leaves every profile field None."""
    stamp = datetime(reported.year, reported.month, reported.day, tzinfo=timezone.utc)
    profile: Dict[str, Any] = {}
    if profiled:
        profile = dict(label_words=list(labels), time_role=role, time_points=list(points), place_keys=list(places),
                       people=list(people), primary_domain=domains[0] if domains else None, domain_keys=list(domains),
                       kinds=list(kinds), sensitivity=sensitivity)
    return FactEntity(id=fact_id, account_id=ACC, created_by_user_id="usr-1", lineage_id=f"lin-{fact_id}",
                      text=text or fact_id, type=FactType.EVENT, reported_date=stamp, created_at=stamp, **profile)


def query(*, intent: str = "none", start: Optional[date] = None, end: Optional[date] = None, subject: str = "",
          kind: str = "", people: Sequence[str] = (), places: Sequence[Tuple[str, str]] = (),
          domains: Sequence[str] = (), kinds: Sequence[str] = (), scope: str = "top", today: date = TODAY) -> FactQuery:
    return FactQuery(today=today, subject=subject, kind=kind, people=tuple(people),
                     places=tuple(QueryPlace(n, c) for n, c in places), time=QueryTime(intent, start, end),
                     domains=tuple(domains), kinds=tuple(kinds), scope=scope, path="test")


def search(facts: Iterable[FactEntity], q: FactQuery, *, semantic: Optional[Sequence[str]] = None,
           labels: Optional[Sequence[str]] = None, sim: Optional[Dict[str, float]] = None,
           place_keys: FrozenSet[str] = frozenset(),
           duplicate: Optional[Callable[[str, str], bool]] = None) -> SearchOutcome:
    return run_search(FactSearchSnapshot.from_facts(list(facts)), q, semantic_ids=semantic, label_ids=labels,
                      similarity=sim or {}, query_place_keys=place_keys, is_duplicate=duplicate or (lambda a, b: False))


def ids(outcome: SearchOutcome, name: str) -> List[str]:
    return outcome.lists[name]
