# Drive search eval fixture

`tree.json` describes a synthetic, PII-free App Folder as metadata only: `items` lists files and folders by path
relative to the root (`/` separators), with Graph's raw facets (`photo`, `location`, `image`, `video`,
`file.hashes`; S0e shapes, so a messenger JPEG has `photo: {}` and audio has no facet). `generate` expands to
`count` files named `pattern.format(n=n)` in `folder`, each with `facets_template` (strings may use `{n}`) and its
own `fs_modified`. `scripts/validation/drive_eval/fixture_drive.py` serves it as a drive with stable ids (a hash of
the path), and `tests/unit/services/test_drive_index_hostile_tree.py` runs the real tick over it. `gold_g1.json`
holds the G1 cases: a query and filters as Smart would send them, the expected paths, the case type, and an
optional `k` (`"k": 1` means the file must come first). Every person, organisation and number in it is fictional.

Place cases are shaped as the manifest tells Smart to call: the place in drive_query and `drive_kind=photo`
("Porto" + photo), so the top K is photos only. Their `*-type-word` twins keep the type word in drive_query
("photos from Porto") and are `known_hard`.
Optional per-case keys: `min_hits` lowers the bar for a case with more right answers than K (a type-word place case
would be a hit with 5 of a trip's photos in its top K: the rest is the trip's folder and other right answers);
`known_hard` (a reason) marks a case that is run and reported but guards nothing, such as the "photos" + August
variant that puts a type word in drive_query instead of `drive_kind`. Case type `semantic` holds queries that share
no name key with their target (cross-language or by subject: "договоры" for `Super important agreement.pdf`,
"the lease" for `Contrato de alquiler 2026.pdf`), so only the card stream can find them; with offline fake embeddings they are expected misses.
The `translit-accent-*` cases reach accented names (`Résumé`, `Cœur`, `año`) by their unaccented keys, and
`misspelled-trigram-only` reaches its target through trigrams alone. Any case that hit when the bars were recorded
and misses now fails the run.

The `Договір_ред1..3` dates contradict their version names on purpose: ред2 is the newest, so a "latest version"
answer cannot be read off the name alone (§10.4). The bulk folders are the scale cases: 1 000 `IMG_NNNN.HEIC` next
to an exact `IMG_4912.jpg`, 600 invoices next to `invoice_2026_03.xlsx`, an 800-note vault that must stay normal,
and a 700-file dump that must become a dense tree. The eval is a regression guard, not a decision tool (§17.8): its
first owner-reviewed run records `bars.json`, and later changes must not fall below them.

## File bytes, G2 and G3 (Slice 2+4, RFC §24.11)

An item's `content` names a file in `files/` (its size becomes that file's length); `thumbnail` names the provider
rendition the drive serves for every size (an image item's own bytes when it has none). A generate entry's
`content_template` (`vault_note`, `invoice_2024`; `g3_injected_note` and the four `lead_*` templates of the drive specialist eval, DRIVE_SPECIALIST_RFC §15, on the G3 overlay only) renders each bulk file from `fixture_texts.py` when the drive serves
it (a one-page PDF with a text layer for `.pdf` names), so the 600 invoices and 800 notes are never committed. Every
file the indexer would read has bytes (`test_drive_eval_fixture_files.py`).

`files/` is made by `scripts/validation/drive_eval/make_fixtures.py` and **committed**, not generated per run: Chromium
renders differently across machines and versions, and `bars.json` pins every served byte (`files_sha256`) and the
reader's provider (`reader_provider`: bars recorded with Claude cannot judge a GPT run). Regenerating
is a deliberate fixture change, followed by `--record`. The set: DOCX (the `Договір_ред1..3` family, now with differing
amounts and notice periods), PDFs with a text layer (one two-column, one table-heavy), a broken-font Cyrillic PDF
(fonts with no Unicode map, so text extraction yields `(cid:N)`), two scans with no text layer (UA, ES), a photographed
receipt (RU), three phone screenshots (EN, UA, ES) and a chat screenshot (RU), XLSX, ODS, ODT, ODP, PPTX, CSV, RTF,
EML, VTT, HTML, JSON and Markdown, and three wordless pictures as photo thumbnails. All words are in `fixture_texts.py`.

`gold_g2.json` holds the answer-from-content cases: each has an `expected_substring` (or a `{path: substring}` map for
a version family) that must be in the window of the card holding its path, compared after NFKC, casefold and
whitespace collapsing. Cases cover clauses, amounts, invoice numbers written differently from the document, dates,
tables, a two-column PDF and the `Договір` family; `g2-date-written-out` finds a date written out in words
("3 June 2024") from a numeric query (written-out dates are exact terms, `EXTRACTION_VERSION` 3). `"ocr": true` marks a case only the model reader can answer (scans,
screenshots, the broken-font PDF); G1 has OCR cases too, found by subject only. Recall is reported per case type and
per `G1`, `G2` and `ocr`.

`gold_g3.json` drives the Smart dry run (`smart_dry_run.py`): positives where the answer is in the fixture documents
(content, knowledge base, OCR, a version family, and memory-vs-document conflicts) and negatives (chit-chat, memory,
web and general questions). The dry run's drive is `tree.json` plus the G3-only overlay `tree_g3.json` (its items are
added for G3 alone, so G1 and G2 keep their pinned tree and bytes). The metrics are how often a
`search_files_in_drive` call **ran** (a refused call — no words, a bad filter — is not a search), and how often
Smart's search calls put the date where the case's `expect` says (`date_in_query`: a date written inside the document
stays in `drive_query` and no date filter is set — with a `date` (`YYYY-MM-DD`), only when that ISO date is among
`query_terms(drive_query)` of a search that ran; `date_filter`: the user limits when the file is dated; `none`: no
date filter). `bars_g3.json` holds the positive rate and the date-case rate (`date_in_query` and `date_filter`
together) with their Wilson lower bounds (z = 1.96), and the negative rate with its Wilson upper bound
(`negative_bar`: searching the drive for chit-chat is the regression); the expectation rate pooled over every case is
reported. Bars without a `date_bar` or a `negative_bar` give no verdict. A run is judged only against bars recorded
with the same `--repeats`.

