<!-- living-doc
covers:
  src/services/email_indexing_service.py
  src/services/email_search_service.py
  src/services/email_card_indexer.py
  src/services/email_card_service.py
  src/services/email_card_search_service.py
  src/services/email_attachment_text_service.py
  src/services/email_review_service.py
  src/services/daily_email_review_task.py
  src/services/start_daily_email_review_task.py
  src/services/gmail_reconnect_flag.py
  src/agents/email_classification_agent.py
  src/agents/email_card_agent.py
  src/agents/email_search_agent.py
  src/adapters/gmail_provider_adapter.py
  src/adapters/firestore_indexed_email_repo.py
  src/adapters/firestore_email_job_repo.py
  src/adapters/firestore_email_card_store.py
  src/adapters/firestore_email_card_index.py
  src/adapters/firestore_email_exclusions_adapter.py
  src/adapters/firestore_email_reader_budget.py
  src/domain/email.py
  src/domain/email_*.py
  src/domain/auto_index_slot.py
  src/composition/email_card_runner.py
  src/ports/email_*.py
  src/ports/indexed_email_repository.py
  src/ports/reader_page_budget.py
  src/adapters/in_memory_email_card_index.py
  src/adapters/job_reader_page_budget.py
  src/domain/reconnect_policy.py
  src/composition/email_classifier_runner.py
  src/composition/email_card_setup.py
  src/composition/email_search_setup.py
  scripts/email/
  src/domain/daily_review_window.py
-->
# Gmail Email Indexing

## Purpose

This block makes the owner's Gmail mailbox part of the exocortex's reach. It does four things: it connects a mailbox
through OAuth, it indexes the mail worth keeping into a searchable store, it lets the chat agents search that store and
read live mail, and it runs a daily inbox review. It is the only place where mail enters the system.

The unit of the index is the **card**: a model-written description of one mail (title, summary, document type, life
areas, the entities, values and dates printed in it, who it is about) plus code-built keys and one vector. A card is
written once, from the full body and the readable attachments, and **the body itself is never stored**; opening a mail
later fetches it from Gmail again. Search reads cards only.

What it is not: it does not write memory facts (the memory consolidation reads the cards and decides, see
[memory](../memory/README.md)), it does not send, delete or label mail (the scope is read-only), and it is not a general
mail client: dropped mail is not in the index at all.

## Owned data

Everything is Firestore, in the `us-production` database, with the environment prefix on collection names.

**Mail documents** — `EnvironmentConfig.domain_email_facts_collection`, one document per mail, id
`{user_id}_{email_id}`. Two writers share the document and never touch each other's fields.

- *The mail record*, written by the indexing job (`FirestoreIndexedEmailRepository.save_batch`): the mail's headers
  (`subject`, `from_address`, `email_date`), the attachment names, `state`, `indexed_at`, and `consolidated_at`, which
  the memory consolidation sets when it has taken the mail (`mark_consolidated`) and which `save_batch` carries over
  rather than clears. Older documents may still hold fields that were removed (fact sentence, tags, v1 vectors); they
  parse and nothing reads them (`scripts/email/drop_v1_vectors.py` frees the vector storage).
- *The card*, written by the card step (`FirestoreEmailCardStore.write_card`) and by the owner's re-card loader. Lifecycle
  fields: `card_state` (`pending`, `carded`, `empty`, `source_gone`, `abandoned`), `card_version`, `people_digest` (a
  fingerprint of the confirmed-people entries the card depends on, so a change in that list makes a card stale),
  `card_model`, `carded_at`, `card_failures` (charged failures on this input), `card_reason` (why a card is not
  settled), `card_vector_pending` (card kept, vector still to be embedded). A settled card (`carded`, `empty`) also holds
  the search keys (`about_keys`, `name_keys`, `people_keys`, `month_keys`, `template_key`), the `card` map and, for
  `carded`, `card_vector`. Readers: `FirestoreEmailCardIndex` (search; `carded` only, never the removed v1 fields) and
  `FirestoreIndexedEmailRepository.memory_batch` (memory).

**Indexing state** — `email_indexing_state_collection`, id `{user_id}_{provider}`. `indexed_through` is the forward
frontier of incremental runs and `oldest_indexed_through` the backward frontier of backfill; both are written only when
a job completes. `cursor_reindex` is a leftover of the removed re-index mode, read only to bootstrap an incremental run.
`reconnect_notified` and `disconnected_by_user_at` belong to the reconnect notice (see Flows). Readers: the next job, the
search agent (`indexed_through` bounds the live "newer than the index" list), the Cabinet status, the reconnect sweep.

