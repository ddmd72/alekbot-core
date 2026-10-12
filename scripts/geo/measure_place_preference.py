#!/usr/bin/env python3
"""
Measures the "heavier place names the photo" preference of PlaceIndex over the bundled table
(src/assets/geo/places_v1.tsv; docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.3). Read-only, offline.

For each PLACE_PREFER_WEIGHT_FACTOR candidate (at the current PLACE_FOOTPRINT_K) and each PLACE_FOOTPRINT_K candidate
(at the current factor) it reports:
  self-name    places whose own centre is named after themselves (a lookup at a town's own coordinates should
               rarely say another town)
  cross-border places whose own centre is named after a place in another country (must be 0)
  expected     the README's expected results (a photo in Trastevere is Rome, in Torrent is Valencia, ...)
and lists a sample of the cross-border cases left at the current factor and k, to be judged by eye.

The baseline row is the rule before the same-country and weight-factor conditions: the heaviest place within
PLACE_PREFER_RADIUS_KM, from any country.

    venv/bin/python scripts/geo/measure_place_preference.py [--factors 1,2,3] [--ks 0,0.001,0.002] [--sample 20]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.adapters.geo_place_table import load_place_rows  # noqa: E402
from src.domain.drive_index_params import PLACE_PREFER_RADIUS_KM  # noqa: E402
from src.domain.place_lookup import (  # noqa: E402
    PLACE_FOOTPRINT_K,
    PLACE_PREFER_WEIGHT_FACTOR,
    PlaceIndex,
    PlaceRow,
)

# (where the photo is taken, lat, lon, the place name it must get; None = no place)
EXPECTED: Sequence[Tuple[str, float, float, Optional[str]]] = (
    ("Trastevere", 41.8894, 12.4700, "Rome (Roma), Italy"),
    ("St. Peter's Square", 41.9022, 12.4568, "Vatican City, Vatican"),
    ("central Porto", 41.1480, -8.6110, "Porto, Portugal"),
    ("Porto, Ribeira", 41.1406, -8.6131, "Porto, Portugal"),
    ("Brooklyn", 40.6782, -73.9442, "New York, United States"),
    ("Brooklyn, Williamsburg", 40.7081, -73.9571, "New York, United States"),
    ("Torrent", 39.4373, -0.4654, "Valencia, Spain"),
    ("Monaco", 43.7384, 7.4246, "Monaco, Monaco"),
    ("Vaduz", 47.1410, 9.5215, "Vaduz, Liechtenstein"),
    ("San Marino (city)", 43.9356, 12.4473, "San Marino, San Marino"),
    ("Andorra la Vella", 42.5063, 1.5218, "Andorra la Vella, Andorra"),
    ("Gibraltar", 36.1408, -5.3536, "Gibraltar, Gibraltar"),
    ("Kehl", 48.5719, 7.8150, "Kehl, Germany"),
    ("Geneva", 46.2044, 6.1432, "Geneva (Genf), Switzerland"),
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


def _measure(index: PlaceIndex, rows: Sequence[PlaceRow]) -> Tuple[int, List[Tuple[PlaceRow, PlaceRow]], List[str]]:
    self_named = 0
    crossed: List[Tuple[PlaceRow, PlaceRow]] = []
    for row in rows:
        got = index.resolve(row.lat, row.lon)
        if got is row:
            self_named += 1
        elif got is not None and got.country != row.country:
            crossed.append((row, got))
    misses = [f"{where}: {index.lookup(lat, lon)!r}" for where, lat, lon, want in EXPECTED
              if index.lookup(lat, lon) != want]
    return self_named, crossed, misses


def _floats(text: str) -> List[float]:
    return [float(x) for x in text.split(",") if x]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--factors", default="1,1.5,2,2.5,3,3.5,5,10,30,50",
                        help="comma-separated PLACE_PREFER_WEIGHT_FACTOR candidates (at the current k)")
    parser.add_argument("--ks", default="0,0.0005,0.001,0.0013,0.0015,0.00175,0.002,0.003,0.004,0.006,0.01",
                        help="comma-separated PLACE_FOOTPRINT_K candidates (at the current factor)")
    parser.add_argument("--sample", type=int, default=20, help="cross-border cases listed at the current settings")
    args = parser.parse_args(argv)
    rows = load_place_rows()
    total = len(rows)
    print(f"{total} places; current PLACE_PREFER_WEIGHT_FACTOR = {PLACE_PREFER_WEIGHT_FACTOR:g}, "
          f"PLACE_FOOTPRINT_K = {PLACE_FOOTPRINT_K:g}\n")
    print(f"{'rule':<20}{'self-name':>20}{'cross-border':>14}  expected")
    runs: List[Tuple[str, PlaceIndex]] = [("baseline", _Baseline(rows))]
    runs += [(f"factor {f:g}", PlaceIndex(rows, prefer_weight_factor=f)) for f in _floats(args.factors)]
    runs += [(f"k {k:g}", PlaceIndex(rows, footprint_k=k)) for k in _floats(args.ks)]
    for label, index in runs:
        self_named, crossed, misses = _measure(index, rows)
        print(f"{label:<20}{self_named:>8} ({100 * self_named / total:5.1f}%){len(crossed):>14}  "
              f"{len(EXPECTED) - len(misses)}/{len(EXPECTED)}")
        for miss in misses:
            print(f"{'':<22}miss  {miss}")
    _, crossed, _ = _measure(PlaceIndex(rows), rows)
    print(f"\ncross-border at the current settings: {len(crossed)}")
    step = max(1, len(crossed) // max(args.sample, 1))
    for row, got in crossed[::step][:args.sample]:
        print(f"  {row.name_en}, {row.country} ({row.weight}) -> {got.name_en}, {got.country} ({got.weight})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
