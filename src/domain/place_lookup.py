"""
Offline place names from a bundled city table (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.3; RFC Δ7).
The nearest place within PLACE_MAX_DISTANCE_KM names a photo, unless a much heavier place lies within
PLACE_PREFER_RADIUS_KM: weight at least PLACE_PREFER_WEIGHT_FACTOR times the nearest's, and either in the nearest
place's country, or with the photo inside its own footprint (PLACE_FOOTPRINT_K * sqrt(weight) km) and outside the
nearest place's. Then the heaviest such place wins: a photo in a Porto parish is "Porto", one in L'Hospitalet "Barcelona",
one in Trastevere "Rome" although the Vatican City row is nearer; Monaco is never "Nice", Kehl never "Strasbourg",
Brazzaville never "Kinshasa". Candidates come from a 1-degree grid: the three latitude rows around the point, and as many longitude
cells as PLACE_MAX_DISTANCE_KM spans at that latitude.

Geo table v2 (§25.4.3, GEO_TABLE_VERSION 2) gives each row its alternate names in en, es, fr, uk and ru, its country's
names in those languages, and the nearest place of PLACE_LARGE_POPULATION people or more within PLACE_MAX_DISTANCE_KM
(chosen when the table is built). `place()` returns them with the display name as the photo's `place_forms`: the
spellings its keys are made from, on the index side only (no query-side table, OQ-V3).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .drive_index_params import PLACE_MAX_DISTANCE_KM, PLACE_PREFER_RADIUS_KM

_EARTH_KM = 6371.0
_KM_PER_DEGREE = 111.32
# Both measured over the bundled table by scripts/geo/measure_place_preference.py (numbers in src/assets/geo/README.md).
PLACE_PREFER_WEIGHT_FACTOR = 3.0
# A row is a point, not a city's extent: the footprint lets a city reach across a border only near its own centre
# and only where the nearest place's own footprint does not hold the photo (an enclave like the Vatican), never
# over a separate border town or city (Kehl, Monaco, Brazzaville).
PLACE_FOOTPRINT_K = 0.0015
# Geo table v2 (§25.4.3): a place this populous near a smaller one joins the smaller one's place forms.
PLACE_LARGE_POPULATION = 100_000


@dataclass(frozen=True)
class PlaceRow:
    lat: float
    lon: float
    name_en: str
    name_local: str
    country: str
    weight: int = 0   # population, tripled for capitals and regional seats
    # Geo table v2 (empty on a v1 row): the place's names in en, es, fr, uk, ru beyond name_en/name_local; its
    # country's names (one tuple shared by every row of the country); the nearest place of PLACE_LARGE_POPULATION
    # people or more within PLACE_MAX_DISTANCE_KM, None when the place is that large itself or none is near.
    alt_names: Tuple[str, ...] = ()
    country_names: Tuple[str, ...] = ()
    large: Optional["PlaceRow"] = None


@dataclass(frozen=True)
class Place:
    """What a photo's coordinates resolve to: the display name (`place_name`) and its spellings (`place_forms`)."""
    name: str
    forms: Tuple[str, ...]


def place_forms(row: PlaceRow) -> Tuple[str, ...]:
    """The row's spellings in a fixed order — its own names, its country's, the nearest large place's — each once
    (case-insensitive; the first spelling is kept)."""
    names: List[str] = [row.name_en, row.name_local, *row.alt_names, row.country, *row.country_names]
    if row.large is not None:
        names += [row.large.name_en, row.large.name_local, *row.large.alt_names]
    seen = set()
    forms: List[str] = []
    for name in names:
        key = name.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            forms.append(name.strip())
    return tuple(forms)


def distance_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """Great-circle distance (haversine); the build script picks a row's large place with it."""
    return _km(a_lat, a_lon, b_lat, b_lon)


def _km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_KM * math.asin(math.sqrt(h))