**Jobs** — `email_indexing_jobs_collection`. One document per run: mode, status (`running`, `completed`, `failed`,
`failed_auth`, `cancelled`), the `next_page_token` that is the resume point, the newest and oldest mail date seen so far,
counters and a capped error list. Written by the indexing service and the Cabinet; read by the worker, the watchdog and
the Cabinet job views.

**Exclusions** — `email_exclusions_collection`. Sender, domain and subject patterns the indexing job drops before the
gate. **Nothing in `src/` writes this collection**; it is read-only for the product and filled by hand if at all.

**Reader budget** — `email_reader_pages_collection`, one document per user holding the pages of scanned PDFs the paid
reader has been given today (UTC). Written only by `FirestoreEmailReaderBudget.reserve`.

**Credentials and settings** — the Gmail grant is a record in the shared credential store
(`FirestoreCredentialStore`, kind `gmail`), described in
[`credential_store_cutover.md`](../../04_solution_strategy/decisions/credential_store_cutover.md); its `needs_reconnect`
flag is this block's signal that Google rejected the grant. The user's switches live in `UserBotConfig`
(`gmail_auto_index`, `gmail_auto_index_hour`, `gmail_daily_review`, `gmail_daily_review_hour`).

## Flows

### Connect and reconnect

1. The Cabinet sends the user to `/auth/connect/<kind>` (`src/web/oauth_app.py::create_oauth_blueprint`), which needs
   the Cabinet session. `OAuthConnectService.start` stores a single-use flow server-side (keyed by a digest of the OAuth
   state, the PKCE verifier sealed) and returns Google's consent URL. A reconnect passes the credential id.
2. Google returns to the one callback shared by every integration. `OAuthConnectService.complete` consumes the flow,
   exchanges the code (`GoogleOAuthProvider`), refuses a consent that lacks the Gmail scope or, on a reconnect, belongs to
   another Google account, and stores the credential (clearing `needs_reconnect`).
3. The mail activation (`oauth_connect_setup.py::build_connect_activations`) calls
   `IntegrationReconnectService.consented`, which clears the disconnect mark and the notice flag. **A consent starts no
   indexing**: it is started from the Cabinet or by the hourly auto-index.
4. Disconnect (`DELETE /api/gmail/disconnect`) first marks the state "disconnected by user", then removes the credential;
   the indexed mail stays.

### Revoked grant

When Google rejects a refresh token (`invalid_grant`) the adapter raises `OAuthGrantRevokedError`; a 403 for a missing
scope raises its subclass `OAuthScopeInsufficientError`. Both background Gmail paths, the indexing worker
(`WorkerHandler._handle_email_indexing`) and the daily review (`DailyEmailReviewTask`), answer with
`GmailReconnectFlag.flag`: it sets `needs_reconnect` on the credential and sends one chat notice, only on the
false-to-true transition, so a dead token does not produce a daily message. The Cabinet status shows **Reconnect**.

A second net catches a credential that is simply gone (the store emptied, a user who never reconnected):
`IntegrationReconnectService.sweep`, run hourly as the `reconnect_notice_sweep` task, finds users whose indexing state
shows a past connection without a credential and sends the same single notice, unless the user disconnected on purpose.
Decisions: [`gmail_grant_revoked_reconnect.md`](../../04_solution_strategy/decisions/gmail_grant_revoked_reconnect.md),
[`reconnect_notice_r1.md`](../../04_solution_strategy/decisions/reconnect_notice_r1.md).

### Indexing job

A job is one resumable run over a date range, executed as a chain of Cloud Tasks, **one Gmail page per task**.

1. A job is created by the Cabinet (`POST /api/gmail/index`, modes `incremental` or `backfill`; `reindex` is refused with
   400 `reindex_disabled`) or by the auto-index fan-out, persisted, and its first task enqueued
   (`TaskDispatchService.enqueue_email_indexing_task`). Cabinet jobs record `triggered_by` `cabinet`, the fan-out
   `scheduler`.
2. `/worker` receives `email_indexing` (`WorkerHandler._handle_email_indexing`); `EmailIndexingService.load_job_for_execution`
   skips a job that is no longer `running` (completed, failed, cancelled) and marks one without a credential `failed_auth`.
3. `EmailIndexingService.run_indexing_job` lists one page matching `GMAIL_DEFAULT_QUERY` (Primary and Updates tabs, no
   spam or trash). An incremental run starts from the day of `indexed_through`, so it re-lists that day.
