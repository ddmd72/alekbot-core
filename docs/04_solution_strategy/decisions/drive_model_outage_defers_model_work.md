# Decision: A model outage defers only model work; parser work, cards and cascades go on

**Date:** 2026-10-13
**Status:** Accepted (User Drive Step 2, Slice 2+4)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §8.2, §24.7, §24.8;
`DriveModelUnavailable` in `src/domain/drive_extraction.py`, `model_outage` / `model_available` in
`src/domain/drive_index_planner.py`.

**Decision:** When the reader or the triage model cannot be used (429, 5xx, a timeout, a network error, or a missing
prompt in Firestore), the indexer sets `model_backoff_until` on the user's sync state with the §7.7 doubling backoff
(60 s to 1 h) and keeps every record that needs the model `pending`. Everything else in the tick continues: parsed
documents, cards, embeddings, cascades, deletions. A provider that refuses one file (HTTP 400) or answers with
invalid JSON twice is a per-file failure that backs off that file alone; a monthly budget that runs out leaves the
work `pending` until the month turns. A missing prompt is logged at ERROR.

**Rejected alternatives:**
- *Treat the outage as systemic (end the tick, back off the user):* one provider's 429 would stop the user's whole
  index, deletions and renames included, for hours.
- *Count it against the file (`MAX_ATTEMPTS`, then `failed`):* an hour-long outage would fail every scan seen during
  it, and nothing would retry them.

**Residual risk:** a prompt deleted by mistake keeps model work waiting with ERROR lines but no alert of its own
beyond the error-log policy.

**Trigger to revisit:** a provider whose outages are per model rather than per account, or a second model provider
used as a live fallback.
