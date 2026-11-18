# Gmail Email Indexing (Building Block)

## 1. Overview

The **Gmail Email Indexing** pipeline extends Alek-Core's memory with the user's email archive. It keeps a selected subset of
the mail (the gate drops what its headers show plainly to be marketing, newsletters, platform activity, sign-in mail or
machine notices, and keeps everything else) and makes each kept mail searchable.

**One card per kept mail (email index v2).** The searchable entry of a mail is its **card**: a model-written description
(title, English summary, document type, life areas, the entities, values and dates printed in the mail, a time envelope with
sent, event, due and validity points, and who the mail is about) plus code-built keys and one vector. The card is written once
from the full body and the text of the readable attachments; neither is stored. Search reads cards only
(`card_state == carded`). The design, its mechanisms and the per-slice status are in
`docs/10_rfcs/EMAIL_INDEX_V2_RFC.md`.

**The card lives on the mail's v1 document** (§4.5). The document keeps the mail's headers (`subject`, `from_address`,
`email_date`), the attachment names and the lifecycle marks (`indexed_at`, `consolidated_at`). The classifier's fact
sentence `text`, `tags`, `category` and `metadata` are no longer written (RFC slice 9b), and the four v1 vectors and
`embedding_pending` are gone (slice 10). A stored document that still carries any of these old fields parses (unknown
fields are ignored) and nothing reads them; `scripts/email/drop_v1_vectors.py` deletes the old vectors to free storage.

**Provider-agnostic design:** Adding Outlook = one new `OutlookProviderAdapter`. Zero changes to domain, services, or agents.

---

## 2. Architecture

### 2.1 Hexagonal Position

```
Driving side (user initiates):
  Cabinet UI → GET /auth/connect/gmail → OAuthConnectService → FirestoreCredentialStore (encrypted)
  Cabinet UI → POST /api/gmail/start-indexing → WorkerHandler → Cloud Tasks

Driven side (background processing):
  Cloud Tasks POST /worker (task_type=email_indexing)
    → WorkerHandler → EmailIndexingService
        → GmailProviderAdapter (list + fetch)
        → EmailClassificationAgent (the gate: keep or drop, with a reason)
        → FirestoreIndexedEmailRepository (store)
        → EmailCardIndexer (card, optional: EMAIL_CARD_ONLINE)
        → re-enqueue if more pages
        → UserNotificationService (completion alert)

Query side (SmartAgent delegation):
  SmartResponseAgent.delegate_to_specialist(intent, query)
    → AgentCoordinator → EmailSearchAgent
        → EmailCardSearchService (search_emails, over the card index)
        → EmailSearchService (live headers / fetch / attachment)
        → GmailProviderAdapter (live fetch for details/attachments)
```

### 2.2 Ports Introduced

| Port | Adapter |
|---|---|
| `EmailProviderPort` | `GmailProviderAdapter` |
| `OAuthCredentialsPort` | `FirestoreCredentialStore` (collection `{prefix}credentials`, see § 3.1) |
| `IndexedEmailRepository` | `FirestoreIndexedEmailRepository` |
| `EmailExclusionsPort` | `FirestoreEmailExclusionsAdapter` |
| `EmailIndexingJobRepository` | `FirestoreEmailJobRepository` |
| `EmailClassifierPort` | `EmailClassifierRunner` (`src/composition/`), which builds `EmailClassificationAgent` per batch |
| `EmailCardWriterPort` | `EmailCardRunner` (`src/composition/email_card_runner.py`), which builds `EmailCardAgent`: one model call per mail |
| `EmailCardStore` | `FirestoreEmailCardStore`: the card's field update on the mail's document (compare-and-set) |
| `EmailCardIndex` | `FirestoreEmailCardIndex` (reads: keys, months, nearest, counts), `InMemoryEmailCardIndex` (tests) |
| `NotificationStatePort` | `FirestoreNotificationStateAdapter` |
| `NotificationChannelFactoryPort` | `NotificationChannelFactory` |

---

## 3. OAuth Authorization Flow

Gmail access requires an incremental OAuth grant (`gmail.readonly` scope) separate from the login OAuth (Google/Firebase).

```
User clicks "Connect Gmail" in Cabinet
  → GET /auth/connect/gmail (requires the Cabinet session)
       → OAuthConnectService.start() stores the flow server-side (only the digest of the state)
       → 302 → Google consent screen

User approves → GET /auth/connect/callback (the one callback of every integration)
  → OAuthConnectService.complete() → GoogleOAuthProvider.exchange(code)
  → credentials persisted to the encrypted credential store (keyed by user_id and credential_id)
  → the mail activation clears the deliberate-disconnect mark and the reconnect-notice flag (indexing is started from the Cabinet or by the hourly auto-index)
  → 302 → Cabinet UI (success)
```

