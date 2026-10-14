#!/usr/bin/env python3
"""
Builds geo table v2 — src/assets/geo/places_v2.tsv, countries_v2.tsv and places_v2.sources.json — the offline place
table for photo cards and their place forms (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.3, §25.4.3; RFC Δ7). One-off:
run it only to (re)build the asset; a table whose content changed needs a GEO_TABLE_VERSION bump in the same commit
(src/assets/geo/README.md).

Inputs (GeoNames, CC BY 4.0, https://www.geonames.org), downloaded into scripts/memory/geo/ (gitignored):
  cities5000.zip        every city with population > 5 000
  countryInfo.txt       country names, their languages and geonameids
  alternateNamesV2.zip  names per language (streamed; only rows of the cities and countries above are kept)
GeoNames publishes no versions: the dumps change daily. Reproducibility is by content — places_v2.sources.json
records each input's sha256, size and HTTP Last-Modified, and the build refuses inputs whose hashes differ from it
unless --new-snapshot is given. The same inputs give byte-identical outputs (every order is fixed).

places_v2.tsv, sorted by (lat, lon, geonameid):
  geonameid, lat, lon   GeoNames id; WGS84 degrees, 4 decimals
  name_en               the preferred English alternate name, else GeoNames `name`
  name_local            the preferred name in the country's first language, else GeoNames `name`
  country               English country name (countryInfo)
  weight                population, tripled for capitals and regional seats (PPLC, PPLA)
  population            GeoNames population
  alt_names             "|"-joined: GeoNames `name`, then the names in en, es, fr, uk, ru (NAMES_RULE), without
                        name_en / name_local
  large_id              geonameid of the nearest row of PLACE_LARGE_POPULATION people or more within
                        PLACE_MAX_DISTANCE_KM of this row (nearer, then more populous, then lower id); empty when the
                        row is that large itself or none is near
countries_v2.tsv, sorted by country: country (as in places_v2.tsv), names ("|"-joined, NAMES_RULE, without `country`).

NAMES_RULE, per place and language: colloquial and historic names are dropped; when the language has preferred names,
those (at most PER_LANGUAGE_MAX, in file order); else the first name in file order. (Kyiv: en "Kyiv", fr "Kiev",
uk "Київ", ru "Киев" — not the inflected "Киеву" GeoNames also lists.)

    venv/bin/python scripts/geo/build_place_table.py [--new-snapshot] [--check]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.domain.drive_index_params import PLACE_MAX_DISTANCE_KM  # noqa: E402
from src.domain.place_lookup import PLACE_LARGE_POPULATION, distance_km  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
CACHE = _REPO / "scripts" / "memory" / "geo"
ASSETS = _REPO / "src" / "assets" / "geo"
PLACES_FILE = "places_v2.tsv"
COUNTRIES_FILE = "countries_v2.tsv"
SOURCES_FILE = "places_v2.sources.json"
_BASE = "https://download.geonames.org/export/dump/"
INPUTS = ("cities5000.zip", "countryInfo.txt", "alternateNamesV2.zip")
LANGUAGES = ("en", "es", "fr", "uk", "ru")
PER_LANGUAGE_MAX = 3
_NAME_MAX = 80
_KM_PER_DEGREE = 111.32
PLACE_COLUMNS = ("geonameid", "lat", "lon", "name_en", "name_local", "country", "weight", "population", "alt_names",
                 "large_id")
# Sections of a city (PPLX), historical, abandoned and destroyed places: a photo in Rome must be "Rome", not the
# district nearest to where it was taken.
_SKIP_FEATURE_CODES = frozenset({"PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH"})


@dataclass(frozen=True)
class City:
    gid: str
    lat: float
    lon: float
    name: str
    cc: str
    population: int
    weight: int


@dataclass(frozen=True)
class Country:
    name: str
    language: str        # first language code, "" when none
    gid: str


# (alternateNameId, name, preferred) per (geonameid, language), in file order
_Names = Dict[Tuple[str, str], List[Tuple[int, str, bool]]]


# -- inputs -----------------------------------------------------------------------------------------------------------

def fetch(name: str, cache: Path = CACHE) -> Path:
    """The cached input, downloaded on first use; its HTTP Last-Modified is kept beside it for the sources record."""
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / name
    if not target.exists():
        print(f"downloading {name} ...", flush=True)
        partial = target.with_name(name + ".part")
        with urllib.request.urlopen(_BASE + name) as response, partial.open("wb") as fh:
            (cache / f"{name}.last-modified").write_text(response.headers.get("Last-Modified", ""), encoding="utf-8")
            while chunk := response.read(1 << 20):
                fh.write(chunk)
        partial.replace(target)
    return target


def read_countries(path: Path) -> Dict[str, Country]:
    """ISO2 -> Country."""
    out: Dict[str, Country] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        languages = cols[15].split(",") if len(cols) > 15 else []
        first = languages[0].split("-")[0] if languages and languages[0] else ""
        out[cols[0]] = Country(cols[4], first, cols[16] if len(cols) > 16 else "")
    return out


def read_cities(path: Path) -> Dict[str, City]:
    out: Dict[str, City] = {}
    with zipfile.ZipFile(path) as zf, zf.open("cities5000.txt") as fh:
        for raw in io.TextIOWrapper(fh, encoding="utf-8"):
            cols = raw.rstrip("\n").split("\t")
            if len(cols) < 15 or cols[7] in _SKIP_FEATURE_CODES:
                continue
            population = int(cols[14] or 0)
            boost = 3 if cols[7] in ("PPLC", "PPLA") else 1   # capitals and regional seats name their area
            out[cols[0]] = City(cols[0], float(cols[4]), float(cols[5]), cols[1], cols[8], population,
                                population * boost)
    return out


def read_alternates(path: Path, wanted: Mapping[str, Set[str]]) -> _Names:
    """The names of the wanted ids in their wanted languages; colloquial and historic names are dropped."""
    names: _Names = {}
    with zipfile.ZipFile(path) as zf, zf.open("alternateNamesV2.txt") as fh:
        for raw in io.TextIOWrapper(fh, encoding="utf-8"):
            cols = raw.rstrip("\n").split("\t")
            if len(cols) < 5 or cols[1] not in wanted or cols[2] not in wanted[cols[1]]:
                continue
            if (len(cols) > 6 and cols[6] == "1") or (len(cols) > 7 and cols[7] == "1"):   # colloquial / historic
                continue
            names.setdefault((cols[1], cols[2]), []).append((int(cols[0]), cols[3], cols[4] == "1"))
    for entries in names.values():
        entries.sort(key=lambda e: e[0])
    return names


def first_name(names: _Names, gid: str, lang: str) -> Optional[str]:
    """The v1 rule for name_en / name_local: the first preferred name, else the first name."""
    entries = names.get((gid, lang), [])
    chosen = [e for e in entries if e[2]] or entries
    return chosen[0][1] if chosen else None


def language_names(names: _Names, gid: str, lang: str) -> List[str]:
    """NAMES_RULE: the preferred names (at most PER_LANGUAGE_MAX), else the first name."""
    entries = names.get((gid, lang), [])
    preferred = [e[1] for e in entries if e[2]]
    return preferred[:PER_LANGUAGE_MAX] if preferred else [e[1] for e in entries[:1]]


def _clean(name: str) -> str:
    return " ".join(name.replace("|", " ").split())[:_NAME_MAX]


def _distinct(names: Iterable[str], exclude: Sequence[str]) -> List[str]:
    seen = {n.casefold() for n in exclude}
    out: List[str] = []
    for name in names:
        name = _clean(name)
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return out


# -- the large place --------------------------------------------------------------------------------------------------

def large_places(cities: Mapping[str, City]) -> Dict[str, str]:
    """geonameid -> the nearest place of PLACE_LARGE_POPULATION people or more within PLACE_MAX_DISTANCE_KM of a
    smaller place (ties: nearer, more populous, lower id)."""
    grid: Dict[Tuple[int, int], List[City]] = {}
    for city in cities.values():
        if city.population >= PLACE_LARGE_POPULATION:
            grid.setdefault((math.floor(city.lat), math.floor(city.lon)), []).append(city)
    out: Dict[str, str] = {}
    for city in cities.values():
        if city.population >= PLACE_LARGE_POPULATION:
            continue
        best: Optional[Tuple[float, int, int, str]] = None
        for candidate in _near(grid, city.lat, city.lon):
            d = distance_km(city.lat, city.lon, candidate.lat, candidate.lon)
            if d <= PLACE_MAX_DISTANCE_KM:
                key = (d, -candidate.population, int(candidate.gid), candidate.gid)
                best = key if best is None or key < best else best
        if best is not None:
            out[city.gid] = best[3]
    return out


def _near(grid: Mapping[Tuple[int, int], List[City]], lat: float, lon: float) -> Iterator[City]:
    """Every grid cell within PLACE_MAX_DISTANCE_KM: the latitude rows around the point and as many longitude cells as
    the distance spans at the disc's poleward edge (all of them near a pole), across the antimeridian."""
    edge = min(90.0, abs(lat) + PLACE_MAX_DISTANCE_KM / _KM_PER_DEGREE)
    width = _KM_PER_DEGREE * math.cos(math.radians(edge))
    span = 180 if width <= 0 else min(180, math.ceil(PLACE_MAX_DISTANCE_KM / width))
    cl, co = math.floor(lat), math.floor(lon)
    for dl in (-1, 0, 1):
        for cell in sorted({(co + do + 180) % 360 - 180 for do in range(-span, span + 1)}):
            yield from grid.get((cl + dl, cell), ())


