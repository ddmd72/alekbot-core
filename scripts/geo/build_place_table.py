#!/usr/bin/env python3
"""
Builds src/assets/geo/places_v1.tsv — the offline place table for photo cards
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.3; plan Δ7). One-off: run it only to (re)build the asset, then bump
GEO_TABLE_VERSION if the table changed (the upgrade step re-resolves place names).

Inputs (GeoNames, CC BY 4.0, https://www.geonames.org), downloaded into scripts/memory/geo/ (gitignored):
  cities5000.zip     every city with population > 5 000
  countryInfo.txt     country names and their languages
  alternateNamesV2.zip  names per language (streamed; only rows of the cities above are kept)

Output columns: lat, lon, name_en, name_local, country, weight — sorted by (lat, lon). weight = population, tripled
for capitals and regional seats (PPLC, PPLA): a same-country place within PLACE_PREFER_RADIUS_KM that is at least
PLACE_PREFER_WEIGHT_FACTOR times heavier than the nearest place names a photo instead of it.
  name_en     the preferred English alternate name, else GeoNames `name`
  name_local  the preferred name in the country's first language, else GeoNames `name`

    venv/bin/python scripts/geo/build_place_table.py
"""
from __future__ import annotations

import csv
import io
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple

_REPO = Path(__file__).resolve().parents[2]
_CACHE = _REPO / "scripts" / "memory" / "geo"
_OUT = _REPO / "src" / "assets" / "geo" / "places_v1.tsv"
_BASE = "https://download.geonames.org/export/dump/"
_FILES = ("cities5000.zip", "countryInfo.txt", "alternateNamesV2.zip")


def _fetch(name: str) -> Path:
    _CACHE.mkdir(parents=True, exist_ok=True)
    target = _CACHE / name
    if not target.exists():
        print(f"downloading {name} ...", flush=True)
        urllib.request.urlretrieve(_BASE + name, target)
    return target


def _countries(path: Path) -> Dict[str, Tuple[str, str]]:
    """ISO2 -> (country name, first language code)."""
    out: Dict[str, Tuple[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        languages = cols[15].split(",") if len(cols) > 15 else []
        first = languages[0].split("-")[0] if languages and languages[0] else ""
        out[cols[0]] = (cols[4], first)
    return out


# Sections of a city (PPLX), historical, abandoned and destroyed places: a photo in Rome must be "Rome", not the
# district nearest to where it was taken.
_SKIP_FEATURE_CODES = frozenset({"PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH"})


def _cities(path: Path) -> Dict[str, List[str]]:
    """geonameid -> [lat, lon, name, country_code, weight]."""
    out: Dict[str, List[str]] = {}
    with zipfile.ZipFile(path) as zf, zf.open("cities5000.txt") as fh:
        for raw in io.TextIOWrapper(fh, encoding="utf-8"):
            cols = raw.rstrip("\n").split("\t")
            if cols[7] in _SKIP_FEATURE_CODES:
                continue
            boost = 3 if cols[7] in ("PPLC", "PPLA") else 1   # capitals and regional seats name their area
            out[cols[0]] = [cols[4], cols[5], cols[1], cols[8], str(int(cols[14] or 0) * boost)]
    return out


def _alternate_names(path: Path, cities: Dict[str, List[str]], wanted: Dict[str, str]) -> Dict[Tuple[str, str], str]:
    """(geonameid, lang) -> name, preferred names winning; only 'en' and each city's local language."""
    names: Dict[Tuple[str, str], str] = {}
    preferred: Dict[Tuple[str, str], bool] = {}
    with zipfile.ZipFile(path) as zf, zf.open("alternateNamesV2.txt") as fh:
        for raw in io.TextIOWrapper(fh, encoding="utf-8"):
            cols = raw.rstrip("\n").split("\t")
            if len(cols) < 5 or cols[1] not in cities:
                continue
            gid, lang, name = cols[1], cols[2], cols[3]
            if lang != "en" and lang != wanted.get(gid):
                continue
            if len(cols) > 6 and (cols[6] == "1" or cols[7] == "1"):   # colloquial / historic
                continue
            key = (gid, lang)
            is_pref = cols[4] == "1"
            if key not in names or (is_pref and not preferred.get(key, False)):
                names[key] = name
                preferred[key] = is_pref
    return names


def main() -> int:
    cities = _cities(_fetch("cities5000.zip"))
    countries = _countries(_fetch("countryInfo.txt"))
    wanted = {gid: countries.get(c[3], ("", ""))[1] for gid, c in cities.items()}
    alt = _alternate_names(_fetch("alternateNamesV2.zip"), cities, wanted)
    rows = []
    for gid, (lat, lon, name, cc, weight) in cities.items():
        country, lang = countries.get(cc, (cc, ""))
        name_en = alt.get((gid, "en"), name)
        name_local = alt.get((gid, lang), name) if lang else name
        rows.append((float(lat), float(lon), name_en, name_local, country, int(weight)))
    rows.sort(key=lambda r: (r[0], r[1]))
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    with _OUT.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(["lat", "lon", "name_en", "name_local", "country", "weight"])
        for lat, lon, en, local, country, weight in rows:
            writer.writerow([f"{lat:.4f}", f"{lon:.4f}", en, local, country, weight])
    print(f"{len(rows)} cities -> {_OUT} ({_OUT.stat().st_size / 1_048_576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
