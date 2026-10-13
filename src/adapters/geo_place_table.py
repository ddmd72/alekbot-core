"""Loads the bundled place table (geo table v2: src/assets/geo/places_v2.tsv and countries_v2.tsv; USER_DRIVE_INDEXING_RFC
§5.3, §25.4.3) into domain PlaceRows. A v1-shaped table (no geonameid, alt_names, large_id) still loads, without
place forms beyond its own names."""
from __future__ import annotations

import csv
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..domain.place_lookup import PlaceRow
from ..utils.logger import logger

_ASSETS = Path(__file__).resolve().parents[1] / "assets" / "geo"
DEFAULT_PLACE_TABLE = _ASSETS / "places_v2.tsv"
DEFAULT_COUNTRY_TABLE = _ASSETS / "countries_v2.tsv"


def load_place_rows(path: Optional[Path] = None, countries_path: Optional[Path] = None) -> List[PlaceRow]:
    """The table's rows. With no `path`, the bundled tables; an explicit `path` reads country names only from an
    explicit `countries_path`. A row's large place is linked by geonameid after every row is read."""
    target = path or DEFAULT_PLACE_TABLE
    countries = _country_names(countries_path or (DEFAULT_COUNTRY_TABLE if path is None else None))
    ids: List[str] = []
    large_ids: List[str] = []
    rows: List[PlaceRow] = []
    with target.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            alt = tuple(name for name in (r.get("alt_names") or "").split("|") if name)
            rows.append(PlaceRow(float(r["lat"]), float(r["lon"]), r["name_en"], r["name_local"], r["country"],
                                 int(r.get("weight") or 0), alt_names=alt,
                                 country_names=countries.get(r["country"], ())))
            ids.append(r.get("geonameid") or "")
            large_ids.append(r.get("large_id") or "")
    by_id: Dict[str, PlaceRow] = {gid: row for gid, row in zip(ids, rows) if gid}
    linked = [replace(row, large=by_id[large]) if large and large in by_id else row
              for row, large in zip(rows, large_ids)]
    logger.info(f"🌍 Place table loaded: {len(linked)} cities from {target.name}"
                f" ({sum(1 for row in linked if row.large is not None)} with a large neighbour,"
                f" {len(countries)} countries named)")
    return linked


def _country_names(path: Optional[Path]) -> Dict[str, Tuple[str, ...]]:
    """country -> its names, one tuple per country (every row of the country shares it)."""
    if path is None:
        return {}
    with path.open(encoding="utf-8", newline="") as fh:
        return {r["country"]: tuple(name for name in (r.get("names") or "").split("|") if name)
                for r in csv.DictReader(fh, delimiter="\t")}