# -- the build --------------------------------------------------------------------------------------------------------

def build(cities_zip: Path, countries_txt: Path, alternates_zip: Path
          ) -> Tuple[List[Tuple[str, ...]], List[Tuple[str, str]]]:
    """(place rows, country rows), both sorted, as written."""
    cities = read_cities(cities_zip)
    countries = read_countries(countries_txt)
    wanted: Dict[str, Set[str]] = {}
    for gid, city in cities.items():
        local = countries.get(city.cc, Country(city.cc, "", "")).language
        wanted[gid] = set(LANGUAGES) | ({local} if local else set())
    for country in countries.values():
        if country.gid:
            wanted.setdefault(country.gid, set()).update(LANGUAGES)
    names = read_alternates(alternates_zip, wanted)
    large = large_places(cities)
    rows: List[Tuple[str, ...]] = []
    for gid, city in cities.items():
        country = countries.get(city.cc, Country(city.cc, "", ""))
        name_en = first_name(names, gid, "en") or city.name
        name_local = (first_name(names, gid, country.language) or city.name) if country.language else city.name
        alternates = [city.name] + [n for lang in LANGUAGES for n in language_names(names, gid, lang)]
        rows.append((gid, f"{city.lat:.4f}", f"{city.lon:.4f}", name_en, name_local, country.name, str(city.weight),
                     str(city.population), "|".join(_distinct(alternates, (name_en, name_local))),
                     large.get(gid, "")))
    rows.sort(key=lambda r: (float(r[1]), float(r[2]), int(r[0])))
    country_rows = sorted({(c.name, "|".join(_distinct([n for lang in LANGUAGES
                                                         for n in language_names(names, c.gid, lang)], (c.name,))))
                           for c in countries.values() if c.name})
    return rows, country_rows


