# RFC: Email Indexing System (Gmail + Future Providers)

**Status:** In Design
**Date:** 2026-02-11
**Updated:** 2026-02-28
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

### 2.1 Hexagonal Architecture

This section is the authoritative component schema.
Adding Outlook = one new `OutlookProviderAdapter`, zero changes to domain/services/agents.

#### 2.1.1 File Structure

```
src/
  domain/
    email.py                                 # All domain models (see §5)

  ports/
    email_provider_port.py                   # EmailProviderPort (ABC)
    oauth_credentials_port.py                # OAuthCredentialsPort (ABC)
    indexed_email_repository.py              # IndexedEmailRepository (ABC)
    email_exclusions_port.py                 # EmailExclusionsPort (ABC)
    email_indexing_job_repository.py         # EmailIndexingJobRepository (ABC)

  adapters/
    gmail_provider_adapter.py                # GmailProviderAdapter(EmailProviderPort)
    firestore_oauth_credentials_adapter.py   # FirestoreOAuthCredentialsAdapter(OAuthCredentialsPort)
    firestore_indexed_email_repo.py          # FirestoreIndexedEmailRepo(IndexedEmailRepository)
    firestore_email_exclusions_adapter.py    # FirestoreEmailExclusionsAdapter(EmailExclusionsPort)
    firestore_email_job_repo.py              # FirestoreEmailIndexingJobRepo(EmailIndexingJobRepository)

  services/
    email_classification_service.py         # LLM batch classification (Gemini Flash, tool-assisted)
    email_indexing_service.py               # Full pipeline orchestration (Flow 1 + 2)
    email_embedding_repair_service.py       # Re-embeds docs where embedding_pending=True

  agents/
    email_agent.py                          # EmailAgent(BaseAgent) — async indexing, multi-provider
    email_search_agent.py                   # EmailSearchAgent(BaseAgent) — Mode A + Mode B

  web/
    oauth_app.py                            # +/auth/connect-gmail
                                            # +/auth/connect-gmail/callback
                                            # +DELETE /auth/disconnect-gmail
    user_cabinet_app.py                     # +/api/gmail/status
                                            # +/api/gmail/index
                                            # +DELETE /api/gmail/disconnect
                                            # +email_daily_summary toggle

  composition/
    service_container.py                    # +wire all email components
```

#### 2.1.2 Port Contracts

**`EmailProviderPort`** — fetch email data from any provider

```python
class EmailProviderPort(ABC):

    @abstractmethod
    async def list_emails(
        self,
        credentials: OAuthCredentials,
        date_from: Optional[datetime] = None,
        page_token: Optional[str] = None,
        max_results: int = 50,
    ) -> Tuple[List[EmailMetadata], Optional[str]]:
        """
        Fetch one page of email metadata.
        Returns (emails, next_page_token). next_page_token=None means last page.
        date_from=None means full history from oldest available.
        """

    @abstractmethod
    async def batch_get_full_content(
        self,
        credentials: OAuthCredentials,
        email_ids: List[str],
    ) -> Dict[str, EmailFullContent]:
        """
        Fetch full content: body text, attachment filenames, attachment binaries.
        Used by:
          - EmailClassificationService: body when snippet is insufficient
          - EmailSearchAgent Mode B: full body + markitdown attachment parsing
        Missing email IDs are silently absent from result (deleted or inaccessible).
        """

    @abstractmethod
    async def refresh_token(self, credentials: OAuthCredentials) -> OAuthCredentials:
        """
        Use refresh_token to get new access_token.
        Raises OAuthExpiredError if refresh_token is expired or revoked.
        """
```

**`OAuthCredentialsPort`** — persist OAuth tokens, provider-agnostic

```python
class OAuthCredentialsPort(ABC):

    @abstractmethod
    async def get_credentials(
        self, user_id: str, provider: str
    ) -> Optional[OAuthCredentials]:
        """Returns None if user has not connected this provider."""

    @abstractmethod
    async def save_credentials(self, credentials: OAuthCredentials) -> None:
        """Upsert. Doc ID: {user_id}_{provider}."""

    @abstractmethod
    async def revoke_credentials(self, user_id: str, provider: str) -> None:
        """Delete stored tokens. Caller is responsible for revoking at the provider first."""

    @abstractmethod
    async def is_connected(self, user_id: str, provider: str) -> bool:
        """Quick existence check — does user have stored credentials for this provider?"""

    @abstractmethod
    async def list_connected_providers(self, user_id: str) -> List[str]:
        """
        All providers with stored credentials for this user.
        Used by EmailIndexingService to fan-out across all connected providers.
        """
```

**`IndexedEmailRepository`** — store and search indexed email facts

```python
class IndexedEmailRepository(ABC):

    @abstractmethod
    async def save_batch(self, emails: List[IndexedEmail]) -> int:
        """
        Upsert batch. email_id is document ID — idempotent on retry.
        Returns count of documents written.
        Firestore max: 500 writes per batch transaction.
        """

    @abstractmethod
    async def find_nearest(
        self,
        user_id: str,
        vectors: Dict[str, List[float]],
        limit: int = 10,
        state: str = "current",
    ) -> List[IndexedEmail]:
        """
        Multi-vector RRF search across provided vector fields.
        vectors keys: "vector" | "tags_vector" | "metadata_vector" | "attachments_vector"
        Absent keys are skipped (e.g., attachments_vector absent → skip that query).
        Returns top-N by RRF score, filtered by user_id and state.
        """

    @abstractmethod
    async def get_indexing_state(
        self, user_id: str, provider: str
    ) -> Optional[IndexingState]:
        """Returns None if never indexed."""

    @abstractmethod
    async def update_indexing_state(self, state: IndexingState) -> None:
        """Advance indexed_through cursor. Called only after each batch completes successfully."""

    @abstractmethod
    async def count_by_user(
        self, user_id: str, provider: Optional[str] = None
    ) -> int:
        """Count indexed email facts. provider=None counts across all providers."""

    @abstractmethod
    async def delete_by_user(self, user_id: str) -> None:
        """Delete ALL indexed facts for user. Called on Gmail disconnect."""

    @abstractmethod
    async def get_unconsolidated_batch(
        self, user_id: str, limit: int = 100
    ) -> List[IndexedEmail]:
        """
        WHERE consolidated_at IS NULL AND user_id = X ORDER BY indexed_at ASC LIMIT N.
        Used by ConsolidationAgent integration pipeline to feed email facts
        into biographical memory (§13.1).
        """

    @abstractmethod
    async def mark_consolidated(
        self, email_ids: List[str], consolidated_at: datetime
    ) -> None:
        """Batch update: set consolidated_at = now() on processed IDs."""

    @abstractmethod
    async def get_pending_embeddings(self, limit: int = 100) -> List[IndexedEmail]:
        """
        WHERE embedding_pending=True LIMIT N.
        Used by EmailEmbeddingRepairService (runs every 6h via Cloud Scheduler).
        """

    @abstractmethod
    async def update_vectors(
        self, email_id: str, vectors: Dict[str, List[float]]
    ) -> None:
        """
        Partial update: write computed vectors dict, set embedding_pending=False.
        Called by repair service after successful re-embedding.
        """
```

**`EmailExclusionsPort`** — filter recurring low-value senders before LLM classification

```python
class EmailExclusionsPort(ABC):

    @abstractmethod
    async def get_exclusions(self, user_id: str) -> List[EmailExclusion]:
        """
        Load all active exclusion patterns for user.
        Called once per indexing job as a fast pre-filter before LLM.
        """

    @abstractmethod
    async def add_exclusions(self, exclusions: List[EmailExclusion]) -> None:
        """
        Persist auto-detected patterns.
        Called when LLM identifies recurring low-value senders during classification.
        Idempotent: no-op if identical pattern already exists.
        """

    @abstractmethod
    async def delete_exclusion(self, user_id: str, exclusion_id: str) -> None:
        """User removes a pattern via Cabinet."""

    @abstractmethod
    async def list_exclusions(self, user_id: str) -> List[EmailExclusion]:
        """
        For Cabinet display — returns all patterns with reason and created_at.
        Semantically distinct from get_exclusions (display vs. filtering),
        but backed by the same underlying query.
        """
```