Credentials stored: `access_token`, `refresh_token`, `token_expiry`, `provider="gmail"`, `scopes=["gmail.readonly"]`, plus the account identity (Google scope `openid email`, the userinfo `sub`). A consent whose identity read fails stores nothing.

### 3.1 Credential store

Gmail, Google Tasks, Microsoft To Do and OneDrive grants live in `{prefix}credentials` (`FirestoreCredentialStore`). Doc id is `{user_id}:{credential_id}`; the account identity is plaintext, tokens and label are one KMS ciphertext. The main service and the indexer hold the key grant (separate service identities); the relay, sandbox and job get none. The old plaintext `oauth_credentials` collection is never read, so each integration is reconnected once. A To Do consent always re-subscribes and re-reads the list once. See `docs/04_solution_strategy/decisions/credential_store_cutover.md`.

### 3.2 Revoked grant and reconnect

`invalid_grant` on refresh raises `OAuthGrantRevokedError`. The daily review and the indexing worker set `OAuthCredential.needs_reconnect` and notify the user only on the False-to-True transition; a reconnect or a successful refresh clears the flag. A 403 for an insufficient scope takes the same path (`OAuthScopeInsufficientError`, a subclass), and a Gmail or Tasks consent without the service scope stores nothing (`OAuthScopeNotGrantedError`). `/api/gmail/status` returns the flag and the Cabinet shows **Reconnect Gmail**. A feature state that shows a past connection (Gmail indexing state, drive sync state, To Do config with a primary list) with no credential also shows Reconnect and sends one notice, unless the user disconnected on purpose; the hourly `reconnect_notice_sweep` lists users from the feature stores. See `decisions/gmail_grant_revoked_reconnect.md` and `decisions/reconnect_notice_r1.md`.

Token refresh is handled transparently by `GmailProviderAdapter` before each API call. A refresh rejected with `invalid_grant` raises `OAuthGrantRevokedError`; the background paths then flag the credentials `needs_reconnect` and notify the user once (see `docs/04_solution_strategy/decisions/gmail_grant_revoked_reconnect.md`).

---

## 4. Indexing Pipeline

### 4.1 Job Lifecycle

```
PENDING → RUNNING → COMPLETED
                 → FAILED (after error; Watchdog marks stale RUNNING as FAILED after 2h)
```

A job is created with `triggered_by` = `"user"` (manual via Cabinet UI), `"scheduler"` (automatic via Cloud Scheduler), or `"auto"` (OAuth callback). Jobs are resumable: every page writes `next_page_token` to Firestore before re-enqueuing the next Cloud Task.

### 4.2 Per-Page Processing (one Cloud Tasks invocation)

Chunk size: **150 emails per page** (`run_indexing_job(page_size=150)`; `GMAIL_DEFAULT_QUERY` filters Primary + Updates tabs, excludes spam/trash).

1. **List:** `GmailProviderAdapter.list_emails(query, page_token)` → `[EmailMetadata]`
1a. **Skip the stored:** `IndexedEmailRepository.get_consolidation_marks(user_id, ids)` is called once for the page, before the gate. In `incremental` and `backfill` mode an email already in the index is dropped (never re-classified, re-fetched or re-saved; one INFO line counts them): the incremental run starts from the day of `indexed_through`, so it re-lists that day. The `reindex` mode is refused (email index v2, card slice 1): `run_indexing_job` ends such a job `failed` with the error `reindex_disabled` before any Gmail call, since its whole-document re-save would wipe the v2 card fields. The Cabinet does not offer it, and `POST /api/gmail/index` refuses it with 400 `reindex_disabled`; the worker refusal stays for any other caller.
2. **Exclusion pre-filter:** Skip senders/domains in `email_exclusions` collection.
3. **Gate:** `classify_batch(emails, user_id, account_id=job.account_id)` on the classifier port → each email gets `keep`, a closed `reason` and `forced` (§5). One INFO line per answered page, counts only: `[EmailGate] page: listed= kept= dropped= forced= (record= correspondence= notice= unsure= promotion= newsletter= social= sign_in= automated=)`. A failed gate call (invalid JSON twice, an unexpected error) keeps nothing: the page's emails count as failed and one ops alert is posted (`🚨 Email classifier failed`).
   A model-provider outage (`PROVIDER_OUTAGE_TYPES`, incl. both providers down) is not a classification failure: it propagates, the job stays `running`, `/worker` answers 503 and Cloud Tasks retries the page; when the attempts run out the job stays `running` until the watchdog (§4.4) marks it `failed`.
