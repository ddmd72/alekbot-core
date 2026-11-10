"""A fact search query (fact profile RFC §5.1): the slots the query step extracted, as one value `FactSearchPort.search`
takes. No parsing here; the slots are already checked.

`FactQuery` and its two parts (`QueryPlace`, `QueryTime`) are one value, so they share this module on purpose.
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Tuple

SCOPE_TOP = "top"
SCOPE_ALL = "all"


@dataclass(frozen=True)
class QueryPlace:
    """A place the question names: a city, a country, or both ("" = not named)."""
    name: str = ""
    country: str = ""


@dataclass(frozen=True)
class QueryTime:
    """The time the question aims at. `intent` is none, future, past, period or latest; `start`/`end` are the stated
    ends (None = not stated or not a valid date)."""
    intent: str = "none"
    start: Optional[date] = None
    end: Optional[date] = None


@dataclass(frozen=True)
class FactQuery:
    """What to look for. `subject` and `kind` are free text to embed (each may be empty); `people`, `places`, `domains`
    and `kinds` narrow by the stored profile; `scope` is `top` (the first 30) or `all` (collect every match);
    `today` is the day the question was asked; `path` names the caller (a log field).
    `message`: the whole text the slots were read from, embedded for the message similarity and the message list.
    `home`: the owner's location exactly as their settings store it, resolved by the search into home-town place keys."""
    today: date
    subject: str = ""
    kind: str = ""
    people: Tuple[str, ...] = ()
    places: Tuple[QueryPlace, ...] = ()
    time: QueryTime = field(default_factory=QueryTime)
    domains: Tuple[str, ...] = ()
    kinds: Tuple[str, ...] = ()
    scope: str = SCOPE_TOP
    path: str = ""
    message: str = ""
    home: str = ""
