# RFC: Email Indexing System (Gmail + Future Providers)

**Status:** In Design
**Date:** 2026-02-11
**Updated:** 2026-02-22
**Owner:** AI Engineering
**Milestone:** Future (Post-MVP)

**Related Building Blocks:** Memory & Context, Search Enrichment
**Related ADRs:** TBD

---

## 1. Problem Statement

### 1.1 Current Memory Search Limitations

Alek-Core's MemorySearchAgent currently searches only Firestore facts:

- **Limited data source:** Only facts consolidated from conversations
- **Cold start problem:** New users have empty memory (no personalization)
- **Missing email data:** User's Gmail contains rich personal history (flights, healthcare, finance, contracts, correspondence)

### 1.2 Why Raw Gmail Search Is Insufficient

Gmail API native search has critical limitations:

- **Keyword-only matching:** No semantic understanding
- **No multilingual support:** "perelioty" won't match "flight"
- **No synonym expansion:** "reys" won't match "perelet"
- **No structured data:** Can't extract dates, amounts, entities across multiple emails

**Example failure:**

```
User query: "find my test results for 2025"
Gmail search: subject:"test results" after:2025/01/01
Result: 2 emails found (missed "results", "test report", "medical")
```

### 1.3 Desired Outcome

Enable Alek to:

1. **Intelligently extract** email knowledge — only emails that likely contain facts (~10-20% of inbox)
2. **Classify and tag** extracted emails (category, entities, tags) for structured retrieval
3. **Answer email-based queries** by fetching full email content at query time and extracting relevant facts via LLM
4. **Remain provider-agnostic** — Gmail today, Outlook in the future, no refactoring

The model is analogous to ConsolidationAgent: like it discards questions and chitchat from conversations, the Email Indexing pipeline discards noise (marketing, shipping notifications, newsletters) and retains only potentially factual emails.

---

## 2. Architecture

### 2.1 Hexagonal Design

Ports are provider-agnostic. Adding Outlook = new adapter, zero changes to domain/services/agent.

```
EmailProviderPort          ← fetch email metadata + snippet from any provider
  └── GmailProviderAdapter       (aiohttp + Gmail REST API)
  └── OutlookProviderAdapter     (future: Microsoft Graph)

OAuthCredentialsPort       ← store/retrieve OAuth tokens per user per provider
  └── FirestoreOAuthCredentialsAdapter

IndexedEmailRepository     ← store/search indexed email metadata
  └── FirestoreIndexedEmailRepo

EmailExclusionsPort        ← filter recurring low-value senders/patterns
  └── FirestoreEmailExclusionsAdapter
```

**Single EmailAgent** — one agent handles all providers simultaneously.
`search_email` queries all connected providers' indexes in parallel.
`index_email` indexes all connected providers in one job.
Eliminates the need for provider-specific agents (no `GmailAgent` + `OutlookAgent` proliferation).

### 2.2 Indexing Pipeline (ASYNC)

Triggered by user via Slack ("index my Gmail") or Cabinet UI. Executed via Cloud Tasks.

```
EmailAgent._handle_indexing()
  → OAuthCredentialsPort.get_credentials(user_id, provider)
  → EmailProviderPort.list_emails(credentials, date_from=indexed_through, page_size=50)
       ↓ returns: List[EmailMetadata] (subject + from + date + labels + snippet)
  → EmailExclusionsPort.get_exclusions(user_id)
       ↓ pre-filter matching senders/patterns (fast, before LLM)
  → EmailClassificationService.classify_batch(emails, batch_size=30–50)
       ↓ single LLM call (Gemini Flash) per batch
       ↓ output per email: {valuable, category, tags[], entities{}, exclusion_candidate?}
  → [if valuable=True] EmbeddingService.embed(tags) → tags_vector
                        EmbeddingService.embed(entities) → metadata_vector
  → IndexedEmailRepository.save_batch(valuable_emails)
  → [if exclusion_candidates] EmailExclusionsPort.add_exclusions(candidates)
  → IndexedEmailRepository.update_indexed_through(user_id, provider, timestamp)
       ↓ advances ONLY after complete batch success (idempotent)
  → Slack notification: "Indexed N emails, found M potentially factual"
```