4. **Filter:** Drop emails where `keep` is false.
5. **Fetch:** `GmailProviderAdapter.batch_get_full_content(kept ids)` → full body + attachment names (the card step's input).
6. **Store:** `FirestoreIndexedEmailRepository.save_batch([IndexedEmail])` — headers, attachment names, `indexed_at`; upsert by `email_id`.
6a. **Card (email index v2, optional):** when `EMAIL_CARD_ONLINE` is on, `EmailCardIndexer.card_page` makes the v2 card of each mail just saved and stores it with one compare-and-set field update (`expect = absent`) on the same document, after the v1 save. A failure of this step is logged (`[EmailCard] page step failed`) and never stops the page; only a revoked grant propagates. See "Card fields on the facts document" below.
7. **Advance:** Update `IndexingJob.next_page_token`, increment counters.
8. **Re-enqueue:** If `next_page_token` present → enqueue next Cloud Task.
9. **Complete:** If no more pages → mark job `COMPLETED`, call `UserNotificationService.send_system_alert()`.

### 4.3 Scheduled Auto-Indexing

Users can enable daily incremental indexing from Cabinet UI > Integrations > Gmail. Two settings stored in `UserBotConfig`:

| Field | Default | Description |
|-------|---------|-------------|
| `gmail_auto_index` | `False` | Enable/disable daily auto-index |
| `gmail_auto_index_hour` | `8` | Local hour (0–23) in user's timezone |

**Flow:** Cloud Scheduler fires `start_email_indexing` hourly → `WorkerHandler._handle_start_email_indexing()` iterates all Gmail users → checks `gmail_auto_index=True` and that the tick falls in the one-hour slot of `gmail_auto_index_hour` on a local date (`domain/auto_index_slot.py`: an hour a spring-forward day skips fires at the next local hour, a repeated hour counts once) → creates incremental job and enqueues first Cloud Task.

The day's start is claimed in the job journal, not by the tick: the job is created under the deterministic id `auto-{user}-{provider}-{local date}` with `create_job_if_absent` (a document `create`), so repeated ticks, the repeated DST hour and two concurrent deliveries start one job per local date (rhythm from stored state, `decisions/worker_task_dedup.md`). A user whose stored timezone is not an IANA zone is skipped with a warning; the other users are still served.

Skips users with a job already running. Uses `OAuthCredentialsPort.list_user_ids("gmail")` for fan-out.

See [../../07_deployment/SCHEDULERS.md](../../07_deployment/SCHEDULERS.md) for scheduler details.

### 4.4 Watchdog

`task_type=email_indexing_watchdog` is triggered by Cloud Scheduler (periodic). It scans all jobs in `RUNNING` state older than 2 hours and marks them `FAILED`, each write conditional on the job still being `running` (`update_job(..., expected_status="running")`), so a job that completes after the scan stays `COMPLETED`. This handles Cloud Tasks timeouts and crash-recovery scenarios.

---

### 4.5 Card fields on the facts document (email index v2, slice 6)

The v2 card is stored as extra fields on the mail's v1 document (`{user_id}_{email_id}` in the v1 facts collection); the
write is a field update through `FirestoreEmailCardStore.write_card` (one transaction: read `card_version` and
`people_digest`, compare with what the writer read, update). **It never touches a v1 field and never touches
`consolidated_at`; it never creates a document.** `IndexedEmail` ignores the extra fields when v1 reads the document.

- Lifecycle: `card_state` (`pending`, `carded`, `empty`, `source_gone`; `abandoned` is slice 7's), `card_version`,
  `people_digest` (fingerprint of the confirmed-people entries the card depends on), `card_model`, `carded_at`,
  `card_failures` (charged failures on this input), `card_reason` (`invalid`, `refused`, `outage`, `attachments`,
  `budget`, `source`, `error`, `gone`), `card_vector_pending` (card kept, vector to be re-embedded).
- Settled cards (`carded`, `empty`) also hold `about_keys`, `name_keys`, `people_keys`, `month_keys`, `template_key`, the
  `card` map (slice 3's document) and, for a carded card with a vector, `card_vector` (768).
- Rules live in `src/domain/email_card_lifecycle.py`; a page spends at most 240 s on cards, four in flight; mail not
  started by then is stored `pending` (reason `budget`).
- **Switch:** `EMAIL_CARD_ONLINE` (optional, **default off**; `1` carding on, anything else off), read with `os.getenv` in
  `src/composition/email_card_setup.py`. Off: v1 indexing is exactly as before. Rollback: unset it; no data change.
- **Indexes** (`config/firestore.indexes.json`, collection group `development_domain_email_facts_v1`): six composites
  (`user_id`, `card_state`, plus each of `about_keys`, `name_keys`, `people_keys`, `month_keys` as CONTAINS; the
  `card_state`, `user_id`, `card_vector` vector index; `user_id`, `card_state` for counts and the re-card scan) and four
  single-field exemptions (`card`, `card_vector`, `about_keys`, `name_keys`). They must be `READY` before the code that
  queries them deploys: search v2 (§6) reads them, the write path needs none. `make deploy-indexes` skips vector indexes,
  so the card vector index is created by `scripts/infrastructure/create_email_card_vector_index.sh` (the owner runs it
  before the deploy; a missing index makes the meaning list fail quietly, so search returns only the keyed results).
- **Readers.** `FirestoreEmailCardIndex` reads `user_id == u` and `card_state == carded` only, with a field projection that
  never includes the v1 vectors or text: keys by `array-contains-any` (`about_keys`, `name_keys`, `people_keys`) and
  `month_keys`, cosine kNN on `card_vector` (the distance comes back as a selected field; no distance threshold, the
  z-score abstains instead), and counts of carded and uncarded mail. An unreadable card is skipped with a WARNING; a
  document of another user is skipped with an ERROR.
- **Scanned PDFs.** A scan of at most 10 pages is read by the paid reader under a per-user daily page cap (knob
  `EMAIL_READER_PAGES_PER_DAY`, `0` turns the reader off online); otherwise its outcome is `scan_not_read`.
- **Re-card.** Stored mail is carded by the owner-run scripts under `scripts/email/` (skip-before-fetch scan, enforced
  dollar and reader-page caps, resume). The v2 write never touches `consolidated_at`, so re-carded mail never re-enters
  memory consolidation.
- Check: `python scripts/email/card_store_status.py [--since YYYY-MM-DD]` prints counts per `card_state` (counts only).
- **Scanned PDFs (slice 5).** A PDF the router would hand to the reader (`no_text`, `low_density`, `garbage_text`,
  `parser_error`) is read by the paid reader when it has 10 pages or fewer (all or nothing; an 11-page scan or one with
  no page count is not read) and the page budget allows it; its outcome is `read_by_reader` and its text feeds the card
  writer and grading like text-layer text. The attachment service reads in two passes (the parser-only read, whose
  page count decides, then a second read with `reader_pages`), asks a `ReaderPageBudget` (online: per user and UTC day,
  one document in `{p}email_reader_pages`, default 40 pages, refusing when the store errors), and starts a read only
  with at least 60 s left before the mail's deadline. A refusal by budget, time or a reader outage (`MODEL_UNAVAILABLE`)
  stays `scan_not_read` and is not cached; the reader's own cost is billed by the reader's ledger. Knob:
  `EMAIL_READER_PAGES_PER_DAY` (optional, default 40, `0` turns the online scan reader off; read with `os.getenv` in
  `composition/email_card_setup.py`). There is no `CARD_VERSION` bump: stored mail whose scans were skipped is selected by
  `python scripts/email/card_store_status.py --scans [--ids-out scripts/memory/email_cards/scans.txt]` and re-carded with
  `recard_subscription.py --max-reader-pages N --max-usd X` (`--dry-run` projects the pages and dollars first;
  `card_probe.py --max-reader-pages N` is the paid probe).
- **Memory reads the cards** (slices 9, 9b): the repository also implements `EmailMemoryBatches` (`memory_batch`), the batch
  consolidation's Stage 3 always reads: unconsolidated mail whose card is settled (`carded`, `empty`,
  `abandoned`, `source_gone`) plus any mail indexed more than 6 h ago, oldest first, projected to the v1 fields without
  vectors plus `card_state`, `card_version`, `template_key`, `card`. Its settled read needs one more composite
  (`user_id`, `consolidated_at`, `card_state`, `indexed_at`); the stale read uses the v1 consolidation index. Because the
  card write never touches `consolidated_at`, a mail consolidated once (as a card, as its headers or skipped) never re-enters. What
  memory is sent of a card: `docs/05_building_blocks/memory/README.md` Step 8.

## 5. EmailClassificationAgent

**Its role.** The classifier is the **gate** (RFC §4): it decides which listed mail is kept (and so carded, searchable and a
memory candidate) and which is dropped. A dropped mail is findable only while recent (the live lists, §6.2) or by a literal
identifier. It writes nothing that is stored: no fact sentence, tags or category (RFC slice 9b).

**One shot.** One call per page in JSON mode (`response_mime_type="application/json"`, no tools, thinking `medium`), from
each mail's subject, From header, sent date and the first 300 characters of its snippet. There is no body tool. An invalid
answer gets the standard parse retry (`MAX_PARSE_RETRIES = 1`); a second invalid answer fails the page (nothing is stored,
the ops alert fires).

**Closed reasons and the code rule** (`src/domain/email_gate.py`). Keep: `record`, `correspondence`, `notice`, `unsure`;
drop: `promotion`, `newsletter`, `social`, `sign_in`, `automated` (the RFC's `document` keep reason is dropped: the gate's
input has no attachment names). The reasons with their meanings are rendered by code as a static block
(`render_gate_reasons_block`, passed through `extra_static_blocks`); the tokens never restate them. Over the answer code
leans to keep (`gate_decisions`): a listed mail the answer leaves out, a reason on neither list, or a reason from the other
decision's list is kept and marked `forced`; an id not on the page is ignored; a duplicate id keeps when either answer
keeps.

**Prompt.** Profile `email_classifier`, blueprint `email_classifier_v1` (outer class `EmailGate`), tokens
`EMAIL_CLASSIFIER_TAXONOMY` (what keeping and dropping cost, why it leans to keep), `EMAIL_CLASSIFIER_COGNITIVE_PROCESS`
(the input fields, decide from them alone) and `OUTPUT_FORMAT_EMAIL_CLASSIFIER` (`[{email_id, decision, reason}]`, every
listed mail once). No Today line.

**How it is built and billed.** `EmailIndexingService` holds `EmailClassifierRunner` (one per process, in `ServiceContainer`),
not the agent: services/ cannot import agents/. Per batch the runner reads the job user's `UserBotConfig`, builds the agent
through `AgentContextBuilder` (the user's tier and model overrides for `email_classifier`; the provider stays Gemini, the only
allowed one) and sets the quota service and the prompt-content store on it, as `UserAgentFactory` does for its agents. The
batch is one execution: `classify_batch` opens a `TokenLedger` for the job's `account_id`, every call (a parse retry too) lands in
it, and it is flushed to the account's usage at the end (also when the batch fails). The runner runs the batch with the job's
user as the request context's user (`request_user_scope`), so each `prompt_content` row carries the job's `user_id` and
`account_id` (`agent_type = email_classifier`, `agent_id = email_classifier_<user_id>`); the prompt is assembled for that
account. Concurrent jobs of two accounts each bill and record to their own. A job with an empty `account_id` is
classified unbilled, with one WARNING `[EmailClassifier] user <8>: … will not be billed`. An unknown user, or a classifier that cannot be
built, fails the job (nothing classified under another configuration). Before roadmap 14b (P9 finding D2) one shared agent
built with the default configuration ran every user's batch outside any billing scope: unbilled and absent from
`prompt_content`.

**Tier:** ECO (Gemini Flash-Lite) by default, the user's `agent_tiers["email_classifier"]` when set. One call per batch (two with a parse retry). Returns `EmailClassificationResult` (`email_id`, `keep`, `reason`, `forced`, `failure`) per email.

---

## 6. EmailSearchAgent

Specialist agent registered in `AgentRegistry`. Accessible exclusively via `SmartResponseAgent.delegate_to_specialist()`.

### 6.1 Three Intents

| Intent | Payload | What Happens |
|---|---|---|
| `search_emails` | `{"query": "..."}` | One small slots call, then `EmailCardSearchService` over the card index (§6.2), plus live headers of mail newer than the index and literal matches of exact keys. |
| `get_email_details` | `{"email_id": "..."}` | Direct Gmail API fetch → full body returned as text |
| `open_email_attachment` | `{"file_ref": "email:<message id>/<part>"}` or `{"email_id": "...", "name": "file.pdf"}` | Gmail API: one listing of the message, one download of the chosen part → `FileConversionService.open_email` → document service (scans through the reader, native PDF/image attached for models that read them) |

### 6.2 Search Implementation (`search_emails`, v2)

`EmailSearchAgent._handle_search_emails_v2` (when it holds a `card_search`); the design is RFC §5.

1. **Request.** The coordinator's timestamp prefix is stripped first; left in, every request would parse as a one-day list
   for today.
2. **Slots.** One call on the agent's ECO Gemini context with its own profile `email_query_slots` (token
   `OUTPUT_FORMAT_EMAIL_QUERY_SLOTS`; no biographical facts, directives or location). Inputs: the request, the last three
   history turns, and `Today: <date> (<weekday>), timezone <zone>` in the user message. Output (JSON schema, closed enums):
   `subject` (English phrase for the meaning search), `doc_types`, `domains`, `people` (names or relation words),
   `time {intent, from, to}` (`period`, `past`, `upcoming`, `latest` or `none`), `exact_keys`, `scope` (`top` or `all`). Code
   validates every slot (`parse_query_slots`); an invalid answer is retried once; a second failure or an error degrades to
   no slots, and the search still runs on the request's own words. A prompt that cannot be built makes no model call.
3. **Search.** `EmailCardSearchService.search` has two modes.
   - **List:** the request names a period and nothing else (no topic word, exact key, person, type or area): every carded
     mail of the period, newest first, folded, with a count line, no embedding call.
   - **Ranked:** the request becomes concepts by the drive's `query_concepts` (numbers first; generic and mail words
     removed). The word streams (`about_keys`, `people_keys`, `name_keys`) go through the drive's tier classifier
     (About, By translation, Labelled) with its allowances; the exact-key, people, doc-type, area and time-window streams
     add candidates; the meaning list (card kNN on `subject`, top 50, robust z-score, abstains on a small or flat pool) is
     the base. **The U rule:** slots add candidates and re-rank, never remove. A candidate's rank is its best tier, then the
     number of slot lists that found it, then the meaning score, then the newest `when`. A time slot filters only when the
     request's own words name a time; `upcoming` matches an event, due or validity point from today on. Nothing matches and
     the meaning base abstains: "No indexed mail matches". A request word no card holds gets a count line naming it.
   - **Size and folding:** the first tier group up to 10 lines, the other tiers together up to 8, meaning-only up to 5
     (`scope=all`: up to 50 together); mail of one `template_key` (sender domain plus the subject with digits, dates and
     amounts masked) folds into its newest member with a "+ N more of the same kind" line; only an exact id or amount match
     stops a mail from folding. A recard line says how many mails still have old entries.