def write(out_dir: Path, rows: Sequence[Tuple[str, ...]], country_rows: Sequence[Tuple[str, str]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / PLACES_FILE).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(PLACE_COLUMNS)
        writer.writerows(rows)
    with (out_dir / COUNTRIES_FILE).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(("country", "names"))
        writer.writerows(country_rows)


def sources(paths: Mapping[str, Path], places: int, countries: int) -> Dict[str, object]:
    """The record of what the table was built from: per input its sha256, size and HTTP Last-Modified."""
    inputs = {}
    for name, path in sorted(paths.items()):
        stamp = path.with_name(f"{name}.last-modified")
        inputs[name] = {"sha256": _sha256(path), "bytes": path.stat().st_size,
                        "last_modified": stamp.read_text(encoding="utf-8").strip() if stamp.exists() else ""}
    return {"source": "GeoNames (https://www.geonames.org), CC BY 4.0", "base_url": _BASE, "inputs": inputs,
            "languages": list(LANGUAGES), "per_language_max": PER_LANGUAGE_MAX,
            "large_population": PLACE_LARGE_POPULATION, "large_max_km": PLACE_MAX_DISTANCE_KM,
            "places": places, "countries": countries}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_differs(recorded: Mapping[str, object], paths: Mapping[str, Path]) -> List[str]:
    """The inputs whose sha256 is not the recorded one."""
    inputs = recorded.get("inputs")
    known = inputs if isinstance(inputs, dict) else {}
    return [name for name, path in sorted(paths.items())
            if not isinstance(known.get(name), dict) or known[name].get("sha256") != _sha256(path)]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Builds geo table v2 from GeoNames.")
    parser.add_argument("--new-snapshot", action="store_true",
                        help=f"build from inputs whose hashes differ from {SOURCES_FILE}")
    parser.add_argument("--check", action="store_true", help="only compare the cached inputs with the record")
    args = parser.parse_args(argv)
    paths = {name: fetch(name) for name in INPUTS}
    record_path = ASSETS / SOURCES_FILE
    if record_path.exists():
        differs = snapshot_differs(json.loads(record_path.read_text(encoding="utf-8")), paths)
        if args.check:
            print(f"inputs differ from the record: {', '.join(differs)}" if differs
                  else "inputs match the recorded snapshot")
            return 1 if differs else 0
        if differs and not args.new_snapshot:
            print(f"inputs differ from {SOURCES_FILE}: {', '.join(differs)} (a new GeoNames snapshot); rebuild with "
                  f"--new-snapshot and bump GEO_TABLE_VERSION", file=sys.stderr)
            return 1
    rows, country_rows = build(paths["cities5000.zip"], paths["countryInfo.txt"], paths["alternateNamesV2.zip"])
    write(ASSETS, rows, country_rows)
    record = sources(paths, len(rows), len(country_rows))
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    size = (ASSETS / PLACES_FILE).stat().st_size / 1_048_576
    print(f"{len(rows)} places, {len(country_rows)} countries -> {ASSETS / PLACES_FILE} ({size:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
