# Decision: Four cross-border place names remain at the chosen footprint

**Date:** 2026-10-12
**Status:** Accepted (User Drive Step 2, Slice 1)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §5.3, RFC Δ7, Δ39;
`src/domain/place_lookup.py`, `src/assets/geo/README.md`, `scripts/geo/measure_place_preference.py`.

**Decision:** A photo is named after the nearest place, unless a place within 15 km weighs at least 3× as much and is
in the same country, or holds the photo inside its own footprint (`0.0015 · √weight` km) while the nearest place's
footprint does not. At these settings every expected case of `scripts/geo/expected_places.py` holds (Trastevere →
Rome, St. Peter's Square → Vatican City, Monaco, Vaduz, Kehl, Brazzaville, central Porto, Brooklyn → New York,
…), self-naming at a place's own centre is 62.7 %, and no centre is named across a border. Four
lookups 2 km off a centre still cross a border: Aflao (Ghana) → Lomé (Togo), Sheung Shui (Hong Kong) → Shenzhen
(China), Şūr Bāhir and Wadi al-Joz (Palestinian Territory) → Jerusalem (Israel), country codes as in GeoNames. They are
accepted: the place name is a search aid on a card, not an authoritative location.

**Rejected alternatives:**
- *k = 0.0013 or lower:* Trastevere becomes "Vatican City" (0 or 2 crossings, one expected case lost).
- *k = 0.002–0.003:* St. Peter's Square becomes "Rome" (8 and 23 crossings); at 0.006 the cases pass again, with 48.
- *Polygons or administrative boundaries instead of points:* a different, much larger dataset and a point-in-polygon
  index for four residual cases.
- *Same-country only, no footprint:* every point in Trastevere is nearer the Vatican row than Rome's.

**Trigger to revisit:** a rebuilt place table (re-run the measurement script and update the README), or a user's
photos named after the wrong country near a border.
