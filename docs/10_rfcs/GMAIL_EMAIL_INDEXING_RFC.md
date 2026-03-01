# RFC: Email Indexing System (Gmail + Future Providers)

**Status:** Phases 1–7 Complete
**Date:** 2026-02-11
**Updated:** 2026-03-01
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
    email_indexing_service.py               # Full pipeline orchestration (Flow 1 + 2)
    email_embedding_repair_service.py       # Re-embeds docs where embedding_pending=True

  agents/
    email_classification_agent.py           # EmailClassificationAgent — agentic LLM batch, get_email_details tool
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
        max_results: int = 100,
        query: Optional[str] = None,
    ) -> Tuple[List[EmailMetadata], Optional[str]]:
        """
        Fetch one page of email metadata.
        Returns (emails, next_page_token). next_page_token=None means last page.
        date_from=None means no lower bound (full history).
        query=None means no Gmail search filter (full mailbox).
        max_results: port default 100; EmailIndexingService passes page_size (default 300).
        Gmail API hard limit: 500 per page.
        """

    @abstractmethod
    async def batch_get_full_content(
        self,
        credentials: OAuthCredentials,
        email_ids: List[str],
        deep: bool = False,
    ) -> Dict[str, EmailFullContent]:
        """
        Fetch full content: body text, attachment filenames, attachment binaries.
        deep=False: body_text + attachment filenames only (attachment_binaries={}).
        deep=True:  also fetches attachment binaries (for markitdown parsing in Mode B).
        Used by:
          - EmailClassificationAgent: body when snippet is insufficient (via get_email_details tool)
          - EmailIndexingService: attachment filenames for all valuable emails after classification
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
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> List[IndexedEmail]:
        """
        Multi-vector RRF search across provided vector fields.
        vectors keys: "vector" | "tags_vector" | "metadata_vector" | "attachments_vector"
        Absent keys are skipped (e.g., attachments_vector absent → skip that query).
        Returns top-N by RRF score, filtered by user_id and state.
        date_from / date_to: optional Firestore pre-filter on email_date field.
        Requires dedicated vector indexes that include email_date — see §3.7.
        """

    @abstractmethod
    async def get_indexing_state(
        self, user_id: str, provider: str
    ) -> Optional[IndexingState]:
        """Returns None if never indexed."""

    @abstractmethod
    async def update_indexing_state(self, state: IndexingState) -> None:
        """Write indexing cursors. Called once per job at completion by _finalize_cursor().
        Each mode writes exactly one cursor field; all others are read from current state
        and preserved unchanged (read-modify-write pattern)."""

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

    @abstractmethod
    async def get_stale_running_jobs(self, updated_before: datetime) -> List[IndexingJob]:
        """
        Return all jobs with status=running and updated_at older than updated_before.
        Used by the watchdog to detect and mark zombie jobs as failed.
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

# — AgentExecutionContext (resolved by AgentContextBuilder, respects UserBotConfig.agent_tiers) —
email_config = UserBotConfig(agent_tiers={"email_classifier": PerformanceTier.BALANCED})
email_context = context_builder.build("email_classifier", email_config)
# email_context.provider = GeminiAdapter (BALANCED → gemini-flash-latest by default)
# email_context.tier = PerformanceTier.BALANCED
# email_context.capabilities.native_tools = True (required for get_email_details tool)

# — Agents (wired before services that depend on them) —
email_classifier = EmailClassificationAgent(
    config=AgentConfig(agent_id="email_classifier", agent_type="email_classifier"),
    execution_context=email_context,           # provider + model_name + tier + capabilities
    prompt_builder=prompt_builder,
    gmail=gmail_provider_adapter,              # used for get_email_details tool
    # user_id omitted — passed per-call to classify_batch(emails, user_id, credentials)
)

