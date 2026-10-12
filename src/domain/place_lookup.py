"""
Offline place names from a bundled city table (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.3; plan Δ7).
Within PLACE_PREFER_RADIUS_KM the heaviest place wins (a photo in Porto is "Porto", not the parish it was taken in);
farther out, the nearest place within PLACE_MAX_DISTANCE_KM. Candidates come from a 1-degree grid: the three
latitude rows around the point, and as many longitude cells as PLACE_MAX_DISTANCE_KM spans at that latitude.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .drive_index_params import PLACE_MAX_DISTANCE_KM, PLACE_PREFER_RADIUS_KM

_EARTH_KM = 6371.0
_KM_PER_DEGREE = 111.32


@dataclass(frozen=True)
class PlaceRow:
    lat: float
    lon: float
    name_en: str
    name_local: str
    country: str
    weight: int = 0   # population, tripled for capitals and regional seats


def _km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_KM * math.asin(math.sqrt(h))


def _lon_cells(lat: float, co: int) -> List[int]:
    """Longitude cells within PLACE_MAX_DISTANCE_KM of a point in cell `co`; a degree of longitude narrows
    towards the poles, so above ~63 degrees a 3x3 grid misses candidates (retro F14). The span is taken at the
    poleward edge of the search disc; near a pole every cell is scanned, each once."""
    edge = min(90.0, abs(lat) + PLACE_MAX_DISTANCE_KM / _KM_PER_DEGREE)
    width_km = _KM_PER_DEGREE * math.cos(math.radians(edge))
    span = 180 if width_km <= 0 else min(180, math.ceil(PLACE_MAX_DISTANCE_KM / width_km))
    cells = {(co + do + 180) % 360 - 180 for do in range(-span, span + 1)}   # wraps across the antimeridian
    return sorted(cells)


class PlaceIndex:
    def __init__(self, rows: Sequence[PlaceRow]) -> None:
        self._cells: Dict[Tuple[int, int], List[PlaceRow]] = {}
        for row in rows:
            self._cells.setdefault((math.floor(row.lat), math.floor(row.lon)), []).append(row)

    def lookup(self, lat: float, lon: float) -> Optional[str]:
        if not (math.isfinite(lat) and math.isfinite(lon)) or abs(lat) > 90 or abs(lon) > 180:
            return None   # a corrupt facet is no place, never an exception (one bad item never stalls, §8.2)
        candidates: List[Tuple[float, PlaceRow]] = []
        cl, co = math.floor(lat), math.floor(lon)
        for dl in (-1, 0, 1):
            for cell_lon in _lon_cells(lat, co):
                for row in self._cells.get((cl + dl, cell_lon), ()):
                    d = _km(lat, lon, row.lat, row.lon)
                    if d <= PLACE_MAX_DISTANCE_KM:
                        candidates.append((d, row))
        if not candidates:
            return None
        near = [c for c in candidates if c[0] <= PLACE_PREFER_RADIUS_KM]
        if near:
            _, row = max(near, key=lambda c: (c[1].weight, -c[0]))
        else:
            _, row = min(candidates, key=lambda c: c[0])
        local = f" ({row.name_local})" if row.name_local and row.name_local != row.name_en else ""
        return f"{row.name_en}{local}, {row.country}"
