# Decision: A model-read document's own date is its effective date; parsed documents keep the file date

**Date:** 2026-10-13
**Status:** Accepted (User Drive Step 2, Slice 2+4; owner decision D13)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §3.1, §10.1, §24.4;
`DocumentBrief.doc_date` in `src/domain/drive_index.py`; the `drive_date_from` field description in
`src/infrastructure/agent_manifest.py`; the `dates` line of Smart's `PROTOCOL_AGENT_SELECTION`.

**Decision:** `effective_date` is the date a user means (§3.1). For a file the reader transcribed, the brief's
`doc_date` (the date printed on the bill, the receipt, the contract) becomes `effective_date`, so "the electricity bill
from March" finds a scan uploaded in April. Parsed documents keep the photo-taken / file date order, because only the
model call produces a brief (D10). The asymmetry is said where it matters: the date filter's field description and
Smart's protocol line tell the model which date a filter matches, and to search again without the filter before saying
nothing was found.

**Rejected alternatives:**
- *A date extracted from parsed text by rules:* "the first date in the document" is wrong for letters, contracts and
  statements more often than right.
- *A brief for every parsed document now:* a model call per file over the whole drive (D10); it stays a §14 extension.
- *Keep the file date everywhere:* a scan's upload date says nothing about the bill on it.

**Trigger to revisit:** the §14 brief for parsed documents ships; then every document gets its own date.
