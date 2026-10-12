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
`PLACE_MAX_DISTANCE_KM` (50 km), else no place. One exception: a place of the **same country as the
nearest one**, within `PLACE_PREFER_RADIUS_KM` (15 km) of the photo and at least
`PLACE_PREFER_WEIGHT_FACTOR` (3) times the nearest place's `weight`, names it instead; of several, the
heaviest. So a photo in a suburb gets the city's name, and never a bigger town across a border. The
result reads `"Rome (Roma), Italy"`; the local name is omitted when it equals the English one.

The factor was picked with `scripts/geo/measure_place_preference.py` (read-only, offline) on the table
built 2026-10-12, 64 458 places. "Self-name" counts places whose own centre is named after themselves;
"cross-border" counts places whose own centre is named after a place in another country:

| Rule | Self-name | Cross-border | Expected cases missed (10 in the script) |
|---|---|---|---|
| before: heaviest within 15 km, any country | 29 240 (45.4%) | 603 | Monaco ("Nice"), Vaduz ("Feldkirch") |
| same country, factor 1 | 29 521 (45.8%) | 0 | Trastevere, Vaduz ("Schaan") |
| same country, factor 1.5 | 33 392 (51.8%) | 0 | Trastevere |
| same country, factor 2 | 36 259 (56.3%) | 0 | Trastevere |
| same country, factor 2.5 | 38 573 (59.8%) | 0 | Trastevere |
| **same country, factor 3** | **40 435 (62.7%)** | **0** | **Trastevere** |
| same country, factor 3.5 | 42 035 (65.2%) | 0 | Trastevere, Brooklyn ("Brooklyn") |
| same country, factor 5 | 45 554 (70.7%) | 0 | Trastevere, Brooklyn |
| same country, factor 10 | 51 351 (79.7%) | 0 | Trastevere, Brooklyn |
| same country, factor 30 | 57 613 (89.4%) | 0 | Trastevere, Brooklyn |
| same country, factor 50 | 59 502 (92.3%) | 0 | Trastevere, Brooklyn, Torrent ("Torrent") |

3 is the largest factor that keeps Brooklyn → New York (New York weighs 3.2 times Brooklyn); Torrent →
Valencia holds up to about 30. A rebuilt table can move these ratios: re-run the script.

Expected results on the built table (pinned by `tests/unit/adapters/test_geo_place_table_expected_places.py`):

| Photo taken in | Place name |
|---|---|
| central Porto, Ribeira | Porto, Portugal (not the parish, not Vila Nova de Gaia) |
| Brooklyn, Williamsburg | New York, United States |
| Torrent | Valencia, Spain |
| Monaco | Monaco, Monaco (not Nice) |
| Vaduz | Vaduz, Liechtenstein (not Feldkirch or Buchs) |
| the Dolomites (Plose) | Brixen (Bressanone), Italy |
| open sea | no place |

Known miss: **Trastevere is "Vatican City, Vatican"**, no longer Rome. Every point in Trastevere is
nearer to the Vatican City row than to Rome's (the Rome row sits east of the centre), and the preference
never crosses a border. Open: whether a heavier foreign place may name a photo whose nearest place is a
tiny enclave.

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
