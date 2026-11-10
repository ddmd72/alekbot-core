"""Place names to atlas rows (fact profile RFC §2.1, §4.4): the city and country a fact's profile names, resolved over
the bundled place table (`place_lookup.PlaceRow`, loaded by `adapters/geo_place_table.py` and passed in).

A city resolves only when exactly one row of the named country carries that name (folded: case, accents, hyphens,
apostrophes and dots ignored); its keys are `pl:<geonameid>`, `pl:<large neighbour's geonameid>` and `co:<country>`.
A country alone gives `co:<country>`. Anything else is unresolved, with one reason: unknown_country, empty,
not_in_country, not_in_atlas or ambiguous. A profile question (RFC §4.7) reads the rows behind that reason
(`candidates`) and an owner's answer the keys of the row it picked (`keys_of`); a place the atlas lacks that the owner
kept as written is `lp:<folded city>` (`local_place_key`). `resolve_home` reads the owner's free-text location into the
home town the fact search scores against (RFC §5.3).
"""
import hashlib
import re
import unicodedata
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .fact_search_rank import HomePlace
from .place_lookup import PlaceRow

# A name read as "<head> <connector> <rest>" is also known by its head ("Frankfurt am Main" as "Frankfurt"), and a name
# with a bracketed suffix by what precedes it ("Frankfurt (Oder)"): a fallback index, read only when no row carries the
# asked name itself.
_CONNECTORS = frozenset({"am", "an", "im", "in", "bei", "de", "del", "di", "da", "sur", "sous", "upon", "on", "nad",
                         "pod", "ob", "lez", "les", "en", "au", "aux", "sul", "sulla", "della", "dei", "dell"})


def _short_name(name: str) -> str:
    """The head of a long name ("" when it has none); `name` is folded."""
    head = re.sub(r"\s*\(.*$", "", name)
    tokens = head.split()
    if len(tokens) >= 2 and tokens[1] in _CONNECTORS:
        return tokens[0]
    return head if head != name else ""


# Short forms the table's country names lack.
_COUNTRY_ALIASES = (("usa", "United States"), ("us", "United States"), ("uk", "United Kingdom"))