**`EmailIndexingJobRepository`** — job journal for resume, retry, and Cabinet history

```python
class EmailIndexingJobRepository(ABC):

    @abstractmethod
    async def create_job(self, job: IndexingJob) -> None:
        """Persist new job record at the start of an indexing run."""

    @abstractmethod
    async def update_job(self, job_id: str, updates: Dict[str, Any]) -> None:
        """
        Partial update called after each successful batch:
          - next_page_token: current cursor (primary resume point on Cloud Tasks timeout)
          - emails_fetched, emails_stored, emails_failed, embedding_pending: running totals
          - errors: append to list (capped at 100 items)
          - status: updated on terminal transitions (completed/failed/paused)
          - updated_at: always refreshed
        """

    @abstractmethod
    async def get_job(self, job_id: str) -> Optional[IndexingJob]:
        """Fetch a specific job by ID."""

    @abstractmethod
    async def get_latest_job(
        self, user_id: str, provider: str
    ) -> Optional[IndexingJob]:
        """
        Last job for user+provider ordered by started_at DESC.
        Cabinet uses this to show current indexing status and enable Retry.
        """

    @abstractmethod
    async def list_jobs(self, user_id: str, limit: int = 10) -> List[IndexingJob]:
        """
        Last N jobs across all providers, ordered by started_at DESC.
        Displayed in Cabinet job history panel.
        """
```

#### 2.1.3 Import Rules

Follows project hexagonal conventions (CLAUDE.md):

| Layer | May import | Must NOT import |
|-------|-----------|-----------------|
| `domain/email.py` | stdlib, pydantic | anything from `src/` |
| `ports/*.py` | domain/, stdlib, ABC | adapters/, services/, config/ |
| `adapters/gmail_provider_adapter.py` | domain/, ports/, config/ | services/, agents/ |
| `adapters/firestore_*.py` | domain/, ports/, config/ | services/, agents/ |
| `services/email_classification_service.py` | domain/, ports/ | adapters/ directly, agents/ |
| `services/email_indexing_service.py` | domain/, ports/ | adapters/ directly, agents/ |
| `services/email_embedding_repair_service.py` | domain/, ports/ | adapters/ directly, agents/ |
| `agents/email_agent.py` | domain/, ports/ (via BaseAgent DI) | adapters/ directly |
| `agents/email_search_agent.py` | domain/, ports/ (via BaseAgent DI) | adapters/ directly |
| `web/oauth_app.py` | domain/, ports/, config/ | adapters/ directly |
| `composition/service_container.py` | everything | — (wiring layer) |

#### 2.1.4 ServiceContainer Wiring

```python
# src/composition/service_container.py (email section)

# — Adapters —
oauth_credentials_adapter = FirestoreOAuthCredentialsAdapter(
    firestore_client, env_prefix
)
gmail_provider_adapter = GmailProviderAdapter()        # stateless; uses credentials at call time
indexed_email_repo = FirestoreIndexedEmailRepo(
    firestore_client, embedding_service, env_prefix
)
email_exclusions_adapter = FirestoreEmailExclusionsAdapter(
    firestore_client, env_prefix
)
email_job_repo = FirestoreEmailIndexingJobRepo(
    firestore_client, env_prefix
)

# — Services —
email_classification_service = EmailClassificationService(
    llm_service=gemini_flash_adapter,          # BALANCED tier — Gemini Flash
    email_provider=gmail_provider_adapter,     # used for get_email_details tool
)
email_indexing_service = EmailIndexingService(
    oauth_credentials=oauth_credentials_adapter,
    email_provider=gmail_provider_adapter,
    classification_service=email_classification_service,
    indexed_email_repo=indexed_email_repo,
    email_exclusions=email_exclusions_adapter,
    job_repo=email_job_repo,
    embedding_service=embedding_service,
)
email_embedding_repair_service = EmailEmbeddingRepairService(
    indexed_email_repo=indexed_email_repo,
    embedding_service=embedding_service,
)

# — Agents —
email_agent = EmailAgent(
    indexing_service=email_indexing_service,
    oauth_credentials=oauth_credentials_adapter,
    slack_client=slack_client,                 # for async completion notifications
)
email_search_agent = EmailSearchAgent(
    indexed_email_repo=indexed_email_repo,
    email_provider=gmail_provider_adapter,
    oauth_credentials=oauth_credentials_adapter,
    llm_service=gemini_flash_adapter,
    embedding_service=embedding_service,
)
```

#### 2.1.5 Port Justification

Each port satisfies the hexagonal rule: 2+ implementations OR testable substitution needed.

| Port | Primary adapter | Second adapter | Test substitute | Verdict |
|------|----------------|----------------|-----------------|---------|
| `EmailProviderPort` | `GmailProviderAdapter` | `OutlookProviderAdapter` (Phase 4) | `AsyncMock(spec=...)` | ✅ 2 concrete implementations planned |
| `OAuthCredentialsPort` | `FirestoreOAuthCredentialsAdapter` | `InMemoryOAuthAdapter` (local/tests) | `AsyncMock(spec=...)` | ✅ unit tests cannot hit Firestore |
| `IndexedEmailRepository` | `FirestoreIndexedEmailRepo` | `InMemoryIndexedEmailRepo` (tests) | `AsyncMock(spec=...)` | ✅ vector search behavior differs from spec-mock alone |
| `EmailExclusionsPort` | `FirestoreEmailExclusionsAdapter` | flat-file impl (local dev) | `AsyncMock(spec=...)` | ✅ testable substitution |
| `EmailIndexingJobRepository` | `FirestoreEmailIndexingJobRepo` | `InMemoryJobRepo` (tests) | `AsyncMock(spec=...)` | ✅ Cabinet retry logic requires real job state in tests |

**Single EmailAgent** — one agent handles all connected providers simultaneously.
`index_email` fans out across all providers via `OAuthCredentialsPort.list_connected_providers()`.
`search_email` queries all providers' indexes in parallel, combined via RRF.
No `GmailAgent` + `OutlookAgent` proliferation — adding Outlook requires zero new agents.

### 2.2 Flow 1: Initial Indexing (ASYNC, one-time)

Triggered by Cabinet button. Executed via Cloud Tasks (can run 1–2 hours for large mailboxes).

**Batch sizing rationale:**
- Gmail metadata page: **100 emails** (`maxResults=100`) — one page = one LLM classification call.
  Aligned deliberately so one "chunk" = 1 Gmail page + 1 LLM call + N full-content fetches.
- LLM classification batch: **100 emails/call** — enough for cross-email pattern detection
  (recurring senders, subscription patterns), not so large as to risk LLM timeout.
- Full content parallel fetch: **semaphore=10** — 250 req/s Gmail quota, 10 concurrent = safe
  at any reasonable page throughput.
- Firestore save batch: up to **500 docs** (Firestore hard limit), but in practice ~15 docs
  per chunk (100 emails × ~15% valuable rate).

**Per-chunk loop** (repeats until all pages exhausted):

