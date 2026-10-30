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
  Cabinet UI → GET /auth/connect-gmail → GoogleOAuthService → FirestoreCredentialStore (encrypted)
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
| `EmailClassifierPort` | `EmailClassificationAgent` |
| `NotificationStatePort` | `FirestoreNotificationStateAdapter` |
| `NotificationChannelFactoryPort` | `NotificationChannelFactory` |

---

## 3. OAuth Authorization Flow

Gmail access requires an incremental OAuth grant (`gmail.readonly` scope) separate from the login OAuth (Google/Firebase).

```
User clicks "Connect Gmail" in Cabinet
  → GET /auth/connect-gmail (requires auth JWT)
       → GmailOAuthService.get_authorization_url()
       → 302 → Google consent screen

User approves → GET /auth/connect-gmail/callback
  → GoogleOAuthService.exchange_code(code)
  → credentials persisted to the encrypted credential store (keyed by user_id and credential_id)
  → WorkerHandler enqueues email_indexing Cloud Task
  → 302 → Cabinet UI (success)
```

Credentials stored: `access_token`, `refresh_token`, `token_expiry`, `provider="gmail"`, `scopes=["gmail.readonly"]`, plus the account identity (Google scope `openid email`, the userinfo `sub`). A consent whose identity read fails stores nothing.

### 3.1 Credential store

Gmail, Google Tasks, Microsoft To Do and OneDrive grants live in `{prefix}credentials` (`FirestoreCredentialStore`). Doc id is `{user_id}:{credential_id}`; the account identity is plaintext, tokens and label are one KMS ciphertext. The main service and the indexer hold the key grant (separate service identities); the relay, sandbox and job get none. The old plaintext `oauth_credentials` collection is never read, so each integration is reconnected once. A To Do consent always re-subscribes and re-reads the list once. See `docs/04_solution_strategy/decisions/credential_store_cutover.md`.

### 3.2 Revoked grant and reconnect

`invalid_grant` on refresh raises `OAuthGrantRevokedError`. The daily review and the indexing worker set `OAuthCredentials.needs_reconnect` and notify the user only on the False-to-True transition; a reconnect or a successful refresh clears the flag. A 403 for an insufficient scope takes the same path (`OAuthScopeInsufficientError`, a subclass), and a Gmail or Tasks consent without the service scope stores nothing (`OAuthScopeNotGrantedError`). `/api/gmail/status` returns the flag and the Cabinet shows **Reconnect Gmail**. A feature state that shows a past connection (Gmail indexing state, drive sync state, To Do config with a primary list) with no credential also shows Reconnect and sends one notice, unless the user disconnected on purpose; the hourly `reconnect_notice_sweep` lists users from the feature stores. See `decisions/gmail_grant_revoked_reconnect.md` and `decisions/reconnect_notice_r1.md`.

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

Chunk size: **100 emails per page** (`GMAIL_DEFAULT_QUERY` filters Primary + Updates tabs, excludes spam/trash).

1. **List:** `GmailProviderAdapter.list_emails(query, page_token)` → `[EmailMetadata]`
2. **Exclusion pre-filter:** Skip senders/domains in `email_exclusions` collection.
3. **Classify:** `EmailClassificationAgent.classify_batch(emails)` → each email gets `category` + `tags` + `should_index` boolean.
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

Skips users with a job already running. Uses `OAuthCredentialsPort.list_users_by_provider("gmail")` for fan-out.

See [../../07_deployment/SCHEDULERS.md](../../07_deployment/SCHEDULERS.md) for scheduler details.

### 4.4 Watchdog

`task_type=email_indexing_watchdog` is triggered by Cloud Scheduler (periodic). It scans all jobs in `RUNNING` state older than 2 hours and marks them `FAILED`. This handles Cloud Tasks timeouts and crash-recovery scenarios.

---

## 5. EmailClassificationAgent

