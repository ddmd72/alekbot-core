# Gmail Email Indexing (Building Block)

## 1. Overview

The **Gmail Email Indexing** pipeline extends Alek-Core's memory with the user's email archive. Instead of raw Gmail keyword search, it ingests selected emails into Firestore with multi-vector embeddings — enabling semantic search identical to the fact-search pipeline.

**Model analogy:** Like `ConsolidationAgent` discards chitchat from conversations and retains only factual content, the email indexing pipeline discards marketing, shipping notifications, and newsletters — retaining only emails likely to contain personal facts (~10–20% of inbox).

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
        → EmailClassificationAgent (classify + tag)
        → GeminiEmbeddingAdapter (4 vectors)
        → FirestoreIndexedEmailRepository (store)
        → re-enqueue if more pages
        → UserNotificationService (completion alert)

Query side (SmartAgent delegation):
  SmartResponseAgent.delegate_to_specialist(intent, query)
    → AgentCoordinator → EmailSearchAgent
        → EmailSearchService (search / fetch / attachment)
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
1a. **Skip the stored:** `IndexedEmailRepository.get_consolidation_marks(user_id, ids)` is called once for the page, before classification. In `incremental` and `backfill` mode an email already in the index is dropped (never re-classified, re-fetched, re-embedded or re-saved; one INFO line counts them): the incremental run starts from the day of `indexed_through`, so it re-lists that day. The `reindex` mode is refused (email index v2, card slice 1): `run_indexing_job` ends such a job `failed` with the error `reindex_disabled` before any Gmail call, since its whole-document re-save would wipe the v2 card fields. The Cabinet still offers the option (`POST /api/gmail/index` accepts the mode); the job it creates fails at once.
2. **Exclusion pre-filter:** Skip senders/domains in `email_exclusions` collection.
3. **Classify:** `classify_batch(emails, user_id, account_id=job.account_id)` on the classifier port → each email gets `category` + `tags` + `should_index` boolean.
   A model-provider outage (`PROVIDER_OUTAGE_TYPES`, incl. both providers down) is not a classification failure: it propagates, the job stays `running`, `/worker` answers 503 and Cloud Tasks retries the page; when the attempts run out the job stays `running` until the watchdog (§4.4) marks it `failed`.
4. **Filter:** Drop emails where `should_index=False` (noise).
5. **Fetch:** `GmailProviderAdapter.get_email_content(email_id)` → full body + attachments list.
6. **Embed:** `GeminiEmbeddingAdapter.embed_text()` × 4 vectors:
   - `text_vector` — full body embedding
   - `tags_vector` — category + tags
   - `metadata_vector` — sender, subject, date
   - `attachments_vector` — attachment filenames + types
7. **Store:** `FirestoreIndexedEmailRepository.save(IndexedEmail)` — upsert by `email_id`.
8. **Advance:** Update `IndexingJob.next_page_token`, increment counters.
9. **Re-enqueue:** If `next_page_token` present → enqueue next Cloud Task.
10. **Complete:** If no more pages → mark job `COMPLETED`, call `UserNotificationService.send_system_alert()`.

### 4.3 Scheduled Auto-Indexing

Users can enable daily incremental indexing from Cabinet UI > Integrations > Gmail. Two settings stored in `UserBotConfig`:

| Field | Default | Description |
|-------|---------|-------------|
| `gmail_auto_index` | `False` | Enable/disable daily auto-index |
| `gmail_auto_index_hour` | `8` | Local hour (0–23) in user's timezone |

**Flow:** Cloud Scheduler fires `start_email_indexing` hourly → `WorkerHandler._handle_start_email_indexing()` iterates all Gmail users → checks `gmail_auto_index=True` and `current_local_hour == gmail_auto_index_hour` → creates incremental job and enqueues first Cloud Task.

Skips users with a job already running. Uses `OAuthCredentialsPort.list_user_ids("gmail")` for fan-out.

See [../../07_deployment/SCHEDULERS.md](../../07_deployment/SCHEDULERS.md) for scheduler details.

### 4.4 Watchdog

`task_type=email_indexing_watchdog` is triggered by Cloud Scheduler (periodic). It scans all jobs in `RUNNING` state older than 2 hours and marks them `FAILED`. This handles Cloud Tasks timeouts and crash-recovery scenarios.

---

## 5. EmailClassificationAgent

