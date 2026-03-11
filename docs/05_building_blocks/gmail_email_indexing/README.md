# Gmail Email Indexing (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the email indexing pipeline: OAuth authorization, background ingestion, classification, semantic search, and live access to the user's Gmail archive.

### When to Read

- **For AI Agents:** Before modifying `EmailSearchAgent`, `EmailClassificationAgent`, or `EmailIndexingService`.
- **For Developers:** When troubleshooting indexing jobs, OAuth flows, or email search quality.

### When to Update

This document MUST be updated when:

- [ ] The indexing pipeline steps or chunking strategy changes.
- [ ] New email providers are added (Outlook, etc.).
- [ ] `EmailSearchAgent` intents or routing logic changes.
- [ ] The Watchdog or job lifecycle transitions change.
- [ ] `UserNotificationService` delivery channels or triggers change.

### Cross-References

- **RFC:** [../../10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md](../../10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md)
- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md)
- **Search Enrichment:** [../search_enrichment/README.md](../search_enrichment/README.md)
- **OAuth Web API:** [../oauth_web_api/README.md](../oauth_web_api/README.md)
- **Database Schema:** [../../08_concepts/DATABASE_SCHEMA.md](../../08_concepts/DATABASE_SCHEMA.md) — Section 6

---

## 1. Overview

The **Gmail Email Indexing** pipeline extends Alek-Core's memory with the user's email archive. Instead of raw Gmail keyword search, it ingests selected emails into Firestore with multi-vector embeddings — enabling semantic search identical to the fact-search pipeline.

**Model analogy:** Like `ConsolidationAgent` discards chitchat from conversations and retains only factual content, the email indexing pipeline discards marketing, shipping notifications, and newsletters — retaining only emails likely to contain personal facts (~10–20% of inbox).

**Provider-agnostic design:** Adding Outlook = one new `OutlookProviderAdapter`. Zero changes to domain, services, or agents.

---

## 2. Architecture

### 2.1 Hexagonal Position

```
Driving side (user initiates):
  Cabinet UI → GET /auth/connect-gmail → GmailOAuthService → OAuthCredentials (Firestore)
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
| `OAuthCredentialsPort` | `FirestoreOAuthCredentialsAdapter` |
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
  → GmailOAuthService.exchange_code(code)
  → OAuthCredentials persisted to Firestore (keyed by user_id)
  → WorkerHandler enqueues email_indexing Cloud Task
  → 302 → Cabinet UI (success)
```

Credentials stored: `access_token`, `refresh_token`, `token_expiry`, `provider="gmail"`, `scopes=["gmail.readonly"]`.

Token refresh is handled transparently by `GmailProviderAdapter` before each API call.

---

## 4. Indexing Pipeline

### 4.1 Job Lifecycle

```
PENDING → RUNNING → COMPLETED
                 → FAILED (after error; Watchdog marks stale RUNNING as FAILED after 2h)
```

A job is created with `triggered_by` = `"user"` (manual) or `"auto"` (future scheduled). Jobs are resumable: every page writes `next_page_token` to Firestore before re-enqueuing the next Cloud Task.

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

### 4.3 Watchdog

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

Sends system alerts to the user's active Slack/Telegram channel when background events complete (email indexing done, errors, etc.).

- **State storage:** `user_notification_state` Firestore collection — persists the user's last active channel (`slack_channel_id` or `telegram_chat_id`).
- **Channel factory:** `NotificationChannelFactory` resolves the appropriate adapter (Slack/Telegram) based on stored state.
- **Triggers:** `EmailIndexingService.completion_alert()` after a job page cycle completes.

---

## 8. WorkerHandler Dispatch

`POST /worker` is the single Cloud Tasks endpoint. `WorkerHandler` dispatches by `task_type`:

| `task_type` | Handler |
|---|---|
| `email_indexing` | `EmailIndexingService.run_indexing_page()` |
| `email_indexing_watchdog` | `EmailIndexingService.run_watchdog()` |
| `consolidation` | `process_user_batches_on_overflow()` |
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
- `src/services/gmail_oauth_service.py`: OAuth token exchange and refresh.
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
