"""Place names to atlas rows (fact profile RFC §2.1, §4.4): the city and country a fact's profile names, resolved over
the bundled place table (`place_lookup.PlaceRow`, loaded by `adapters/geo_place_table.py` and passed in).

A city resolves only when exactly one row of the named country carries that name (folded: case, accents, hyphens,
apostrophes and dots ignored); its keys are `pl:<geonameid>`, `pl:<large neighbour's geonameid>` and `co:<country>`.
A country alone gives `co:<country>`. Anything else is unresolved, with one reason: unknown_country, empty,
not_in_country, not_in_atlas or ambiguous.
"""
import hashlib
import re
import unicodedata
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .place_lookup import PlaceRow

# Short forms the table's country names lack.
_COUNTRY_ALIASES = (("usa", "United States"), ("us", "United States"), ("uk", "United Kingdom"))


def fold(name: str) -> str:
    """Casefolded, accents stripped, hyphens, apostrophes and dots read as spaces, spaces collapsed."""
    folded = unicodedata.normalize("NFKD", name.casefold())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[-‐–'’.]", " ", folded).split())


def country_key(country: str) -> str:
    return "co:" + re.sub(r"[^a-z0-9]+", "_", fold(country)).strip("_")


def atlas_digest(rows: Sequence[PlaceRow]) -> str:
    """Digest of everything resolution reads from the table: any change to a row's names, country, weight or large
    neighbour changes it."""
    h = hashlib.sha256()
    for row in rows:
        large = row.large.geonameid if row.large is not None else ""
        h.update("\x1f".join([row.geonameid, row.name_en, row.name_local, row.country, str(row.weight), large,
                              "|".join(row.alt_names), "|".join(row.country_names)]).encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()[:16]


# (keys, None) when resolved, else ((), the reason it is not)
PlaceResolution = Tuple[Tuple[str, ...], Optional[str]]


class PlaceNames:
    """The name index of the place table: one instance per process (it reads every row once)."""

    def __init__(self, rows: Sequence[PlaceRow]) -> None:
        self._by_name: Dict[str, Set[int]] = {}
        self._rows: List[PlaceRow] = list(rows)
        self._country_of: Dict[str, str] = {}
        for i, row in enumerate(self._rows):
            for name in {row.name_en, row.name_local, *row.alt_names}:
                if name:
                    self._by_name.setdefault(fold(name), set()).add(i)
            for name in (row.country, *row.country_names):
                if name:
                    self._country_of.setdefault(fold(name), row.country)
        for alias, country in _COUNTRY_ALIASES:
            if fold(country) in self._country_of:
                self._country_of.setdefault(alias, self._country_of[fold(country)])

    def resolve(self, city: str, country: str) -> PlaceResolution:
        named = self._country_of.get(fold(country)) if country.strip() else None
        if country.strip() and named is None:
            return ((), "unknown_country")
        if not city.strip():
            return ((country_key(named),), None) if named else ((), "empty")
        candidates = self._by_name.get(fold(city), set())
        if named:
            inside = {i for i in candidates if self._rows[i].country == named}
            if not inside:
                return ((), "not_in_country" if candidates else "not_in_atlas")
            candidates = inside
        if not candidates:
            return ((), "not_in_atlas")
        if len(candidates) > 1:
            return ((), "ambiguous")
        row = self._rows[next(iter(candidates))]
        keys = [f"pl:{row.geonameid}"]
        if row.large is not None:
            keys.append(f"pl:{row.large.geonameid}")
        keys.append(country_key(row.country))
        return (tuple(keys), None)

    def query_keys(self, name: str, country: str) -> Tuple[str, ...]:
        """The place keys a query's place reaches (fact profile RFC §5.3): a country alone gives its `co:` key (a country
        reaches every city carrying it); a city gives its own `pl:` key and its large neighbour's. A query key is one
        vote, not a stored identity, so an ambiguous city takes the heaviest row (a weight tie, the first row of the
        table). A name nobody knows gives no key."""
        city, named = fold(name or ""), fold(country or "")
        as_country = self._country_of.get(city)
        if (as_country is not None and (not named or self._country_of.get(named) == as_country)
                and city not in self._by_name):
            return (country_key(as_country),)
        inside = self._country_of.get(named)
        candidates = self._by_name.get(city, set())
        if inside:
            candidates = {i for i in candidates if self._rows[i].country == inside}
        if not candidates:
            return (country_key(as_country),) if as_country is not None else ()
        row = self._rows[max(candidates, key=lambda i: (self._rows[i].weight or 0, -i))]
        return (f"pl:{row.geonameid}",) + ((f"pl:{row.large.geonameid}",) if row.large is not None else ())