4. **Fresh and keyed lists (unchanged).** The index lags by up to a day and keeps a filtered subset, so the same intent adds two
   live, header-only lists decided by code (no model call; `decisions/email_search_fresh_headers.md`), read through
   `EmailProviderPort.list_headers` (newest first, never promotions, spam or trash, bounds as epoch seconds). Each header
   is sender, subject, date (the user's local time) and snippet; the body and the attachment names come with
   `get_email_details`.
   - **Fresh headers.** Mail newer than the index, cap 100. The window opens at the older of the index watermark
     (`indexed_through`) and 00:00 of yesterday in the user's timezone; with no watermark there is no lower bound. In v2 the
     time window bounds the list only when it filters and its intent is `period` or `past`. A truncated list says so.
   - **Exact keys.** Up to 3 identifiers the slots call copied from the request (code keeps only those present in the
     request; at least 3 characters, deduplicated) are matched literally as a phrase across the whole mailbox at any date,
     cap 20 per key, and also against the card keys.
   - **Failures.** A failing live listing never fails the search: not connected adds the `not_connected` note kind, any other
     error logs a warning and adds `unavailable`. A failing card search fails the search; there is no silent fallback to v1.
   An id already shown by the index is dropped from the later lists.
5. **Result.** One JSON object: `index` (`mode`, `sections` as `[kind, text]` pairs, `shown_ids`, `folded_ids`,
   `has_attachments`), `fresh`, `by_key`, `notes`. A section is `tool` text (heads, count lines, footers, hints) or `mail`
   text (card lines and fold lines). `_format_email_search_compact` prints tool sections as they are and wraps each mail
   section in its own `wrap_untrusted` block.
6. **No switch.** The v1 path and `EMAIL_SEARCH_V2` are gone (RFC slice 10): a card-search error fails the search. Logs:
   `[EmailSearchV2]` (mode, shown, uncarded), `[EmailQuerySlots]` (outcome), `meaning list skipped` (the vector index is
   missing); never the request text or mail content.

### 6.3 Live Access (`get_email_details`, `open_email_attachment`)

No search — direct Gmail API call. The stored index provides `email_id`; the full body is fetched fresh at query time. This avoids storing full email bodies in Firestore (privacy + storage cost).

**Body text.** `GmailProviderAdapter` reads the `text/plain` part as written; a message with only an HTML body is converted in the main service (BeautifulSoup) and an external link keeps its URL: `Read more (https://example.org/a?id=7)`, without `utm_*` parameters and fragment, cut at 300 characters with "…"; a mailto:, relative or in-page link keeps its text only. It is the drive parser's form, one rule in `src/domain/link_url.py` (EXTERNAL_FILES_RFC O-U5-2, O-U5-6). Every reader of a live body (`get_email_details`, the daily review, the indexing pass's classification) gets this text.