```
┌─ CHUNK (100 emails) ────────────────────────────────────────────────┐
│                                                                      │
│  EmailProviderPort.list_emails(page_token, max_results=100)          │
│       ↓ EmailMetadata × 100 (subject, from, date, snippet)          │
│                                                                      │
│  EmailExclusionsPort.get_exclusions(user_id)   ← once per job       │
│       ↓ pre-filter known low-value senders (fast, before LLM)       │
│                                                                      │
│  EmailClassificationService.classify_batch(emails_100)              │
│       ↓ Gemini Flash — agentic, calls get_email_details tool        │
│         for ambiguous emails (vague subject / unknown sender)        │
│         format=full for those only                                   │
│       ↓ output: [{email_id, valuable, category, fact, tags}] × 100  │
│                                                                      │
│  [collect valuable_ids where valuable=True]  ← typically ~15        │
│                                                                      │
│  EmailProviderPort.batch_get_full_content(credentials, valuable_ids) │
│       ↓ asyncio.gather + semaphore=10                               │
│       ↓ format=full → body_text (discarded) + attachment filenames  │
│                                                                      │
│  EmbeddingService.embed_batch(text + tags + metadata + attachments) │
│       → vector, tags_vector, metadata_vector, attachments_vector    │
│                                                                      │
│  IndexedEmailRepository.save_batch(~15 docs)   ← idempotent        │
│  EmailExclusionsPort.add_exclusions(detected patterns)              │
│  IndexedEmailRepository.update_indexing_state(batch_max_date)       │
│       ↑ advances ONLY after batch fully written (resume cursor)     │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
       ↓ next_page_token → repeat until None

Slack notification: "✅ Gmail indexed: N total, M stored"
```

**Resume on Cloud Tasks timeout:** `next_page_token` persisted in `IndexingJob` after every
chunk. On retry, job reads `next_page_token` from Firestore and continues where it left off.
No emails re-fetched, no emails lost.

### 2.3 Flow 2: Daily Incremental + Proactive Digest (ASYNC, scheduled)

Triggered by Cloud Scheduler (daily). Indexes only new emails since `indexed_through`,
then sends a proactive digest to the user via SmartAgent.

Controlled by per-user setting: `email_daily_summary: bool` (default: false).

```
Cloud Scheduler (daily) → POST /worker/email-digest
  → [for each user with email_daily_summary=True AND gmail connected]
       EmailIndexingService.run_incremental(user_id, provider="gmail")
         → same pipeline as Flow 1, but date_from=indexed_through
         → returns List[IndexedEmail] newly_indexed
       [if newly_indexed not empty]
         → ConversationHandler.handle_system_message(
               user_id=user_id,
               message=build_digest_prompt(newly_indexed),
               marker="system_alert"
           )
```

**System message format:**

```
[system_alert] Daily email digest trigger.
New confirmed facts indexed from Gmail (last 24h):

1. [travel] User booked flight KBP→BCN March 15 2025, ref RYR1234
2. [healthcare] Lab results received: GFR >90, HbA1c 5.1%
3. [finance] Invoice paid to DigitalOcean $24.00

Search the web for any relevant context (upcoming events, related news).
Send the user a concise proactive summary.
```

**Mechanism:** `system_alert` marker in message → ConversationHandler routes to SmartAgent →
SmartAgent recognizes system-triggered context, performs web search if relevant, sends
digest directly to user via Slack. Validated pattern — no new infrastructure needed.

### 2.4 Flow 3: EmailSearchAgent (SYNC, on-demand)

New agent called by SmartAgent via tool when user queries email content.
Two search modes selected by the agent based on query:

**Mode A — Index Search (fast, ~0.5s):**

```
SmartAgent → tool call: search_email(query, mode="index")
  → EmailSearchAgent._search_index()
       → ECO-tier LLM extracts search keys from query
       → EmbeddingService.embed(keys) → query vectors
       → IndexedEmailRepository.find_nearest(
             user_id, [vector, tags_vector, metadata_vector, attachments_vector]
           ) → 4 parallel queries → RRF → top N IndexedEmail
       → returns List[IndexedEmail] (fact sentences + metadata)
  → SmartAgent uses results inline
```

**Mode B — Deep Search (rich, ~3–5s):**

```
SmartAgent → tool call: search_email(email_ids, mode="deep")
  → EmailSearchAgent._search_deep()
       → EmailProviderPort.batch_get_full_content(credentials, email_ids)
            ↓ format=full: body text + attachment binaries
       → [for each attachment] markitdown.parse(binary) → text
       → LLM extracts structured facts from full content + attachment text
       → returns rich extracted facts
  → SmartAgent uses results inline
```

**When does SmartAgent choose which mode?**
- Index search: broad queries ("find my healthcare emails", "any flights in March")
- Deep search: specific follow-up ("get details from that Ryanair booking", "read the lab report")
- SmartAgent decides based on whether `email_ids` are already known from prior index search

### 2.5 Reliability & Error Recovery

**Principle: self-healing by default. Human action only when programmatically impossible.**

| Failure | Response | User sees |
|---|---|---|
| Gmail rate limit | Exponential backoff → auto-retry (transparent) | Nothing |
| LLM batch timeout | Retry 1x → skip batch + log → continue job | Nothing during job; summary mentions skipped count |
| Embedding fail | Store doc with `vectors=null`, `embedding_pending=True` | Nothing — repair job fixes it |
| Cloud Tasks timeout | Reads `next_page_token` from job journal → resumes | Nothing |
| Cloud Tasks auto-retry (≤5x) | Built-in | Nothing |
| OAuth token expired | Job stops, cannot auto-fix | "Gmail disconnected. Reconnect: /cabinet" |
| >10% batches failed | Job completes but flagged | "✅ Done with warnings. N emails skipped. Retry in Cabinet." |
| Firestore persistent failure | Job fails | "❌ Indexing failed. Details in Cabinet." |

**Repair Job** (Cloud Scheduler, every 6h — lightweight, no user interaction):
```
Query: embedding_pending=True → re-embed → update vectors → embedding_pending=False
```

**Job Journal** — stored in `{env}_email_indexing_jobs_v1` (one doc per run):
```yaml
job_id, user_id, provider, triggered_by, status
next_page_token        # primary resume cursor
last_email_date        # fallback if token expired
emails_fetched, emails_stored, emails_failed, embedding_pending
errors: [{email_id, stage, error}]  # capped at 100
started_at, updated_at, completed_at
```

Cabinet shows job history (last N runs) with Retry button. No raw logs exposed.

### 2.6 OAuth: Gmail Incremental Consent

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

### 3.2 Collection: `{env}_domain_email_facts_v1`

Doc ID: `{email_id}` (idempotent upsert — safe on retry)

Structure mirrors `FactEntity` to enable an identical search pattern to MemorySearchAgent.

```yaml
# Identifiers
email_id: "msg_xyz123"        # = document ID
user_id: "user_abc"
account_id: "account_xyz"
source: "gmail"               # "gmail" | "outlook"

# Content — mirrors FactEntity
text: "User booked flight KBP→BCN on March 15 2025, ref RYR1234 via Ryanair"
                              # Extracted fact sentence — primary search field
vector: [0.042, -0.318, ...]  # embed(text) — 768 dim
tags_vector: [0.123, -0.456, ...]     # embed(tags joined) — 768 dim
metadata_vector: [-0.789, 0.012, ...] # embed(structured values: amounts, dates, refs) — 768 dim
attachments_vector: [0.211, 0.034, ...]  # embed(attachment filenames joined) — 768 dim; null if no attachments

# Classification
category: "travel"            # See §3.5 for category list
tags: ["flight", "ryanair", "booking", "bcn", "kyiv"]

# Structured metadata (for display + metadata_vector embedding)
metadata:
  subject: "Your flight KBP-BCN is confirmed"
  from_address: "noreply@ryanair.com"
  snippet: "Booking confirmed. Flight FR8421..."  # kept in metadata only
  flight_number: "FR8421"
  departure_city: "Kyiv"
  arrival_city: "Barcelona"
  departure_date: "2025-03-15"
  airline: "Ryanair"
  confirmation_code: "ABC123"

# Email-specific fields
subject: "Your flight KBP-BCN is confirmed"  # top-level for display
from_address: "noreply@ryanair.com"
email_date: 2025-03-10T14:30:00Z              # original email date
attachments: ["booking_confirmation.pdf"]     # attachment filenames (top-level, not just metadata)

# Lifecycle
state: "current"              # "current" | "archived"
indexed_at: 2026-02-22T10:00Z
consolidated_at: null         # set when sent to ConsolidationAgent; null = pending
```