**Key principle:** `indexed_through` tracks which time period has been processed (not which emails).
On retry after failure, re-process from last successful `indexed_through`. Idempotent upserts handle duplicates.

### 2.3 Search Pipeline (SYNC)

Triggered by user query. Runs inline (no async). Target latency: <3s.

```
EmailAgent._handle_search()
  → LLM generates structured search keys from user query:
       {domain: "HEALTH", tags: ["lab", "test_results", "analysis"]}
       (identical approach to MemorySearchAgent key generation)
  → EmbeddingService.embed(tags) → tags_query_vector
    EmbeddingService.embed(entities_hint) → metadata_query_vector
  → IndexedEmailRepository.search_by_vector(
       user_id, tags_query_vector, metadata_query_vector, limit=50
    ) → List[IndexedEmail] with email_ids, ordered by date DESC
  → For each connected provider:
       EmailProviderPort.batch_get_full_content(credentials, email_ids)
       ↓ returns full email body text (no attachments, limit 50 per provider)
  → LLM extracts requested facts from full email content
  → Returns structured facts to SmartAgent
```

**SmartAgent receives structured facts** — indistinguishable from conversation-derived facts.
Users don't see raw emails; they see extracted, structured answers.

**Batch limit:** 50 emails per provider at search time (empirical, tunable).
**Token expiry at search time:** graceful LLM error ("Gmail not accessible, please reconnect in Cabinet").

### 2.4 OAuth: Gmail Incremental Consent

Firebase Auth already handles `openid email profile` scopes for web login.
Gmail access needs `gmail.readonly` added as incremental consent.

`FirebaseAuthAdapter.get_authorization_url()` gains `additional_scopes: Optional[List[str]] = None` (backward-compatible).

New endpoints in `src/web/oauth_app.py`:
- `GET /auth/connect-gmail` — requires active session; triggers incremental OAuth consent with `gmail.readonly`
- `GET /auth/connect-gmail/callback` — exchanges code; stores tokens via `OAuthCredentialsPort`; redirects to `/cabinet`
- `DELETE /auth/disconnect-gmail` — revokes + deletes stored credentials

Gmail OAuth tokens are stored separately from Firebase auth tokens (different scopes, different TTLs).

---

## 3. Firestore Schema

### 3.1 Collection: `{env}_oauth_credentials`

Doc ID: `{user_id}_{provider}`

```yaml
user_id: "user_abc"
provider: "gmail"               # "gmail" | "outlook"
access_token: "ya29.xxx"        # Encrypted at rest (Firestore default)
refresh_token: "1//xxx"         # Long-lived, used for token refresh
token_expiry: 2026-03-01T12:00Z
scopes: ["gmail.readonly"]
email_address: "user@gmail.com" # Provider account email (for display)
created_at: 2026-02-22T10:00Z
updated_at: 2026-02-22T10:00Z
```

### 3.2 Collection: `{env}_indexed_emails`

Doc ID: `{email_id}` (idempotent upsert — safe on retry)

```yaml
email_id: "msg_xyz123"
user_id: "user_abc"
account_id: "account_xyz"
provider: "gmail"

# Email metadata
subject: "Your flight KBP-BCN is confirmed"
from_address: "noreply@ryanair.com"
date: 2025-03-10T14:30:00Z
labels: ["INBOX", "IMPORTANT"]

# LLM-generated classification
category: "travel"              # See §3.5 for category list
tags: ["flight", "ryanair", "booking", "BCN", "KBP"]
entities:
  flight_number: "FR8421"
  departure_city: "Kyiv"
  arrival_city: "Barcelona"
  departure_date: "2025-03-15"
  airline: "Ryanair"
  confirmation_code: "ABC123"

# Vector indexes (like FactEntity.tags_vector + metadata_vector)
tags_vector: [0.123, -0.456, ...]      # embedding of tags[] — 768 dim
metadata_vector: [-0.789, 0.012, ...]  # embedding of entities{} — 768 dim

indexed_at: 2026-02-22T10:00Z
```

**Not stored:** snippet (classification helper only), summary, subject_embedding.
Full email content is fetched from Gmail at query time.

### 3.3 Collection: `{env}_email_indexing_state`

Doc ID: `{user_id}_{provider}`

