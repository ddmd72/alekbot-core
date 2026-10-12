#!/usr/bin/env python3
"""
Measures the "heavier place names the photo" preference of PlaceIndex over the bundled table
(src/assets/geo/places_v1.tsv; docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.3). Read-only, offline.

For each PLACE_PREFER_WEIGHT_FACTOR candidate it reports:
  self-name    places whose own centre is named after themselves (a lookup at a town's own coordinates should
               rarely say another town)
  cross-border places whose own centre is named after a place in another country (must be 0)
  expected     the README's expected results (a photo in Trastevere is Rome, in Torrent is Valencia, ...)

The baseline row is the rule before the same-country and weight-factor conditions: the heaviest place within
PLACE_PREFER_RADIUS_KM, from any country.

    venv/bin/python scripts/geo/measure_place_preference.py [--factors 1,2,3,5,10]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.adapters.geo_place_table import load_place_rows  # noqa: E402
from src.domain.drive_index_params import PLACE_PREFER_RADIUS_KM  # noqa: E402
from src.domain.place_lookup import PLACE_PREFER_WEIGHT_FACTOR, PlaceIndex, PlaceRow  # noqa: E402

# (where the photo is taken, lat, lon, the place name it must get; None = no place)
EXPECTED: Sequence[Tuple[str, float, float, Optional[str]]] = (
    ("Trastevere", 41.8894, 12.4700, "Rome (Roma), Italy"),
    ("central Porto", 41.1480, -8.6110, "Porto, Portugal"),
    ("Porto, Ribeira", 41.1406, -8.6131, "Porto, Portugal"),
    ("Brooklyn", 40.6782, -73.9442, "New York, United States"),
    ("Brooklyn, Williamsburg", 40.7081, -73.9571, "New York, United States"),
    ("Torrent", 39.4373, -0.4654, "Valencia, Spain"),
    ("Monaco", 43.7384, 7.4246, "Monaco, Monaco"),
    ("Vaduz", 47.1410, 9.5215, "Vaduz, Liechtenstein"),
    ("the Dolomites (Plose)", 46.6930, 11.7300, "Brixen (Bressanone), Italy"),
    ("open sea", 0.0, -30.0, None),
)


class _Baseline(PlaceIndex):
    """The rule before the fix: the heaviest place within PLACE_PREFER_RADIUS_KM, any country."""

    def _choose(self, candidates: Sequence[Tuple[float, PlaceRow]]) -> PlaceRow:
        near = [c for c in candidates if c[0] <= PLACE_PREFER_RADIUS_KM]
        if near:
            return max(near, key=lambda c: (c[1].weight, -c[0]))[1]
        return min(candidates, key=lambda c: c[0])[1]


def _measure(index: PlaceIndex, rows: Sequence[PlaceRow]) -> Tuple[int, int, List[str]]:
    self_named = cross_border = 0
    for row in rows:
        got = index.resolve(row.lat, row.lon)
        if got is row:
            self_named += 1
        elif got is not None and got.country != row.country:
            cross_border += 1
    misses = [f"{where}: {index.lookup(lat, lon)!r}" for where, lat, lon, want in EXPECTED
              if index.lookup(lat, lon) != want]
    return self_named, cross_border, misses


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--factors", default="1,1.5,2,3,5,10,20,30,50",
                        help="comma-separated PLACE_PREFER_WEIGHT_FACTOR candidates")
    args = parser.parse_args(argv)
    rows = load_place_rows()
    total = len(rows)
    print(f"{total} places; current PLACE_PREFER_WEIGHT_FACTOR = {PLACE_PREFER_WEIGHT_FACTOR:g}\n")
    print(f"{'rule':<14}{'self-name':>20}{'cross-border':>14}  expected")
    runs: List[Tuple[str, PlaceIndex]] = [("baseline", _Baseline(rows))]
    runs += [(f"factor {f:g}", PlaceIndex(rows, prefer_weight_factor=f))
             for f in (float(x) for x in args.factors.split(","))]
    for label, index in runs:
        self_named, cross_border, misses = _measure(index, rows)
        ok = len(EXPECTED) - len(misses)
        print(f"{label:<14}{self_named:>8} ({100 * self_named / total:5.1f}%){cross_border:>14}  "
              f"{ok}/{len(EXPECTED)}")
        for miss in misses:
            print(f"{'':<16}miss  {miss}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