**Not stored:** full email body (fetched live from Gmail at query time), attachment content.
**4 vectors:** `vector` (fact text) + `tags_vector` + `metadata_vector` + `attachments_vector`.
`attachments_vector` is null when no attachments — EmailSearchAgent skips that query.

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
    "collectionGroup": "{env}_domain_email_facts_v1",
    "fields": [
      {"fieldPath": "user_id", "order": "ASCENDING"},
      {"fieldPath": "email_date", "order": "DESCENDING"}
    ]
  },
  {
    "collectionGroup": "{env}_domain_email_facts_v1",
    "fields": [
      {"fieldPath": "user_id", "order": "ASCENDING"},
      {"fieldPath": "consolidated_at", "order": "ASCENDING"}
    ]
  },
  {
    "collectionGroup": "{env}_domain_email_facts_v1",
    "fields": [
      {"fieldPath": "vector", "vectorConfig": {"dimension": 768, "flat": {}}}
    ]
  },
  {
    "collectionGroup": "{env}_domain_email_facts_v1",
    "fields": [
      {"fieldPath": "tags_vector", "vectorConfig": {"dimension": 768, "flat": {}}}
    ]
  },
  {
    "collectionGroup": "{env}_domain_email_facts_v1",
    "fields": [
      {"fieldPath": "metadata_vector", "vectorConfig": {"dimension": 768, "flat": {}}}
    ]
  },
  {
    "collectionGroup": "{env}_domain_email_facts_v1",
    "fields": [
      {"fieldPath": "attachments_vector", "vectorConfig": {"dimension": 768, "flat": {}}}
    ]
  }
]
```

---

## 4. Email Classification (Agentic, Tool-Assisted)

Model: Gemini Flash (BALANCED tier). One agent invocation per batch of up to 100 emails.

**Approach:** Agentic with `get_email_details` tool. The model receives all email metadata,
autonomously decides which emails need full body + attachment names (ambiguous snippets,
truncated content, unknown senders), calls the tool for those, then classifies everything.

**Tool:** `get_email_details(email_ids: List[str])` → returns `{body_text, attachments: List[str]}`
per email via `format=full` Gmail API. Attachment filenames are first-class signals
(e.g., `contract_rigert.pdf` confirms legal value even if subject is vague).

**Prompt design:** Groovy DSL cognitive process framework (not heuristics list).
Primary filter: `reasoning_test: "Will this email still be informative and useful in 30 days?"`.
The model reasons from first principles rather than pattern-matching.
See `scripts/email/test_email_classification_poc.py` for current prompt.

**Output per email:**

```json
{
  "email_id": "msg_xyz123",
  "valuable": true,
  "category": "travel",
  "fact": "User booked flight KBP→BCN on March 15 2025, ref RYR1234 via Ryanair",
  "tags": ["flight", "ryanair", "booking", "bcn", "kyiv"],
  "reason": "Confirmed booking with reference number — remains useful in 30+ days"
}
```

```json
{
  "email_id": "msg_abc456",
  "valuable": false,
  "category": null,
  "fact": null,
  "tags": [],
  "reason": "LinkedIn notification — social noise, no confirmed event"
}
```

**`valuable=false`** — email discarded, not written to Firestore.
**`fact`** — self-contained sentence in past tense; becomes `text` field in `IndexedEmail`.
Stored entities extracted from `fact` + `tags` populate `metadata` for `metadata_vector`.

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

@dataclass
class EmailFullContent:
    """
    Returned by EmailProviderPort.batch_get_full_content().
    Used by EmailClassificationService (ambiguous snippet) and
    EmailSearchAgent Mode B (deep search + markitdown attachment parsing).
    Attachment binaries populated only when deep=True is passed to the adapter.
    """
    email_id: str
    body_text: str                        # Plain text body (HTML stripped by adapter)
    body_html: Optional[str]              # Original HTML (structured extraction if needed)
    attachments: List[str]                # Attachment filenames only
    attachment_binaries: Dict[str, bytes] # filename → bytes; empty dict if deep=False

class IndexedEmail(BaseModel):
    """Stored in Firestore — mirrors FactEntity structure for identical search pattern."""
    # Identifiers
    email_id: str                              # = Firestore document ID
    user_id: str
    account_id: str
    source: str                                # "gmail" | "outlook"

    # Content (mirrors FactEntity)
    text: str                                  # extracted fact sentence
    vector: Optional[List[float]] = None       # embed(text)
    tags_vector: Optional[List[float]] = None  # embed(tags joined)
    metadata_vector: Optional[List[float]] = None  # embed(structured values)
    attachments_vector: Optional[List[float]] = None  # embed(attachment filenames); None if no attachments

    tags: List[str]
    category: str
    metadata: Dict[str, Any]                   # subject, from_address, snippet + structured entities

    # Email-specific
    subject: str                               # top-level for display
    from_address: str
    email_date: datetime                       # original email date
    attachments: List[str] = []               # attachment filenames

    # Lifecycle
    state: str = "current"
    indexed_at: datetime
    consolidated_at: Optional[datetime] = None  # set when batch sent to ConsolidationAgent

@dataclass
class IndexingState:
    user_id: str
    provider: str
    indexed_through: Optional[datetime]   # None = never indexed

class IndexingJob(BaseModel):
    """One record per indexing run — used for resume, retry, and Cabinet history."""
    job_id: str
    user_id: str
    provider: str
    triggered_by: str              # "cabinet" | "scheduler"
    status: str                    # "running"|"completed"|"failed"|"failed_auth"|"paused"
    next_page_token: Optional[str] # primary resume cursor
    last_email_date: Optional[datetime]  # fallback cursor if page token expired
    emails_fetched: int = 0
    emails_stored: int = 0
    emails_failed: int = 0
    embedding_pending: int = 0
    errors: List[Dict[str, Any]] = []  # capped at 100: {email_id, stage, error}
    started_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None

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
| `src/ports/email_exclusions_port.py` | ABC: `get_exclusions`, `add_exclusions`, `delete_exclusion`, `list_exclusions` |
| `src/ports/email_indexing_job_repository.py` | ABC: `create_job`, `update_job`, `get_job`, `get_latest_job`, `list_jobs` |
| `src/adapters/firestore_oauth_credentials_adapter.py` | Firestore impl. Doc ID: `{user_id}_{provider}` |
| `src/adapters/gmail_provider_adapter.py` | `aiohttp` Gmail REST. Pagination via `pageToken`. Token refresh via `oauth2.googleapis.com/token`. `batch_get_full_content`: `format=full` → body + attachment filenames; `deep=True` → also attachment binaries. |
| `src/adapters/firestore_indexed_email_repo.py` | Batch writes (500/batch). 4-vector RRF search. Indexing state. Repair query (`embedding_pending=True`). Consolidation query (`consolidated_at IS NULL`). |
| `src/adapters/firestore_email_exclusions_adapter.py` | Exclusion patterns per user. Auto-populated during indexing. |
| `src/adapters/firestore_email_job_repo.py` | Job journal. Partial updates after each batch. Cabinet history + resume cursor. |
| `src/services/email_classification_service.py` | Agentic LLM batch. Gemini Flash. `get_email_details` tool. Pydantic output. Exclusion candidate detection. |
| `src/services/email_indexing_service.py` | Full indexing pipeline. Fans out across connected providers. Advances `indexed_through` only on batch success. |
| `src/services/email_embedding_repair_service.py` | Query `embedding_pending=True` → re-embed → `update_vectors`. Called by Cloud Scheduler every 6h. |
| `src/agents/email_agent.py` | `EmailAgent(BaseAgent)`. `_handle_indexing()` (Flow 1 + 2). Multi-provider fan-out. Slack notification on completion. |
| `src/agents/email_search_agent.py` | `EmailSearchAgent(BaseAgent)`. `_search_index()` (Mode A, RRF) + `_search_deep()` (Mode B, markitdown). Called by SmartAgent via tool. |

---

## 7. Modified Components

| File | Change |
|------|--------|
| `src/adapters/firebase_auth_adapter.py` | Add `additional_scopes: Optional[List[str]] = None` to `get_authorization_url()` — backward-compatible |
| `src/web/oauth_app.py` | Blueprint factory gains `oauth_credentials: OAuthCredentialsPort`. New endpoints: `/auth/connect-gmail`, `/auth/connect-gmail/callback`, `DELETE /auth/disconnect-gmail` |
| `src/web/user_cabinet_app.py` | New endpoints: `GET /api/gmail/status`, `POST /api/gmail/index`, `DELETE /api/gmail/disconnect`. Setting: `email_daily_summary` toggle. |
| `src/handlers/agent_worker_handler.py` | Slack notification on async task completion (TODO already exists). `__init__` gains `slack_client: Optional[AsyncWebClient]` |
| `src/composition/service_container.py` | Wire all new services and adapters |
| `main.py` | Register `EmailAgent` (intents: `index_email` ASYNC) + `EmailSearchAgent` (intents: `search_email` SYNC) |
| `firestore.indexes.json` | Add composite + vector indexes for `{env}_domain_email_facts_v1` and `{env}_email_indexing_jobs_v1` |
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

8. `src/adapters/gmail_provider_adapter.py` — metadata + full content fetch (`deep` flag)
9. `src/ports/indexed_email_repository.py` + `src/ports/email_exclusions_port.py` + `src/ports/email_indexing_job_repository.py`
10. `src/adapters/firestore_indexed_email_repo.py` + `src/adapters/firestore_email_exclusions_adapter.py` + `src/adapters/firestore_email_job_repo.py`
11. `firestore.indexes.json` — vector + composite indexes for `{env}_domain_email_facts_v1` and `{env}_email_indexing_jobs_v1`
12. `src/services/email_classification_service.py` — agentic batch, `get_email_details` tool
13. `src/services/email_indexing_service.py` — pipeline orchestration
14. `src/services/email_embedding_repair_service.py` — repair job
15. Tests: `tests/unit/ports/test_indexed_email_repository.py`, `tests/unit/ports/test_email_indexing_job_repository.py`, `tests/unit/services/test_email_classification_service.py`, `tests/unit/services/test_email_indexing_service.py`

### Phase 3 — Agent + Integration

16. `src/agents/email_agent.py` — `_handle_indexing()` (Flow 1 + 2), multi-provider fan-out
17. `src/agents/email_search_agent.py` — `_search_index()` (Mode A) + `_search_deep()` (Mode B)
18. `src/handlers/agent_worker_handler.py` — Slack notification on async task completion
19. `src/web/user_cabinet_app.py` — `/api/gmail/index` + job history endpoint
20. `src/composition/service_container.py` — wire all email components (see §2.1.4)
21. `main.py` — register `EmailAgent` (intent: `index_email` ASYNC) + `EmailSearchAgent` (intent: `search_email` SYNC)
22. `requirements.txt` — add `google-auth>=2.0.0`, `google-auth-oauthlib>=1.0.0`
23. Tests: `tests/unit/agents/test_email_agent.py`, `tests/unit/agents/test_email_search_agent.py`, `tests/e2e/test_email_agent_flow.py`

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

- **Attachment filenames only:** Filenames stored (search signal); attachment content never stored
- **Snippets discarded:** Used only during indexing LLM call, never persisted
- **Encryption:** Firestore encryption at rest (GCP default)
- **User-scoped:** Multi-tenant isolation (`user_id` + `account_id` on every document)
- **Retention:** User can delete all indexed data via `/api/gmail/disconnect`

### 11.3 Live Gmail Access (Search)

At search time, full email content is fetched live from Gmail API.
This content is processed in memory by the LLM call and never stored to Firestore or logs.

---

## 12. User Experience

**Cabinet UX principle:** Cabinet exposes user-facing entities only — buttons, status indicators,
plain-language summaries. No raw logs, no technical fields, no job internals.
"3 batches failed" is an implementation detail; "89 emails skipped, retry available" is a user message.
All technical detail lives in `IndexingJob` (Firestore) accessible via dev tools, not via UI.
This constraint applies to all Cabinet pages related to email — design for the user, not the debugger.

### 12.1 Gmail Status Panel

The Gmail section in Cabinet has two states depending on whether the user has connected Gmail.

**State A — Not connected:**

```
┌─────────────────────────────────────────────────┐
│  📧 Gmail                                        │
│                                                  │
│  Not connected                                   │
│                                                  │
│  [Connect Gmail]                                 │
│                                                  │
│  Connect your Gmail to let Alek search your      │
│  email history and extract facts from it.        │
│  Read-only access. You can disconnect any time.  │
└─────────────────────────────────────────────────┘
```

**State B — Connected, never indexed:**

```
┌─────────────────────────────────────────────────┐
│  📧 Gmail — user@gmail.com ✓                     │
│                                                  │
│  Not yet indexed                                 │
│                                                  │
│  [Index emails (last 3 years)]  [Disconnect]     │
└─────────────────────────────────────────────────┘
```

**State C — Connected, indexed:**

```
┌─────────────────────────────────────────────────┐
│  📧 Gmail — user@gmail.com ✓                     │
│                                                  │
│  Last indexed: Feb 28, 2026                      │
│  Stored: 1,287 emails  │  Coverage: Nov 2023 – Feb 2026 │
│                                                  │
│  [Index new emails]     [Disconnect]             │
│                                                  │
│  Last job: ✅ Feb 28 — 151 new, 1,287 total      │
│  (or) Last job: ⚠️ Feb 25 — 3 emails skipped     │
│  (or) Last job: 🔄 Running... Feb 28             │
└─────────────────────────────────────────────────┘
```

### 12.2 Connect Gmail

**Flow:**

```
[Connect Gmail] clicked
  ↓
