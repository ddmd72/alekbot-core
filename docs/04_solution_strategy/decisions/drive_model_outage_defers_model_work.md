# Decision: A model outage defers only model work; parser work, cards and cascades go on

**Date:** 2026-10-13
**Status:** Accepted (User Drive Step 2, Slice 2+4)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §8.2, §24.7, §24.8;
`DriveModelUnavailable` in `src/domain/drive_extraction.py`, `classify_model_error` in
`src/infrastructure/drive_model_call.py`, `model_outage` / `model_available` in `src/domain/drive_index_planner.py`,
`_Models` in `src/services/drive_indexing_service.py`.

**Fallback first (amended 2026-10-13, RFC D7 rev 3.17).** Both agents now fail over to Gemini Flash
(`gemini-3.8-flash`): a 429, 5xx, timeout, network failure or open breaker on the primary is re-served there by
`BaseAgent._call_llm`, and the call succeeds. Everything below applies when the fallback fails too (or there is
none: no `GEMINI_API_KEY`, a Gemini primary, an open Gemini breaker): `_call_llm` raises
`BothProvidersUnavailableError`, and `classify_model_error` reads its **primary** cause, so a primary 5xx followed
by a Gemini failure is still charged to the file and a primary 429 still is not. An account-level 4xx on the primary
(`LLMAccountError`, a key, a permission, a model id) is not a failover trigger: it is not re-served and stays an
uncharged outage, as before.

**Decision:** When the reader or the triage model cannot be used now — 429, 5xx, a timeout of a call that cannot be
halved, a network error, an open breaker, an account-level 4xx (key, permission, model id), a provider with no key on
the indexer, a missing or incomplete prompt in Firestore, or anything unexpected — the agent raises
`DriveModelUnavailable` (`src/infrastructure/drive_model_call.py` sorts every failed call). The indexer sets
`model_backoff_until` on the user's sync state with the §7.7 doubling backoff (60 s to 1 h) and keeps every record that
needs the model `pending`, its attempts untouched. Everything else in the tick continues: parsed documents, cards,
embeddings, cascades, deletions. A missing prompt is logged at ERROR and evicted from the prompt cache, so an upload
takes effect at once.

What a file or batch spent before the outage (pages answered, images, calls, cost — all already billed) is counted
against the month and the tick summary, and written to the sync state at once, but it does not end the outage: the
backoff keeps doubling. Only model work that goes through resets it.

**One exception: an outage that may be the file's own is charged to it** (final review F1-1). Some files always get
a 5xx, an unexpected adapter error or a single-page timeout from the provider. Treated as a plain outage, such a
file came back after every backoff, re-read (and re-billed) the windows before the failing one, and drained the
month's pages month after month. So `DriveModelUnavailable` carries `charges_file`: true for a 5xx, a timeout or an
unexpected error; false for a 429, an account-level 4xx (including a credit-balance or billing 400, below), a network failure or an
open breaker, which are the model's
for everyone. When the reader made a call for the file and the outage may be its own, the tick charges it
(`outage_charged`): `model_outages + 1`, the file waits for the backoff in class 3, behind other due work, and the
`MAX_ATTEMPTS`-th charged outage is a counted failure, `failed / extract_error` under the reader's link. A new
content version, a content outcome, or a new reader version starts it again. Triage outages are never charged.

Not outages, because they are about the input, not the model:
- a provider refusal (413, 400 or 422) of a PDF window halves it down to one page, and a single page still refused
  is skipped (an account-level 400 — Anthropic's "credit balance too low", a billing error — is not a refusal: the
  adapter recognises it by the provider's error body as `LLMAccountError`, and it is an uncharged outage): the rest of the file is indexed `truncated`, `pages_refused` counted;
- a refusal of every page of a PDF, or of an image, is terminal: `too_large` when a refusal was 413, else
  `unsupported` (both `metadata_only`, no retry, no re-bill); in triage the refused image is a photograph without a
  caption;
- a window that timed out is halved once; a half that times out again is a counted per-file failure (`error`), since
  the model is answering and these pages are too dense for it (a single-page window or an image that times out is
  an outage);
- output that stays invalid after the parse retry is a per-file failure that backs off that file alone; in triage,
  such a call (or an answer stopped at the output limit, or a refusal raised before any HTTP exchange) is not halved:
  its images each take a per-file retry, and only a refused batch is halved;
- a monthly budget that runs out leaves the work `pending` until the month turns.

**Rejected alternatives:**
- *Treat the outage as systemic (end the tick, back off the user):* one provider's 429 would stop the user's whole
  index, deletions and renames included, for hours.
- *Count every outage against the file (`MAX_ATTEMPTS`, then `failed`):* an hour-long rate limit would fail every
  scan seen during it, and nothing would retry them. Only the outages that may be the file's own are charged.
- *Never charge a file:* a file that always gets a 5xx retried forever, each time re-billing the windows before it.
- *Let usage spent before an outage end it (as a success would):* a provider failing every few calls would reset the
  backoff each time and be hammered at the base delay.
- *Treat every timeout as an outage:* a window too dense to finish in time would defer the file forever and re-read
  its finished windows on each try.

**Residual risk:** a prompt deleted by mistake keeps model work waiting with ERROR lines but no alert of its own
beyond the error-log policy. A real provider outage of 5xx responses long enough to hit the same file three times
fails that file until it is edited or the reader's version changes (RFC §24.14). Anthropic's "credit balance too
low", an HTTP 400, was once read as a refusal of the input and demoted every file and image tried during a credit
lapse; it is now an uncharged outage (status alone cannot tell it apart, so the adapter classifies it by the error
body, RFC §24.14). A Gemini billing error is not recognised that way, and since rev 3.17 Gemini serves the
fallback: a 4xx the fallback raises is not wrapped by `_call_llm` and is sorted as if the primary had raised it —
a Gemini 400 on a failover call is a refusal of that input (a page skipped, an image `unsupported`), not an outage.
Wire tests pin what the adapter sends so that a systematic Gemini 400 does not arise (RFC §24.14).

**Trigger to revisit:** a provider whose outages are per model rather than per account; a Gemini 4xx observed on a
failover call (then wrap the fallback's non-failover errors for the drive agents as an outage).