```yaml
user_id: "user_abc"
provider: "gmail"
indexed_through: 2026-02-21T23:59:59Z   # null = never indexed
updated_at: 2026-02-22T10:00Z
```

Advances only on complete batch success. Next indexing run uses `indexed_through` as `after:` filter.

### 3.4 Collection: `{env}_email_exclusions`

Doc ID: auto

```yaml
user_id: "user_abc"
pattern_type: "sender_domain"   # "sender_email" | "sender_domain" | "subject_pattern"
pattern: "linkedin.com"
reason: "Recurring LinkedIn notifications — no factual content"
created_at: 2026-02-22T10:00Z
```

Populated automatically when LLM detects recurring low-value senders during indexing.
Fetched once per indexing job; applied as pre-filter before LLM classification.

### 3.5 Email Categories

```
travel       — flights, hotels, car rentals, train bookings
finance      — invoices, receipts, bank statements, contracts
healthcare   — medical appointments, lab results, prescriptions, analyses
work         — meetings, projects, contracts, employment
legal        — official documents, registrations, permits
personal     — family, friends, personal correspondence
subscription — recurring service notifications (low value, often excluded)
```

### 3.6 Firestore Index Configuration

```json
[
  {
    "collectionGroup": "{env}_indexed_emails",
    "fields": [
      {"fieldPath": "user_id", "order": "ASCENDING"},
      {"fieldPath": "date", "order": "DESCENDING"}
    ]
  },
  {
    "collectionGroup": "{env}_indexed_emails",
    "fields": [
      {"fieldPath": "tags_vector", "vectorConfig": {"dimension": 768, "flat": {}}}
    ]
  },
  {
    "collectionGroup": "{env}_indexed_emails",
    "fields": [
      {"fieldPath": "metadata_vector", "vectorConfig": {"dimension": 768, "flat": {}}}
    ]
  }
]
```

---

## 4. Email Classification (Single-Pass LLM Batch)

Input: 30–50 `EmailMetadata` items (subject + from + date + labels + snippet).
Model: Gemini Flash (ECO tier). One LLM call per batch.

**Output per email:**

```json
{
  "email_id": "msg_xyz123",
  "valuable": true,
  "category": "travel",
  "tags": ["flight", "ryanair", "booking", "BCN", "KBP"],
  "entities": {
    "flight_number": "FR8421",
    "departure_city": "Kyiv",
    "arrival_city": "Barcelona",
    "departure_date": "2025-03-15",
    "airline": "Ryanair"
  },
  "exclusion_candidate": null
}
```

```json
{
  "email_id": "msg_abc456",
  "valuable": false,
  "exclusion_candidate": {
    "pattern_type": "sender_domain",
    "pattern": "linkedin.com",
    "reason": "LinkedIn notification — no factual content"
  }
}
```

**`valuable=false`** — email discarded, not written to Firestore.
**`exclusion_candidate`** — LLM flags pattern; stored to `{env}_email_exclusions` for future pre-filtering.

**Value filter heuristic (prompt instruction):**
- Valuable: travel bookings, medical records, financial transactions, contracts, legal docs, meaningful personal correspondence
- Not valuable: newsletters, marketing, shipping tracking, social notifications, system alerts, chitchat

---

## 5. Domain Models (`src/domain/email.py`)

```python
@dataclass
class OAuthCredentials:
    user_id: str
    provider: str           # "gmail" | "outlook"
    access_token: str
    refresh_token: str
    token_expiry: datetime
    scopes: List[str]
    email_address: str      # provider account email (display only)

@dataclass
class EmailMetadata:
    """Returned by EmailProviderPort — used during indexing, NOT stored."""
    email_id: str
    provider: str
    subject: str
    from_address: str
    date: datetime
    labels: List[str]
    snippet: str            # First ~200 chars — classification helper only

class IndexedEmail(BaseModel):
    """Stored in Firestore — email_id is the primary key for Gmail batch fetch."""
    email_id: str
    user_id: str
    account_id: str
    provider: str
    subject: str
    from_address: str
    date: datetime
    labels: List[str]
    category: str
    tags: List[str]
    entities: Dict[str, Any]
    tags_vector: Optional[List[float]] = None
    metadata_vector: Optional[List[float]] = None
    indexed_at: datetime

@dataclass
class IndexingState:
    user_id: str
    provider: str
    indexed_through: Optional[datetime]   # None = never indexed

class EmailExclusion(BaseModel):
    user_id: str
    pattern_type: str       # "sender_email" | "sender_domain" | "subject_pattern"
    pattern: str
    reason: str
    created_at: datetime
```