**Attachment refs.** `get_email_details` lists every attachment once, from the live message (nested, inline and forwarded parts included; each listed part carries `displayed_inline` from its own headers, the listing is unchanged), as a system-made label `[Email file: "<name>" (<size>, <mime>) ref=email:<message id>/<part>]` printed after the untrusted block, and the labels ride in the `email_file_context` history key so a later turn opens the same file by ref. `open_email_attachment` takes a `file_ref` or `email_id` + `name` (`name` is matched exactly, then NFC + case-folded; several matches return the candidate labels as a success with a "pass one ref" note, never a failure). With neither, a message with one attachment opens it and a message with several returns the candidates. **One attachment per call**; for several files the model makes one call per attachment (they run in parallel). Each call lists the message itself, so N opens cost 2N Gmail calls. A scanned PDF goes to the paid reader (up to 200 pages, 60 000 characters per open), the daily review included. The Gmail `attachmentId` is not stored: it is not stable between reads, so the ref carries only the part id. The removed in-process path (`get_attachment`, markitdown in the main service) no longer exists.

**Save and send.** An attachment can be saved to the user's drive (`copy_file_to_drive` with its `email:` ref) and sent to the chat as a file (`send_file_to_user`, FileManagement). Both fetch it with one listing and one download (`FileConversionService.resolve_file`). A send refuses active kinds (HTML, SVG, scripts, executables, shortcuts, disk images, Office or ODF files with macros) with a note and fails closed when a ZIP-family file cannot be checked; the macro check runs in the sandbox (`decisions/email_send_macro_check_in_sandbox.md`, `docs/05_building_blocks/file_conversion/README.md`).