The classifier is a **shared singleton** in `ServiceContainer` (not per-user). It is the explicit exception to the `OUTPUT_FORMAT` token rule:

- **Reason:** Gemini cannot combine `response_mime_type="application/json"` with function calling (tool use) in a single request. The classifier uses tool calling, so JSON mode is unavailable.
- **Instead:** Output format instructions are embedded in the cognitive process prompt. `_parse_response()` extracts JSON from markdown code blocks (`re.search`).
- **This is the only agent** in the codebase where regex fallback in `_parse_response` is permitted. All other agents use `json.loads()` directly.

**Tier:** BALANCED (Gemini Flash). Single LLM call per batch. Returns `EmailClassificationResult` per email.

---

## 6. EmailSearchAgent

Specialist agent registered in `AgentRegistry`. Accessible exclusively via `SmartResponseAgent.delegate_to_specialist()`.

### 6.1 Three Intents

| Intent | Payload | What Happens |
|---|---|---|
| `search_emails` | `{"query": "..."}` | LLM key extraction → 7-stream multi-vector RRF search in `domain_email_facts_v1` |
| `get_email_details` | `{"email_id": "..."}` | Direct Gmail API fetch → full body returned as text |
| `get_email_attachment` | `{"email_id": "...", "filename": "file.pdf"}` | Gmail API download → markitdown conversion → text returned |

### 6.2 Search Implementation (`search_emails`)

Mirrors the `SearchEnrichmentService` RRF pattern for facts:

- LLM (BALANCED tier) extracts `keywords`, `primary_query`, `alternative_query`, `date_range`, `senders` from the user's question.
- `EmailSearchService` runs 7 parallel Firestore queries across 4 vectors.
- Results merged via Reciprocal Rank Fusion (RRF), top-K returned.

### 6.3 Live Access (`get_email_details`, `get_email_attachment`)

No search — direct Gmail API call. The stored index provides `email_id`; the full body is fetched fresh at query time. This avoids storing full email bodies in Firestore (privacy + storage cost).

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

Settings: `gmail_daily_review*` in `UserBotConfig`; Cabinet endpoint `/api/gmail/daily-review`. The hourly fan-out and the once-per-local-date mark are described in `docs/07_deployment/SCHEDULERS.md` (Daily Email Review). The worker fetches the last 24 h of emails (cap 200, full body cleaned by the adapter: BS4, `html.unescape`, invisible-Unicode strip) and passes a structured JSON array to Smart through `notify(save_history=False)`. Smart's protocol: triage each email as [ACTION], [FYI], [DIGEST] or [NOISE]; deep-read with `get_email_details` / `get_email_attachment`; research with `search_web`; then `create_html_page` for a report whose subjects link to Gmail, plus a short chat message. `notify_document_link` saves the report URL and a `fetch_url` hint to history.

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

- `src/domain/email.py`: All email domain models (EmailMetadata, IndexedEmail, IndexingJob, OAuthCredentials, etc.)
- `src/services/email_indexing_service.py`: Indexing pipeline orchestration.
- `src/services/email_search_service.py`: Search, details, and attachment retrieval.
- `src/services/google_oauth_service.py`: OAuth token exchange and refresh.
- `src/adapters/firestore_credential_store.py`: encrypted credential store.
- `src/services/user_notification_service.py`: System alert delivery.
- `src/agents/email_search_agent.py`: Specialist agent (3 intents).
- `src/agents/email_classification_agent.py`: Batch classifier (shared singleton).
- `src/handlers/worker_handler.py`: Cloud Tasks dispatcher.
- `src/adapters/gmail_provider_adapter.py`: Gmail API client.
- `src/web/oauth_app.py`: `/auth/connect-gmail` and `/auth/connect-gmail/callback`.
- `src/web/user_cabinet_app.py`: `/api/gmail/*` management endpoints.

---

## 11. Status

**Status:** ✅ Production Ready (Phases 1–7 complete)

**Last Updated:** 2026-03-02