4. Mail already in the index is dropped (`get_consolidation_marks` is the existence check), then mail matching an
   exclusion.
5. **The gate** classifies the rest, from headers and a snippet only (see Classification).
6. The kept mail's full content is fetched, and `save_batch` stores the mail record.
7. If the card step is attached, `EmailCardIndexer.card_page` cards the mail just saved (see Cards). It cannot fail the
   page.
8. The job document is updated with the counters and the next page token; if there is one, the worker enqueues the next
   task. When the last page is done the cursor is written to the indexing state **and then** the job is marked
   `completed`. The cursor moves only at completion, so a job that dies mid-way never advances it.

Nobody is notified when a job completes.

A model-provider outage during the gate is not a failure of the job: it propagates, the job stays `running`, `/worker`
answers 503 and Cloud Tasks retries the page. Any other error ends the job `failed`, or `failed_auth` when the error
text reads like an authorisation problem. The Cabinet can cancel a running job.

### Scheduled auto-indexing and the watchdog

Cloud Scheduler fires `start_email_indexing` every hour (`WorkerHandler._handle_start_email_indexing`).
`EmailIndexingService.start_indexing_for_eligible_users` visits every user with a Gmail credential who has
`gmail_auto_index` on, whose clock falls in the slot of `gmail_auto_index_hour` (`auto_index_slot_date`; a skipped DST
hour fires at the next local hour, a repeated hour counts once), who has no running job and a credential. The day is
claimed **in the job journal, not by the tick**: the job is created under the deterministic id
`auto-{user}-{provider}-{local date}` with `create_job_if_absent`, so repeated ticks and concurrent deliveries start one
job per local date (`worker_task_dedup.md`). A user with an unusable timezone is skipped and the rest are served.

`email_indexing_watchdog` (`EmailIndexingService.mark_stale_jobs_failed`) marks `running` jobs that have not been updated
for a couple of hours as `failed`, each write conditional on the job still being `running`, so a job that finishes during
the scan keeps its status. This is the only way out of a job whose Cloud Tasks attempts ran out.

### Classification (the gate)

The classifier decides which listed mail is kept (and so carded, searchable and a memory candidate) and which is
dropped; it stores nothing itself. One call per page, in JSON mode with no tools, over the subject, sender, date and the
start of the snippet. The answer is closed: a reason from `KEEP_REASONS` or `DROP_REASONS`
(`src/domain/email_gate.py`). Code leans to keep: `gate_decisions` keeps and marks `forced` any mail the answer omits,
any unknown reason, or a reason that belongs to the other decision. An invalid answer is retried
(`max_parse_retries`); a second failure fails the page: those mails are not stored, an ops alert is posted, and the job
moves on.

The agent is built per batch by `EmailClassifierRunner` (composition, because services cannot import agents), for the
job's user and account, so the calls are billed to that account and recorded in `prompt_content`. Provider is Gemini
only; the default tier is BALANCED (`_DEFAULT_AGENT_TIERS`), overridable per user.

### Cards

Behind the switch `EMAIL_CARD_ONLINE`, `attach_email_card_step` (`src/composition/email_card_setup.py`) gives the
indexing service an `EmailCardIndexer`. For each saved mail it builds the input (body, recipients, the text of readable
attachments from `EmailAttachmentTextService`), asks the card writer (`EmailCardAgent` through `EmailCardRunner`), builds
the keys and the vector (`EmailCardService`), and stores the result with one compare-and-set field update
(`FirestoreEmailCardStore.write_card`). A page spends at most `CARD_PAGE_BUDGET_S` on cards with `CARD_CONCURRENCY` in
flight; mail not started in time is stored `pending` with reason `budget` and is picked up by a re-card.

Scanned PDFs go to a paid reader under a per-user daily page cap (`EMAIL_READER_PAGES_PER_DAY`; `0` turns it off), booked
through `FirestoreEmailReaderBudget`; a refusal leaves the attachment unread, not the card unwritten.

Stored mail is re-carded by the owner, in two steps: `scripts/email/recard_subscription.py` writes cards to a local file
(nothing reaches Firestore), and `scripts/email/load_cards.py --apply` loads the settled ones through the same
`write_card`. `scripts/email/card_store_status.py` prints counts per `card_state`.

### Search and live access

The chat agent reaches mail through `EmailSearchAgent`, only by delegation from Smart, with three intents.