---

## 6. New Components

| File | Purpose |
|------|---------|
| `src/domain/email.py` | All email domain models (above) |
| `src/ports/oauth_credentials_port.py` | ABC: `get_credentials`, `save_credentials`, `revoke_credentials`, `is_connected` |
| `src/ports/email_provider_port.py` | ABC: `list_emails(credentials, date_from, page_token)`, `batch_get_full_content(credentials, email_ids)`, `refresh_token` |
| `src/ports/indexed_email_repository.py` | ABC: `save_batch`, `search_by_vector`, `get_indexing_state`, `update_indexing_state`, `count_by_user`, `delete_by_user` |
| `src/ports/email_exclusions_port.py` | ABC: `get_exclusions(user_id)`, `add_exclusions(List[EmailExclusion])`, `delete_exclusion` |
| `src/adapters/firestore_oauth_credentials_adapter.py` | Firestore impl. Doc ID: `{user_id}_{provider}` |
| `src/adapters/gmail_provider_adapter.py` | `aiohttp` Gmail REST. Pagination via `pageToken`. Token refresh via POST to `https://oauth2.googleapis.com/token`. `batch_get_full_content` fetches body (no attachments). |
| `src/adapters/firestore_indexed_email_repo.py` | Batch writes (500/batch). Vector search (RRF on `tags_vector` + `metadata_vector`). Indexing state. |
| `src/services/email_classification_service.py` | Single-pass LLM batch. Gemini Flash. Pydantic output. Exclusion candidate detection. |
| `src/services/email_indexing_service.py` | Full indexing pipeline. Advances `indexed_through` only on batch success. |
| `src/agents/email_agent.py` | `EmailAgent(BaseAgent)`. `_handle_search()` + `_handle_indexing()`. Multi-provider dispatch. |

---

## 7. Modified Components

| File | Change |
|------|--------|
| `src/adapters/firebase_auth_adapter.py` | Add `additional_scopes: Optional[List[str]] = None` to `get_authorization_url()` — backward-compatible |
| `src/web/oauth_app.py` | Blueprint factory gains `oauth_credentials: OAuthCredentialsPort`. New endpoints: `/auth/connect-gmail`, `/auth/connect-gmail/callback`, `DELETE /auth/disconnect-gmail` |
| `src/web/user_cabinet_app.py` | New endpoints: `GET /api/gmail/status`, `POST /api/gmail/index`, `DELETE /api/gmail/disconnect` |
| `src/handlers/agent_worker_handler.py` | Slack notification on async task completion (TODO already exists). `__init__` gains `slack_client: Optional[AsyncWebClient]` |
| `src/composition/service_container.py` | Wire all new services and adapters |
| `main.py` | Register `EmailAgent` + `AgentManifest(intents={"search_email": SYNC, "index_email": ASYNC})` |
| `firestore.indexes.json` | Add composite + vector indexes for `{env}_indexed_emails` |
| `requirements.txt` | Add `google-auth>=2.0.0`, `google-auth-oauthlib>=1.0.0` |

---

## 8. Implementation Phases

### Phase 1 — OAuth + Credentials

1. `src/domain/email.py` — domain models
2. `src/ports/oauth_credentials_port.py` + `src/ports/email_provider_port.py`
3. `src/adapters/firebase_auth_adapter.py` — `additional_scopes` param
4. `src/adapters/firestore_oauth_credentials_adapter.py`
5. `src/web/oauth_app.py` — `/auth/connect-gmail` + callback + disconnect
6. `src/web/user_cabinet_app.py` — `/api/gmail/status` + `/api/gmail/disconnect`
7. Tests: `tests/unit/ports/test_oauth_credentials_port.py`, `tests/unit/ports/test_email_provider_port.py`

### Phase 2 — Indexing Pipeline

