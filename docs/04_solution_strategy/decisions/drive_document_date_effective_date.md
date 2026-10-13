# Decision: A model-read document's own date is its effective date; parsed documents keep the file date

**Date:** 2026-10-13
**Status:** Accepted (User Drive Step 2, Slice 2+4; owner decision D13)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §3.1, §10.1, §24.4;
`DocumentBrief.doc_date` in `src/domain/drive_index.py`, `_effective` in `src/domain/drive_index_planner.py`; the
`drive_query` and `drive_date_from` field descriptions in `src/infrastructure/agent_manifest.py`; the `dates` line of
Smart's `PROTOCOL_AGENT_SELECTION`.

**Decision:** `effective_date` is the date a user means (§3.1). For a file the reader transcribed, the brief's
`doc_date` (the date printed on the bill, the receipt, the contract) becomes `effective_date`, so a date filter — the
user limiting when the file is dated, "the bills dated March" — matches the printed date of a scan uploaded in April.
That is all the filter rescues. A question about what a document says, "how much do I owe for electricity for
March?", puts the subject and the month in `drive_query`, and the card and content streams search them (G3 case
`p-electricity`, `expect: date_in_query`); a March filter is not what finds that bill. Parsed documents keep the photo-taken / file date order, because only the model call
produces a brief (D10). The model is not taught the asymmetry. It gets one distinction, the same sentence in the
`drive_query` and `drive_date_from` field descriptions: dates written inside the document go in `drive_query` (the
content exact-term stream matches a numeric or written-out date with a day, month and year in ISO form, on both
sides); the date filters only when the user limits when the file is dated. Smart's protocol keeps only the retry
rule: "If a date-filtered search does not show the file asked for, search again without the filter." Each result
labels its date by source (`taken`, `written in the document`, `file date`), a per-result fact rather than a rule, so
a file date is never quoted as the document's own. G3 scores where Smart put each case's date (`expect`:
`date_in_query`, `date_filter`, `none`) as its own rate; a `date_in_query` case with a `date` holds only when that ISO
date is among the exact terms of the `drive_query` Smart sent.

**Rejected alternatives:**
- *A date extracted from parsed text by rules:* "the first date in the document" is wrong for letters, contracts and
  statements more often than right.
- *A brief for every parsed document now:* a model call per file over the whole drive (D10); it stays a §14 extension.
- *Keep the file date everywhere:* a scan's upload date says nothing about the bill on it.
- *Tell the model which kind of file matches which date:* a rule about our storage that the model would have to apply
  to files it has not seen yet; the slice's review (E-M3) kept that subtlety out of every model-facing text and left
  one distinction about the user's words instead.

**Trigger to revisit:** the §14 brief for parsed documents ships; then every document gets its own date.