- **`search_emails`** (`EmailSearchAgent._handle_search_emails_v2`). A small model call turns the request into closed
  slots (subject, document types, areas, people, a time intent, exact keys; `parse_query_slots` validates them, and a
  failure degrades to a search on the request's own words). `EmailCardSearchService.search` then answers from the card
  index in one of two modes: a **list** when the request names only a period (every carded mail of that period, newest
  first) or a **ranked** search that merges key streams and a vector "meaning" list. Slots add candidates and re-rank but
  never remove one. Mail from one sender template folds into its newest member. A card-search error fails the search;
  there is no fallback to anything older.
- The same intent adds two **live header lists** decided by code, because the index lags and keeps only a subset: mail
  newer than the index watermark, and literal matches of identifiers the request contained, across the whole mailbox
  (`EmailSearchService.list_headers`; `email_search_fresh_headers.md`). A failing live list adds a note and never fails
  the search.
- **`get_email_details`** fetches the full body from Gmail (`EmailSearchService.get_details_with_parts`) and lists every
  attachment as a label carrying an `email:` ref; the labels ride in the history so a later turn can open a file.
- **`open_email_attachment`** takes such a ref (or a message id and a name) and opens one attachment through
  `FileConversionService.open_email` (scans through the reader). One attachment per call. Saving or sending an attachment
  uses the same ref: see [file_conversion](../file_conversion/README.md).

### Daily email review

Hourly, `StartDailyEmailReviewTask` finds users with `gmail_daily_review` whose review slot is open and claims a
per-(user, local date) mark before enqueuing one `daily_email_review` task, so the review runs once per local date; the
mark is released if the enqueue fails. `DailyEmailReviewTask` asks `EmailReviewService.fetch_review_payload` for the last
day of mail by exact instants (a window slightly wider than 24 h, so consecutive reviews leave no gap), with full cleaned
bodies, and hands it to Smart through `UserNotificationService.notify`. Smart triages, may read attachments and search
memory and the web, and delivers an HTML report plus a short chat message. A revoked grant goes to the reconnect flag
above; an empty answer is final (no re-run), a turn that never started is retried by Cloud Tasks, one that started is not.
Scheduling details: [SCHEDULERS.md](../../07_deployment/SCHEDULERS.md).

### Memory feed

The memory consolidation's mail stage reads unconsolidated mail through `EmailMemoryBatches.memory_batch`: mail whose card
is settled, plus any mail old enough that its card is not coming, oldest first. Because a card write never touches
`consolidated_at`, a mail consolidated once never returns, even after a re-card. What memory receives of a card:
[memory](../memory/README.md).

## Invariants and traps

- **Mail is someone else's text.** Every place that shows mail to a model wraps it with `wrap_untrusted`
  (`src/domain/untrusted_text.py`) under a fresh nonce: the live body (`EmailSearchService.get_details_with_parts`), each
  mail section of search results (`src/infrastructure/delegation_engine.py::_format_email_search_compact`; the tool's own
  heads and hints stay outside), and the card writer's input (`src/domain/email_card_input.py`). Enforced by
  `tests/unit/services/test_email_details_untrusted_marking.py` and
  `tests/unit/infrastructure/test_email_search_lines_untrusted_marking.py`.
- **A card write touches card fields only and never creates a document.** `FirestoreEmailCardStore.write_card` refuses
  any key outside `CARD_FIELDS`, reports `MISSING` for an absent document and `CONFLICT` when the stored `card_version`
  and `people_digest` differ from what the writer read. Enforced by `tests/unit/adapters/test_email_card_store_kit.py`.
- **`save_batch` rewrites the whole mail document**, which would erase the card fields. That is why a stored mail is never
  re-saved: the job drops stored ids before the gate, and `reindex` is refused (`REINDEX_DISABLED`) in the service and in
  the Cabinet. Enforced by `tests/unit/services/test_email_indexing_skip_stored.py` and
  `tests/unit/services/test_email_indexing_reindex_refused.py`.
- **The gate leans to keep.** A dropped mail is findable only by the live lists or a literal identifier, so anything the
  answer does not settle cleanly is kept. `tests/unit/domain/test_email_gate.py`.
- **The index cursor moves only when a job completes.** A cancelled, failed or killed job leaves it; the next incremental
  run re-lists from the same day and skips what is stored. A mail whose page failed the gate is not stored and is not
  re-listed once the cursor passes it. Enforced by code structure (`_finalize_cursor` is called only on the completion
  branch of `run_indexing_job`); no dedicated test pins it.
- **A day's auto-index starts once.** The claim is the deterministic job id, not the scheduler tick or a Cloud Tasks
  name, which only dedupe for a short time. `tests/unit/services/test_autoindex_hour_instants.py` (skipped and repeated
  DST hours), `tests/unit/adapters/test_email_job_store_conditional.py` (`create_job_if_absent`).
- **Only the watchdog's conditional write ends a stuck job**, and it must not overwrite a job that completed meanwhile
  (`update_job(..., expected_status="running")`). `tests/unit/services/test_email_indexing_service_store_pins.py`.
- **Indexing refreshes the access token in memory only.** `GmailProviderAdapter.refresh_token` returns a new credential
  and persists nothing; the search service and the review persist theirs. The indexing path therefore refreshes again on
  every page, and a successful refresh there does not clear `needs_reconnect`; a reconnect does.
- **`failed` versus `failed_auth` is decided by keywords** in the error text (`run_indexing_job`). A new error message that
  contains `auth`, `token` or `credentials` will be filed as an authorisation failure.
- **A reconnect does not re-index.** A consent only writes the credential.
  `tests/unit/test_reconnect_does_not_reindex.py`.
- **One reconnect notice per breakage.** The flag goes up before the send and comes down only when nothing can have been
  sent. `tests/unit/services/test_gmail_reconnect_flag.py`, `tests/unit/services/test_integration_reconnect_service.py`.
- **The card step is on by default in deploys but off in code.** `email_card_online_enabled` defaults off; `make deploy`
  passes `_EMAIL_CARD_ONLINE` (default on, `EMAIL_CARD_ONLINE_DEV=0` in `.env` turns it off). With it off, kept mail gets
  no card and memory sees headers only. `tests/unit/test_cloudbuild_email_card_switch.py`.
- **Indexes and the vector index come first.** The card queries need composite indexes
  (`config/firestore.indexes.json`) and a vector index that `make deploy-indexes` skips
  (`scripts/infrastructure/create_email_card_vector_index.sh`). Without the vector index the meaning list disappears
  quietly and search returns only keyed results. `tests/unit/test_firestore_indexes_email_cards.py`.
- **`abandoned` is never written.** `CardState.ABANDONED` exists and memory selection reads it, but no code sets it; a mail
  that fails to card repeatedly stays `pending` and is retried by a re-card.
- **Exclusions have no writer** (see Owned data); do not document or build on a Cabinet flow for them.

## Links

- [`EMAIL_INDEX_V2_RFC.md`](../../10_rfcs/EMAIL_INDEX_V2_RFC.md): the card, the gate, the card search, the memory feed.
- [`GMAIL_EMAIL_INDEXING_RFC.md`](../../10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md): the original pipeline.
- [`EXTERNAL_FILES_RFC.md`](../../10_rfcs/EXTERNAL_FILES_RFC.md): untrusted marking, attachment refs, the document service.
- [`SECRETS_AT_REST_RFC.md`](../../10_rfcs/SECRETS_AT_REST_RFC.md) and the decisions
  [`credential_store_cutover.md`](../../04_solution_strategy/decisions/credential_store_cutover.md),
  [`gmail_grant_revoked_reconnect.md`](../../04_solution_strategy/decisions/gmail_grant_revoked_reconnect.md),
  [`reconnect_notice_r1.md`](../../04_solution_strategy/decisions/reconnect_notice_r1.md).
- [`worker_task_dedup.md`](../../04_solution_strategy/decisions/worker_task_dedup.md),
  [`email_search_fresh_headers.md`](../../04_solution_strategy/decisions/email_search_fresh_headers.md),
  [`email_send_macro_check_in_sandbox.md`](../../04_solution_strategy/decisions/email_send_macro_check_in_sandbox.md).
- Deployment: [SCHEDULERS.md](../../07_deployment/SCHEDULERS.md), [README.md](../../07_deployment/README.md) (the
  `EMAIL_CARD_ONLINE`, `EMAIL_READER_PAGES_PER_DAY` knobs).

## Where to start reading

1. `src/services/email_indexing_service.py::EmailIndexingService.run_indexing_job`: one page, end to end.
2. `src/handlers/worker_handler.py::WorkerHandler._handle_email_indexing`: how a page becomes a task chain.
3. `src/services/email_card_indexer.py::EmailCardIndexer.card_page`: how a mail becomes a stored card.
4. `src/agents/email_search_agent.py::EmailSearchAgent._handle_search_emails_v2`: the read side.
5. `src/services/email_card_search_service.py::EmailCardSearchService.search`: ranking over the card index.
6. `src/services/oauth_connect_service.py::OAuthConnectService.complete`: connect and reconnect.
7. `src/services/gmail_reconnect_flag.py::GmailReconnectFlag.flag`: what a dead grant does.
