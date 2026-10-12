"""Loads the bundled place table (src/assets/geo/places_v1.tsv) into domain PlaceRows."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import List, Optional

from ..domain.place_lookup import PlaceRow
from ..utils.logger import logger

DEFAULT_PLACE_TABLE = Path(__file__).resolve().parents[1] / "assets" / "geo" / "places_v1.tsv"


def load_place_rows(path: Optional[Path] = None) -> List[PlaceRow]:
    target = path or DEFAULT_PLACE_TABLE
    with target.open(encoding="utf-8", newline="") as fh:
        rows = [PlaceRow(float(r["lat"]), float(r["lon"]), r["name_en"], r["name_local"], r["country"],
                         int(r.get("weight") or 0))
                for r in csv.DictReader(fh, delimiter="\t")]
    logger.info(f"🌍 Place table loaded: {len(rows)} cities from {target.name}")
    return rows