GET /auth/connect-gmail
  ↓ requires active web session (user already logged in to Cabinet)
  ↓ builds Google OAuth URL with scope: gmail.readonly
  ↓ uses FirebaseAuthAdapter.get_authorization_url(additional_scopes=["gmail.readonly"])
Redirect → Google consent screen: "Alek wants to read your Gmail"
  ↓
User grants access
  ↓
GET /auth/connect-gmail/callback?code=...
  → exchange code → access_token + refresh_token
  → OAuthCredentialsPort.save_credentials(user_id, provider="gmail", ...)
  → redirect to /cabinet
Cabinet shows State B (connected, not yet indexed)
```

**Implementation note:** This is incremental consent layered on top of the existing Firebase
OAuth session (`openid email profile`). Gmail tokens are stored separately in
`{env}_oauth_credentials` — they have their own `token_expiry` and refresh lifecycle,
independent of the Firebase session token.

### 12.3 Index Now (Force Indexing)

**Button label:** "Index emails (last 3 years)" on first run; "Index new emails" when already indexed.

**Scope:** Always indexes the window `[today − 3 years, today]`. Within that window, the
service resumes from `indexed_through` (if exists) — so only unindexed emails are fetched.
On first run: full 3-year range. On subsequent runs: only the gap since last run.

**Why 3 years fixed:** Longer history has diminishing biographical value. Old subscriptions,
expired tickets, past addresses — mostly noise. 3 years captures active life context.
The lower bound is computed server-side at the time the job is enqueued.

**Flow:**

```
[Index new emails] clicked
  ↓
POST /api/gmail/index
  → IndexingJob created: {triggered_by="cabinet", date_from=today-3years}
  → Cloud Tasks job enqueued (async — runs in the background)
  ↓