def _lon_cells(lat: float, co: int) -> List[int]:
    """Longitude cells within PLACE_MAX_DISTANCE_KM of a point in cell `co`; a degree of longitude narrows
    towards the poles, so above ~63 degrees a 3x3 grid misses candidates. The span is taken at the
    poleward edge of the search disc; near a pole every cell is scanned, each once."""
    edge = min(90.0, abs(lat) + PLACE_MAX_DISTANCE_KM / _KM_PER_DEGREE)
    width_km = _KM_PER_DEGREE * math.cos(math.radians(edge))
    span = 180 if width_km <= 0 else min(180, math.ceil(PLACE_MAX_DISTANCE_KM / width_km))
    cells = {(co + do + 180) % 360 - 180 for do in range(-span, span + 1)}   # wraps across the antimeridian
    return sorted(cells)


class PlaceIndex:
    def __init__(self, rows: Sequence[PlaceRow], prefer_weight_factor: float = PLACE_PREFER_WEIGHT_FACTOR,
                 footprint_k: float = PLACE_FOOTPRINT_K) -> None:
        self._prefer_weight_factor = prefer_weight_factor
        self._footprint_k = footprint_k
        self._cells: Dict[Tuple[int, int], List[PlaceRow]] = {}
        for row in rows:
            self._cells.setdefault((math.floor(row.lat), math.floor(row.lon)), []).append(row)

    def lookup(self, lat: float, lon: float) -> Optional[str]:
        """The place name a photo taken here gets, e.g. "Rome (Roma), Italy"; None when no place is near."""
        row = self.resolve(lat, lon)
        return None if row is None else _display(row)

    def place(self, lat: float, lon: float) -> Optional[Place]:
        """The display name and the place forms of the place a photo taken here gets (§25.4.3); None as lookup()."""
        row = self.resolve(lat, lon)
        return None if row is None else Place(name=_display(row), forms=place_forms(row))

    def resolve(self, lat: float, lon: float) -> Optional[PlaceRow]:
        """The place row behind lookup()."""
        if not (math.isfinite(lat) and math.isfinite(lon)) or abs(lat) > 90 or abs(lon) > 180:
            return None   # a corrupt facet is no place, never an exception (one bad item never stalls, §8.2)
        candidates = self._candidates(lat, lon)
        if not candidates:
            return None
        return self._choose(candidates)

    def footprint_km(self, row: PlaceRow) -> float:
        """How far from its row a place holds a photo across a border: k * sqrt(weight) km."""
        return self._footprint_k * math.sqrt(max(row.weight, 0))

    def _candidates(self, lat: float, lon: float) -> List[Tuple[float, PlaceRow]]:
        """(distance km, row) of every place within PLACE_MAX_DISTANCE_KM."""
        candidates: List[Tuple[float, PlaceRow]] = []
        cl, co = math.floor(lat), math.floor(lon)
        for dl in (-1, 0, 1):
            for cell_lon in _lon_cells(lat, co):
                for row in self._cells.get((cl + dl, cell_lon), ()):
                    d = _km(lat, lon, row.lat, row.lon)
                    if d <= PLACE_MAX_DISTANCE_KM:
                        candidates.append((d, row))
        return candidates

    def _choose(self, candidates: Sequence[Tuple[float, PlaceRow]]) -> PlaceRow:
        """The nearest place, or the heaviest one within PLACE_PREFER_RADIUS_KM that outweighs it by the factor and
        is in its country, or holds the photo in its footprint while the nearest place's footprint does not (§5.3)."""
        nearest_d, nearest = min(candidates, key=lambda c: (c[0], -c[1].weight))
        floor = nearest.weight * self._prefer_weight_factor
        crossable = nearest_d > self.footprint_km(nearest)
        heavier = [c for c in candidates
                   if c[0] <= PLACE_PREFER_RADIUS_KM and c[1].weight >= floor and c[1].weight > nearest.weight
                   and (c[1].country == nearest.country or (crossable and c[0] <= self.footprint_km(c[1])))]
        if not heavier:
            return nearest
        return max(heavier, key=lambda c: (c[1].weight, -c[0]))[1]


def _display(row: PlaceRow) -> str:
    local = f" ({row.name_local})" if row.name_local and row.name_local != row.name_en else ""
    return f"{row.name_en}{local}, {row.country}"
