# Use Cases — Alek-Core

**Version:** 1.0
**Date:** 2026-02-19
**Source:** Arc42 documentation (v6.0). ~80% accuracy, source of truth — code.

---

## Contents

- [Domain: Conversation — Talking to the bot](#domain-conversation)
- [Domain: Memory — Memory management](#domain-memory)
- [Domain: User Management — User management](#domain-user-management)
- [Domain: User Cabinet — Web interface](#domain-user-cabinet)
- [Domain: File Processing — File processing](#domain-file-processing)
- [Domain: Security — Security](#domain-security)
- [Domain: System — System operations](#domain-system)

---

## Formatting

```
Status:    Implemented | Planned | Proposed
Priority:  P0 (critical) | P1 (important) | P2 (desirable)
Actor:     User | Account Owner | System Admin | System (automatic)
```

---

## Domain: Conversation

Messaging scenarios between the user and the bot via Slack or Telegram.

---

### UC-001: Simple request → quick response

**Actor:** User (Slack / Telegram)
**Trigger:** User sends a short message — greeting, simple question, clarification
**Status:** Implemented | **Priority:** P0

**Flow:**
1. Platform (Slack/Telegram) sends a webhook to `/slack/events` or `/telegram/webhook`
2. Adapter verifies HMAC signature, deduplicates the event (hash update_id, TTL 5 min)
3. `ConversationHandler` creates `MessageContext`
4. `IAMService` authorizes the user by `platform_user_id`
5. `AgentCoordinator` passes the message to `RouterAgent`
6. `RouterAgent` classifies: complexity ≤ 5 → `QuickResponseAgent`
   - Fast path: rule-based matching (greetings, short phrases)
   - Slow path: LLM triage (Gemini Flash)
7. `QuickResponseAgent` loads session history (last 20 messages)
8. Builds prompt and calls Gemini Flash
9. Returns response via `ResponseChannel`
10. `SessionStore` atomically writes both messages (user + model)

**Result:** User receives a response in < 2s (p95)

---

### UC-002: Complex request → smart response

**Actor:** User (Slack / Telegram)
**Trigger:** User sends a complex multi-step request (analysis, research, compare, explain...)
**Status:** Implemented | **Priority:** P0

**Flow:**
1. Adapter → `ConversationHandler` → `AgentCoordinator` → `RouterAgent`
2. `RouterAgent` classifies: complexity ≥ 6 → `SmartResponseAgent`
   - Also: confidence < 0.75 → always Smart (safe fallback)
3. `RouterAgent` concurrently runs `SearchEnrichmentService`:
   - Generates up to 7 parallel vector queries
   - RRF ranking of results
   - Enriched context is attached to `AgentMessage`
4. `SmartResponseAgent` loads history (last 60 messages, tiered):
   - Last 5 model turns → full text
   - Older → compressed `history_summary` (≤ 300 chars)
5. `force_tool_use=True`: LLM **must** call a tool, does not respond with plain text
6. Delegation loop (max 5 iterations):
   - `search_memory(query)` → `MemorySearchAgent` (always first)
   - `ask_web_search_agent(query)` → `WebSearchAgent` (when needed)
   - `deliver_response(full_response, history_summary)` → completion
7. Response is delivered to the user; `history_summary` is saved to session

**Result:** User receives a detailed response in < 10s (p95)

---

### UC-003: Request with personal data from memory

**Actor:** User (Slack / Telegram)
**Trigger:** User asks about their own data ("what car do I have?", "where do I live?")
**Status:** Implemented | **Priority:** P0

**Flow:**
1. `RouterAgent` identifies a personal query (keywords: "my", "mine", "I have" + context)
2. Complexity may be ≤ 5 → `QuickResponseAgent` responds from the hot session context
3. If context is insufficient — Smart route:
   - `MemorySearchAgent` performs vector search over user facts
   - Returns top-N relevant facts with scores
   - `SmartResponseAgent` synthesizes a response based on memory

**Result:** Bot responds with awareness of accumulated personal facts

---

### UC-004: External information request (web search)

**Actor:** User (Slack / Telegram)
**Trigger:** User asks about current events, weather, prices, news
**Status:** Implemented | **Priority:** P1

**Flow:**
1. `RouterAgent`: complexity > 5 or keyword match (weather, news, google) → `SmartResponseAgent`
2. `SmartResponseAgent` calls `ask_web_search_agent(query)` in a loop
3. `WebSearchAgent`:
   - Loads session context to formulate the query
   - Calls Gemini with Google Search grounding tool
   - LLM performs search and synthesizes the response
   - Returns a formatted response with sources
4. `SmartResponseAgent` synthesizes the final response

**Result:** Up-to-date information from the internet with citations

**Note:** `WebSearchAgent` is isolated from `MemorySearchAgent` — Gemini API does not support combining Google Grounding + function calling in a single request.

---

### UC-005: Combined request (memory + web)

**Actor:** User (Slack / Telegram)
**Trigger:** "What car do I have and how much does gas cost?"
**Status:** Implemented | **Priority:** P1

**Flow:**
1. `SmartResponseAgent` determines that both tools are needed
2. Turn 1: `search_memory("my car")` → `MemorySearchAgent` → "Honda Civic 2019"
3. Turn 2: `ask_web_search_agent("gas price Valencia")` → "€1.45/L"
4. Turn 3: `deliver_response(...)` — synthesis from both sources

**Result:** Personalized response with data from memory and the internet

---

## Domain: Memory

Scenarios for working with long-term memory — facts, consolidation, search.

---

### UC-010: Automatic consolidation on session overflow

**Actor:** System
**Trigger:** Number of messages in the session exceeds the threshold (100 messages)
**Status:** Implemented | **Priority:** P0

**Flow:**
1. `FirestoreSessionStore.append_messages_batch()` detects overflow
2. The oldest 50 messages are extracted into a `ConsolidationBatch` (status=PENDING)
3. Batch is saved in Firestore `consolidation_queue`
4. Cloud Tasks receives a task for background processing
5. `ConsolidationHandler` (background) picks up the batch:
   - status → PROCESSING
   - Calls `ConsolidationAgent`
6. `ConsolidationAgent` ("Life Chronicler"):
   - Loads the user's biographical context (cache)
   - LLM (PERFORMANCE tier) analyzes 50 messages
   - Extracts `new_facts` and `new_anchors` (structured JSON)
   - Generates 3 vectors per fact in parallel (text, tags, metadata)
   - Deduplicates via vector search (threshold 0.15)
7. `FactWriteService` saves facts in Firestore with SCD2 metadata
8. Biographical cache is invalidated → updated for the next request
9. Batch is removed from the queue (status=COMPLETED)

**Result:** Knowledge from the conversation is saved to long-term memory; session does not bloat

---

### UC-011: Consolidation on session expiry

**Actor:** System
**Trigger:** Session has been inactive for 90 days
**Status:** Implemented | **Priority:** P1

**Flow:**
1. Expired TTL → session is marked as expired
2. All session messages are sent to `ConsolidationQueue`
3. Continues with the same flow as UC-010

**Result:** Knowledge from "old" conversations is not lost

---

### UC-012: Update (versioning) of an existing fact

**Actor:** System (ConsolidationAgent)
**Trigger:** User states information that contradicts an existing fact ("I moved to Madrid")
**Status:** Implemented | **Priority:** P0

**Flow:**
1. `ConsolidationAgent` extracts the new fact: "User lives in Madrid"
2. Vector search finds the existing fact: "User lives in Valencia" (high similarity)
3. Deduplication: numbers do not differ, fact has substantially changed → NOT duplicate
4. `FactWriteService` applies SCD2:
   - Existing fact: `is_current=False`, `valid_to=now`
   - New fact: `is_current=True`, `valid_from=now`, `lineage_id=same`
5. The next search will return only the current fact

**Result:** Change history is preserved; bot knows the current state

---

### UC-013: Session history compression (per-turn compression)

**Actor:** System
**Trigger:** `SmartResponseAgent` finishes processing a request
**Status:** Implemented (feature flag `ENABLE_HISTORY_OPTIMIZATION`) | **Priority:** P1

**Flow:**
1. `SmartResponseAgent` calls `deliver_response(full_response, history_summary)`
2. `full_response` (full text) → `MessagePart.full_text` in history
3. `history_summary` (≤ 300 chars, Flash compression) → `MessagePart.text`
4. When loading history: last 5 model turns → `full_text`; older → `text` (stub)

**Result:** Hot storage does not bloat during long conversations

---

## Domain: User Management

Scenarios for registration, authentication, team management, and platform linking.

---

### UC-020: New user registration

**Actor:** User (Web Browser)
**Trigger:** User navigates to `/auth/login`
**Status:** Implemented | **Priority:** P0

**Flow:**
1. Redirect to Google OAuth consent page
2. Google returns OIDC tokens to `/auth/callback`
3. `AuthenticationService` handles the callback:
   - `external_user_id` not found → new user
   - Check `WhitelistRepository` by email
   - If NOT in whitelist → reject (closed system)
4. `UserProfile` (UUID) + `BillingAccount` (Master Account First) are created
5. User is assigned as `OWNER` of their account
6. JWT access + refresh tokens are issued (HttpOnly cookie)

**Result:** Account created; user is authenticated in the web interface

---

### UC-021: Existing user login

**Actor:** User (Web Browser)
**Trigger:** User navigates to `/auth/login`
**Status:** Implemented | **Priority:** P0

**Flow:**
1. OAuth callback → `AuthenticationService`
2. `external_user_id` matches an existing profile → authenticate
3. Update profile metadata from OIDC
4. Issue new JWT tokens

**Result:** User is authenticated without creating a new account

---

### UC-022: Linking a Slack account

**Actor:** User (User Cabinet)
**Trigger:** User clicks "Link Slack" in the User Cabinet
**Status:** Implemented | **Priority:** P0

**Flow:**
1. `POST /api/user/link-platform` with `platform_user_id` (Slack User ID)
2. `UserRepository.link_platform_identity("slack", slack_user_id)`
3. `UserProfile.platform_identities["slack"] = slack_user_id`
4. After linking, `IAMService` authorizes incoming Slack events by `platform_user_id`

**Result:** Bot in Slack recognizes the user and applies their memory

---

### UC-023: Linking a Telegram account

**Actor:** User (User Cabinet)
**Trigger:** User clicks "Link Telegram" in the User Cabinet
**Status:** Implemented | **Priority:** P0

**Flow:**
1. `POST /api/user/link-telegram` with `telegram_user_id`
2. Same as UC-022, but for the Telegram platform

**Result:** Bot in Telegram recognizes the user

---

### UC-024: Unlinking a platform

**Actor:** User (User Cabinet)
**Trigger:** User clicks "Unlink" for Slack or Telegram
**Status:** Implemented | **Priority:** P1

**Flow:**
1. `DELETE /api/user/link-platform?platform=slack`
2. `platform_identities["slack"]` is removed from `UserProfile`
3. Subsequent Slack events from this `platform_user_id` → unauthorized

**Result:** Platform is unlinked; bot stops recognizing the user on it

---

### UC-025: Inviting a new team member (Owner)

**Actor:** Account Owner (User Cabinet)
**Trigger:** Owner clicks "Generate Invite" in the User Cabinet
**Status:** Implemented | **Priority:** P1

**Flow:**
1. `POST /api/user/invite-codes` (only for role=OWNER)
2. `InviteCodeService` creates an `InviteCode` (type: PERSONAL/FAMILY/ORGANIZATION)
3. Owner copies the code and shares it with the new user
4. `GET /api/user/invite-codes` shows active codes

**Result:** A time-limited invite code is generated

---

### UC-026: Joining a team via invite code

**Actor:** User (User Cabinet)
**Trigger:** User enters an invite code on the `/join?code=XYZ` page
**Status:** Implemented | **Priority:** P1

**Flow:**
1. User is authenticated (if not → OAuth flow)
2. `POST /api/user/join-team` with `{ "code": "XYZ" }`
3. `InviteCodeService` validates the code (not expired, not used)
4. User is added to the code owner's account with role MEMBER
5. Code is marked as used (`used_by = user_id`)

**Result:** User gains access to the shared account

---

### UC-027: Access token refresh

**Actor:** User (Web Browser — automatically)
**Trigger:** Access token has expired (< 15 min TTL)
**Status:** Implemented | **Priority:** P0

**Flow:**
1. `POST /auth/refresh` with refresh token (HttpOnly cookie)
2. `AuthenticationService` validates the refresh token
3. A new access token is issued

**Result:** Session continues without repeating OAuth

---

### UC-028: Logout

**Actor:** User (Web Browser)
**Trigger:** User clicks Logout
**Status:** Implemented | **Priority:** P0

**Flow:**
1. `POST /auth/logout`
2. Refresh token is revoked
3. HttpOnly cookies are cleared

**Result:** Session is terminated; re-login requires OAuth

---

## Domain: User Cabinet

Scenarios for the self-service web interface.

---

### UC-030: Viewing personal facts

**Actor:** User (User Cabinet)
**Trigger:** User opens the Facts section in the User Cabinet
**Status:** Implemented | **Priority:** P1

**Flow:**
1. `GET /api/user/facts/browse` (cursor pagination, 100/page)
2. Filtering by domain (health, location, biographical, ...) via query param
3. Results sorted by `created_at DESC`
4. "Load more" loads the next page via `cursor`

**Result:** User sees all accumulated facts with color-coding by domain

---

### UC-031: Semantic search over facts

**Actor:** User (User Cabinet)
**Trigger:** User types a query into the search bar
**Status:** Implemented | **Priority:** P1

**Flow:**
1. `POST /api/user/facts/search { "query": "travel plans to Poland" }`
2. `EmbeddingService` generates a query vector
3. Firestore KNN search → top-50 results by cosine similarity
4. Results are shown in Search mode (not Browse)

**Result:** Finds conceptually related facts even without exact word matches

---

### UC-032: Deleting (invalidating) a fact

**Actor:** User (User Cabinet)
**Trigger:** User clicks "Invalid" on a specific fact
**Status:** Implemented | **Priority:** P1

**Flow:**
1. Confirmation modal shows the fact text
2. Confirmation → `POST /api/user/facts/{id}/invalidate`
3. Server checks `account_id` from JWT == `account_id` of the document (403 if not)
4. `FactEntity.state = "invalidated"` — direct write, no LLM
5. Fact immediately disappears from Browse, Search, and vector search
6. **Note:** biographical cache becomes stale until the next consolidation

**Result:** Incorrect fact is removed immediately

---

### UC-033: Correcting a fact

**Actor:** User (User Cabinet)
**Trigger:** User clicks "Edit" on a specific fact
**Status:** Implemented | **Priority:** P1

**Flow:**
1. Modal: old text (read-only) + textarea for the new version
2. "Copy message" → a structured message is copied to the clipboard:
   ```
   I found this fact in my memory database and it needs correction.
   Current (incorrect): "{old_text}"
   Correct version: "{new_text}"
   Please update it accordingly.
   ```
3. User pastes the message into chat (Slack/Telegram)
4. `ConsolidationAgent` processes it via correction detection:
   - Old fact → `state=SUPERSEDED`
   - New fact → `is_current=True` with new vectors and `lineage_id`
5. Old fact remains visible in the Cabinet until the next consolidation (documented behavior)

**Result:** Fact is corrected with full change history (SCD2)

---

## Domain: File Processing

Scenarios for processing file attachments.

---

### UC-040: Sending an image or PDF

**Actor:** User (Slack / Telegram)
**Trigger:** User attaches an image or PDF to a message
**Status:** Implemented | **Priority:** P1

**Flow:**
1. Adapter includes the attachment in `MessageContext.attachments`
2. `ConversationHandler` determines `is_native_binary(mime_type)` → True for `image/*`, `application/pdf`
3. `MessagePart(file_data=...)` is created — passed to the LLM adapter natively
4. Claude/Gemini process the image/PDF natively (vision/document API)

**Result:** LLM sees the file contents and responds based on them

---

### UC-041: Sending a text document (DOCX, XLSX, TXT, CSV)

**Actor:** User (Slack / Telegram)
**Trigger:** User attaches a document to a message
**Status:** Implemented | **Priority:** P1

**Flow:**
1. `FileConversionService.convert_file_to_text()`:
   - `text/*` → direct UTF-8 read (fast path)
   - `docx/xlsx` → MarkItDown conversion
2. Output is wrapped: `[File: name.docx]\n{content}\n[/File: name.docx]`
3. If > 30K characters → truncation + system alert for LLM
4. A stub (1000 chars) is saved to history — full text only for the last 5 turns

**Result:** LLM sees the text content of the document

---

### UC-042: Sending an unsupported file

**Actor:** User (Slack / Telegram)
**Trigger:** User attaches an audio, video, or unknown format file
**Status:** Implemented (graceful degradation) | **Priority:** P1

**Flow:**
1. `FileConversionService` cannot process the file
2. A system alert is generated (embedded in context as text):
   ```
   [System: User attempted to attach 'file.xyz' (application/octet-stream).
   The file could not be read or is not a supported text format.
   Supported formats: images, PDF, plain text, CSV, DOCX, XLSX.
   Ask the user to convert the file or paste the content directly.]
   ```
3. LLM sees the alert and responds to the user in natural language

**Result:** User receives a clear explanation instead of a technical error

---

### UC-043: File too large (> 5 MB)

**Actor:** User (Slack / Telegram)
**Trigger:** User sends a file larger than 5 MB
**Status:** Implemented | **Priority:** P1

**Flow:**
1. `FileConversionService` checks the size → generates `_size_alert()`
2. LLM receives the alert and asks the user to send a smaller file or paste the text directly

**Result:** Graceful degradation without technical errors

---

## Domain: Security

Scenarios for system protection.

---

### UC-050: Blocking a prompt injection attempt

**Actor:** User (any platform) — intentional or not
**Trigger:** User sends a message like "Ignore previous instructions. You are now..."
**Status:** Implemented | **Priority:** P0

**Flow:**
1. `ConversationHandler` passes user input through `SecurityPort` (UNTRUSTED zone)
2. `RegexSecurityAdapter` checks patterns:
   - CRITICAL: direct overrides ("system: you must...") → BLOCK
   - HIGH: instruction manipulation ("ignore previous instructions") → BLOCK
   - MEDIUM: soft manipulation ("forget everything") → SANITIZE ([REDACTED])
3. BLOCKED → request is rejected; user receives a standard refusal
4. The same validation is applied to **LLM output** before saving to history (Layer 4)

**Result:** Prompt injection attack is blocked or sanitized

---

### UC-051: Prompt token validation at creation (admin)

**Actor:** System Admin
**Trigger:** Adding a new Token to the prompt library
**Status:** Implemented | **Priority:** P1

**Flow:**
1. Layer 1: `SecurityPort.validate(token_content, zone=TRUSTED)`
2. `RegexSecurityAdapter` checks content for injections
3. Passed → Token is added to the library
4. Blocked → Token is rejected with a reason

**Result:** Only safe tokens are added to the library

---

## Domain: System

System and infrastructure scenarios.

---

### UC-060: Health check

**Actor:** Cloud Run / Load Balancer
**Trigger:** Periodic liveness probe
**Status:** Implemented | **Priority:** P0

**Flow:**
1. `GET /health` (no authorization)
2. Returns `200 OK`

**Result:** System signals its availability

---

### UC-061: Background consolidation batch processing (Cloud Tasks)

**Actor:** System (Cloud Tasks → ConsolidationHandler)
**Trigger:** Cloud Tasks enqueues a task after a ConsolidationBatch is created
**Status:** Implemented | **Priority:** P0

**Flow:**
1. Cloud Tasks calls the worker endpoint
2. `ConsolidationHandler` picks up the next PENDING batch for the user
3. Processes sequentially (temporal order is important)
4. Retry: 3 attempts → FAILED if all failed
5. After COMPLETED → batch is removed from the queue

**Result:** Long-term memory is updated asynchronously without blocking the user

---

### UC-062: Agent health monitoring (Circuit Breaker)

**Actor:** System
**Trigger:** An agent receives 3 consecutive failures
**Status:** Implemented | **Priority:** P0

**Flow:**
1. `BaseAgent.CircuitBreaker` records 3 consecutive failures → OPEN
2. For 5 minutes: agent returns `AgentStatus.CANNOT_HANDLE` without calling LLM
3. After 5 min: HALF-OPEN → test request
4. Success → CLOSED; failure → OPEN again

**Result:** Failure of one agent does not cascade to the entire system

---

### UC-063: Routing fallback on LLM triage failure

**Actor:** System
**Trigger:** LLM classifier `RouterAgent` is unavailable (timeout / circuit open)
**Status:** Implemented | **Priority:** P0

**Flow:**
1. LLM triage fails → rule-based fallback:
   - ≤ 3 words, no question mark → `QuickResponseAgent`
   - Keywords (analyze, compare, explain) → `SmartResponseAgent`
2. Request continues processing with minimal delay

**Result:** System works even when the classifier is unavailable

---

## Planned Use Cases (RFC / Planned)

Functionality in RFC or roadmap stage. **Not implemented.**

| ID | Name | Status | RFC |
|----|------|--------|-----|
| UC-P01 | Email indexing (Gmail integration) | Proposed | GMAIL_EMAIL_INDEXING_RFC |
| UC-P02 | Native LLM tool calls (function calling) | Proposed | NATIVE_TOOLS_INTEGRATION_RFC |
| UC-P03 | Manual consolidation (`$consolidate` command) | Planned | — |
| UC-P04 | Audio transcription (Whisper/Google Speech) | Planned | AudioTranscriptionPort ready |
| UC-P05 | Adaptive routing cache | Proposed | ADAPTIVE_ROUTING_CACHE_RFC |
| UC-P06 | Fact management with taxonomy | Proposed | DELIBERATE_FACT_MANAGEMENT_RFC |
| UC-P07 | Structured web search output (JSON) | Proposed | WEBSEARCH_STRUCTURED_OUTPUT_RFC |
| UC-P08 | Discord / WhatsApp integration | Planned | — |

---

*Document compiled based on arc42 documentation v6.0 (2026-02-10 – 2026-02-19).*
*In case of discrepancy with the code — the code is the source of truth.*