**How it is built and billed.** `EmailIndexingService` holds `EmailClassifierRunner` (one per process, in `ServiceContainer`),
not the agent: services/ cannot import agents/. Per batch the runner reads the job user's `UserBotConfig`, builds the agent
through `AgentContextBuilder` (the user's tier and model overrides for `email_classifier`; the provider stays Gemini, the only
allowed one) and sets the quota service and the prompt-content store on it, as `UserAgentFactory` does for its agents. The
batch is one execution: `classify_batch` opens a `TokenLedger` for the job's `account_id`, every turn of the tool loop lands in
it, and it is flushed to the account's usage at the end (also when the batch fails). The runner runs the batch with the job's
user as the request context's user (`request_user_scope`), so each `prompt_content` row carries the job's `user_id` and
`account_id` (`agent_type = email_classifier`, `agent_id = email_classifier_<user_id>`); the prompt is assembled for that
account. Concurrent jobs of two accounts each bill and record to their own. A job with an empty `account_id` is
classified unbilled, with one WARNING `[EmailClassifier] user <8>: … will not be billed`. An unknown user, or a classifier that cannot be
built, fails the job (nothing classified under another configuration). Before roadmap 14b (P9 finding D2) one shared agent
built with the default configuration ran every user's batch outside any billing scope: unbilled and absent from
`prompt_content`.

The classifier is the explicit exception to the `OUTPUT_FORMAT` token rule:

- **Reason:** Gemini cannot combine `response_mime_type="application/json"` with function calling (tool use) in a single request. The classifier uses tool calling, so JSON mode is unavailable.
- **Instead:** Output format instructions are embedded in the cognitive process prompt. `_parse_response()` extracts JSON from markdown code blocks (`re.search`).
- **This is the only agent** in the codebase where regex fallback in `_parse_response` is permitted. All other agents use `json.loads()` directly.

**Tier:** BALANCED (Gemini Flash) by default, the user's `agent_tiers["email_classifier"]` when set. Single LLM call per batch. Returns `EmailClassificationResult` per email.

---

## 6. EmailSearchAgent

Specialist agent registered in `AgentRegistry`. Accessible exclusively via `SmartResponseAgent.delegate_to_specialist()`.

### 6.1 Three Intents

| Intent | Payload | What Happens |
|---|---|---|
| `search_emails` | `{"query": "..."}` | LLM key extraction → 7-stream multi-vector RRF search in `domain_email_facts_v1`, plus live headers of mail newer than the index and literal matches of exact keys (§6.2) |
| `get_email_details` | `{"email_id": "..."}` | Direct Gmail API fetch → full body returned as text |
| `open_email_attachment` | `{"file_ref": "email:<message id>/<part>"}` or `{"email_id": "...", "name": "file.pdf"}` | Gmail API: one listing of the message, one download of the chosen part → `FileConversionService.open_email` → document service (scans through the reader, native PDF/image attached for models that read them) |

### 6.2 Search Implementation (`search_emails`)

Mirrors the `SearchEnrichmentService` RRF pattern for facts:

- LLM (BALANCED tier) extracts `primary_query`, `alternative_query`, `tags`, `date_from`, `date_to` and `exact_keys` from the user's question (no sender field).
- `EmailSearchService` runs 7 parallel Firestore queries across 4 vectors.
- Results merged via Reciprocal Rank Fusion (RRF), top-K returned.