# — Services —
email_indexing_service = EmailIndexingService(
    gmail=gmail_provider_adapter,
    email_repo=indexed_email_repo,
    job_repo=email_job_repo,
    exclusions_repo=email_exclusions_adapter,
    classifier=email_classifier,
    embedding=embedding_service,
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
email_search_service = EmailSearchService(
    indexed_email_repo=indexed_email_repo,
    oauth_credentials=oauth_credentials_adapter,
    gmail_provider=gmail_provider_adapter,
    embedding_service=embedding_service,
)

# Per-user — constructed by UserAgentFactory, not ServiceContainer directly
email_search_agent = EmailSearchAgent(
    config=AgentConfig(agent_id=f"email_search_agent_{user_id}", agent_type="email_search"),
    execution_context=context_builder.build("email_search", user_profile.config),
    prompt_builder=prompt_builder,
    email_search_service=email_search_service,
    user_id=user_id,
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
- Gmail metadata page: **300 emails** (`maxResults=300`, default) — one page = one LLM classification call.
  Aligned deliberately so one "chunk" = 1 Gmail page + 1 LLM call + N full-content fetches.
  Configurable via `--count` flag in `run_indexing.py`. Gmail API hard limit: 500.
- LLM classification batch: **300 emails/call** — enough for cross-email pattern detection
  (recurring senders, subscription patterns). Empirically confirmed reliable at this size.
- Full content parallel fetch: **semaphore=10** — 250 req/s Gmail quota, 10 concurrent = safe
  at any reasonable page throughput.
- Firestore save batch: up to **500 docs** (Firestore hard limit), but in practice ~45 docs
  per chunk (300 emails × ~15% valuable rate).
- **Empirical valuable rate:** 73/500 emails = 14.6% in the first production run (Feb 2026).
  Confirms the 10–20% design assumption.

**Per-chunk loop** (repeats until all pages exhausted):

```
┌─ CHUNK (300 emails, default) ───────────────────────────────────────┐
│                                                                      │
│  EmailProviderPort.list_emails(page_token, max_results=300)          │
│       ↓ EmailMetadata × 300 (subject, from, date, snippet)          │
│                                                                      │
│  EmailExclusionsPort.get_exclusions(user_id)   ← once per job       │
│       ↓ pre-filter known low-value senders (fast, before LLM)       │
│                                                                      │
│  EmailClassificationAgent.classify_batch(emails_N)                  │
│       ↓ Gemini Flash — agentic, calls get_email_details tool        │
│         for ambiguous emails (vague subject / unknown sender)        │
│         format=full for those only                                   │
│       ↓ output: [{email_id, valuable, category, fact, tags,         │
│                   valuable_type}] × N                                │
│                                                                      │
│  [collect valuable_ids where valuable=True]  ← typically ~45        │
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
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
       ↓ next_page_token → repeat until None

When `next_page_token` is None (all pages consumed):
  _finalize_cursor(mode, date_from) — writes the appropriate cursor once:
    backfill → oldest_indexed_through = date_from
    reindex  → cursor_reindex         = date_from
    incremental → indexed_through    = max(existing, job.max_email_date)
  Each branch reads current state first and preserves all other cursor fields.

Slack notification: "✅ Gmail indexed: N total, M stored"
```

**Resume on Cloud Tasks timeout:** `next_page_token` persisted in `IndexingJob` after every
chunk. On retry, job reads `next_page_token` from Firestore and continues where it left off.
No emails re-fetched, no emails lost.

**Cursor write is terminal-only:** cursors (`indexed_through`, `oldest_indexed_through`,
`cursor_reindex`) are written exactly once — at job completion in `_finalize_cursor()`.
There are no per-batch cursor writes. Ownership is exclusive: each mode writes exactly one
cursor field and preserves the other two from current Firestore state.

### 2.3 Flow 2: Daily Incremental + Proactive Digest (ASYNC, scheduled)

Triggered by Cloud Scheduler (daily). Indexes only new emails since `indexed_through`
(cursor_max), then sends a proactive digest to the user via SmartAgent.

**Incremental bootstrap (when cursor_max is null):**
If `indexed_through` is null (incremental has never run), the service derives `date_from`
from other confirmed cursors: `max(oldest_indexed_through, cursor_reindex)`.
If all three cursors are null — today-only (single-day run).
No pre-write to Firestore: `_finalize_cursor` writes `indexed_through` at completion.

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

Specialist agent called by Smart and Quick via `delegate_to_specialist`. Three intents, all routed
to the same `EmailSearchAgent` instance. Routing is done on `AgentMessage.payload` keys — no
separate agent classes per intent.

```
Smart/Quick → delegate_to_specialist(intent, query, context={...})
  → EmailSearchAgent.execute()
  → dispatch on payload keys:
      email_id + filename  →  _handle_get_attachment()
      email_id only        →  _handle_get_details()
      query only           →  _handle_search_emails()
```

**Intent: `search_emails`** — semantic vector search

```
payload: { query: "..." }

Step 1 — Query key extraction (ECO-tier LLM, ~0.5s):
  LLM with EmailSearchAgent prompt (PromptBuilder, agent_type="email_search")
  Input:  EMAIL_SEARCH_REQUEST "user query" + last 3 history turns + biographical context
  Output: { primary_query, alternative_query, tags, date_from, date_to }  ← JSON, no code block
    date_from / date_to: YYYY-MM-DD strings extracted from any time signal in the query
    ("in 2023", "last 3 months", "since January") — null if no time signal present.
    LLM uses current_date_time to resolve relative expressions.
  Fallback: if LLM fails → uses raw query string for primary/alt/tags, dates=null

Step 2 — 7-stream multi-vector RRF (EmailSearchService, ~1–2s):
  3 embed calls in parallel (primary, alternative, tags_text)
  2 find_nearest calls in parallel (date_from/date_to passed as Firestore pre-filter):
    Call A (3 streams): vector:embed(primary)     + tags_vector:embed(tags)     + metadata_vector:embed(primary)
    Call B (4 streams): vector:embed(alternative) + tags_vector:embed(primary)  + metadata_vector:embed(tags)
                        + attachments_vector:embed(tags)
  Pre-filter applied only when dates are present; otherwise no date constraint.
  Cosine distance threshold: 0.4 — results farther than this are discarded by Firestore before RRF.
  Second-level RRF merge (k=60) → all IndexedEmail that passed the cosine distance filter.
  No artificial output cap — Firestore server-side threshold (0.4 = similarity ≥ 0.6) is the only gate.
  Logged: "A={n} B={n} → merged={n}"

Step 3 — Return to delegating agent:
  JSON string: { "count": N, "emails": [{ email_id, from, date, text, attachments }] }
  SmartAgent synthesizes natural-language response from the email list.
```

**Intent: `get_email_details`** — full Gmail body, no LLM

```
payload: { email_id: "19bd6ad70f3e911b" }

EmailSearchService.get_details(email_id, user_id):
  → get OAuth credentials (refresh if expired)
  → gmail_provider.batch_get_full_content([email_id], deep=False)
  → format: subject, from, date, body_text[:5000], attachment filenames

Returns: formatted text string to delegating agent.
```

**Intent: `get_email_attachment`** — extract attachment as text, no LLM

```
payload: { email_id: "19bd6ad70f3e911b", filename: "cerfa-15646.pdf" }

EmailSearchService.get_attachment(email_id, filename, user_id):
  → get OAuth credentials (refresh if expired)
  → gmail_provider.batch_get_full_content([email_id], deep=True)
  → size guard: > 3 MB single file or > 10 MB total → error message
  → convert_file_to_text(bytes, filename, mime) via markitdown
  → _truncate_with_alert(text, filename)  ← 30 000 char limit

Returns: converted text string to delegating agent.
```

**EmailSearchService methods:**

| Method | Intent | Description |
|--------|--------|-------------|
| `vector_search(primary, alternative, tags, user_id)` | `search_emails` | 7-stream RRF, returns JSON |
| `get_details(email_id, user_id)` | `get_email_details` | Gmail full body, ≤5000 chars |
| `get_attachment(email_id, filename, user_id)` | `get_email_attachment` | Gmail attachment → text via markitdown |

**Debug files produced per `search_emails` request:**
- `debug_prompts/email_search_prompt_*.txt` — system instruction + user message
- `debug_prompts/email_search_response_*.txt` — LLM JSON output (primary_query, alternative_query, tags)
- `debug_prompts/email_search_to_smart_response_*.txt` — JSON result returned to delegating agent

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
| User cancels via Cabinet | `status=cancelled` written to Firestore → next worker task reads it → returns 200 (skip) → Cloud Tasks marks task done → chain stops naturally | Cancel button in Cabinet; takes effect within 1 task (~60s max) |
| Job stuck in `running` (crash / deploy) | Watchdog (Cloud Scheduler, every 30 min) checks `updated_at < now - 2h` → marks as `failed` | Cabinet shows "❌ Failed" with Retry |

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
valuable_type: "confirmed_event"  # "confirmed_event" | "biographical_signal"
                              # confirmed_event: directly proves a specific event occurred
                              # biographical_signal: reveals lasting context (school, membership, location)

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
embedding_pending: false      # true if embedding failed; EmailEmbeddingRepairService re-embeds on next run
consolidated_at: null         # set when sent to ConsolidationAgent; null = pending
```

**Not stored:** full email body (fetched live from Gmail at query time), attachment content.
**4 vectors:** `vector` (fact text) + `tags_vector` + `metadata_vector` + `attachments_vector`.
`attachments_vector` is null when no attachments — EmailSearchAgent skips that query.
If `embedding_pending=true` all vector fields are null — document saved without vectors, repair job fills them in.

### 3.3 Collection: `{env}_email_indexing_state`

Doc ID: `{user_id}_{provider}`

```yaml
user_id: "user_abc"
provider: "gmail"
indexed_through:         2026-02-21T23:59:59Z   # null = incremental never completed
oldest_indexed_through:  2023-02-15T00:00:00Z   # null = backfill never completed
cursor_reindex:          2023-02-15T00:00:00Z   # null = reindex never completed
updated_at: 2026-02-22T10:00Z
```

**Three independent cursors — exclusive ownership:**
| Cursor | Written by | Value at write |
|--------|-----------|----------------|
| `indexed_through` | incremental only | `max(existing, job.max_email_date)` |
| `oldest_indexed_through` | backfill only | `date_from` (job start date) |
| `cursor_reindex` | reindex only | `date_from` (job start date) |

Each cursor is written exactly once per job at completion (`_finalize_cursor`).
The writer reads current Firestore state first and preserves the other two cursors unchanged.
Reindex and backfill never touch `indexed_through`. Overlapping ranges are intentional and allowed.

**Incremental bootstrap:** if `indexed_through` is null, `date_from = max(oldest_indexed_through, cursor_reindex)`.
All null → today-only run. No pre-write; `_finalize_cursor` writes `indexed_through` at completion.

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

### 3.5 Collection: `{env}_user_notification_state`

Doc ID: `{user_id}`

```yaml
user_id: "user_abc"
platform: "slack"        # "slack" | "telegram"
channel_id: "C0123456"   # Slack channel ID or Telegram chat ID
updated_at: 2026-03-01T12:00Z
```

Written on every incoming user message (best-effort). Used by `UserNotificationService` to know
where to deliver background notifications. Document is created at first user interaction and
overwritten on every subsequent message — always reflects the most recent active channel.

No index required (doc ID = user_id → direct lookup).

### 3.6 Email Categories

```
travel       — flights, hotels, car rentals, train bookings
finance      — invoices, receipts, bank statements, contracts
healthcare   — medical appointments, lab results, prescriptions, analyses
work         — meetings, projects, contracts, employment
legal        — official documents, registrations, permits
personal     — family, friends, personal correspondence
subscription — recurring service notifications (low value, often excluded)
```

### 3.7 Firestore Index Configuration

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
  },
  {
    "collectionGroup": "{env}_email_indexing_jobs_v1",
    "fields": [
      {"fieldPath": "status", "order": "ASCENDING"},
      {"fieldPath": "updated_at", "order": "ASCENDING"}
    ]
  }
]
```

---

## 4. Email Classification (Agentic, Tool-Assisted)

Model: Gemini Flash (BALANCED tier, `AgentExecutionContext`). One agent invocation per batch of up to 300 emails (default page size).

**Approach:** Agentic with `get_email_details` tool. The model receives all email metadata,
autonomously decides which emails need full body + attachment names (ambiguous snippets,
truncated content, unknown senders), calls the tool for those, then classifies everything.

**Tool:** `get_email_details(email_ids: List[str])` → returns `{body_text, attachments: List[str]}`
per email via `format=full` Gmail API. Attachment filenames are first-class signals
(e.g., `contract_rigert.pdf` confirms legal value even if subject is vague).

**Prompt design:** Groovy DSL cognitive process framework (`EMAIL_CLASSIFIER_COGNITIVE_PROCESS` token in Firestore).
Two-test selection model (email passes if EITHER test is satisfied):

- **TEST A — Confirmed event:** Does this email directly confirm a real-world event that happened?
  Examples: booking confirmation, receipt, delivery confirmation, medical result, contract signed.
  `valuable_type: "confirmed_event"`.
- **TEST B — Biographical signal:** Does this email reveal something about the user's life,
  relationships, memberships, or circumstances — even if no event is confirmed?
  Examples: school notification revealing a child's grade/school, club membership email,
  utility bill revealing address, gym schedule revealing habits.
  `valuable_type: "biographical_signal"`.
- **Neither test passes** → DISCARD.

**Output per email (valuable):**

```json
{
  "email_id": "msg_xyz123",
  "valuable": true,
  "category": "travel",
  "fact": "User booked flight KBP→BCN on March 15 2025, ref RYR1234 via Ryanair",
  "tags": ["flight", "ryanair", "booking", "bcn", "kyiv"],
  "valuable_type": "confirmed_event",
  "reason": "Confirmed booking with reference number — remains useful in 30+ days"
}
```

```json
{
  "email_id": "msg_ecole456",
  "valuable": true,
  "category": "personal",
  "fact": "User received notification from Ensemble Scolaire Saint-Benoît regarding a meeting for parents of 11th-grade students on March 3, 2026",
  "tags": ["school", "parents", "angers", "ecole"],
  "valuable_type": "biographical_signal",
  "reason": "Reveals child's school and grade level — lasting biographical context"
}
```

```json
{
  "email_id": "msg_abc789",
  "valuable": false,
  "category": null,
  "fact": null,
  "tags": [],
  "valuable_type": "confirmed_event",
  "reason": "LinkedIn notification — social noise, no confirmed event"
}
```

**`valuable=false`** — email discarded, not written to Firestore.
**`fact`** — self-contained sentence in past tense; becomes `text` field in `IndexedEmail`.
Stored entities extracted from `fact` + `tags` populate `metadata` for `metadata_vector`.
**Empirical rate:** 73/500 = 14.6% valuable in first production run (Feb 2026).

---

## 5. EmailSearchAgent Prompt System (PromptBuilder v4)

### 5.1 Firestore Documents

All tokens uploaded to `{env}_domain_prompt_tokens_v3_system` via `firestore_utils/upload.py`.

| Document ID | Collection | Description |
|-------------|-----------|-------------|
| `emailsearch_agent_v1` | `{env}_domain_prompt_blueprints_v3` | Blueprint: `outer_class="EmailSearchAgent extends Agent"`, `class_order=["properties","cognitive_process","output_format"]` |
| `email_search` | `{env}_domain_prompt_profiles_v3` | Agent profile: 3 tokens (orders 10/20/30, all `non_overridable=true`) |
| `EMAILSEARCH_PROPERTIES` | `{env}_domain_prompt_tokens_v3_system` | `class="properties"`, archetype string |
| `EMAILSEARCH_COGNITIVE_PROCESS` | `{env}_domain_prompt_tokens_v3_system` | `class="cognitive_process"`, 6-step Groovy DSL (steps 1–4: subject/primary/alt/tags; step 5: DATE RANGE → date_from/date_to; step 6: OUTPUT) |
| `EMAILSEARCH_OUTPUT_FORMAT` | `{env}_domain_prompt_tokens_v3_system` | `class="output_format"`, JSON schema + field guidelines |

**Critical:** profile document ID must match `agent_type="email_search"` passed to
`build_for_agent()` → `get_agent_profile(agent_type)` looks for doc with that exact ID.

### 5.2 Cognitive Process (Query Extraction)

The LLM extracts 3 orthogonal search vectors from the user's natural language query:

```groovy
EmailSearchAgent extends Agent {
  cognitive_process {
    steps: [
      "1. SUBJECT: core topic — event, document type, entity, or amount",
      "2. PRIMARY: what the indexer's fact would say (not 'email about X', but the fact itself)",
      "3. ALTERNATIVE: orthogonal angle — counterparty, amount, reference, outcome. Zero overlap with primary.",
      "4. TAGS: 3–5 short English category terms. Must not repeat words from primary or alternative.",
      "5. OUTPUT: { primary_query, alternative_query, tags }. ALL in ENGLISH. Nothing before { or after }."
    ]
  }
  output_format {
    contract: "RAW JSON only — first char {, last char }. No prose, no code block."
    json_schema: { required: ["primary_query", "alternative_query", "tags"] }
  }
}
```

**Context injected:** `build_for_agent(include_biographical=True)` injects biographical facts
(same as MemorySearchAgent). Additionally, the last 3 conversation turns are included as
message history so the agent can resolve pronouns and contextual references.

### 5.3 Example Extraction

```
Input: EMAIL_SEARCH_REQUEST "family and France information from the last two months"

Output:
{
  "primary_query": "family members visiting France location dates",
  "alternative_query": "Olena Nazar Lyuda Angers visit",
  "tags": ["Family Travel", "France Base", "Visits"]
}
```

The biographical context (facts about Olena, Nazar, Lyuda Marinova in Angers) allows the LLM
to expand the vague "family" reference into concrete person names, improving vector search recall.

---

## 6. Domain Models (`src/domain/email.py`)

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
    EmailSearchService.get_attachment() (markitdown attachment parsing on demand).
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
    valuable_type: str = "confirmed_event"     # "confirmed_event" | "biographical_signal"
    metadata: Dict[str, Any]                   # subject, from_address, snippet + structured entities

    # Email-specific
    subject: str                               # top-level for display
    from_address: str
    email_date: datetime                       # original email date
    attachments: List[str] = []               # attachment filenames

    # Lifecycle
    state: str = "current"
    indexed_at: datetime
    embedding_pending: bool = False            # True if vectors not computed yet; EmailEmbeddingRepairService picks these up
    consolidated_at: Optional[datetime] = None  # set when batch sent to ConsolidationAgent

@dataclass
class IndexingState:
    user_id: str
    provider: str
    indexed_through: Optional[datetime] = None          # None = incremental never completed
    oldest_indexed_through: Optional[datetime] = None   # None = backfill never completed
    cursor_reindex: Optional[datetime] = None           # None = reindex never completed

class IndexingJob(BaseModel):
    """One record per indexing run — used for resume, retry, and Cabinet history."""
    job_id: str
    user_id: str
    provider: str
    triggered_by: str              # "cabinet" | "scheduler" | "manual_script"
    status: str                    # "running"|"completed"|"failed"|"failed_auth"|"cancelled"
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

## 7. New Components

| File | Purpose |
|------|---------|
| `src/domain/email.py` | All email domain models (above) |
| `src/ports/oauth_credentials_port.py` | ABC: `get_credentials`, `save_credentials`, `revoke_credentials`, `is_connected` |
| `src/ports/email_provider_port.py` | ABC: `list_emails(credentials, date_from, page_token)`, `batch_get_full_content(credentials, email_ids)`, `refresh_token` |
| `src/ports/indexed_email_repository.py` | ABC: `save_batch`, `search_by_vector`, `get_indexing_state`, `update_indexing_state`, `count_by_user`, `delete_by_user` |
| `src/ports/email_exclusions_port.py` | ABC: `get_exclusions`, `add_exclusions`, `delete_exclusion`, `list_exclusions` |
| `src/ports/email_indexing_job_repository.py` | ABC: `create_job`, `update_job`, `get_job`, `get_latest_job`, `list_jobs`, `get_stale_running_jobs` |
| `src/adapters/firestore_oauth_credentials_adapter.py` | Firestore impl. Doc ID: `{user_id}_{provider}` |
| `src/adapters/gmail_provider_adapter.py` | `aiohttp` Gmail REST. Pagination via `pageToken`. Token refresh via `oauth2.googleapis.com/token`. `batch_get_full_content`: `format=full` → body + attachment filenames; `deep=True` → also attachment binaries. |
| `src/adapters/firestore_indexed_email_repo.py` | Batch writes (500/batch). 4-vector RRF search. Indexing state. Repair query (`embedding_pending=True`). Consolidation query (`consolidated_at IS NULL`). |
| `src/adapters/firestore_email_exclusions_adapter.py` | Exclusion patterns per user. Auto-populated during indexing. |
| `src/adapters/firestore_email_job_repo.py` | Job journal. Partial updates after each batch. Cabinet history + resume cursor. |
| `src/agents/email_classification_agent.py` | `EmailClassificationAgent(BaseAgent)`. Agentic Gemini Flash batch. `get_email_details` tool calls `batch_get_full_content(deep=False)` for ambiguous emails. Pydantic JSON output per email. |
| `src/services/email_indexing_service.py` | Full indexing pipeline. `GMAIL_DEFAULT_QUERY` constant. Per-chunk: exclusion pre-filter → classify → batch_get_full_content → embed → save. Advances `indexed_through` only on batch success. |
| `src/services/email_embedding_repair_service.py` | Query `embedding_pending=True` → re-embed → `update_vectors`. Called by Cloud Scheduler every 6h. |
| `src/agents/email_agent.py` | `EmailAgent(BaseAgent)`. `_handle_indexing()` (Flow 1 + 2). Multi-provider fan-out. Slack notification on completion. |
| `src/services/email_search_service.py` | `EmailSearchService`. Three methods: `vector_search()` (7-stream RRF, returns JSON), `get_details()` (Gmail full body ≤5000 chars), `get_attachment()` (markitdown conversion, 3 MB / 10 MB limits). Called by EmailSearchAgent — no infrastructure imports (all ports injected). |
| `src/agents/email_search_agent.py` | `EmailSearchAgent(BaseAgent)`. MemorySearchAgent-like: 1 ECO-tier LLM call extracts `{primary_query, alternative_query, tags}` from request → `EmailSearchService.vector_search()` → JSON result to delegating agent. intent=`search_emails`. Fully logged: 3 debug files per request. |
| `src/ports/notification_state_port.py` | `NotificationStatePort` ABC: `save(user_id, platform, channel_id)` + `get(user_id) -> Optional[NotificationChannel]` |
| `src/ports/notification_channel_factory_port.py` | `NotificationChannelFactoryPort` ABC: `create(platform, channel_id) -> Optional[ResponseChannel]` |
| `src/adapters/firestore_notification_state_adapter.py` | Persists last active channel per user. Collection `{env}_user_notification_state`, doc ID = `user_id`. Written on every user message. |
| `src/adapters/notification_channel_factory.py` | `NotificationChannelFactory(NotificationChannelFactoryPort)`. Late-binding: `set_slack_adapter()` + `set_telegram_adapter()` called after each platform adapter is instantiated in `main.py`. Only component that knows both adapters — correct hexagonal boundary. |
| `src/services/user_notification_service.py` | `UserNotificationService`: load channel → create `ResponseChannel` → route `AgentMessage` through `AgentCoordinator` (QuickAgent) → deliver formatted text to channel. `save_channel()` called best-effort from `ConversationHandler`. |

---

## 8. Modified Components

| File | Change |
|------|--------|
| `src/adapters/firebase_auth_adapter.py` | Add `additional_scopes: Optional[List[str]] = None` to `get_authorization_url()` — backward-compatible |
| `src/web/oauth_app.py` | Blueprint factory gains `oauth_credentials: OAuthCredentialsPort`. New endpoints: `/auth/connect-gmail`, `/auth/connect-gmail/callback`, `DELETE /auth/disconnect-gmail` |
| `src/web/user_cabinet_app.py` | Endpoints: `GET /api/gmail/status` (gains `indexing_active` bool + `active_job_id`), `POST /api/gmail/index` (async 202 → Cloud Tasks), `POST /api/gmail/jobs/<job_id>/cancel` (sets `status=cancelled`), `GET /api/gmail/jobs/<job_id>` (polling), `DELETE /api/gmail/disconnect`. Factory gains `email_job_repo` + `task_queue` params. |
| `src/handlers/conversation_handler.py` | Gains `notification_service: Optional[UserNotificationService]`. Calls `notification_service.save_channel()` on every incoming message (`asyncio.create_task`, best-effort). `_save_history_with_retry()`: 3-attempt retry with 0.5s/1.0s backoff for transient gRPC errors on `append_messages_batch`. |
| `src/composition/slack_adapter_factory.py` | `create_adapter()` gains `notification_service` param; passes to `ConversationHandler`. |
| `src/composition/telegram_adapter_factory.py` | Same as SlackAdapterFactory. |
| `src/services/email_indexing_service.py` | `completion_alert(job: IndexingJob) -> str` static method added. Three-cursor model: `_finalize_cursor` writes one cursor per mode (read-modify-write); `_advance_cursor` removed; incremental bootstrap from other cursors when cursor_max is null; tz normalization for Firestore-returned datetimes. |
| `main.py` | `/worker` route: `email_indexing` task type handler (Cloud Tasks chain). Status guard: `if job.status != "running": return 200 (skipped)` — prevents zombie chains on cancel/crash. `email_indexing_watchdog` task type: marks stale jobs as failed. Notification infrastructure wired: `FirestoreNotificationStateAdapter` + `NotificationChannelFactory` + `UserNotificationService`. `notification_channel_factory.set_slack_adapter()` / `set_telegram_adapter()` after each platform adapter. |
| `src/composition/service_container.py` | Wire all new services and adapters. `EmailSearchService` instantiated here; passed to `UserAgentFactory.agent_services()` dict. |
| `src/services/user_agent_factory.py` | `EmailSearchAgent` constructed per-user (like `WebSearchAgent`). `EmailSearchService` injected from `ServiceContainer`. |
| `src/services/agent_context_builder.py` | `"email_search"` strategy: ECO tier, `allowed_providers: ["gemini", "claude"]`. |
| `src/domain/user.py` | `_DEFAULT_AGENT_TIERS`: added `"email_search": PerformanceTier.ECO`. |
| `src/agents/core/quick_response_agent.py` | `QUICK_INTENTS` expanded: `{"search_memory", "search_web_light", "search_emails"}`. |
| `main.py` | Register `EmailAgent` (intents: `index_email` ASYNC) + `EmailSearchAgent` (intents: `search_emails` SYNC) |
| `config/firestore.indexes.json` | Add composite + vector indexes for `{env}_domain_email_facts_v1` and `{env}_email_indexing_jobs_v1`. Added watchdog index: `status ASC + updated_at ASC` on jobs collection. |
| `src/adapters/gmail_provider_adapter.py` | `list_emails()`: when `page_token` is provided, skip `q=` parameter entirely. Gmail resumes from token's embedded query (includes original `after:` filter). Passing `q` alongside `pageToken` caused Gmail to restart search without date filter — full inbox scans on every resume page. |
| `src/adapters/firestore_email_job_repo.py` | `get_stale_running_jobs(updated_before)`: Firestore query `status==running AND updated_at < threshold`. |
| `requirements.txt` | Add `google-auth>=2.0.0`, `google-auth-oauthlib>=1.0.0` |

---

## 9. Implementation Phases

### Phase 1 — OAuth + Credentials ✅ Complete (2026-02-28)

1. ✅ `src/domain/email.py` — domain models
2. ✅ `src/ports/oauth_credentials_port.py` + `src/ports/email_provider_port.py`
3. ✅ `src/adapters/firebase_auth_adapter.py` — `additional_scopes` param
4. ✅ `src/adapters/firestore_oauth_credentials_adapter.py`
5. ✅ `src/web/oauth_app.py` — `/auth/connect-gmail` + callback + disconnect
6. ✅ `src/web/user_cabinet_app.py` — `/api/gmail/status` + `/api/gmail/disconnect`
7. ✅ Tests: `tests/unit/ports/test_oauth_credentials_port.py`, `tests/unit/ports/test_email_provider_port.py`
8. ✅ `src/services/gmail_oauth_service.py` — token exchange + email fetch (`openid email` scopes)
9. ✅ `cloudbuild-dev.yaml` + `cloudbuild-prod.yaml` — `GMAIL_OAUTH_REDIRECT_URI_{DEV,PROD}` secrets

### Phase 2 — Indexing Pipeline ✅ Complete (2026-02-28)

10. ✅ `src/adapters/gmail_provider_adapter.py` — metadata + full content fetch (`deep` flag)
11. ✅ `src/ports/indexed_email_repository.py` — composite doc ID `{user_id}_{email_id}` for global uniqueness
12. ✅ `src/ports/email_classifier_port.py` — new port (hexagonal compliance, `EmailClassificationAgent` implements it)
13. ✅ `src/ports/email_exclusions_port.py` + `src/ports/email_indexing_job_repository.py`
14. ✅ `src/adapters/firestore_indexed_email_repo.py` + `src/adapters/firestore_email_exclusions_adapter.py` + `src/adapters/firestore_email_job_repo.py`
15. ✅ `firestore.indexes.json` — vector + composite indexes for `{env}_domain_email_facts_v1` and `{env}_email_indexing_jobs_v1`
16. ✅ `src/agents/email_classification_agent.py` — agentic batch classifier with `get_email_details` tool; implements `EmailClassifierPort`; uses `AgentExecutionContext`; TEST A/TEST B biographical_signal support
17. ✅ `src/services/email_indexing_service.py` — pipeline orchestration; default `date_from = now - 3yr`; `page_size=300` (default, max 500); `start_job()` convenience method
18. ✅ `src/services/email_embedding_repair_service.py` — repair job
19. ✅ Tests: ports contracts + `test_email_indexing_service.py`

### Phase 3 — Agent + Integration ✅ Complete (2026-03-01)

20. ✅ `src/web/user_cabinet_app.py` — `/api/gmail/index` (Re-index button, max_pages cap via `GMAIL_INDEX_MAX_PAGES` env)
21. ✅ `main.py` — `EmailIndexingService` wired; Gmail section complete
22. ✅ `src/services/email_search_service.py` — `vector_search()` (7-stream RRF, JSON output) + `get_details()` + `get_attachment()` (markitdown, size guards)
23. ✅ `src/agents/email_search_agent.py` — MemorySearchAgent-like: ECO LLM extracts `{primary_query, alternative_query, tags}` → `EmailSearchService.vector_search()` → JSON to delegating agent
24. ✅ `src/composition/service_container.py` + `src/services/user_agent_factory.py` — `EmailSearchService` wired; `EmailSearchAgent` constructed per-user
25. ✅ `main.py` — `EmailSearchAgent` registered (intent: `search_emails` SYNC)
26. ✅ `src/agents/core/quick_response_agent.py` — `QUICK_INTENTS` includes `search_emails`
27. ✅ `src/services/agent_context_builder.py` + `src/domain/user.py` — `email_search` strategy (ECO, gemini+claude)
28. ✅ Firestore prompt tokens: `EMAILSEARCH_PROPERTIES`, `EMAILSEARCH_COGNITIVE_PROCESS`, `EMAILSEARCH_OUTPUT_FORMAT` + blueprint `emailsearch_agent_v1` + profile `email_search`
29. ✅ Firestore vector indexes: all 4 vector fields on `development_domain_email_facts_v1` created via gcloud CLI
60. ✅ `src/ports/indexed_email_repository.py` — `find_nearest` gains `date_from: Optional[datetime]` and `date_to: Optional[datetime]` parameters. Pre-filter on `email_date` field applied when present.
61. ✅ `src/adapters/firestore_indexed_email_repo.py` — implements date pre-filter: `FieldFilter("email_date", ">=", date_from)` / `FieldFilter("email_date", "<=", date_to)` chained before `find_nearest()`.
62. ✅ `src/services/email_search_service.py` — `vector_search()` accepts `date_from`/`date_to`, passes them via `**date_kwargs` to both `find_nearest` calls.
63. ✅ `src/agents/email_search_agent.py` — `_parse_date()` converts YYYY-MM-DD string from LLM output to `datetime`; result passed to `vector_search`.
64. ✅ `firestore_utils/uploads/EMAILSEARCH_COGNITIVE_PROCESS.groovy` — step 5 "DATE RANGE" added: LLM extracts `date_from`/`date_to` from time signals; uses `current_date_time` for relative expressions; null if no signal.
65. ✅ `firestore_utils/uploads/EMAILSEARCH_OUTPUT_FORMAT.groovy` — `date_from` and `date_to` added as nullable string fields with `YYYY-MM-DD` pattern to JSON schema.
66. ✅ `config/firestore.indexes.json` — 8 new vector indexes (4 vector fields × 2 collections) with `email_date` pre-filter field: `user_id + state + email_date + {vector_field}`.
67. ✅ `src/adapters/firestore_indexed_email_repo.py` — `_MAX_COSINE_DISTANCE = 0.4` (cosine similarity ≥ 0.6 required); results beyond threshold discarded server-side by Firestore before RRF scoring.
68. ✅ `src/services/email_search_service.py` — `output_limit` cap removed; Firestore cosine distance threshold 0.4 (similarity ≥ 0.6) is the only gate; logs `A={n} B={n} → merged={n}` after RRF merge.
30. ✅ Debug logging: 3 files per request (`email_search_prompt_*`, `email_search_response_*`, `email_search_to_smart_response_*`)
31. ✅ Tests: `tests/unit/agents/test_email_search_agent.py` (31 tests), `tests/unit/services/test_email_search_service.py` (23 tests)
32. ✅ Completion notification — `UserNotificationService` (see Phase 5)

### Phase 5 — Async Cloud Tasks Pipeline + Notifications ✅ Complete (2026-03-01)

The indexing pipeline runs as a Cloud Tasks chain to avoid Cloud Run CPU throttling:
one HTTP request per Gmail page, each enqueuing the next until `next_page_token` is exhausted.
`UserNotificationService` delivers completion messages via the user's last active channel (Slack or Telegram),
routing through QuickAgent so the message is formatted in the user's communication style.

**Why Cloud Tasks chain and not a single long-running request:**
Cloud Run throttles CPU to ~5% when no HTTP requests are in-flight. `asyncio.create_task()` for
background work suffers this starvation — Firestore grpc.aio calls take 74–180s instead of ~700ms.
Each Cloud Tasks delivery is its own HTTP request with full CPU allocation.

**Why route through QuickAgent:**
The completion alert is a fact string; the formatting decision (tone, language, structure) belongs
to the LLM with user context loaded, not to the infrastructure layer.

38. ✅ `main.py` — `/worker` handler: `email_indexing` task type
    - `get_job(job_id)` + `get_credentials(user_id, "gmail")` → `run_indexing_job(max_pages=1)`
    - if `next_page_token` → `enqueue_email_indexing_task(job_id)` (chain continues)
    - if done → `notification_service.notify(user_id, account_id, completion_alert(job))`
39. ✅ `src/services/email_indexing_service.py` — `completion_alert(job: IndexingJob) -> str` (`@staticmethod`)
    - Returns: `"Email indexing complete: N emails indexed[, M failed]."` — fact only, no formatting
    - Owned by the service that does the work — not by the worker or notification layer
40. ✅ `src/ports/notification_state_port.py` — `NotificationStatePort` ABC: `save(user_id, platform, channel_id)` + `get(user_id) -> Optional[NotificationChannel]`
41. ✅ `src/ports/notification_channel_factory_port.py` — `NotificationChannelFactoryPort` ABC: `create(platform, channel_id) -> Optional[ResponseChannel]`
42. ✅ `src/adapters/firestore_notification_state_adapter.py` — persists last active channel per user. Collection: `{env}_user_notification_state`, doc ID = `user_id`
43. ✅ `src/adapters/notification_channel_factory.py` — `NotificationChannelFactory`: late-binding `set_slack_adapter()` / `set_telegram_adapter()`. Only component that knows both platform adapters — correct boundary.
44. ✅ `src/services/user_notification_service.py` — `notify(user_id, account_id, system_alert)`:
    - Load last channel from `NotificationStatePort`
    - Create `ResponseChannel` via `NotificationChannelFactoryPort`
    - Build `AgentMessage` with `current_message_parts=[MessagePart(text=f"[System: {alert} Your response to this message will be read by the user. Inform them of the event details in your usual manner of communication.]")]`
    - Route to `quick_response_agent_{user_id}` via `AgentCoordinator`
    - Deliver result via `ResponseChannel`
45. ✅ `src/handlers/conversation_handler.py` — `save_channel()` called on every incoming message (best-effort `asyncio.create_task`)
46. ✅ `src/composition/slack_adapter_factory.py` + `src/composition/telegram_adapter_factory.py` — `notification_service` param passed to `ConversationHandler`
47. ✅ `src/web/user_cabinet_app.py` — `POST /api/gmail/index` async path: create job → `job_repo.create_job(job)` → `task_queue.enqueue_email_indexing_task(job_id)` → 202 `{"job_id", "status": "running"}`; `GET /api/gmail/jobs/<job_id>` for status polling; `GET /api/gmail/status` gains `indexing_active` boolean
48. ✅ `src/web/static/cabinet.html` — `setInterval(loadGmailStatus, 5000)` for live status; button disabled with "Indexing…" text when `indexing_active=true`; 202 handler shows toast + reloads status

### Phase 6 — Graceful Degradation ✅ Complete (2026-03-01)

**Problem:** A running indexing chain (Cloud Tasks) had no graceful stop mechanism. Changing job status
in Firestore had no effect — the worker didn't check it. Stuck jobs remained `running` forever
after crashes or manual stops. The only options were purging the Cloud Tasks queue or deleting the service.

**Root causes fixed:**
1. Worker did not check job status before executing → zombie chains on cancel/crash/deploy
2. `list_emails()` passed `q=` alongside `pageToken` → Gmail restarted search without `after:` → full inbox scans on resume pages

49. ✅ `src/adapters/gmail_provider_adapter.py` — `list_emails()`: skip `q=` when `page_token` provided.
    Gmail's `pageToken` carries the original query including `after:` filter. Passing `q` alongside it
    caused Gmail to restart the search from scratch without date constraints on every resume page.
50. ✅ `main.py` — Worker status guard: `if job.status != "running": return jsonify({"status": "skipped"}), 200`.
    Cloud Tasks marks a 200 response as success (no retry). Chain stops within 1 task (≤60s).
51. ✅ `src/web/user_cabinet_app.py` — `POST /api/gmail/jobs/<job_id>/cancel`:
    - Validates ownership (`job.user_id == g.user_id`)
    - Validates state (`status == "running"` → else 409)
    - Writes `status=cancelled, updated_at=now`
    - Returns 200 `{"status": "cancelled"}`
52. ✅ `GET /api/gmail/status` — gains `active_job_id: Optional[str]` and `oldest_indexed_through: Optional[str]` in response.
    `active_job_id` is needed by Cabinet to call the cancel endpoint.
    `oldest_indexed_through` is the earliest date successfully indexed (set once on first full backfill completion).
53. ✅ `src/web/static/cabinet.html` — Cancel button: visible only when `indexing_active=true && active_job_id`.
    Calls cancel endpoint → `loadGmailStatus()` on success. Button label "Cancelling…" while in-flight.
    Coverage period display: when both `oldest_indexed_through` and `indexed_through` are present, shows
    `Coverage: 11 Feb 2020 – 1 Mar 2026` (full date, en-GB locale). If only `indexed_through` is set, falls
    back to `Indexed through: DATE`. If neither — "Not indexed yet."
54. ✅ `src/ports/email_indexing_job_repository.py` — `get_stale_running_jobs(updated_before: datetime)` added.
55. ✅ `src/adapters/firestore_email_job_repo.py` — implements `get_stale_running_jobs`:
    query `status==running AND updated_at < threshold`. Requires composite index.
56. ✅ `main.py` — `task_type=email_indexing_watchdog` handler:
    - Calls `get_stale_running_jobs(now - 2h)`
    - Marks each stale job `status=failed, updated_at=now`
    - Returns `{"status": "ok", "marked_failed": N}`
    - Triggered by Cloud Scheduler every 30 min (see item 57)
57. ✅ `scripts/infrastructure/setup-email-watchdog-scheduler.sh` — creates Cloud Scheduler job
    `alek-email-watchdog-{env}` (30 min schedule, OIDC auth, POST to `/worker`).
    One-time setup per environment.
58. ✅ `config/firestore.indexes.json` — composite index `status ASC + updated_at ASC` on
    `{env}_email_indexing_jobs_v1` for the watchdog query.
59. ✅ `src/web/user_cabinet_app.py` — enqueue-before-save order:
    `enqueue_email_indexing_task(job_id)` runs BEFORE `create_job(job)`.
    If enqueue fails → job is never written → no orphaned "running" job in Firestore.
    Edge case: Cloud Tasks may call `/worker` before Firestore write completes (~ms window) →
    worker returns 404 → Cloud Tasks retries → by retry, job is saved. Acceptable.

**Domain model update:** `IndexingJob.status` now accepts `"cancelled"` as a valid terminal state.
Cabinet treats `cancelled` identically to idle — shows "Index new emails" button.

### Phase 4 — Live Email Access Intents ✅ Complete (2026-03-01)

33. ✅ `src/agents/email_search_agent.py` — two new intents wired to existing `EmailSearchService` methods:
    - `get_email_details` — routes on `payload.email_id` (no LLM, direct Gmail API)
    - `get_email_attachment` — routes on `payload.email_id + payload.filename` (no LLM, markitdown)
    - `execute()` dispatches on payload keys; `can_handle()` accepts `email_id` or `query`
34. ✅ `main.py` — `EmailSearchAgent` manifest updated: `{"search_emails", "get_email_details", "get_email_attachment"}` SYNC
35. ✅ `src/agents/core/quick_response_agent.py` — `QUICK_INTENTS` updated (all 3 email intents); `_INTENT_REMAP = {"search_web": "search_web_light"}` (Quick downgrades web search internally — LLM sees single `search_web` intent)
36. ✅ `firestore_utils/uploads/PROTOCOL_AGENT_SELECTION.groovy` — unified protocol for Smart and Quick:
    - `search_memory`, `search_web` (single intent name), `email_search_agent.intents { search_emails, get_email_details, get_email_attachment }`
    - Replaces separate `PROTOCOL_QUICK_AGENT_SELECTION` and `PROTOCOL_SMART_AGENT_SELECTION` tokens
37. ✅ Tests: `test_email_search_agent.py` extended with `TestGetEmailDetails` (5 tests), `TestGetEmailAttachment` (6 tests), `TestCanHandleEmailId` (3 tests)

### Phase 7 — Cursor Model + Reliability Fixes ✅ Complete (2026-03-01)

**Three-cursor model:** replaced the single `indexed_through` cursor with three independent,
mode-exclusive cursors. Each mode writes exactly one cursor at job completion only (no per-batch writes).

69. ✅ `src/domain/email.py` — `IndexingState`: added `oldest_indexed_through` and `cursor_reindex`
    fields (both `Optional[datetime] = None`). All three cursors default to None.
70. ✅ `src/adapters/firestore_indexed_email_repo.py` — reads/writes all three cursor fields;
    `get_indexing_state` reads `cursor_reindex` from Firestore; `update_indexing_state` writes it.
71. ✅ `src/services/email_indexing_service.py` — `_finalize_cursor(mode, date_from)` rewritten:
    - Reads current Firestore state first (read-modify-write)
    - `backfill` branch: writes `oldest_indexed_through = date_from`, preserves others unchanged
    - `reindex` branch: writes `cursor_reindex = date_from`, preserves others unchanged
    - `incremental` branch: writes `indexed_through = max(existing, job.max_email_date)`, preserves others unchanged
    - Removed `_advance_cursor` (was writing `oldest_indexed_through` per-batch; incompatible with Cloud Tasks chain where older pages could overwrite newer ones)
72. ✅ `src/services/email_indexing_service.py` — incremental bootstrap:
    if `indexed_through` is null → `date_from = max(oldest_indexed_through, cursor_reindex)`;
    all null → today-only run. No pre-write to Firestore before job execution.
73. ✅ `src/services/email_indexing_service.py` — timezone normalization at job load time:
    Firestore returns `DatetimeWithNanoseconds` (tz-aware) for `max_email_date`/`min_email_date`
    on Cloud Tasks re-invocation. Fix: `replace(tzinfo=None)` when `tzinfo is not None` before
    entering the main batch loop. Prevents `TypeError` on timezone-aware vs naive comparison.
74. ✅ `src/services/email_indexing_service.py` — reindex mode no longer calls `clear_indexing_state`
    (was wiping `indexed_through`; reindex must never touch the incremental cursor).

**ConversationHandler gRPC retry:**

75. ✅ `src/handlers/conversation_handler.py` — `_save_history_with_retry()` replaces bare
    `append_messages_batch()` call. 3 attempts with 0.5s/1.0s exponential backoff for transient
    gRPC errors (`RST_STREAM`, `UNAVAILABLE`, `INTERNAL`). Non-transient errors and all final
    failures propagate normally to the outer `except` handler.
    Root cause: Firestore gRPC connection resets after long agent runs (~27k tokens); the grpc.aio
    channel expires during the response, causing `RST_STREAM error code 2` on the first Firestore
    write post-agent. Retry resolves it within 1–2 attempts in practice.

---

## 10. Cost Analysis

### 10.1 One-Time Indexing

**Empirically confirmed rate:** 14.6% (73/500 in first production run, Feb 2026).
**Assumption for projection:** 10,000 emails × ~15% = ~1,500 indexed.

| Component | Quantity | Unit Cost | Total |
|-----------|----------|-----------|-------|
| Gmail API — metadata list (format=metadata) | ~34 pages × 300 | $0 (free) | $0 |
| LLM classification (Gemini Flash) | ~34 batches × 300 | $0.003/batch | $0.10 |
| Embeddings (tags + metadata vectors) | 1,500 × 2 | $0.00001 each | $0.03 |
| Firestore writes | 1,500 docs | $0.000018/write | $0.03 |
| **TOTAL (one-time)** | | | **~$0.16** |

Previous RFC estimated $0.58 (indexed everything). Filtering + larger batches (fewer LLM calls) reduces cost ~3.6x.

### 10.2 Incremental Updates (Daily)

**Assumptions:** 20 new emails/day, 15% pass filter = 3 indexed/day.

| Component | Monthly Cost |
|-----------|-------------|
| Gmail API | $0 |
| LLM classification | $0.02 |
| Embeddings | <$0.01 |
| Firestore writes | <$0.01 |
| **TOTAL (monthly)** | **~$0.03** |

### 10.3 Search Cost (per query)

| Component | Cost |
|-----------|------|
| Firestore vector search | $0.00006 |
| Embedding (query) | $0.00001 |
| Gmail batch fetch (50 emails, `format=full`) | Gmail free tier |
| LLM fact extraction (Gemini Flash or Claude) | ~$0.001 |
| **TOTAL** | **~$0.001** |

---

## 11. Performance

### 11.1 Indexing (10K emails)

| Stage | Time |
|-------|------|
| Gmail API metadata fetch (200 pages) | ~20s |
| LLM classification (200 batches, parallelized) | ~30s |
| Embeddings + Firestore writes (1,500 docs) | ~10s |
| **Total** | **~60s** |

### 10.2 Search Latency (Empirically Measured, 2026-03-01)

| Stage | Time |
|-------|------|
| LLM query extraction (ECO, Gemini Flash Lite) | ~0.5s |
| 3 embedding calls (parallel) | ~0.3s |
| 2 Firestore find_nearest calls (parallel, 7 streams total) | ~0.7–1.2s |
| RRF merge + JSON serialization | <5ms |
| **Total (p50, vector search only)** | **~4–6s** |

End-to-end measured: 4659ms and 5916ms in first two production runs (dev environment).
The bulk of latency is LLM key extraction (~0.5s) + Firestore vector queries (~1s) + overall agent overhead.

**Live Gmail access available on demand.** Vector search (`search_emails`) returns indexed facts only.
Full email body and attachment text are fetched live via `get_email_details` and `get_email_attachment`
intents — both available to Smart and Quick agents as of Phase 4.

---

## 12. Security & Privacy

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

## 13. User Experience

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
│  Coverage: 11 Nov 2023 – 28 Feb 2026             │
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
- `running` → "🔄 Indexing..." (disabled) + **"Cancel"** (red, calls `POST /api/gmail/jobs/<id>/cancel`)
- `cancelled` → "Index new emails" (treated as idle; user-initiated stop)
- `failed` → "Retry indexing" (re-enqueues same job)

**Endpoint:** `POST /api/gmail/index` — no body required. Server computes `date_from`.
Returns HTTP 202 `{job_id, status: "running"}`. Cabinet shows toast "Indexing started in background.
You'll be notified when done." and immediately polls `GET /api/gmail/status`. Cabinet polls every
5 seconds via `setInterval(loadGmailStatus, 5000)` — button stays disabled while `indexing_active=true`.
No websocket needed; the Slack/Telegram notification is the authoritative completion signal for the user.

### 12.4 Indexing Completion Notification

Delivery path:
```
EmailIndexingService.completion_alert(job) → str
  ↓ "Email indexing complete: N emails indexed[, M failed]."   ← fact only, no formatting
UserNotificationService.notify(user_id, account_id, alert)
  ↓ load last active channel (Slack/Telegram) from NotificationStatePort
  ↓ build AgentMessage: [System: {alert} Your response to this message will be read by the user.
                          Inform them of the event details in your usual manner of communication.]
  ↓ route to quick_response_agent_{user_id} via AgentCoordinator
  ↓ QuickAgent formats in user's communication style (tone, language, emojis — LLM decision)
  ↓ deliver via ResponseChannel (Slack or Telegram)
```

**Design rationale:**
- `completion_alert()` lives on `EmailIndexingService` — the service that does the work owns the summary
- Framing in `UserNotificationService` carries fact + WHAT to do, not HOW to say it — tone is QuickAgent's decision based on user profile
- Delivery channel = last active platform (saved on every user message); if user switches from Slack to Telegram, next notification goes to Telegram automatically
- Channel not yet known (no prior messages) → notification silently skipped; user sees job result in Cabinet on next visit

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

### 13.6 Query Flow (Slack) — Current Implementation

```
User: "find my test results for 2025"

Router → SmartAgent
SmartAgent → delegate_to_specialist(intent="search_emails", query="...")
  → EmailSearchAgent: ECO LLM extracts keys (~0.5s)
       primary_query:   "medical test results lab report 2025"
       alternative_query: "Synevo GFR HbA1c kidney analysis"
       tags: ["healthcare", "medical", "lab results"]
  → EmailSearchService.vector_search() 7-stream RRF (~1.5s)
  → returns JSON { "count": 5, "emails": [...fact sentences + email_ids...] }
SmartAgent synthesizes response from JSON (~1s)

Bot: (~4–6s total)
"Found 5 medical emails from 2025:
  📋 March 28 — GFR (CKD-EPI) >90 mL/min (Normal). HbA1c 5.1%.
     📎 lab_report_march.pdf
  📋 January 15 — Blood panel: Uric acid elevated (Hyperuricemia confirmed).
  ..."

User: "покажи детали мартовского анализа"

SmartAgent → delegate_to_specialist(intent="get_email_details", email_id="msg_mar28")
  → EmailSearchAgent._handle_get_details()
  → GmailProviderAdapter.batch_get_full_content([email_id], deep=False)
  → returns subject, from, date, body_text[:5000], attachment filenames

Bot: "Март 28 — Синево. ..." (full body text formatted by SmartAgent)
```

**Production example** (2026-03-01, ~5.9s, query: "family and France last two months"):
```json
{
  "primary_query": "family members visiting France location dates",
  "alternative_query": "Olena Nazar Lyuda Angers visit",
  "tags": ["Family Travel", "France Base", "Visits"]
}
→ 10 emails returned:
  Ryanair VLC→NTE (for Nantes/Angers trip, May 2026),
  cerfa exit permit for Nazar, Location Sharing with Olena+Nazar,
  school notification from Ensemble Scolaire Saint-Benoît (Angers)
```

---

## 14. Future Enhancements

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

## 15. Alternatives Considered

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

## 16. Open Questions

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
- ✅ `OAuthCredentialsPort` + `FirestoreOAuthCredentialsAdapter`
- ✅ `EmailProviderPort` + `GmailProviderAdapter` (metadata + full content, `query` + `deep` params)
- ✅ `IndexedEmailRepository` + `FirestoreIndexedEmailRepository`
- ✅ `EmailExclusionsPort` + `FirestoreEmailExclusionsAdapter`
- ✅ `EmailIndexingJobRepository` + `FirestoreEmailJobRepository`
- ✅ `EmailClassificationAgent` (agentic LLM batch with `get_email_details` tool — implemented as agent, not service)
- ✅ `EmailIndexingService` (pipeline orchestration, `GMAIL_DEFAULT_QUERY` default filter)
- ✅ `EmailEmbeddingRepairService` (repair job skeleton)
- ✅ `{env}_domain_email_facts_v1` Firestore collection (created + validated)
- ✅ `{env}_oauth_credentials` Firestore collection
- ✅ `{env}_email_indexing_jobs_v1` Firestore collection (job journal, validated)
- ✅ `{env}_email_indexing_state` Firestore collection (cursor tracking, validated)
- ✅ `{env}_domain_email_facts_v1` vector + composite Firestore indexes (`config/firestore.indexes.json` — 4 vector indexes + composite indexes for unconsolidated batch + job queries)
- ❌ `EmailAgent` (async indexing, multi-provider)
- ❌ `EmailSearchAgent` (Mode A index search + Mode B deep search)
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

### Блок 2 — Адаптеры + индексы Firestore ✅

- ✅ `src/adapters/gmail_provider_adapter.py` — aiohttp Gmail REST; metadata + full content (`deep` flag); token refresh; `query` + `date_from` объединяются в `q=` параметр
- ✅ `src/adapters/firestore_oauth_credentials_adapter.py` — upsert/get/delete; doc ID: `{user_id}_{provider}`
- ✅ `src/adapters/firestore_indexed_email_repo.py` — save_batch (500/batch); 4-vector RRF search; consolidation query; repair query; cursor tracking
- ✅ `src/adapters/firestore_email_exclusions_adapter.py` — exclusion patterns per user
- ✅ `src/adapters/firestore_email_job_repo.py` — job journal; partial updates; resume cursor
- ✅ `config/firestore.indexes.json` — 4 vector indexes (`vector`, `tags_vector`, `metadata_vector`, `attachments_vector`) + composite indexes для `get_unconsolidated_batch` и job queries; оба коллекции dev + prod

### Блок 3 — Сервисы pipeline ✅

- ✅ `src/agents/email_classification_agent.py` — **реализован как агент, не сервис** (требует LLM + tool calling); agentic Gemini Flash; `get_email_details` tool; `AgentExecutionContext`; TEST A/TEST B (confirmed_event + biographical_signal); per-chunk 300 emails (default, configurable)
- ✅ `src/services/email_indexing_service.py` — per-chunk loop; `GMAIL_DEFAULT_QUERY` дефолтный фильтр; `page_size=300` (default, max 500); resume от indexed_through; batch_get_full_content parallel (semaphore=10); advances cursor only on success
- ✅ `src/services/email_embedding_repair_service.py` — query embedding_pending=True → re-embed → update_vectors
- ✅ `tests/unit/services/test_email_indexing_service.py`
- ✅ `tests/unit/agents/test_email_classification_agent.py` — 13 тестов: classify_batch (happy path, missing emails, invalid JSON + retry, LLM error, empty input, request fields, tags normalization), tool calling path, MAX_TURNS, can_handle, execute, prompt_builder guard

### Блок 4 — Скрипт + первый production прогон ✅

- ✅ `scripts/email/run_indexing.py` — ручной wireset; `--after`, `--max-pages`, `--no-filter`, `--resume-token` флаги; GMAIL_DEFAULT_QUERY явно пробрасывается
- ✅ Первый прогон: классификация работает; коллекции созданы в `us-production`; `development_domain_email_facts_v1`, `development_email_indexing_jobs_v1`, `development_email_indexing_state` — все документы корректны

### Блок 5 — Web + Cabinet ← ТЕКУЩИЙ

- [ ] `src/adapters/firebase_auth_adapter.py` — `additional_scopes` param (backward-compatible)
- [ ] `src/web/oauth_app.py` — `/auth/connect-gmail` (incremental OAuth, gmail.readonly layered on existing session) + callback + `DELETE /auth/disconnect-gmail`
- [ ] `src/web/user_cabinet_app.py` — `/api/gmail/status` + `/api/gmail/index` + `/api/gmail/disconnect`
- [ ] `requirements.txt` — `google-auth>=2.0.0`, `google-auth-oauthlib>=1.0.0`

### Блок 6 — ServiceContainer + EmailAgent + graceful degradation

- [ ] `src/composition/service_container.py` — wire all email components (see §2.1.4)
- [ ] `src/agents/email_agent.py` — `_handle_indexing()` (Flow 1 + 2); multi-provider fan-out; Slack completion notification
- [ ] `main.py` — register EmailAgent (intent: index_email ASYNC)
- [ ] Graceful degradation: LLM-interpreted success/error notifications in chat (same pattern as ConversationHandler router errors)
- [ ] `tests/unit/agents/test_email_agent.py`

### Блок 7 — EmailSearchAgent

- [ ] `src/agents/email_search_agent.py` — Mode A (vector RRF, ~0.5s) + Mode B (markitdown + deep=True, ~3–5s)
- [ ] Wire to SmartAgent via `search_email` tool + `main.py` registration (intent: search_email SYNC)
- [ ] `tests/unit/agents/test_email_search_agent.py`
- [ ] Validate: тестовый запрос из Slack "покажи мои рейсы" → results from indexed email facts

### Блок 8 — ConsolidationAgent hook

- [ ] Расширить ConsolidationAgent: после обычного батча → `get_unconsolidated_batch(user_id, limit=200)` → email тридж → `mark_consolidated`
- [ ] Обогащённый кандидат: `email_id + attachments + metadata.subject/from` в system_alert prompt
- [ ] Добавить тег `email` в инструкцию промпта консолидатора
- [ ] Тест на результате полной индексации из Блока 6

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

### 2026-03-02 — QuickAgent delegation parity, router threshold, email 0-results fix

**1. QuickResponseAgent — delegation context parity with SmartResponseAgent**

Root cause of wrong dispatch in EmailSearchAgent: Quick's `delegate_to_specialist` tool schema
had no `context` field, so the LLM could not pass structured parameters (e.g. `email_id`,
`filename`). The coordinator's `params` mechanism (`extra_payload = context.get("params", {})`)
existed but was never populated by Quick, causing EmailSearchAgent to always fall through to
`_handle_search_emails` instead of `_handle_get_details` / `_handle_get_attachment`.

Three changes in `src/agents/core/quick_response_agent.py`:
- `delegate_to_specialist` schema: added `context: object` optional field with description
  "Optional extra parameters for the specialist agent".
- `_delegate_quick`: extract `context_params = args.get("context", {})` and forward as
  `"params": context_params` in `delegation_context`.
- `_execute_quick_parallel`: pass `memory_context` to parallel `other_calls` so specialist
  agents receive memory context regardless of call order.

**2. RouterAgent — routing condition simplified**

`needs_memory_search` signal removed from routing decision. Quick handles memory search
internally via its delegation loop (`search_memory` intent), so the signal was redundant
and was routing an unnecessary share of queries to Smart.

Threshold raised from `complexity_score > 5` to `complexity_score > 6`, routing more
routine queries to the cheaper Quick tier.

```python
# Before:
if routing_metadata.needs_memory_search or routing_metadata.complexity_score > 5:
    return self.smart_agent_id
# After:
if routing_metadata.complexity_score > 6:
    return self.smart_agent_id
```

**3. EmailSearchService — 0-results response**

`vector_search()` returned `{"count": 0, "emails": []}` (26 chars) when no results were found.
The calling LLM (Quick or Smart) received this ambiguous JSON and produced an empty `full_response`.

Fix in `src/services/email_search_service.py`:
```python
# Before:
return json.dumps({"count": 0, "emails": []}, ensure_ascii=False)
# After:
return "No emails found matching your query."
```

The plain string gives the LLM explicit content to synthesize a user-facing message from.

---

### 2026-03-01 (session 2) — Classifier output truncation, DatetimeWithNanoseconds, GCS debug logging, smart completion message

**1. JSON truncation in `classify_batch` — root cause and fix**

Root cause: `enable_reasoning=True` + `max_tokens=32000` on Gemini Flash (which resolves to
Gemini 3 Flash via `gemini-flash-latest`). Gemini 3 Flash uses a combined token budget for
thinking + text output. With `thinking_budget=-1` (unlimited), the model allocated ~30K tokens
to internal reasoning, leaving only ~1.3K for text → JSON array truncated mid-item.

Token trace confirming the root cause:
```
in=62407 (input) + thinking=30719 + out=1277 (text) = total=94403
max_tokens=32000 → 32000 - 30719 = 1281 left for text → truncation
```

Fixes applied:
- `max_tokens=32000` → `max_tokens=65535` in `email_classification_agent.py`.
  Gemini 3 Flash max output: 65,536 tokens. After fix: 30K thinking + 35K text = 65K fits.
- `enable_reasoning=True` restored (was incorrectly removed in a draft — reasoning is intentional
  for classification quality).

**2. Gemini 3 Flash — `thinking_level` API**

`gemini-flash-latest` now resolves to Gemini 3 Flash, which uses `thinking_level`
(MINIMAL/LOW/MEDIUM/HIGH) instead of the numeric `thinking_budget` used in Gemini 2.5 Flash.
The two parameters cannot be combined in the same request.

Change in `src/adapters/gemini_adapter.py`:
```python
# Before (Gemini 2.5 Flash):
thinking_config=types.ThinkingConfig(thinking_budget=-1) if enable_reasoning else None

# After (model-aware):
thinking_config=(
    types.ThinkingConfig(thinking_level=types.ThinkingLevel.HIGH)
    if enable_reasoning and model_name == "gemini-flash-latest"
    else types.ThinkingConfig(thinking_budget=-1)
    if enable_reasoning
    else None
)
```

Only `gemini-flash-latest` uses the new `thinking_level=HIGH` API. Any other model with
`enable_reasoning=True` falls back to `thinking_budget=-1` (legacy Gemini 2.5 behavior).
Currently only `EmailClassificationAgent` sets `enable_reasoning=True` (BALANCED tier = Flash).

**3. `DatetimeWithNanoseconds._nanosecond` AttributeError — fix**

Root cause: `max_email_date` / `min_email_date` are read from Firestore as
`DatetimeWithNanoseconds` (a `datetime` subclass). When written back via `update_job()`,
the Firestore SDK calls `.timestamp_pb()` → accesses `._nanosecond` → AttributeError
(attribute missing in the SDK version installed in Cloud Run).

Fix in `src/adapters/firestore_email_job_repo.py` — `update_job` normalizes before write:
```python
sanitized = {
    k: datetime(v.year, v.month, v.day, v.hour, v.minute, v.second, v.microsecond, v.tzinfo)
    if isinstance(v, datetime) and type(v) is not datetime else v
    for k, v in updates.items()
}
await self.collection.document(job_id).update(sanitized)
```

Only activates for datetime subclasses; plain `datetime` (e.g., `datetime.utcnow()`) passes through unchanged.

**4. GCS debug logging for LLM prompts/responses**

`src/utils/debug_logger.py` extended with GCS backend:
- When `DEBUG_PROMPTS_BUCKET` env var is set → writes to GCS (Cloud Run mode)
- When not set → existing local filesystem behavior (unchanged)
- GCS client lazy-initialized, failures are non-fatal (warning only)

**GCS path structure:** `gs://{bucket}/{agent_name}/YYYY-MM-DD/{type}_{timestamp}.{ext}`

Example:
```
email_classification/2026-03-01/prompt_20260301_143022.txt
email_classification/2026-03-01/response_20260301_143045.txt
consolidation_v3/2026-03-01/prompt_20260301_150001.txt
router_agent/2026-03-01/response_20260301_150010.txt
```

Root level = stable set of agent names (~8). Date folders are inside each agent directory.
`{type}`: `prompt`, `response` → `.txt`; `tools`, `summary` → `.json`.

**Agents that write debug logs:** `router_agent`, `quick_response_agent`, `smart_response_agent`,
`memory_search_agent`, `consolidation_v2`, `consolidation_v3`, `email_search`, `email_classification`.

**`email_classification` logging (added in this session):**
- `log_prompt` — once before the tool-calling loop; captures `emails_json` + full `system_instruction`
- `log_response` — on the final turn (when `tool_calls` is empty); captures raw JSON output + token counts

**Bug fixed:** `log_tool_calls` and `log_consolidation_summary` previously only wrote to local
filesystem even when `DEBUG_PROMPTS_BUCKET` was set. Fixed to use the same `{agent}/date/type.json`
GCS path. Both methods are currently not called by any agent (available for future use).

`cloudbuild-dev.yaml` — `DEBUG_PROMPTS=true,DEBUG_PROMPTS_BUCKET=$_DEBUG_PROMPTS_BUCKET`
in `--set-env-vars`. Bucket value passed via Cloud Build substitution variable; Makefile
`deploy-dev` passes `_DEBUG_PROMPTS_BUCKET=$(DEBUG_PROMPTS_BUCKET)` from `.env`.
Prod (`cloudbuild-prod.yaml`) — no debug logging (intentional).

**5. Consolidation completion — smart notification via UserNotificationService**

Replaced raw `response_channel.send_message("✅ Консолідація завершена...")` in `conversation_handler.py`
with `self._notification_service.notify(user_id, account_id, system_alert)`. The completion
message now routes through QuickAgent and is formatted in the user's communication style,
consistent with the email indexer notification pattern.

Fallback: if `_notification_service` is None (not wired), plain `send_message` is used.

Dead code removed: `run_consolidation_process()` in `consolidation_handler.py` was never called
from any external file. Also removed unused `ResponseChannel` import from that file.

---

### 2026-03-01 — Email triage stability: Cloud Run timeout, LLM timeout, gRPC retry

**Root cause of silent email triage hang (e54980e4):**
- Two concurrent `$consolidate` commands ran simultaneously. One Gemini Pro call with 200-email
  prompt (~215K chars) never returned — no error logged, process killed silently by Cloud Run.
- Cloud Run default request timeout is 300s. The `$consolidate` path keeps the HTTP request alive
  (`await _execute_consolidation_background()`), so any LLM call approaching 300s causes Cloud Run
  to terminate the process before Python can log or recover.

**Fixes applied:**

1. **Cloud Run `--timeout=900`** — added to both `cloudbuild-dev.yaml` and `cloudbuild-prod.yaml`.
   Gives the consolidation worker up to 15 minutes before Cloud Run kills the HTTP request.
   This ensures our Python-level timeout fires and logs before Cloud Run terminates.

2. **`LLMRequest.timeout: Optional[int] = None`** — new field in `src/ports/llm_service.py`.
   Passed through to `GeminiAdapter.generate_content()` via `asyncio.wait_for(..., timeout=N)`.
   When timeout fires → `asyncio.TimeoutError` is raised, propagates up, caught by outer
   `except Exception` in `process_user_batches_on_overflow` → logged with full context.

3. **`ConsolidationAgent` LLM calls: `timeout=500`** — both the main tool-calling loop and the
   summarization call in `src/agents/consolidation_agent.py`. 500s < 900s Cloud Run timeout,
   ensuring we always get a logged error instead of a silent kill.

4. **`_EMAIL_TRIAGE_BATCH_SIZE = 200`** — unchanged (200 is correct for cross-email pattern
   detection; the hang was timeout-related, not batch-size-related).

**Not yet root-caused:** Whether the Gemini Pro call genuinely exceeded 500s or was killed by
Cloud Run at 300s before this fix. Next occurrence will have a clear `TimeoutError` log entry.

### 2026-02-28 — biographical_signal, page_size=300, AgentExecutionContext wiring

**Classification:**
- Added second selection test (TEST B — biographical_signal) to §4. Classification now uses
  two-test model: TEST A (confirmed_event) and TEST B (biographical_signal). Email passes if either test is satisfied.
- `valuable_type` field added to §3.2 Firestore schema, §5 `IndexedEmail` domain model,
  §4 output examples. Enum: `"confirmed_event" | "biographical_signal"`.
- Empirical rate confirmed: 73/500 = 14.6% valuable (first production run Feb 2026).
  Matches the 10–20% design assumption. §9.1 cost analysis updated.

**Batch sizing:**
- Default page_size updated to 300 (was 100) across §2.1.2, §2.2 rationale, CHUNK diagram, §18 Блок 3.
  `EmailIndexingService.page_size=300`, Gmail API hard limit 500. Configurable via `--count` flag.
  ~45 docs/chunk at ~15% rate (was ~15 docs).

**ServiceContainer wiring (§2.1.4):**
- `EmailClassificationAgent` now wired via `AgentExecutionContext` pattern (not raw `llm=`, `model_name=`).
  `context_builder.build("email_classifier", email_config)` returns the context;
  `AgentProviderStrategy` maps `"email_classifier"` → BALANCED tier → Gemini Flash.

### 2026-02-28 — Блоки 2–4 реализованы и валидированы

**Реализация (diverges from RFC in several places):**

- `EmailClassificationAgent` (`src/agents/email_classification_agent.py`) — реализован как агент (не сервис,
  как было в RFC §2.1.1). Причина: требует BaseAgent инфраструктуры для LLM + tool calling loop.
- `EmailProviderPort.list_emails` получил `query: Optional[str] = None` параметр (не было в RFC §2.1.2).
  Adapter объединяет `query` и `date_from` в единый Gmail `q=` параметр.
- `EmailProviderPort.batch_get_full_content` получил `deep: bool = False` (был в §6, но отсутствовал в
  формальном контракте §2.1.2). `deep=False` (default) — body + attachment filenames only. `deep=True` —
  также скачивает бинарные attachment для markitdown парсинга (Mode B).
- `EmailIndexingService` получил `GMAIL_DEFAULT_QUERY = "{category:primary category:updates} -in:spam"` —
  константа уровня модуля, дефолтный `gmail_query` параметр `run_indexing_job()`. Передаётся явно в
  `run_indexing.py`; `--no-filter` флаг позволяет отключить для отладки.
- `IndexedEmail` получил `embedding_pending: bool = False` поле (не было в RFC §5). Если embedding упал —
  документ сохраняется с `embedding_pending=True`, repair service подхватывает позднее.

**Валидация (Блок 4):**

- Все три коллекции в `us-production` созданы и содержат корректные документы.
- `development_domain_email_facts_v1`: все 4 векторных поля (`vector`, `tags_vector`, `metadata_vector`,
  `attachments_vector`) либо заполнены, либо `null` при отсутствии вложений / при `embedding_pending=True`.
- `development_email_indexing_state`: курсор `indexed_through` установлен корректно после каждого чанка.
- `development_email_indexing_jobs_v1`: журнал работает; `status="completed"` при успехе.
- Дедупликация by email_id работает из коробки через Firestore `batch.set(doc_id=email_id)`.

**Расхождения с §16.2 исправлены** — все завершённые компоненты переведены в ✅.

**Блоки 1–4 полностью завершены.** Блок 5 (EmailAgent + EmailSearchAgent + Cabinet UI) — следующий этап.

### 2026-02-28 — firestore.indexes.json + classification tests completed

- `config/firestore.indexes.json` — confirmed already present with all required email indexes:
  4 vector indexes for `{dev,prod}_domain_email_facts_v1` (`user_id + state + {field}`);
  composite index `user_id + consolidated_at + indexed_at` for `get_unconsolidated_batch`;
  composite indexes `user_id + started_at` and `user_id + provider + started_at` for job queries.
- `tests/unit/agents/test_email_classification_agent.py` — 13 tests added: classify_batch happy path,
  missing email fallback, invalid JSON + retry, LLM error, empty input, LLMRequest field validation,
  tags normalization, JSON retry success, prompt_builder guard, can_handle, execute, tool calling path,
  MAX_TURNS. 1257 unit tests passing.

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
