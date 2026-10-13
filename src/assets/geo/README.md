# Offline place table (geo table v2: `places_v2.tsv`, `countries_v2.tsv`)

The bundled city table that names the place a photo was taken in, for drive photo cards
(`docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md` §5.3; RFC Δ7), and, since geo table v2 (§25.4.3,
`GEO_TABLE_VERSION` 2), the spellings of that place a photo's search keys are made from (`place_forms`). Lookups
are offline: no geocoding service is called, and a photo's coordinates never leave the service.

## Attribution

Place names from GeoNames (https://www.geonames.org), CC BY 4.0
(https://creativecommons.org/licenses/by/4.0/). The tables are a reduction of the GeoNames dumps
`cities5000`, `countryInfo` and `alternateNamesV2`.

## Source snapshot

GeoNames publishes no versions; its dumps change daily. `places_v2.sources.json` records what the tables were built
from: each input's sha256, size and HTTP `Last-Modified`, the languages, the per-language cap and the large-place
rule. Built 2026-10-13 from the dumps of that day (`cities5000.zip` Last-Modified Tue, 13 Oct 2026 01:57:19 GMT,
`alternateNamesV2.zip` 02:02:37 GMT, `countryInfo.txt` 02:02:56 GMT): 64 505 places (5.1 MB) and 252 countries
(19 KB). The same inputs give byte-identical tables; the build refuses inputs whose hashes differ from the record
unless `--new-snapshot` is given.

## Contents

`places_v2.tsv` — tab-separated, UTF-8, one header row, sorted by `(lat, lon, geonameid)`:

| Column | Meaning |
|---|---|
| `geonameid` | the GeoNames id |
| `lat`, `lon` | WGS84 degrees, 4 decimals |
| `name_en` | the preferred English alternate name, else the GeoNames `name` |
| `name_local` | the preferred name in the country's first language, else the GeoNames `name` |
| `country` | English country name (`countryInfo`) |
| `weight` | population, tripled for capitals and regional seats (`PPLC`, `PPLA`) |
| `population` | the GeoNames population |
| `alt_names` | `\|`-joined: the GeoNames `name`, then the names in en, es, fr, uk, ru, without `name_en` / `name_local` |
| `large_id` | the `geonameid` of the nearest place of 100 000 people or more (`PLACE_LARGE_POPULATION`) within `PLACE_MAX_DISTANCE_KM` (50 km) of this row; empty when the place is that large itself or none is near |

`countries_v2.tsv` — `country` (as in `places_v2.tsv`) and its `names` in en, es, fr, uk, ru (`|`-joined, without
`country`).

The names rule, per place and language: colloquial and historic names are dropped; when the language has preferred
names, those (at most three, in file order); else the first name. Kyiv gets "Kiev" (fr) and "Киев" (ru) beside
`name_en` "Kyiv" and `name_local` "Київ", not GeoNames' inflected "Киеву"; Spain gets "España", "Espagne",
"Іспанія", "Испания".

City sections (`PPLX`) and historical, abandoned or destroyed places (`PPLH`, `PPLQ`, `PPLW`,
`PPLCH`) are left out, so a photo in Rome is "Rome", not the district nearest to where it was taken.

## Place forms (§25.4.3)

`PlaceIndex.place(lat, lon)` returns the display name (`place_name`, unchanged from v1) and `place_forms`: the
chosen row's `name_en`, `name_local` and `alt_names`, its country and the country's names, then its large place's
`name_en`, `name_local` and `alt_names` — each once, case-insensitively, in that order. A photo in Sitges holds
"Sitges", "Ситжес", "Spain", "España", …, and its large neighbour's names. They are written on the index side only:
no query-side table (OQ-V3).

**Loading cost on the indexer** (measured 2026-10-13, Python 3.13, `load_place_rows()` + `PlaceIndex`): about 0.9 s
and 32 MB retained (v1: 27 MB), 45 MB peak while loading; once per process. A `place()` call is about 0.2 ms.

How the table is read: `src/adapters/geo_place_table.py` loads it into `PlaceRow`s, and
`src/domain/place_lookup.py` (`PlaceIndex`) names a photo after the nearest place within
`PLACE_MAX_DISTANCE_KM` (50 km), else no place. One exception: a heavier place within
`PLACE_PREFER_RADIUS_KM` (15 km) of the photo names it instead when both hold:

- its `weight` is at least `PLACE_PREFER_WEIGHT_FACTOR` (3) times the nearest place's, and
- it is in the **nearest place's country**, or the photo lies inside its own **footprint** and outside
  the nearest place's. A footprint is `PLACE_FOOTPRINT_K` (0.0015) × √weight km.

Of several, the heaviest wins. So a photo in a suburb gets the city's name (Torrent is "Valencia"), and a
city reaches across a border only near its own centre and only where the nearest place's own footprint
does not hold the photo: a row is a point, not the city's extent, and without the footprint every point
in Trastevere, nearer to the Vatican City row than to Rome's, would be "Vatican City". Footprints: Rome
4.0 km, Strasbourg 1.4 km (Kehl, 5.3 km away, stays "Kehl"), Nice 0.9 km (Monaco stays "Monaco"),
Vatican City 0.07 km, Brazzaville 3.7 km (so Kinshasa's 10.4 km never takes it). The result reads
`"Rome (Roma), Italy"`; the local name is omitted when it equals the English one.

Both constants were picked with `scripts/geo/measure_place_preference.py` (read-only, offline) on the v1
table built 2026-10-12, 64 458 places, against the 17 expected cases in
`scripts/geo/expected_places.py` (the one list; `tests/unit/adapters/test_geo_expected_places.py` pins
it). Three counts:

- **Self-name**: places whose own centre is named after themselves.
- **Centre crossed**: places whose own centre is named after a place in another country. It is 0 by
  construction under the footprint rule: a place's centre is inside its own footprint, so the
  preference never crosses a border there. It only measures the rule before this one.
- **Off-centre crossed**: lookups 2 km north of each centre where the preference crossed a border (the
  chosen place's country is not the nearest place's). This is the count that moves with k.

Factor, at k = 0.0015:

| Rule | Self-name | Centre crossed | Off-centre crossed | Expected cases missed |
|---|---|---|---|---|
| before: heaviest within 15 km, any country | 29 240 (45.4%) | 603 | 635 | St. Peter's, Monaco, Vaduz, San Marino, Gibraltar, Kehl, Brazzaville |
| factor 1 | 29 521 (45.8%) | 0 | 4 | Vaduz ("Schaan"), San Marino ("Serravalle"), Kehl ("Offenburg") |
| factor 1.5 | 33 392 (51.8%) | 0 | 4 | San Marino |
| factor 2 | 36 259 (56.3%) | 0 | 4 | San Marino |
| factor 2.5 | 38 573 (59.8%) | 0 | 4 | none |
| **factor 3** | **40 435 (62.7%)** | **0** | **4** | **none** |
| factor 3.5 | 42 035 (65.2%) | 0 | 4 | Brooklyn ("Brooklyn") |
| factor 10 | 51 351 (79.7%) | 0 | 4 | Brooklyn |
| factor 50 | 59 502 (92.3%) | 0 | 4 | Brooklyn, Torrent ("Torrent") |

k, at factor 3 (self-name 40 435, 62.7%, and centre crossed 0 for every k):

| k | Off-centre crossed | Expected cases missed |
|---|---|---|
| 0 (no footprint) | 0 | Trastevere ("Vatican City") |
| 0.001 | 0 | Trastevere |
| 0.0013 | 2 | Trastevere |
| **0.0015** | **4** | **none** |
| 0.00175 | 6 | none |
| 0.002 | 8 | St. Peter's Square ("Rome") |
| 0.003 | 23 | St. Peter's Square |
| 0.006 | 48 | none (the Vatican's own footprint now holds St. Peter's) |
| 0.01 | 70 | none |

**Re-measured on geo table v2** (2026-10-13 snapshot, 64 505 places): the same picture. At factor 3 and k = 0.0015
self-name is 40 437 (62.7%), centre crossed 0, off-centre crossed 4 (the same four below), and all 17 expected cases
pass; the factor and k rows move by at most a few places and miss the same cases as the v1 tables above. The
constants stand.

Factor 3 is the largest that keeps Brooklyn → New York (New York weighs 3.2 times Brooklyn). k must
reach Trastevere (3.4 km from the Rome row: k ≥ 0.0013); 0.0015 is the smallest measured value that
passes every case, and border crossings grow with k from there. A rebuilt table can move these
numbers: re-run the script.

The four border crossings left at these settings, 2 km north of the named place:

| Nearest place | Named after |
|---|---|
| Aflao, Ghana | Lomé, Togo |
| Sheung Shui, Hong Kong | Shenzhen, China |
| Şūr Bāhir, Palestinian Territory | Jerusalem, Israel |
| Wadi al-Joz, Palestinian Territory (north of the Jerusalem row) | Jerusalem, Israel |

Expected results on the built table:

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
| Brazzaville | Brazzaville, Republic of the Congo (not Kinshasa) |
| the Dolomites (Plose) | Brixen (Bressanone), Italy |
| open sea | no place |

## Rebuilding

```bash
venv/bin/python scripts/geo/build_place_table.py            # refuses a new GeoNames snapshot
venv/bin/python scripts/geo/build_place_table.py --check    # do the cached inputs match the record?
venv/bin/python scripts/geo/build_place_table.py --new-snapshot
```

The script downloads the three GeoNames inputs into `scripts/memory/geo/` (gitignored, about 210 MB) on first run,
with each one's `Last-Modified` beside it, reuses them afterwards, and overwrites `places_v2.tsv`,
`countries_v2.tsv` and `places_v2.sources.json`. It needs network access. Re-check the expected results above
against the new table (`tests/unit/adapters/test_geo_expected_places.py`, and re-run
`scripts/geo/measure_place_preference.py`) before committing it.

## Version bump rule

When the table's content changes, bump `GEO_TABLE_VERSION` in `src/domain/drive_index.py` in the
same commit. Records carry the `geo_table_version` their place name was resolved with, and the
upgrade step re-resolves place names (and, from v2, place forms and the keys made from them) of records below the
current version. A table change without a bump leaves stale place names on existing records. The v1 table
(`places_v1.tsv`) is gone from this branch; tag `drive-search-v1` still has it.
