# Offline place table (`places_v1.tsv`)

The bundled city table that names the place a photo was taken in, for drive photo cards
(`docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md` §5.3; Slice 1 plan Δ7). Lookups are offline: no geocoding
service is called, and a photo's coordinates never leave the service.

## Attribution

Place names from GeoNames (https://www.geonames.org), CC BY 4.0
(https://creativecommons.org/licenses/by/4.0/). The table is a reduction of the GeoNames dumps
`cities5000`, `countryInfo` and `alternateNamesV2`.

## Contents

Tab-separated, UTF-8, one header row, sorted by `(lat, lon)`:

| Column | Meaning |
|---|---|
| `lat`, `lon` | WGS84 degrees, 4 decimals |
| `name_en` | the preferred English alternate name, else the GeoNames `name` |
| `name_local` | the preferred name in the country's first language, else the GeoNames `name` |
| `country` | English country name (`countryInfo`) |
| `weight` | population, tripled for capitals and regional seats (`PPLC`, `PPLA`) |

City sections (`PPLX`) and historical, abandoned or destroyed places (`PPLH`, `PPLQ`, `PPLW`,
`PPLCH`) are left out, so a photo in Rome is "Rome", not the district nearest to where it was taken.
Built 2026-10-12: 64 458 places, about 3.3 MB.

How the table is read: `src/adapters/geo_place_table.py` loads it into `PlaceRow`s, and
`src/domain/place_lookup.py` (`PlaceIndex`) names a photo after the nearest place within
`PLACE_MAX_DISTANCE_KM` (50 km), else no place. One exception: a heavier place within
`PLACE_PREFER_RADIUS_KM` (15 km) of the photo names it instead when both hold:

- its `weight` is at least `PLACE_PREFER_WEIGHT_FACTOR` (3) times the nearest place's, and
- it is in the **nearest place's country**, or the photo lies inside its own **footprint**,
  `PLACE_FOOTPRINT_K` (0.0015) × √weight km, capped at 15 km.

Of several, the heaviest wins. So a photo in a suburb gets the city's name (Torrent is "Valencia"), and a
city reaches across a border only near its own centre: a row is a point, not the city's extent, and
without the footprint every point in Trastevere, nearer to the Vatican City row than to Rome's, would
be "Vatican City". Rome's footprint is 4.0 km, Strasbourg's 1.4 km (Kehl, 5.3 km away, stays "Kehl"),
Nice's 0.9 km (Monaco stays "Monaco"). The result reads `"Rome (Roma), Italy"`; the local name is
omitted when it equals the English one.

Both constants were picked with `scripts/geo/measure_place_preference.py` (read-only, offline) on the
table built 2026-10-12, 64 458 places, against the 16 expected cases below. "Self-name" counts places
whose own centre is named after themselves; "cross-border" counts places whose own centre is named
after a place in another country.

Factor, at k = 0.0015:

| Rule | Self-name | Cross-border | Expected cases missed |
|---|---|---|---|
| before: heaviest within 15 km, any country | 29 240 (45.4%) | 603 | St. Peter's, Monaco, Vaduz, San Marino, Gibraltar, Kehl |
| factor 1 | 29 519 (45.8%) | 5 | Vaduz ("Schaan"), San Marino ("Serravalle"), Kehl ("Offenburg") |
| factor 1.5 | 33 390 (51.8%) | 5 | San Marino |
| factor 2 | 36 257 (56.2%) | 5 | San Marino |
| factor 2.5 | 38 571 (59.8%) | 5 | none |
| **factor 3** | **40 432 (62.7%)** | **5** | **none** |
| factor 3.5 | 42 032 (65.2%) | 5 | Brooklyn ("Brooklyn") |
| factor 10 | 51 349 (79.7%) | 4 | Brooklyn |
| factor 50 | 59 498 (92.3%) | 4 | Brooklyn, Torrent ("Torrent") |

k, at factor 3:

| k | Self-name | Cross-border | Expected cases missed |
|---|---|---|---|
| 0 (no footprint) | 40 435 (62.7%) | 0 | Trastevere ("Vatican City") |
| 0.001 | 40 435 (62.7%) | 0 | Trastevere |
| 0.0013 | 40 433 (62.7%) | 3 | Trastevere |
| **0.0015** | **40 432 (62.7%)** | **5** | **none** |
| 0.00175 | 40 432 (62.7%) | 5 | none |
| 0.002 | 40 430 (62.7%) | 8 | St. Peter's Square ("Rome") |
| 0.003 | 40 427 (62.7%) | 22 | St. Peter's Square |
| 0.006 | 40 402 (62.7%) | 66 | St. Peter's Square, Kehl ("Strasbourg") |
| 0.01 | 40 366 (62.6%) | 122 | St. Peter's Square, Kehl |

Factor 3 is the largest that keeps Brooklyn → New York (New York weighs 3.2 times Brooklyn). k must
reach Trastevere (3.4 km from the Rome row: k ≥ 0.0013) and stop short of St. Peter's Square (4.7 km:
k < 0.0018); 0.0015 is the middle. A rebuilt table can move these numbers: re-run the script.

The five cross-border names left at these settings, all megacity footprints over a neighbour across a
border: Brazzaville → Kinshasa, Aflao (Ghana) → Lomé (Togo), Si Chiang Mai (Thailand) → Vientiane
(Laos), San Tin and Sheung Shui (Hong Kong) → Shenzhen.

Expected results on the built table (pinned by `tests/unit/adapters/test_geo_place_table_expected_places.py`):

| Photo taken in | Place name |
|---|---|
| Trastevere | Rome (Roma), Italy (not Vatican City, whose row is nearer) |
| St. Peter's Square | Vatican City, Vatican |
| central Porto, Ribeira | Porto, Portugal (not the parish, not Vila Nova de Gaia) |
| Brooklyn, Williamsburg | New York, United States |
| Torrent | Valencia, Spain |
| Monaco | Monaco, Monaco (not Nice) |
| Vaduz | Vaduz, Liechtenstein (not Feldkirch or Buchs) |
| San Marino (the city) | San Marino, San Marino (not Serravalle) |
| Andorra la Vella | Andorra la Vella, Andorra |
| Gibraltar | Gibraltar, Gibraltar (not Algeciras) |
| Kehl | Kehl, Germany (not Strasbourg) |
| Geneva | Geneva (Genf), Switzerland |
| the Dolomites (Plose) | Brixen (Bressanone), Italy |
| open sea | no place |

## Rebuilding

```bash
venv/bin/python scripts/geo/build_place_table.py
```

The script downloads the three GeoNames inputs into `scripts/memory/geo/` (gitignored) on first run,
reuses them afterwards, and overwrites `src/assets/geo/places_v1.tsv`. It needs network access.
Re-check the expected results above against the new table (and re-run
`scripts/geo/measure_place_preference.py`) before committing it.

## Version bump rule

When the table's content changes, bump `GEO_TABLE_VERSION` in `src/domain/drive_index.py` in the
same commit. Records carry the `geo_table_version` their place name was resolved with, and the
upgrade step re-resolves place names of records below the current version. A table change without a
bump leaves stale place names on existing records.