### 6.4 Untrusted marking

An email is someone else's text, so neither tool output lets it pass as an instruction (EXTERNAL_FILES_RFC §4.6, P14). `get_email_details` puts the body and the attachment names in one `<external_content nonce=… ref=<email id>>` block. In `search_emails` the card lines (sender, date, title, summary, entities, attachment names) and fold lines of each **mail section** go in their own block; the tool sections (the heads, count lines, the "open the mail before quoting" reminder, hints) stay outside, since they are instructions of the tool. Both use `wrap_untrusted` from `src/domain/untrusted_text.py` with a fresh nonce per block and print `EXTERNAL_REMINDER` first; a forged closing tag in an email is escaped. The standing `untrusted` line of `PROTOCOL_AGENT_SELECTION` names email bodies and search results (shipped by the owner's prompt upload).

---

## 7. UserNotificationService

Sends background notifications to the user's last active channel when background events complete (email indexing done, errors, reminders, the daily review).

- **State storage:** `user_notification_state` Firestore collection — persists the user's last active channel (`slack_channel_id` or `telegram_chat_id`); system notifications go to the primary channel.
- **Channel factory:** `NotificationChannelFactory` resolves the appropriate adapter (Slack/Telegram) based on stored state.
- **`notify()`:** routes a `system_alert` through a formatter agent (Quick by default; `agent_id_override` selects Smart for reminders and the daily review) and delivers the formatted text, with session history.
- **`notify_raw()`:** sends the given text directly, with no reformatting.
- **Rich content:** `notify()` and `notify_answer_copy()` deliver rich content through the platform's `RichContentService.deliver`, the one routine the chat turn uses too, so a `file` or `widget` is uploaded, never posted as its fallback line (`decisions/rich_content_one_delivery_path.md`).
- **Usage:** `prompt_tokens` in `UsageMetadata` is always uncached input (OpenAI and Gemini subtract cached from total).
- **Triggers:** `EmailIndexingService.completion_alert()` after a job page cycle completes.