def fold(name: str) -> str:
    """Casefolded, accents stripped, hyphens, apostrophes and dots read as spaces, spaces collapsed."""
    folded = unicodedata.normalize("NFKD", name.casefold())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[-‐–'’.]", " ", folded).split())


def country_key(country: str) -> str:
    return "co:" + re.sub(r"[^a-z0-9]+", "_", fold(country)).strip("_")


def local_place_key(city: str) -> str:
    """The key of a place the owner confirmed that the atlas lacks (fact profile RFC §4.7, §5.3): `lp:` and the folded
    city, spaces as underscores. Written on the fact by the derivation and added by the query side for a named city."""
    folded = fold(city)
    if not folded:
        raise ValueError("a local place needs a city")
    return "lp:" + folded.replace(" ", "_")


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
        self._by_short: Dict[str, Set[int]] = {}
        self._rows: List[PlaceRow] = list(rows)
        self._country_of: Dict[str, str] = {}
        self._by_id: Dict[str, int] = {}
        for i, row in enumerate(self._rows):
            if row.geonameid:
                self._by_id.setdefault(row.geonameid, i)
            for name in {row.name_en, row.name_local, *row.alt_names}:
                if name:
                    self._by_name.setdefault(fold(name), set()).add(i)
            for name in {row.name_en, row.name_local, *row.alt_names}:
                short = _short_name(fold(name)) if name else ""
                if short:
                    self._by_short.setdefault(short, set()).add(i)
            for name in (row.country, *row.country_names):
                if name:
                    self._country_of.setdefault(fold(name), row.country)
        for alias, country in _COUNTRY_ALIASES:
            if fold(country) in self._country_of:
                self._country_of.setdefault(alias, self._country_of[fold(country)])

    def resolve(self, city: str, country: str) -> PlaceResolution:
        named, reason, rows = self._match(city, country)
        if reason is not None:
            return ((), reason)
        if not rows:
            return ((country_key(named),), None) if named else ((), "empty")
        return (self._keys(self._rows[next(iter(rows))]), None)

    def candidates(self, city: str, country: str) -> Tuple[Optional[str], Tuple[PlaceRow, ...]]:
        """The rows a profile question offers for a place (fact profile RFC §4.7): `(None, (row,))` when `resolve`
        resolves the city (`(None, ())` for a country alone); `("ambiguous", rows)` the rows it could not pick between;
        `("not_in_country", rows)` the rows carrying the name in any country (by name, else by short name); any other
        reason with no rows. Rows heaviest first (a weight tie keeps table order)."""
        _, reason, rows = self._match(city, country)
        if reason not in (None, "ambiguous", "not_in_country"):
            return (reason, ())
        ordered = sorted(rows, key=lambda i: (-(self._rows[i].weight or 0), i))
        return (reason, tuple(self._rows[i] for i in ordered))

    def keys_of(self, geonameid: str) -> Tuple[str, ...]:
        """The keys `resolve` gives the row with this geonameid (`pl:`, its large neighbour's `pl:`, `co:`); `()` when
        no row of the table has it (the row left the atlas)."""
        index = self._by_id.get(geonameid) if geonameid else None
        return self._keys(self._rows[index]) if index is not None else ()

    def canonical_country(self, country: str) -> Optional[str]:
        """The table's name of a country given by any of its names or short forms; None when the table lacks it."""
        return self._country_of.get(fold(country))

    @staticmethod
    def _keys(row: PlaceRow) -> Tuple[str, ...]:
        keys = [f"pl:{row.geonameid}"]
        if row.large is not None:
            keys.append(f"pl:{row.large.geonameid}")
        keys.append(country_key(row.country))
        return tuple(keys)

    def _match(self, city: str, country: str) -> Tuple[Optional[str], Optional[str], Set[int]]:
        """(the named country, the reason it is not resolved, the rows): reason None with one row resolves the city,
        reason None with no rows is a country alone; `ambiguous` carries the rows of the name it could not pick between,
        `not_in_country` the rows carrying the name in other countries (by name, else by short name); any other reason
        carries no rows."""
        named = self._country_of.get(fold(country)) if country.strip() else None
        if country.strip() and named is None:
            return (None, "unknown_country", set())
        if not city.strip():
            return (named, None, set()) if named else (None, "empty", set())
        candidates = self._by_name.get(fold(city), set())
        shorts = self._by_short.get(fold(city), set())
        if named:
            inside = {i for i in candidates if self._rows[i].country == named}
            if not inside:
                inside = {i for i in shorts if self._rows[i].country == named}
            if not inside:
                if candidates or shorts:
                    return (named, "not_in_country", set(candidates or shorts))
                return (named, "not_in_atlas", set())
            candidates = inside
        elif not candidates:
            candidates = shorts
        if not candidates:
            return (named, "not_in_atlas", set())
        if len(candidates) > 1:
            return (named, "ambiguous", set(candidates))
        return (named, None, set(candidates))

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
            candidates = {i for i in self._by_short.get(city, set()) if not inside or self._rows[i].country == inside}
        if not candidates:
            return (country_key(as_country),) if as_country is not None else ()
        row = self._rows[max(candidates, key=lambda i: (self._rows[i].weight or 0, -i))]
        return (f"pl:{row.geonameid}",) + ((f"pl:{row.large.geonameid}",) if row.large is not None else ())


def resolve_home(names: PlaceNames, location: str) -> Optional[HomePlace]:
    """The owner's home town from the location as their settings store it. The text is split at commas, brackets and
    slashes (digits dropped); every part is tried as the city with each other part, then none, as the country, and the
    first city that resolves to a `pl:` key wins, with its local key. When none resolves, only the first part's local
    key is kept; None when there is not even that."""
    parts = [" ".join(re.sub(r"\d+", " ", part).split()) for part in re.split(r"[,(/)]", location or "")]
    parts = [part for part in parts if part]
    for i, city in enumerate(parts):
        for country in [part for j, part in enumerate(parts) if j != i] + [""]:
            keys = names.query_keys(city, country)
            if keys and keys[0].startswith("pl:"):
                return HomePlace(keys=frozenset(keys), lp=local_place_key(city) if fold(city) else None)
    if parts and fold(parts[0]):
        return HomePlace(keys=frozenset(), lp=local_place_key(parts[0]))
    return None
