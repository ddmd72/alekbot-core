"""Parsing the query step's JSON into a `FactQuery` (fact profile RFC §5.1).

The model writes the slots; code decides what survives. `parse_slots` never trusts a value: a slot outside its closed
set is dropped, a date that does not parse is dropped, a value in another script than English is refused (the prompt
asks for English; a Cyrillic value would only add noise to every list). Dropped and refused values are reported by slot
name so the caller can log them without logging what the user asked.

`ParsedSlots` and `parse_slots` live together because the result is the parser's own contract.
"""
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, List, Mapping, Optional, Tuple

from .fact_domains import FACT_DOMAINS
from .fact_profile_answer import PROFILE_KINDS
from .fact_query import SCOPE_ALL, SCOPE_TOP, FactQuery, QueryPlace, QueryTime

# The life areas a question can name: every domain that has a LIFE AREAS row (so not `agent_directives`).
QUERY_DOMAINS = tuple(e.key for e in FACT_DOMAINS if e.query_row is not None)
QUERY_INTENTS = ("none", "future", "past", "period", "latest")
MAX_QUERY_DOMAINS = 2
_CYRILLIC = re.compile("[Ѐ-ӿ]")


@dataclass(frozen=True)
class ParsedSlots:
    """The parsed query plus what the parser took out, each as a slot name (one entry per removed value)."""
    query: FactQuery
    refused: Tuple[str, ...]
    dropped: Tuple[str, ...]


def semantic_only_query(request: str, *, today: date, path: str) -> FactQuery:
    """The degraded query: the raw request as the one semantic list, nothing else."""
    return FactQuery(today=today, subject=request, path=path)


def parse_slots(raw: Mapping[str, Any], *, today: date, path: str) -> ParsedSlots:
    """The query the model's slots describe. A payload that is not a mapping is invalid output (`ValueError`)."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"query slots must be a JSON object, got {type(raw).__name__}")
    refused: List[str] = []
    dropped: List[str] = []

    def english(value: str, slot: str) -> bool:
        if _CYRILLIC.search(value):
            if slot not in refused:
                refused.append(slot)
            return False
        return True

    subject = _text(raw.get("subject"))
    if not english(subject, "subject"):
        subject = ""
    kind = _text(raw.get("kind"))
    if not english(kind, "kind"):
        kind = ""

    people = tuple(p for p in (_text(v) for v in _items(raw.get("people"))) if p and english(p, "people"))

    places: List[QueryPlace] = []
    for item in _items(raw.get("places")):
        if not isinstance(item, Mapping):
            continue
        name, country = _text(item.get("name")), _text(item.get("country"))
        if (name or country) and english(name, "places") and english(country, "places"):
            places.append(QueryPlace(name=name, country=country))

    time_raw = raw.get("time")
    time_raw = time_raw if isinstance(time_raw, Mapping) else {}
    intent = time_raw.get("intent")
    intent = intent if intent in QUERY_INTENTS else "none"
    start = _day(time_raw.get("from"), "from", dropped)
    end = _day(time_raw.get("to"), "to", dropped)

    domains_in = _distinct_strings(raw.get("domains"))
    known = [d for d in domains_in if d in QUERY_DOMAINS]
    domains = tuple(known[:MAX_QUERY_DOMAINS])
    dropped += ["domains"] * (len(domains_in) - len(domains))

    kinds_in = _distinct_strings(raw.get("kinds"))
    kinds = tuple(k for k in kinds_in if k in PROFILE_KINDS)
    dropped += ["kinds"] * (len(kinds_in) - len(kinds))

    # `domain_link` is read and ignored: two areas are always searched as two lists (RFC §12 item 3).
    query = FactQuery(
        today=today, subject=subject, kind=kind, people=people, places=tuple(places),
        time=QueryTime(intent=intent, start=start, end=end), domains=domains, kinds=kinds,
        scope=SCOPE_ALL if raw.get("scope") == SCOPE_ALL else SCOPE_TOP, path=path,
    )
    return ParsedSlots(query=query, refused=tuple(refused), dropped=tuple(dropped))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _items(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _distinct_strings(value: Any) -> List[str]:
    return list(dict.fromkeys(v for v in _items(value) if isinstance(v, str)))


def _day(value: Any, slot: str, dropped: List[str]) -> Optional[date]:
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        dropped.append(slot)
        return None
