# Decision: A model outage defers only model work; parser work, cards and cascades go on

**Date:** 2026-10-13
**Status:** Accepted (User Drive Step 2, Slice 2+4)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §8.2, §24.7, §24.8;
`DriveModelUnavailable` in `src/domain/drive_extraction.py`, `classify_model_error` in
`src/infrastructure/drive_model_call.py`, `model_outage` / `model_available` in `src/domain/drive_index_planner.py`,
`_Models` in `src/services/drive_indexing_service.py`.

**Decision:** When the reader or the triage model cannot be used now — 429, 5xx, a timeout of a call that cannot be
halved, a network error, an open breaker, an account-level 4xx (key, permission, model id), a provider with no key on
the indexer, a missing or incomplete prompt in Firestore, or anything unexpected — the agent raises
`DriveModelUnavailable` (`src/infrastructure/drive_model_call.py` sorts every failed call). The indexer sets
`model_backoff_until` on the user's sync state with the §7.7 doubling backoff (60 s to 1 h) and keeps every record that
needs the model `pending`, its attempts untouched. Everything else in the tick continues: parsed documents, cards,
embeddings, cascades, deletions. A missing prompt is logged at ERROR and evicted from the prompt cache, so an upload
takes effect at once.

What a file or batch spent before the outage (pages answered, images, calls, cost — all already billed) is counted
against the month and the tick summary, but it does not end the outage: the backoff keeps doubling. Only model work
that goes through resets it.

Not outages, because they are about the input, not the model:
- a provider refusal (413, 400 or 422) of a PDF window halves it down to one page, and a single page still refused
  is skipped: the rest of the file is indexed `truncated`, `pages_refused` counted;
- a refusal of every page of a PDF, or of an image, is terminal: `too_large` when a refusal was 413, else
  `unsupported` (both `metadata_only`, no retry, no re-bill); in triage the refused image is a photograph without a
  caption;
- a window that timed out is halved once; a half that times out again is a counted per-file failure (`error`), since
  the model is answering and these pages are too dense for it (a single-page window or an image that times out is
  an outage);
- output that stays invalid after the parse retry is a per-file failure that backs off that file alone;
- a monthly budget that runs out leaves the work `pending` until the month turns.

**Rejected alternatives:**
- *Treat the outage as systemic (end the tick, back off the user):* one provider's 429 would stop the user's whole
  index, deletions and renames included, for hours.
- *Count it against the file (`MAX_ATTEMPTS`, then `failed`):* an hour-long outage would fail every scan seen during
  it, and nothing would retry them.
- *Let usage spent before an outage end it (as a success would):* a provider failing every few calls would reset the
  backoff each time and be hammered at the base delay.
- *Treat every timeout as an outage:* a window too dense to finish in time would defer the file forever and re-read
  its finished windows on each try.

**Residual risk:** a prompt deleted by mistake keeps model work waiting with ERROR lines but no alert of its own
beyond the error-log policy.

**Trigger to revisit:** a provider whose outages are per model rather than per account, or a second model provider
used as a live fallback.