8. `src/adapters/gmail_provider_adapter.py` — metadata + full content fetch
9. `src/ports/indexed_email_repository.py` + `src/ports/email_exclusions_port.py`
10. `src/adapters/firestore_indexed_email_repo.py`
11. `firestore.indexes.json` — vector indexes
12. `src/services/email_classification_service.py`
13. `src/services/email_indexing_service.py`
14. Tests: `tests/unit/ports/test_indexed_email_repository.py`, `tests/unit/services/test_email_classification_service.py`, `tests/unit/services/test_email_indexing_service.py`

### Phase 3 — Agent + Integration

15. `src/agents/email_agent.py` — `_handle_search()` + `_handle_indexing()`
16. `src/handlers/agent_worker_handler.py` — Slack notification
17. `src/web/user_cabinet_app.py` — `/api/gmail/index`
18. `src/composition/service_container.py` — wire everything
19. `main.py` — register agent + manifest
20. `requirements.txt` + `main.py` — add google-auth packages
21. Tests: `tests/unit/agents/test_email_agent.py`, `tests/e2e/test_email_agent_flow.py`

---

## 9. Cost Analysis

### 9.1 One-Time Indexing

**Assumptions:** 10,000 emails. ~15% pass value filter = 1,500 indexed.

| Component | Quantity | Unit Cost | Total |
|-----------|----------|-----------|-------|
| Gmail API — metadata list (format=metadata) | 200 pages × 50 | $0 (free) | $0 |
| LLM classification (Gemini Flash) | 200 batches × 50 | $0.001/batch | $0.20 |
| Embeddings (tags + metadata vectors) | 1,500 × 2 | $0.00001 each | $0.03 |
| Firestore writes | 1,500 docs | $0.000018/write | $0.03 |
| **TOTAL (one-time)** | | | **~$0.26** |

Previous RFC estimated $0.58 (indexed everything). Filtering reduces cost ~2.2x.

### 9.2 Incremental Updates (Daily)

**Assumptions:** 20 new emails/day, 15% pass filter = 3 indexed/day.

| Component | Monthly Cost |
|-----------|-------------|
| Gmail API | $0 |
| LLM classification | $0.02 |
| Embeddings | <$0.01 |
| Firestore writes | <$0.01 |
| **TOTAL (monthly)** | **~$0.03** |

### 9.3 Search Cost (per query)

| Component | Cost |
|-----------|------|
| Firestore vector search | $0.00006 |
| Embedding (query) | $0.00001 |
| Gmail batch fetch (50 emails, `format=full`) | Gmail free tier |
| LLM fact extraction (Gemini Flash or Claude) | ~$0.001 |
| **TOTAL** | **~$0.001** |

---

## 10. Performance

### 10.1 Indexing (10K emails)

| Stage | Time |
|-------|------|
| Gmail API metadata fetch (200 pages) | ~20s |
| LLM classification (200 batches, parallelized) | ~30s |
| Embeddings + Firestore writes (1,500 docs) | ~10s |
| **Total** | **~60s** |

### 10.2 Search Latency

| Stage | Time |
|-------|------|
| LLM key generation | ~0.5s |
| Firestore vector search | ~0.3s |
| Gmail batch fetch (50 emails, `format=full`) | ~1–2s |
| LLM fact extraction | ~1–2s |
| **Total (p50)** | **~3–4s** |

Search latency is higher than a pure vector search (0.3s) because it fetches live email content from Gmail. This is acceptable for a dedicated email search intent — user explicitly asked about emails.

---

## 11. Security & Privacy

### 11.1 OAuth Consent

- **Optional feature:** User explicitly enables Gmail indexing via Cabinet
- **Scope:** `gmail.readonly` — no write/send/delete access
- **Incremental consent:** Added to existing Google OAuth session, not a new login
- **Revocable:** `/api/gmail/disconnect` revokes token at Google and deletes from Firestore
- **Per-user, per-provider tokens:** Isolated in `{env}_oauth_credentials`

### 11.2 Data Storage

- **Metadata + tags only:** No email body, no attachments stored in Firestore
- **Snippets discarded:** Used only during indexing LLM call, never persisted
- **Encryption:** Firestore encryption at rest (GCP default)
- **User-scoped:** Multi-tenant isolation (`user_id` + `account_id` on every document)
- **Retention:** User can delete all indexed data via `/api/gmail/disconnect`

### 11.3 Live Gmail Access (Search)