Cabinet: button changes to "🔄 Indexing..." (disabled)
  ↓
[Background, Cloud Tasks]
  → EmailIndexingService.run(user_id, provider="gmail", date_from=today-3years)
  → resumes from indexed_through if already set (skips already-indexed range)
  → same pipeline: metadata → classify → batch_get_full_content → embed → store
  ↓
Slack notification on completion (§12.4)
Cabinet: refreshes job status on next visit
```

**Button states:**
- `idle (never indexed)` → "Index emails (last 3 years)"
- `idle (indexed before)` → "Index new emails"
- `running` → "🔄 Indexing..." (disabled, no double-submit)
- `failed` → "Retry indexing" (re-enqueues same job)

**Endpoint:** `POST /api/gmail/index` — no body required. Server computes `date_from`.
Returns `{job_id, status: "enqueued"}`. Cabinet polls `GET /api/gmail/status` for updates
(or user manually refreshes — no websocket needed).

### 12.4 Indexing Completion (Slack notification)

```
[Job completes — Cloud Tasks worker sends Slack message]

Bot: "✅ Gmail indexed
      New this run: 151 emails processed → 19 stored
      Total in index: 1,287 emails
        ✈️ Travel: 89   💰 Finance: 341   🏥 Healthcare: 63
        💼 Work: 512    ⚖️ Legal: 28      👤 Personal: 254
      Excluded senders: 12 patterns auto-detected
      Coverage: Nov 2023 – Feb 2026"

[If warnings:]
Bot: "⚠️ Gmail indexed with warnings
      3 emails skipped (rate limit retries exhausted).
      Everything else stored. Retry available in Cabinet."

[If failed:]
Bot: "❌ Gmail indexing failed — token expired.
      Reconnect Gmail: /cabinet"
```

### 12.5 Disconnect Gmail

**Flow:**

```
[Disconnect] clicked
  ↓
Cabinet shows confirmation dialog:
  "This will remove Gmail access and delete all indexed email data.
   Your biographical facts (already consolidated to memory) are not affected.
   Are you sure?"
  [Cancel]  [Yes, disconnect]
  ↓
DELETE /auth/disconnect-gmail
  → EmailProviderPort.revoke_token(credentials)   ← revoke at Google
  → OAuthCredentialsPort.revoke_credentials(user_id, "gmail")  ← delete tokens
  → IndexedEmailRepository.delete_by_user(user_id)  ← delete all indexed facts
  ↓
Cabinet shows State A (not connected)
```

**Important:** Disconnect does NOT affect biographical facts already consolidated into
`{env}_domain_facts_v2` by ConsolidationAgent. The email index (`{env}_domain_email_facts_v1`)
is deleted; consolidated memory is untouched.

### 12.6 Query Flow (Slack)

```
User: "find my test results for 2025"

Bot: (3–4s — Mode A: vector search → top 5 email records → Mode B if follow-up)

Bot: "Found 5 medical emails from 2025 with test results:

      📋 March 28 — GFR (CKD-EPI) >90 mL/min (Normal). HbA1c 5.1%.
         📎 lab_report_march.pdf
      📋 January 15 — Blood panel: Uric acid elevated (Hyperuricemia confirmed).
      📋 May 7 — Kidney CT — no active stones detected.
      📋 August 3 — Periodontitis follow-up post-curettage: stable.
      📋 November 20 — Lipid panel: Dyslipidemia under management."

User: "покажи детали мартовского анализа"

Bot: (EmailSearchAgent Mode B — fetches full email + parses lab_report_march.pdf)

Bot: "Лабораторный отчёт от 28 марта 2025:
      Креатинин: 82 мкмоль/л (N 62–115)
      Мочевина: 5.1 ммоль/л
      GFR (CKD-EPI): 94 мл/мин — норма
      HbA1c: 5.1% — норма
      Issued by: Synevo, Kyiv"
```

---

## 13. Future Enhancements

### 13.1 ConsolidationAgent Integration (UAT Validated)

**Status: UAT validated (2026-02-28).** Not a future vision — an empirically confirmed mechanism.

**UAT results** (`test_consolidation_dryrun.py`, batch=151 email facts):
- Input: 151 classified email facts (3 months, primary + updates folders)
- Output: 19 biographical facts created, 0 noise written, 132 silently discarded
- Elapsed: 183s (single Opus call, multi-turn tool loop)
- Quality: correct domain/temporal_class/context_priority on all 19; rich structured metadata
  (card_last_4, dates, costs, institutions); cross-email merging (Bank of America: multiple
  receipts → one fact with card_last_4 and account_last_4); biographical context used
  correctly (Freebox + school location tied to family base in Angers)
- Batch size note: 150-fact batch produced _more accurate_ results than smaller batches —
  ConsolidationAgent detects patterns across items (e.g., recurring subscriptions, related accounts)

Email archive → ConsolidationAgent is the correct gate for biographical memory.
ConsolidationAgent already handles: domain taxonomy, temporal class, deduplication,
SCD2 versioning, conflict resolution, decomposition. No second classification layer needed.

**Pipeline:**

```
{env}_domain_email_facts_v1 (WHERE consolidated_at IS NULL)
  ↓ batch of N facts (periodic job or on-demand)
ConsolidationAgent prompt (system_alert):
  "[system_alert] Система по поручению пользователя просканировала ящик электронной почты
   и сделала выборку кандидатов для занесения в базу фактов. Выборка содержит шум.
   Оцени входящие данные и обработай по своему алгоритму.

   Кандидаты:
   1. {"email_id": "msg_xyz123", "fact": "User booked flight KBP→BCN March 15 2025 ref RYR1234",
       "category": "travel", "tags": ["flight", "ryanair", "booking"],
       "date": "2025-03-10", "attachments": ["booking_confirmation.pdf"],
       "metadata": {"subject": "Your flight confirmed", "from": "noreply@ryanair.com"}}
   ..."

Note: all fields from `IndexedEmail` included — `email_id` for traceability,
`attachments` (filenames) to help ConsolidationAgent assess evidential weight
(e.g., "lab_results.pdf" → stronger signal than snippet alone),
`metadata.subject` + `from` for sender context.
  ↓
ConsolidationAgent applies full 8-step deliberation:
  - Searches existing facts DB for duplicates
  - Creates/updates/discards based on taxonomy and lifecycle
  - Decides what is biographically significant (healthcare, legal, work, personal)
  - Discards transactional noise (travel receipts, subscription confirmations)
  ↓