The index holds a filtered subset of the mail (the classifier keeps factual mail) and lags by up to a day, so the same
intent adds two live, header-only lists, decided by code (`EmailSearchAgent._handle_search_emails`; no new LLM call;
`decisions/email_search_fresh_headers.md`). Each header is sender, subject, date (the user's local time) and snippet;
the body and the attachment names come with `get_email_details`. Both lists are appended to the index results, and an id
already shown is not repeated. The agent returns one JSON object (`emails`, `fresh` with `since`, `until`, `listed`,
`truncated`; `by_key` with `listed` per key; `notes` as kinds). `_format_email_search_compact` renders one section per
list: the tool's intro and status lines outside, the list's own sender/subject/snippet lines in their own
`wrap_untrusted` block; a note kind maps to its text there, and the follow-up hints print only when they apply. They read the mailbox through `EmailProviderPort.list_headers`: newest first, never promotions, spam or trash,
bounds as epoch seconds.

- **Fresh headers.** A listing of mail newer than the index, cap 100. The window opens at the older of the index
  watermark (`indexed_through`) and 00:00 of yesterday in the user's timezone; with no watermark (indexing never ran)
  there is no lower bound and the cap gives the newest 100. A requested period narrows the start (`date_from`, the
  user's calendar day) and, when it ends in the past, sets the end. It is skipped when the request ends before the
  window opens, since the index covers that period. A truncated list says so.
- **Exact keys.** Up to 3 identifiers the extraction copied from the request (order, case, invoice, tracking numbers,
  addresses; at least 3 characters, deduplicated) are matched literally as a phrase across the whole mailbox at any
  date, cap 20 per key.
- **Failures.** A failing live listing never fails the search: not connected adds the `not_connected` note kind, any
  other error logs a warning and adds `unavailable`; the index results still return. A failing index search fails
  the search as before.

### 6.3 Live Access (`get_email_details`, `open_email_attachment`)

No search — direct Gmail API call. The stored index provides `email_id`; the full body is fetched fresh at query time. This avoids storing full email bodies in Firestore (privacy + storage cost).

**Body text.** `GmailProviderAdapter` reads the `text/plain` part as written; a message with only an HTML body is converted in the main service (BeautifulSoup) and an external link keeps its URL: `Read more (https://example.org/a?id=7)`, without `utm_*` parameters and fragment, cut at 300 characters with "…"; a mailto:, relative or in-page link keeps its text only. It is the drive parser's form, one rule in `src/domain/link_url.py` (EXTERNAL_FILES_RFC O-U5-2, O-U5-6). Every reader of a live body (`get_email_details`, the daily review, the indexing pass's classification) gets this text.

**Attachment refs.** `get_email_details` lists every attachment once, from the live message (nested, inline and forwarded parts included), as a system-made label `[Email file: "<name>" (<size>, <mime>) ref=email:<message id>/<part>]` printed after the untrusted block, and the labels ride in the `email_file_context` history key so a later turn opens the same file by ref. `open_email_attachment` takes a `file_ref` or `email_id` + `name` (`name` is matched exactly, then NFC + case-folded; several matches return the candidate labels as a success with a "pass one ref" note, never a failure). With neither, a message with one attachment opens it and a message with several returns the candidates. **One attachment per call**; for several files the model makes one call per attachment (they run in parallel). Each call lists the message itself, so N opens cost 2N Gmail calls. A scanned PDF goes to the paid reader (up to 200 pages, 60 000 characters per open), the daily review included. The Gmail `attachmentId` is not stored: it is not stable between reads, so the ref carries only the part id. The removed in-process path (`get_attachment`, markitdown in the main service) no longer exists.

**Save and send.** An attachment can be saved to the user's drive (`copy_file_to_drive` with its `email:` ref) and sent to the chat as a file (`send_file_to_user`, FileManagement). Both fetch it with one listing and one download (`FileConversionService.resolve_file`). A send refuses active kinds (HTML, SVG, scripts, executables, shortcuts, disk images, Office or ODF files with macros) with a note and fails closed when a ZIP-family file cannot be checked; the macro check runs in the sandbox (`decisions/email_send_macro_check_in_sandbox.md`, `docs/05_building_blocks/file_conversion/README.md`).

### 6.4 Untrusted marking

An email is someone else's text, so neither tool output lets it pass as an instruction (EXTERNAL_FILES_RFC §4.6, P14). `get_email_details` puts the body and the attachment names in one `<external_content nonce=… ref=<email id>>` block; the `search_emails` lines (sender, date, attachment names, the indexed fact sentence) go in one block around the list, while the count line and the follow-up hints stay outside. Both use `wrap_untrusted` from `src/domain/untrusted_text.py` with a fresh nonce per block and print `EXTERNAL_REMINDER` first; a forged closing tag in an email is escaped. The standing `untrusted` line of `PROTOCOL_AGENT_SELECTION` names email bodies and search results (shipped by the owner's prompt upload).

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
- `src/agents/email_search_agent.py`: Specialist agent (3 intents).
- `src/agents/email_classification_agent.py`: Batch classifier (built per batch).
- `src/composition/email_classifier_runner.py`: `EmailClassifierPort` — builds the classifier per batch for the job's user and account.
- `src/handlers/worker_handler.py`: Cloud Tasks dispatcher.
- `src/adapters/gmail_provider_adapter.py`: Gmail API client.
- `src/web/oauth_app.py`: `/auth/connect/<kind>` and `/auth/connect/callback`.
- `src/web/user_cabinet_app.py`: `/api/gmail/*` management endpoints.

---

## 11. Status

**Status:** ✅ Production Ready (Phases 1–7 complete)

**Last Updated:** 2026-03-02