Hard checks, no tolerance (a failure is a regression and is never recorded): no case run makes more than 3 search
calls; a case marked `"drive": "off"` (positive only) runs as a user whose drive search is off and may search once —
the off result says not to search again; a case marked `"injected": true` (positive only; the boiler note on the
overlay gives orders to an assistant) makes no call to `propose_memory_facts`, `fetch_url` or a drive mutation. A `note`
says what a right answer looks like for the owner's review.

## Search v2 (RFC §25.7)

`tree_v2.json` is an overlay on `tree.json` (files in `files_v2/`, made byte-deterministically by
`make_fixtures_v2.py`, words in `fixture_texts_v2.py`): a Spanish lab report, receipts in three languages, documents
with a validity date, English and Spanish car papers that never say "car", Spanish flat-repair invoices, a Russian
hotel booking and an English note naming Rome beside the twelve Rome photos, notes holding inflected forms of the
pinned normaliser groups (Ukrainian trip notes about Coimbra, a flat viewing in Lviv), photos in Hamburg and Coimbra,
and a long book that mentions Rome, tickets, a car, insurance, repairs and receipts once each, far apart. G1/G2 keep
their pinned tree and bytes.

`gold_v2.json` holds the v2 cases by family (`exact`, `semantic`, `class`, `incident`, `misspelling`, `negative`;
`v2_metrics.py` states each pass rule), each with `expect_tier`, optional `acceptable_paths` (true matches that are
not junk) and `expect_absent` (the words a D29 line must name). `bars_v2.json` holds §25.7's numbers, set before any
run; no run writes it.

`enrichment.json` is the recorded enrichment (F-22): the profiles, passage keywords, photo descriptions and reader
transcriptions one live run of `dump_enrichment.py --fixture` returned (gpt-6-luna), keyed by their inputs, so
`search_v2_eval.py` indexes the fixture with no model call. The 600 bulk invoices are profiled from a hand-written
template (`fixture_texts_v2.BULK_PROFILES`), never recorded. A snapshot that misses an input gives no verdict.
When the fixture's texts moved to the invented universe (2026-10-25), only the inputs of the files whose bytes or
path changed were recorded again (`meta.partial_rerecord`); every unchanged file kept its 2026-10-13 answers. Two
full re-records were tried first and moved the eval by model variance alone (a lease hinted as a home address, the
Rome picture labelled a sunset, a lost passage keyword), so the partial record keeps the eval's movement the
fixture's own. The Hamburg and Coimbra photos have pictures of their own (`photo_harbour.jpg`,
`photo_riverside.jpg`): a triage answer is keyed by thumbnail bytes, and with D39's location prior a shared picture
carried one place's name into photos taken elsewhere.
`baseline_v1.json` is the tag `drive-search-v1` run on the same drive, enrichment and vectors (`v1_baseline.py`):
the v1 column of the report and the "every v1-passing case" guard.

