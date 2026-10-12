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
`src/domain/place_lookup.py` (`PlaceIndex`) picks the heaviest place within
`PLACE_PREFER_RADIUS_KM` (15 km), else the nearest within `PLACE_MAX_DISTANCE_KM` (50 km), else no
place. The result reads `"Rome (Roma), Italy"`; the local name is omitted when it equals the English one.

Expected results on the built table:

| Photo taken in | Place name |
|---|---|
| Trastevere | Rome (Roma), Italy |
| central Porto | Porto, Portugal (not the parish it was taken in) |
| Brooklyn | New York, United States |
| the Dolomites | Brixen (Bressanone), Italy |
| open sea | no place |

## Rebuilding

```bash
venv/bin/python scripts/geo/build_place_table.py
```

The script downloads the three GeoNames inputs into `scripts/memory/geo/` (gitignored) on first run,
reuses them afterwards, and overwrites `src/assets/geo/places_v1.tsv`. It needs network access.
Re-check the expected results above against the new table before committing it.

## Version bump rule

When the table's content changes, bump `GEO_TABLE_VERSION` in `src/domain/drive_index.py` in the
same commit. Records carry the `geo_table_version` their place name was resolved with, and the
upgrade step re-resolves place names of records below the current version. A table change without a
bump leaves stale place names on existing records.
