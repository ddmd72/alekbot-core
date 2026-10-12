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

The `Договор_ред1..3` dates contradict their version names on purpose: ред2 is the newest, so a "latest version"
answer cannot be read off the name alone (§10.4). The bulk folders are the scale cases: 1 000 `IMG_NNNN.HEIC` next
to an exact `IMG_4912.jpg`, 600 invoices next to `invoice_2026_03.xlsx`, an 800-note vault that must stay normal,
and a 700-file dump that must become a dense tree. The eval is a regression guard, not a decision tool (§17.8): its
first owner-reviewed run records `bars.json`, and later changes must not fall below them.

## File bytes, G2 and G3 (Slice 2+4, RFC §24.11)

An item's `content` names a file in `files/` (its size becomes that file's length); `thumbnail` names the provider
rendition the drive serves for every size (an image item's own bytes when it has none). A generate entry's
`content_template` (`vault_note`, `invoice_2024`) renders each bulk file from `fixture_texts.py` when the drive serves
it (a one-page PDF with a text layer for `.pdf` names), so the 600 invoices and 800 notes are never committed. Every
file the indexer would read has bytes (`test_drive_eval_fixture_files.py`).

`files/` is made by `scripts/validation/drive_eval/make_fixtures.py` and **committed**, not generated per run: Chromium
renders differently across machines and versions, and `bars.json` pins every served byte (`files_sha256`). Regenerating
is a deliberate fixture change, followed by `--record`. The set: DOCX (the `Договор_ред1..3` family, now with differing
amounts and notice periods), PDFs with a text layer (one two-column, one table-heavy), a broken-font Cyrillic PDF
(fonts with no Unicode map, so text extraction yields `(cid:N)`), two scans with no text layer (UA, ES), a photographed
receipt (RU), three phone screenshots (EN, UA, ES) and a chat screenshot (RU), XLSX, ODS, ODT, ODP, PPTX, CSV, RTF,
EML, VTT, HTML, JSON and Markdown, and three wordless pictures as photo thumbnails. All words are in `fixture_texts.py`.

`gold_g2.json` holds the answer-from-content cases: each has an `expected_substring` (or a `{path: substring}` map for
a version family) that must be in the window of the card holding its path, compared after NFKC, casefold and
whitespace collapsing. Cases cover clauses, amounts, invoice numbers written differently from the document, dates,
tables, a two-column PDF and the `Договор` family. `"ocr": true` marks a case only the model reader can answer (scans,
screenshots, the broken-font PDF); G1 has OCR cases too, found by subject only. Recall is reported per case type and
per `G1`, `G2` and `ocr`.

`gold_g3.json` drives the Smart dry run (`smart_dry_run.py`): positives where the answer is in the fixture documents
(content, knowledge base, OCR, a version family, and memory-vs-document conflicts) and negatives (chit-chat, memory,
web and general questions). The metrics are how often Smart calls `search_files_in_drive`, and how often its search
calls put the date where the case's `expect` says (`date_in_query`: a date written inside the document stays in
`drive_query` and no date filter is set; `date_filter`: the user limits when the file is dated; `none`: no date
filter). `bars_g3.json` holds the positive and expectation rates and their Wilson lower bounds (z = 1.96), which are
the bars; the negative rate is reported. A run is judged only against bars recorded with the same `--repeats`. A
`note` says what a right answer looks like for the owner's review.