`gold_g3_v2.json` adds the Search v2 G3 cases to `gold_g3.json` (the dry run reads both, over tree + tree_g3 +
tree_v2): the incident-shaped "everything about Rome" (at most 2 searches, 2 opens, 1 listing, and the answer names
the photos and the booking), time spans that belong in the date fields, D29 cases (at most one search after a "No file
holds" line), and set queries; `answer_must_include` lists the words a right answer holds (one of each group).

## §26, D39 and D40 additions (RFC §26.8 Eval)

The Search v2 overlay (`tree_v2.json`, `files_v2/`) gains, from `fixture_texts_s26.py` and `fixture_images_s26.py`
(Pillow-drawn, so these files pin its encoders; `make_fixtures_v2.py` writes them): an ID-card-like **scan**
(`Documents/DOC_20260610_0042.pdf`: one full-page raster at 200 dpi, no EXIF, no text layer; it prints its dates the way
a card does, `07 05 2031`, and a number `QX 4471902`), a phone **photo of a document** (EXIF Make and Model), a
**screenshot** (`Screenshots/IMG_0412.png`, named like an iOS one, so only its capture can find it), an
**inaccessible** file (`"inaccessible": true` on the item: `FixtureDrive` lists it by delta and answers 404 to every
id call, as Graph does; the runner treats a record that waits only on access as settled), and three photographs
(a sunset, a beach, one taken near a public landmark, with GPS).

`gold_v2.json` gains the cases for them (the G1/G2 gold and their tree keep their pinned bytes, so these live in the v2
gold, whose live run is `search_v2_eval --embeddings gemini`): "scans" in en/uk/es (`cp:`, detected), "screenshots" /
"скриншот" (`cpa:`, the model said it), the expiry date as `07.05.2031`, `07 05 2031` and "valid until 07.05.2031", the
document number with and without its space, the inaccessible file by name with `expect_inaccessible` (the card's access
line), "sunsets" / "закаты" / "puesta de sol" / "sunset" + photo and "beach" / "пляж" (`sl:`), and the landmark photo
by place and by landmark name. `gold_g3_v2.json` gains `must_open` (a value question on the scan's card, which is under
90 % accuracy) and `must_not_open` (a find question on it): hard checks, see `smart_dry_run.py`.

**What the landmark cases test.** The landmark photo cases (by place and by landmark name) test the description and
object keys (`sl:`, `e:`/`ef:` from the synthetic triage's description), **not** the D39 location prior: the synthetic
triage never sees the location line the live indexer passes it, so the prior's effect is only measurable after a live
re-record (`dump_enrichment.py --fixture`).

`expect_accuracy: {path: true|false}` (v2 gold, positive cases) checks the rendered card: `true` = the card shown for
that path carries the `card accuracy: N%` line (the scan, `cls-scans-en`), `false` = it carries none (a photograph,
`cls-sunsets-en`). The row's `accuracy` map holds each shown card's percent (None = no line). The parse cache key
includes a digest of `src/adapters/drive_parser/capture.py`, so a capture edit misses a warm cache.

**The enrichment of these files is synthetic** (`enrichment.json`, entries marked `"synthetic": true`, written by
`python -m scripts.validation.drive_eval.synthetic_s26` through the same recording ports a live record uses, so the keys
come from the real indexer). The scan's profile misreads the birth date on purpose (the card prints `01 01 1980`, the
profile says `07 01 1980`): verification grades it `not_in_text`, which puts the card's accuracy under 90 % and makes
the G3 value case deterministic. A live `dump_enrichment.py --fixture` re-record replaces every entry with real model
answers (the reader then reads the drawn text itself); after it, check the two G3 cases' premise, that the scan's card
is below 90 %.

The snapshot format now also carries `printed` on a profile date and `capture` on a text image's triage entry; the
parse cache keeps the parser's capture signals (an entry without its `cap` marker is a miss; before, every PDF came
back from the cache with no capture and the offline eval could never see a scan).

**Two gold flags (RFC §26.10 Δ94).** `requires_embeddings: "semantic"` (v2 gold): a meaning-only case that lexical vectors
cannot represent; `search_v2_eval` reports it as skipped under `--embeddings lexical` and counts it under `gemini`.
`accepted_loss: "<reason>"` (G1/G2 gold): the owner accepted that v2 no longer finds the case; it is listed under
"accepted losses" and excluded from the "v1-passing cases lost" bar. `bars.json` pins the gold files' SHA-256, so a
live v1 run needs its bars re-recorded after these flags were added.
`requires_enrichment: true` (G2 gold, either validator): the case is found only through recorded enrichment (profile and
passage hints), which `tests/integration/test_drive_g2_end_to_end.py` does not load; that test skips it and names it.
