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