At search time, full email content is fetched live from Gmail API.
This content is processed in memory by the LLM call and never stored to Firestore or logs.

---

## 12. User Experience

### 12.1 Connect Gmail (Cabinet)

```
User opens /cabinet → "Connect Gmail" button
  ↓
Redirect to Google consent: "Allow gmail.readonly access"
  ↓
Return to /cabinet: "Gmail connected ✓ (user@gmail.com)"
  ↓
"Index Gmail" button becomes active
```

### 12.2 Indexing Flow (Slack)

```
User: "index my Gmail"
Bot: "Starting Gmail indexing job... I'll notify you when done 📬"

[Background, Cloud Tasks, ~60s]

Bot: "✅ Gmail indexed
      Processed 10,432 emails
      Stored 1,287 potentially factual emails:
        ✈️ Travel: 89
        💰 Finance: 341
        🏥 Healthcare: 63
        💼 Work: 512
        ⚖️ Legal: 28
        👤 Personal: 254
      Excluded recurring senders: 12 patterns auto-detected"
```

### 12.3 Query Flow (Slack)

```
User: "find my test results for 2025"

Bot: (3–4s — fetches full email content from Gmail)

Bot: "Found 5 medical emails from 2025 with test results:

      📋 March 28 — GFR (CKD-EPI) >90 mL/min (Normal). HbA1c 5.1%.
      📋 January 15 — Blood panel: Uric acid elevated (Hyperuricemia confirmed).
      📋 May 7 — Kidney CT — no active stones detected.
      📋 August 3 — Periodontitis follow-up post-curettage: stable.
      📋 November 20 — Lipid panel: Dyslipidemia under management."
```

---

## 13. Future Enhancements

### 13.1 Self-Enriching Memory Loop

**Vision:** High-confidence email facts automatically create FactEntity records.

```
EmailAgent finds flight BCN→KBP, March 15 2025
  ↓
Confidence > 0.9 → ConsolidationAgent-style fact creation:
  FactEntity(domain=TRAVEL, text="Flight BCN→KBP, March 15 2025, FR8421",
             tags=["travel", "flight", "ryanair"])
  ↓
Next conversation: "when were you in Kyiv?" → answers from memory (no Gmail needed)
```

**Benefit:** Memory grows from email history without user action.

### 13.2 Attachment Analysis

Parse PDF attachments via `markitdown[all]` (already in requirements):
- Medical reports → diagnoses, prescriptions, values
- Invoices → amounts, dates, vendors
- Contracts → parties, deadlines, terms

**Challenge:** Privacy controls — user must explicitly opt in per email/category.

### 13.3 Outlook / Microsoft Graph

`OutlookProviderAdapter` implementing `EmailProviderPort` — no changes to domain, services, or agent.
`OAuthCredentialsPort` already supports `provider` field — Outlook tokens stored alongside Gmail.

### 13.4 Person-Based Retrieval (Deferred)

"Give me history with Vasya" requires:
- Contact normalization (from_address → person identity)
- Reverse lookup (person name → known email addresses)

**Status:** Out of scope for Phase 1–3. Requires contact management feature.

### 13.5 Proactive Insights

```
Bot: "You have 3 unpaid invoices in your inbox (total €1,234). Want reminders?"
Bot: "Your PZU insurance renewal is in 2 weeks based on your email."
```

---

## 14. Alternatives Considered

### 14.1 Index All Emails (Original RFC Approach)

**Pros:** Simple, complete recall
**Cons:** 10K Firestore docs per user, high noise (~85% low-value), expensive embeddings on junk

**Verdict:** Rejected — analogous to storing every Slack message instead of consolidated facts

### 14.2 Pure Vector Search on Stored Summaries

**Pros:** Fast (0.3s), no live Gmail API call at query time
**Cons:** Summary quality degrades over time (LLM generates imperfect summaries); full email content gives much richer fact extraction

**Verdict:** Rejected — full email content at search time gives better answer quality

### 14.3 Gmail Search with Query Expansion

**Pros:** No indexing needed, real-time
**Cons:** Keyword-only, multilingual failures, no structured data, no filtering by value

**Verdict:** Rejected — insufficient quality (see §1.2)

### 14.4 Separate GmailAgent + OutlookAgent