### 7.1 Daily email review

Settings: `gmail_daily_review*` in `UserBotConfig`; Cabinet endpoint `/api/gmail/daily-review`. The hourly fan-out and the once-per-local-date mark are described in `docs/07_deployment/SCHEDULERS.md` (Daily Email Review). The worker fetches the last 25 h of emails by exact instants (epoch-second `after:`), so consecutive daily reviews never leave a gap; an overlap of about an hour is accepted (cap 200, full body cleaned by the adapter: BS4, `html.unescape`, invisible-Unicode strip, links kept as in §6.3) and passes a structured JSON array to Smart through `notify(save_history=False)`. Smart's protocol: triage each email as [ACTION], [FYI], [DIGEST] or [NOISE]; deep-read with `get_email_details` / `get_email_attachment`; research with `search_web`; then `create_html_page` for a report whose subjects link to Gmail, plus a short chat message. `notify_document_link` saves the report URL and a `fetch_url` hint to history.

---

## 8. WorkerHandler Dispatch

`POST /worker` is the single Cloud Tasks endpoint. `WorkerHandler` dispatches by `task_type`:

| `task_type` | Handler |
|---|---|
| `email_indexing` | `EmailIndexingService.run_indexing_page()` |
| `email_indexing_watchdog` | `EmailIndexingService.run_watchdog()` |
| `consolidation` | `ConsolidationService.process_user_batches()` |
| `agent_execution` | `AgentWorkerHandler` (ASYNC agent tasks) |