Mark processed facts: consolidated_at = now()
```

**Category routing:** ConsolidationAgent naturally handles this. `travel` and `finance`
receipts are typically EPHEMERAL (ttl=14 days) or discarded. `healthcare`, `legal`, `work`
become STABLE/PERMANENT biographical facts. No explicit routing rule needed.

**Idempotency:** `consolidated_at` on each email fact. Batch query:
`WHERE consolidated_at IS NULL AND user_id = X LIMIT 100`.
Re-runs are safe — ConsolidationAgent deduplication prevents double-writes.

**Trigger:** Piggybacked onto normal conversation consolidation. After ConsolidationAgent
finishes processing a conversation batch (overflow trigger or manual `$consolidate`),
it checks `IndexedEmailRepository.get_unconsolidated_batch(user_id, limit=200)`.
If non-empty → runs email triage as a second pass within the same Cloud Tasks execution.

This avoids a separate scheduler and naturally throttles email triage to the same cadence
as conversation consolidation. If the user isn't chatting, email facts queue up and are
processed on the next consolidation event. Logic can be extended later (e.g., add nightly
fallback scheduler) without changing the core flow.

**Pending prompt improvement:** ConsolidationAgent should be instructed to always add `email`
to the `tags` array on facts derived from email sources. This enables future `tag:email`
filtered queries to retrieve the full email-sourced knowledge base. Currently not in the
consolidation prompt — to be added before production deployment.

### 13.2 Attachment Deep-Parse (Conversational, Opt-In)

Attachment content (PDF → text) is never fetched automatically. It is accessed on-demand
through a natural conversation, where the user explicitly asks to see attachment details.

**Why conversational and not a command:** The user may not know in advance which emails
have interesting attachments. The natural flow is discovery → curiosity → request.

**Example conversation:**

```
User:   "напомни мне все покупки из амазона за осень"
Bot:    "Found 4 Amazon orders Oct–Dec 2025:
         • Oct 12 — Kindle Paperwhite (Order #123-456) — 89 EUR
           📎 invoice_amazon_oct12.pdf
         • Nov 3  — AirPods Pro (Order #789-012) — 249 EUR
           📎 invoice_amazon_nov3.pdf
         ..."

User:   "подожди ты купил корову — у тебя написано что-то про ноябрь"
Bot:    "Нет записей о покупке коровы. По ноябрю — только AirPods Pro.
         Хочешь я загляну в инвойс подробнее?"

User:   "да посмотри детали в аттачменте"
Bot:    (calls EmailSearchAgent.search_deep(email_id="msg_nov3") → Mode B)
        (GmailProviderAdapter.batch_get_full_content → attachment binary)
        (markitdown.parse(binary) → invoice text)
        (LLM extracts structured facts from text)
        "Инвойс Amazon от 3 ноября: AirPods Pro (Gen 2) — 249 EUR.
         Доставлено 7 ноября на адрес Puçol, Spain.
         Order: 789-012-3456789."
```

**How `email_id` survives across turns:**

When SmartAgent surfaces email facts, it includes `email_refs` in `rich_content` JSON:

```json
{
  "full_response": "Found 4 Amazon orders...",
  "response_summary": "4 Amazon orders Oct-Dec 2025",
  "rich_content": {
    "email_refs": [
      {"email_id": "msg_oct12", "subject": "Your Amazon order #123-456", "attachments": ["invoice_amazon_oct12.pdf"]},
      {"email_id": "msg_nov3",  "subject": "Your Amazon order #789-012", "attachments": ["invoice_amazon_nov3.pdf"]}
    ]
  }
}
```

Next turn: "посмотри детали в аттачменте" → SmartAgent finds `email_refs` in conversation
history → resolves most recent / most relevant `email_id` → calls `search_email(mode="deep")`.
No new state slots needed — `rich_content` is already part of conversation history.

**Privacy:** opt-in by the nature of the conversation. User must explicitly ask to read
attachment content. Bot never fetches attachment binaries without a direct user request.

**Infrastructure required (all already in RFC):**
- `EmailSearchAgent.search_deep(email_ids)` → Mode B (§2.4)
- `EmailProviderPort.batch_get_full_content(deep=True)` → attachment binaries
- `markitdown[all]` — already in `requirements.txt`
- `rich_content.email_refs` — SmartAgent JSON output format (§4, `OUTPUT_FORMAT_JSON` token)

**Attachment types:**
- Medical reports → diagnoses, lab values, prescriptions
- Invoices → amounts, items, delivery details
- Contracts → parties, terms, dates, obligations

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
   - **Decision:** Conversational opt-in (see §13.2). Filenames always stored and shown to user.
     Attachment content (PDF → text) fetched only when user explicitly asks in conversation.
     No command, no category toggle — the request is the consent.

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
- ❌ `EmailExclusionsPort` + `FirestoreEmailExclusionsAdapter`
- ❌ `EmailIndexingJobRepository` + `FirestoreEmailIndexingJobRepo`
- ❌ `EmailClassificationService` (agentic LLM batch with tool use)
- ❌ `EmailIndexingService` (pipeline orchestration, multi-provider fan-out)
- ❌ `EmailEmbeddingRepairService` (repair job, Cloud Scheduler every 6h)
- ❌ `EmailAgent` (async indexing, multi-provider)
- ❌ `EmailSearchAgent` (Mode A index search + Mode B deep search)
- ❌ `{env}_domain_email_facts_v1` Firestore collection + vector indexes
- ❌ `{env}_oauth_credentials` Firestore collection
- ❌ `{env}_email_indexing_jobs_v1` Firestore collection (job journal)
- ❌ Cabinet UI: "Connect Gmail" + "Index Gmail" buttons

---

## 18. Implementation Plan

Critical path to first production run. Cabinet and search deferred until core pipeline works.
Mark items `✅` as completed.

### Блок 1 — Фундамент (domain + ports + тесты контрактов) ✅

- ✅ `src/domain/email.py` — все domain models (OAuthCredentials, EmailMetadata, EmailFullContent, EmailClassificationResult, IndexedEmail, IndexingState, IndexingJob, EmailExclusion)
- ✅ `src/ports/email_provider_port.py` — ABC (list_emails, batch_get_full_content, refresh_token)
- ✅ `src/ports/oauth_credentials_port.py` — ABC (get/save/revoke credentials, is_connected, list_connected_providers)
- ✅ `src/ports/indexed_email_repository.py` — ABC (save_batch, find_nearest, indexing state, consolidation batch, repair batch, vector update)
- ✅ `src/ports/email_exclusions_port.py` — ABC (get/add/delete/list exclusions)
- ✅ `src/ports/email_indexing_job_repository.py` — ABC (create/update/get/get_latest/list jobs)
- ✅ `tests/unit/ports/test_email_ports.py` — 35 port contract tests, all passing

### Блок 2 — Адаптеры + индексы Firestore

- [ ] `src/adapters/gmail_provider_adapter.py` — aiohttp Gmail REST; metadata + full content (deep flag); token refresh
- [ ] `src/adapters/firestore_oauth_credentials_adapter.py` — upsert/get/delete; doc ID: `{user_id}_{provider}`
- [ ] `src/adapters/firestore_indexed_email_repo.py` — save_batch (500/batch); 4-vector RRF search; consolidation query; repair query
- [ ] `src/adapters/firestore_email_exclusions_adapter.py` — exclusion patterns per user
- [ ] `src/adapters/firestore_email_job_repo.py` — job journal; partial updates; resume cursor
- [ ] `firestore.indexes.json` — composite + vector indexes для `{env}_domain_email_facts_v1` и `{env}_email_indexing_jobs_v1`

### Блок 3 — Сервисы pipeline

- [ ] `src/services/email_classification_service.py` — agentic Gemini Flash; `get_email_details` tool; per-chunk 100 emails; exclusion candidate detection
- [ ] `src/services/email_indexing_service.py` — per-chunk loop; resume от indexed_through; batch_get_full_content parallel (semaphore=10); advances cursor only on success
- [ ] `src/services/email_embedding_repair_service.py` — query embedding_pending=True → re-embed → update_vectors
- [ ] `tests/unit/services/test_email_classification_service.py`
- [ ] `tests/unit/services/test_email_indexing_service.py`

### Блок 4 — Скрипт + первый production прогон

- [ ] `scripts/email/run_indexing.py` — ручной wireset (паттерн как test_consolidation_dryrun.py); запуск на реальном Gmail без Cabinet
- [ ] Первый прогон: проверить качество классификации, attachment fetch, Firestore запись

### Блок 5 — ConsolidationAgent hook

- [ ] Расширить ConsolidationAgent: после обычного батча → `get_unconsolidated_batch(user_id, limit=200)` → email тридж → `mark_consolidated`
- [ ] Обогащённый кандидат: `email_id + attachments + metadata.subject/from` в system_alert prompt
- [ ] Добавить тег `email` в инструкцию промпта консолидатора
- [ ] Тест на результате прогона из блока 4

### Блок 6 — EmailAgent + EmailSearchAgent

- [ ] `src/agents/email_agent.py` — `_handle_indexing()` (Flow 1 + 2); multi-provider fan-out; Slack notification
- [ ] `src/agents/email_search_agent.py` — Mode A (vector RRF) + Mode B (markitdown, deep=True)
- [ ] `tests/unit/agents/test_email_agent.py`
- [ ] `tests/unit/agents/test_email_search_agent.py`

### Блок 7 — Web + Cabinet (последним)

- [ ] `src/adapters/firebase_auth_adapter.py` — `additional_scopes` param (backward-compatible)
- [ ] `src/web/oauth_app.py` — `/auth/connect-gmail` + callback + `DELETE /auth/disconnect-gmail`
- [ ] `src/web/user_cabinet_app.py` — `/api/gmail/status` + `/api/gmail/index` + `/api/gmail/disconnect` + email_daily_summary toggle
- [ ] `src/handlers/agent_worker_handler.py` — Slack notification on async task completion
- [ ] `src/composition/service_container.py` — wire all email components (see §2.1.4)
- [ ] `main.py` — register EmailAgent (intent: index_email ASYNC) + EmailSearchAgent (intent: search_email SYNC)
- [ ] `requirements.txt` — `google-auth>=2.0.0`, `google-auth-oauthlib>=1.0.0`
- [ ] `tests/e2e/test_email_agent_flow.py`

---

## 17. References

- **Gmail API:** https://developers.google.com/gmail/api/guides
- **Gmail REST Messages:** https://developers.google.com/gmail/api/reference/rest/v1/users.messages
- **Firestore Vector Search:** https://firebase.google.com/docs/firestore/vector-search
- **OAuth Multi-Tenant RFC:** [MULTI_TENANT_OAUTH_RFC.md](./MULTI_TENANT_OAUTH_RFC.md)
- **Search Enrichment Building Block:** [../05_building_blocks/search_enrichment/README.md](../05_building_blocks/search_enrichment/README.md)

### POC Scripts

| Script | Purpose |
|--------|---------|
| `scripts/email/test_email_classification_poc.py` | Fetch Gmail metadata, classify via Gemini Flash (agentic, tool-assisted), print table + save JSON. Validates `valuable%` and edge case handling before committing to schema. |
| `scripts/email/test_consolidation_dryrun.py` | Load classified email facts from POC JSON, feed to ConsolidationAgent as `system_alert` message. Fact reads hit real Firestore (dedup/conflict detection). Fact writes intercepted — nothing written. Validates §13.1 pipeline. |

---

## Changelog

### 2026-02-28 — Cabinet UX fully specified

§12 rewritten with 6 subsections:
- §12.1 Gmail Status Panel — 3 states (not connected / connected+unindexed / connected+indexed)
  with ASCII mockups for each state
- §12.2 Connect Gmail — incremental OAuth flow (gmail.readonly layered on existing Firebase session)
- §12.3 Index Now — "Index emails (last 3 years)" on first run / "Index new emails" on subsequent.
  Date window: server computes `today − 3 years`. Service resumes from `indexed_through` → only
  unindexed gap fetched. Button states: idle / running (disabled) / failed (Retry).
- §12.4 Slack completion notification — success / warnings / failed variants
- §12.5 Disconnect Gmail — confirmation dialog + revoke at Google + delete index (not memory)
- §12.6 Query Flow (Slack) — updated with attachment filename display + Mode B follow-up example

### 2026-02-28 — Gmail batch sizing, email consolidation trigger

- **§2.2 Flow 1:** Per-chunk loop fully specified with batch sizing rationale:
  100 emails/page (Gmail) = 100 emails/LLM call (aligned). Full-content fetch:
  `asyncio.gather + semaphore=10`. Firestore save: ~15 docs/chunk (100 × 15% rate).
  Resume cursor (`next_page_token`) persisted after every chunk — Cloud Tasks timeout safe.
- **§13.1 trigger:** Email facts sent to ConsolidationAgent after normal conversation
  consolidation (overflow or `$consolidate`), as a second pass in the same Cloud Tasks job.
  No new scheduler. Extensible later.

### 2026-02-28 — Batch fetch, enriched candidates, conversational attachment access

- **§2.2 Flow 1:** Added explicit `batch_get_full_content(valuable_ids)` step after classification.
  Pattern: parallel async with `asyncio.gather` + `semaphore=10` (Gmail quota-safe).
  Classifier fetches format=full only for ambiguous emails via tool; all valuable emails
  get a subsequent batch fetch for attachment filenames.
- **§13.1 candidate JSON:** Enriched with `email_id`, `attachments`, `metadata.subject/from`.
  ConsolidationAgent uses attachment filenames as evidential weight signal.
- **§13.2 Attachment Deep-Parse:** Fully designed — conversational opt-in pattern.
  `email_refs` in SmartAgent `rich_content` carries email_id across conversation turns.
  No new state slots — email_id lives in conversation history via existing rich_content JSON.
  Privacy: user request is the consent. `markitdown[all]` already in requirements.
- **§15 Q3:** Attachment opt-in resolved — conversational approach, not command/toggle.

### 2026-02-28 — ConsolidationAgent UAT validated

`test_consolidation_dryrun.py` completed on 151 classified email facts:

- 19 biographical facts (CREATE), 132 discarded, 0 noise written. Elapsed: 183s.
- Pattern detection across batch: multiple bank receipts → one consolidated fact with
  card/account last_4; recurring subscriptions merged; biographical context (Angers family
  base) correctly applied to Freebox + school entries.
- Larger batches (150) outperform smaller ones — cross-item patterns visible.
- Pending: add `email` tag instruction to consolidation prompt before production.
- `test_consolidation_dryrun.py` script added to §17 POC Scripts table.
- §13.1 updated from "POC confirmed" to "UAT validated" with empirical results.

### 2026-02-28 — Hexagonal architecture expanded

Full port contracts written (§2.1.2):
- `EmailProviderPort`: 3 methods (list_emails, batch_get_full_content, refresh_token)
- `OAuthCredentialsPort`: 5 methods (get/save/revoke credentials, is_connected, list_connected_providers)
- `IndexedEmailRepository`: 10 methods (save_batch, find_nearest, indexing state, consolidation batch, repair batch, vector update)
- `EmailExclusionsPort`: 4 methods (get/add/delete/list exclusions)
- `EmailIndexingJobRepository`: 5 methods (create/update/get/get_latest/list jobs) — **new 5th port**

Added: §2.1.1 file structure, §2.1.3 import rules table, §2.1.4 ServiceContainer wiring,
§2.1.5 port justification table. `EmailFullContent` domain model added to §5.
`EmailEmbeddingRepairService` and `FirestoreEmailIndexingJobRepo` added to §6.
Stale `{env}_indexed_emails` reference corrected to `{env}_domain_email_facts_v1` in §7.
Implementation phases updated with all new files.

### 2026-02-27 — POC findings integrated

POC script (`scripts/email/test_email_classification_poc.py`) completed. Key findings:

- **Classification prompt:** Replaced heuristics list with Groovy DSL cognitive process framework.
  Primary filter: 30-day relevance test. More reliable on edge cases (attachment-confirmed facts,
  rental contracts, borderline reminders).
- **Agentic classification:** Tool-assisted (`get_email_details`) replaces single-pass batch.
  Model autonomously decides which emails need full body + attachment names.
- **Attachment filenames as search signals:** `format=full` required (not metadata).
  Attachment names stored as top-level field + `attachments_vector` (4th vector).
- **Schema updated:** Added `text` (fact sentence), `vector`, `attachments`, `attachments_vector`,
  `consolidated_at`. Collection renamed to `{env}_domain_email_facts_v1`.
- **ConsolidationAgent integration (Variant D) validated:** Feeding email fact lists to
  ConsolidationAgent via prompt works. Agent correctly identifies biographical significance.
  Promoted from §13 "Vision" to confirmed approach with `consolidated_at` tracking.
- **Subject vector removed:** `metadata_vector` (structured values) retained. `subject` embedded
  into `text` indirectly — no separate subject vector needed.

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