**Pros:** Isolation
**Cons:** Duplicate intents (`search_gmail` vs `search_outlook`), simultaneous multi-provider search impossible, global refactoring to add each provider

**Verdict:** Rejected — single EmailAgent with multi-provider dispatch is strictly better

---

## 15. Open Questions

1. **Should we index Sent emails?** Or only Inbox?
   - **Decision:** Index all (sent reveals user behavior patterns)

2. **How far back to index by default?**
   - **Decision:** Default: all available history. User can specify date range.

3. **Attachment opt-in granularity?**
   - **Decision:** Phase 2 decision. Default: no attachments.

4. **Batch fetch limit at search time (50)?**
   - **Decision:** Start at 50, tune empirically based on latency + answer quality.

5. **Token refresh during search?**
   - **Decision:** Auto-refresh via `EmailProviderPort.refresh_token()`. If refresh fails → graceful LLM error.

---

## 16. Dependencies

### 16.1 Existing Infrastructure

- ✅ OAuth Multi-Tenant — full web OAuth flow in `src/web/oauth_app.py`
  (Quart app: `/auth/login`, `/auth/callback`, `/auth/link-oauth`; Google OAuth via FirebaseAuthAdapter)
- ✅ SearchEnrichmentService — vector search ready (RRF pattern reused for email search)
- ✅ Firestore multi-tenant isolation (`{env}_` prefix, `user_id` filtering)
- ✅ Cloud Tasks + AgentCoordinator — ASYNC execution ready
- ✅ AgentRegistry (ACP v2) — `email_agent` manifest slot ready
- ✅ AgentWorkerHandler — has TODO for post-completion notification

### 16.2 New Components Required

- ❌ Gmail incremental OAuth consent (`/auth/connect-gmail` endpoint)
- ❌ `OAuthCredentialsPort` + `FirestoreOAuthCredentialsAdapter`
- ❌ `EmailProviderPort` + `GmailProviderAdapter` (metadata + full content)
- ❌ `IndexedEmailRepository` + `FirestoreIndexedEmailRepo`
- ❌ `EmailExclusionsPort` + adapter
- ❌ `EmailClassificationService` (single-pass LLM batch)
- ❌ `EmailIndexingService` (pipeline orchestration)
- ❌ `EmailAgent` (search + indexing)
- ❌ `{env}_indexed_emails` Firestore collection + vector indexes
- ❌ `{env}_oauth_credentials` Firestore collection
- ❌ Cabinet UI: "Connect Gmail" + "Index Gmail" buttons

---

## 17. References

- **Gmail API:** https://developers.google.com/gmail/api/guides
- **Gmail REST Messages:** https://developers.google.com/gmail/api/reference/rest/v1/users.messages
- **Firestore Vector Search:** https://firebase.google.com/docs/firestore/vector-search
- **OAuth Multi-Tenant RFC:** [MULTI_TENANT_OAUTH_RFC.md](./MULTI_TENANT_OAUTH_RFC.md)
- **Search Enrichment Building Block:** [../05_building_blocks/search_enrichment/README.md](../05_building_blocks/search_enrichment/README.md)

---

## Changelog

### 2026-02-22 — Full redesign

Major architectural rethink after design session:

- **Concept shift:** "Index everything" → "Intelligent knowledge extraction" (like ConsolidationAgent)
- **Storage:** Only valuable emails (~10-20%) stored. Snippet not stored. No summary stored.
- **Schema:** `tags_vector` + `metadata_vector` (like FactEntity), replacing `subject_embedding`
- **Search:** Vector search → email_ids → live Gmail batch fetch → LLM fact extraction. Not a pure DB search.
- **Agent:** Single `EmailAgent` (multi-provider dispatch) replacing `GmailAgent` + future `OutlookAgent`
- **Ports renamed:** `EmailProviderPort` (not `GmailApiPort`), `OAuthCredentialsPort` (reusable)
- **Exclusions list:** Auto-populated during indexing when LLM detects recurring low-value senders
- **Period tracking:** `indexed_through` timestamp per user/provider (replaces per-email tracking)
- **Hexagonal from start:** Outlook-ready without refactoring

### 2026-02-11 — Initial RFC

- Initial RFC created
- Problem statement defined
- Architecture designed (index-everything approach)
- Implementation plan outlined (4 weeks)
- Cost analysis completed