---

## 9. Cabinet UI Integration

The Cabinet web UI (`src/web/user_cabinet_app.py`) exposes management endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /api/gmail/status` | Current indexing state (job status, email count) |
| `POST /api/gmail/start-indexing` | Manually trigger a new indexing job |
| `GET /api/gmail/jobs` | List all past indexing jobs |
| `POST /api/gmail/exclusions` | Add a sender/domain exclusion |
| `DELETE /api/gmail/exclusions/<id>` | Remove an exclusion |
| `POST /api/gmail/disconnect` | Revoke Gmail OAuth and delete credentials |

---

## 10. Code References

- `src/domain/email.py`: All email domain models (EmailMetadata, IndexedEmail, IndexingJob, etc.); the email-index key `GMAIL_INDEX_PROVIDER` is not a credential kind
- `src/services/email_indexing_service.py`: Indexing pipeline orchestration.
- `src/services/email_search_service.py`: Search, details, and attachment retrieval.
- `src/services/oauth_connect_service.py`: the connect flow (start, complete); `src/adapters/google/google_oauth_provider.py`: Google consent URL, code exchange, revoke. Token refresh lives in `GmailProviderAdapter`.
- `src/adapters/firestore_credential_store.py`: encrypted credential store.
- `src/services/user_notification_service.py`: System alert delivery.
- `src/agents/email_search_agent.py`: Specialist agent (3 intents); `search_emails` runs the slots call and `EmailCardSearchService`.
- `src/agents/email_classification_agent.py`: the gate (built per batch); `src/domain/email_gate.py`: its closed reasons and the code rule.
- `src/composition/email_classifier_runner.py`: `EmailClassifierPort` — builds the classifier per batch for the job's user and account.
- `src/handlers/worker_handler.py`: Cloud Tasks dispatcher.
- `src/adapters/gmail_provider_adapter.py`: Gmail API client.
- `src/web/oauth_app.py`: `/auth/connect/<kind>` and `/auth/connect/callback`.
- `src/web/user_cabinet_app.py`: `/api/gmail/*` management endpoints.
- `src/domain/email_attachment.py`, `src/domain/email_attachment_policy.py`, `src/services/email_attachment_text_service.py`: attachment text for the email index v2 (noise filter, the readable kinds, the input cut, one outcome per listed file). Email index v2; read by the card step (slice 6).
- `src/domain/email_card_source.py`, `email_card_prompt.py`, `email_card_reply.py`, `email_card_document.py`, `src/ports/email_card_writer_port.py`, `src/agents/email_card_agent.py`, `src/composition/email_card_runner.py`, `src/services/email_card_service.py`: the email index v2 card writer (one Flash-Lite call per mail, then build and embed). Wired online by `src/composition/email_card_setup.py` behind `EMAIL_CARD_ONLINE` (slice 6; stores through `src/services/email_card_indexer.py`, `src/adapters/firestore_email_card_store.py`, `src/domain/email_card_lifecycle.py`). `EmailFullContent.recipients` carries the To and Cc addresses (the "owner is a direct recipient" input).
- Email card search (slice 8): `src/domain/email_search_*.py` (query, time, merge, fold, texts, render, params), `src/domain/email_query_slots.py` (the slots prompt, schema and user message), `src/ports/email_card_index.py` with `src/adapters/firestore_email_card_index.py` and `src/adapters/in_memory_email_card_index.py`, `src/services/email_card_search_service.py`, `src/composition/email_search_setup.py` (the build), `scripts/infrastructure/create_email_card_vector_index.sh`, and the offline snapshot harness `scripts/validation/email_eval/search_v2_snapshot.py`. Tokens: `OUTPUT_FORMAT_EMAIL_QUERY_SLOTS`, blueprint `email_query_slots_v1`, profile `email_query_slots`.
- Memory feed (slices 9, 9b): the filtered card, or the headers of a mail with no readable card, to Stage 3 email triage; see `docs/05_building_blocks/memory/README.md` and RFC §6.7.

---

## 11. Status

**Status:** Phases 1–7 complete (v1 pipeline). Email index v2 (RFC `docs/10_rfcs/EMAIL_INDEX_V2_RFC.md` §9 has the per-slice
table): the card, attachment text, extraction, the online path and the re-card are built and live; scanned PDFs are built;
the card search is the only email search (no switch); the memory feed is always on (no switch); the v1 vectors, their
indexes, the v1 search and the embedding repair are removed (slice 10, no rollback); the classifier is a one-shot gate with
closed reasons and writes no fact sentence (slice 9b). With `EMAIL_CARD_ONLINE` off, kept mail never gets a card and memory
sees its headers only.

**Last Updated:** 2026-11-16
