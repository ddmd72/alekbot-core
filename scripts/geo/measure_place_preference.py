#!/usr/bin/env python3
"""
Measures the "heavier place names the photo" preference of PlaceIndex over the bundled table
(src/assets/geo/places_v1.tsv; docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.3). Read-only, offline.

For each PLACE_PREFER_WEIGHT_FACTOR candidate (at the current PLACE_FOOTPRINT_K) and each PLACE_FOOTPRINT_K candidate
(at the current factor) it reports:
  self-name           places whose own centre is named after themselves (a lookup at a town's own coordinates
                      should rarely say another town)
  centre crossed      places whose own centre is named after a place in another country (0 by construction under
                      the current rule: a centre is inside its own footprint, so no border is crossed there)
  off-centre crossed  lookups 2 km north of each centre where the preference crossed a border (the chosen place's
                      country is not the nearest place's)
  expected            the cases in scripts/geo/expected_places.py (Trastevere is Rome, L'Hospitalet is Barcelona, ...)
and lists the border crossings left at the current factor and k, to be judged by eye.

The baseline row is the rule before the same-country and weight-factor conditions: the heaviest place within
PLACE_PREFER_RADIUS_KM, from any country.

    venv/bin/python scripts/geo/measure_place_preference.py [--factors 1,2,3] [--ks 0,0.001,0.002] [--sample 20]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.geo.expected_places import EXPECTED_PLACES  # noqa: E402
from src.adapters.geo_place_table import load_place_rows  # noqa: E402
from src.domain.drive_index_params import PLACE_PREFER_RADIUS_KM  # noqa: E402
from src.domain.place_lookup import (  # noqa: E402
    PLACE_FOOTPRINT_K,
    PLACE_PREFER_WEIGHT_FACTOR,
    PlaceIndex,
    PlaceRow,
)

class _Baseline(PlaceIndex):
    """The rule before the fix: the heaviest place within PLACE_PREFER_RADIUS_KM, any country."""

    def _choose(self, candidates: Sequence[Tuple[float, PlaceRow]]) -> PlaceRow:
        near = [c for c in candidates if c[0] <= PLACE_PREFER_RADIUS_KM]
        if near:
            return max(near, key=lambda c: (c[1].weight, -c[0]))[1]
        return min(candidates, key=lambda c: c[0])[1]


_OFF_CENTRE_KM = 2.0


class _Result(NamedTuple):
    self_named: int
    centre_crossed: List[Tuple[PlaceRow, PlaceRow]]                 # (origin row, the place it is named after)
    off_centre_crossed: List[Tuple[PlaceRow, PlaceRow, PlaceRow]]   # (origin row, nearest place, chosen place)
    misses: List[str]


def _measure(index: PlaceIndex, rows: Sequence[PlaceRow]) -> _Result:
    """Centre: a lookup at each place's own coordinates. Off-centre: a lookup _OFF_CENTRE_KM north of it, counted
    when the preference crossed a border (the chosen place's country is not the nearest place's): that is the rule
    at work, while comparing with the origin row's country would measure border geography."""
    self_named = 0
    centre_crossed: List[Tuple[PlaceRow, PlaceRow]] = []
    off_crossed: List[Tuple[PlaceRow, PlaceRow, PlaceRow]] = []
    for row in rows:
        got = index.resolve(row.lat, row.lon)
        if got is row:
            self_named += 1
        elif got is not None and got.country != row.country:
            centre_crossed.append((row, got))
        candidates = index._candidates(min(90.0, row.lat + _OFF_CENTRE_KM / 111.32), row.lon)
        if candidates:
            nearest = min(candidates, key=lambda c: (c[0], -c[1].weight))[1]
            chosen = index._choose(candidates)
            if chosen.country != nearest.country:
                off_crossed.append((row, nearest, chosen))
    misses = [f"{where}: {index.lookup(lat, lon)!r}" for where, lat, lon, want in EXPECTED_PLACES
              if index.lookup(lat, lon) != want]
    return _Result(self_named, centre_crossed, off_crossed, misses)


def _floats(text: str) -> List[float]:
    return [float(x) for x in text.split(",") if x]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--factors", default="1,1.5,2,2.5,3,3.5,10,50",
                        help="comma-separated PLACE_PREFER_WEIGHT_FACTOR candidates (at the current k)")
    parser.add_argument("--ks", default="0,0.001,0.0013,0.0015,0.00175,0.002,0.003,0.006,0.01",
                        help="comma-separated PLACE_FOOTPRINT_K candidates (at the current factor)")
    parser.add_argument("--sample", type=int, default=20, help="cross-border cases listed at the current settings")
    args = parser.parse_args(argv)
    rows = load_place_rows()
    total = len(rows)
    print(f"{total} places; current PLACE_PREFER_WEIGHT_FACTOR = {PLACE_PREFER_WEIGHT_FACTOR:g}, "
          f"PLACE_FOOTPRINT_K = {PLACE_FOOTPRINT_K:g}\n")
    print(f"{'rule':<20}{'self-name':>20}{'centre crossed':>16}{'off-centre crossed':>20}  expected")
    runs: List[Tuple[str, PlaceIndex]] = [("baseline", _Baseline(rows))]
    runs += [(f"factor {f:g}", PlaceIndex(rows, prefer_weight_factor=f)) for f in _floats(args.factors)]
    runs += [(f"k {k:g}", PlaceIndex(rows, footprint_k=k)) for k in _floats(args.ks)]
    for label, index in runs:
        r = _measure(index, rows)
        print(f"{label:<20}{r.self_named:>8} ({100 * r.self_named / total:5.1f}%){len(r.centre_crossed):>16}"
              f"{len(r.off_centre_crossed):>20}  {len(EXPECTED_PLACES) - len(r.misses)}/{len(EXPECTED_PLACES)}")
        for miss in r.misses:
            print(f"{'':<22}miss  {miss}")
    r = _measure(PlaceIndex(rows), rows)
    print(f"\ncentre crossed at the current settings: {len(r.centre_crossed)}")
    for row, got in r.centre_crossed[:args.sample]:
        print(f"  {row.name_en}, {row.country} -> {got.name_en}, {got.country}")
    print(f"off-centre crossed at the current settings ({_OFF_CENTRE_KM:g} km north): {len(r.off_centre_crossed)}")
    for row, nearest, chosen in r.off_centre_crossed[:args.sample]:
        print(f"  north of {row.name_en}: nearest {nearest.name_en}, {nearest.country} ({nearest.weight}) -> "
              f"{chosen.name_en}, {chosen.country} ({chosen.weight})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
