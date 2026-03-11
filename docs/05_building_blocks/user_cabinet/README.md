# User Cabinet

**Status:** ✅ Production Ready (2026-02-18)

**Purpose:** Web-based self-service portal for authenticated users. Allows platform linking (Slack, Telegram), browsing and semantically searching personal memory (facts), fact management (remove/correct), and managing team invites.

**Code:** `src/web/user_cabinet_app.py`, `src/web/static/cabinet.html`

---

## Overview

User Cabinet is a single-page application served at `/cabinet`. It authenticates users via JWT (HttpOnly cookie, same as OAuth Web API) and exposes a REST API consumed by the frontend.

### Key Features

- ✅ **Platform Linking** — Link/unlink Slack and Telegram identities; shows current linked state
- ✅ **Facts Browser** — Cursor-based paginated view of personal facts (100/page, domain filter)
- ✅ **Semantic Search** — Vector-based search over personal facts via `POST /api/user/facts/search`
- ✅ **Fact Removal** — Direct invalidation via `POST /api/user/facts/{id}/invalidate`; immediate effect, no LLM required
- ✅ **Fact Correction** — UI generates a pre-formatted message for the user to paste into chat; ConsolidationAgent handles the update
- ✅ **Team Invites** — Owner-only invite code generation and management
- ✅ **Hexagonal Architecture** — Web layer depends only on `FactRepository` and `EmbeddingService` ports

---

## Architecture Position

```
┌─────────────────────────────────────┐
│         Web Browser (SPA)           │
│  cabinet.html  (Tailwind + vanilla) │
└────────────────┬────────────────────┘
                 │  HTTP / JWT cookie
                 ▼
┌─────────────────────────────────────┐
│     User Cabinet Blueprint          │  ← Adapter Layer
│  (user_cabinet_app.py / Quart)      │
│  @auth_required  ←  SessionService  │
└────────────────┬────────────────────┘
                 │
        ┌────────┴─────────┐
        ▼                  ▼
┌──────────────┐  ┌───────────────────┐
│FactRepository│  │ EmbeddingService  │  ← Ports (ABC)
│    (port)    │  │    (port)         │
└──────┬───────┘  └────────┬──────────┘
       ▼                   ▼
┌──────────────┐  ┌────────────────────┐
│ Firestore    │  │ GeminiEmbedding    │  ← Adapters
│ FactRepo     │  │ Adapter            │
└──────────────┘  └────────────────────┘
```

**Composition root:** `main.py` injects adapters — `FirestoreFactRepository` and `GeminiEmbeddingAdapter` — typed as ports.

---

## API Contract

All endpoints require a valid JWT access token (HttpOnly cookie `access_token` or `Authorization: Bearer <token>` header). Invalid/expired token → `401`.

### Platform State

#### `GET /api/user/platforms`

Returns current platform link state for the authenticated user.

**Response:**
```json
{
  "platforms": {
    "slack": "U0A93H4Q6QY",
    "telegram": "670659908"
  }
}
```
Unlinked platforms are absent from the map.

#### `POST /api/user/link-platform`

**Body:** `{ "platform": "slack", "platform_user_id": "U0A93H4Q6QY" }`

#### `DELETE /api/user/link-platform?platform=slack`

#### `POST /api/user/link-telegram`

**Body:** `{ "telegram_user_id": "670659908" }`

---

### Facts Browser (Browse mode)

#### `GET /api/user/facts/browse`

Cursor-based paginated retrieval ordered by `created_at DESC`.

**Query params:**

| Param | Default | Description |
|-------|---------|-------------|
| `limit` | 100 | Page size (max 500) |
| `cursor` | — | Doc ID of last item from previous page |
| `domain` | — | Filter: `health`, `location`, `biographical`, etc. |

**Response:**
```json
{
  "facts": [
    {
      "id": "doc_id",
      "text": "User lives in Valencia, Spain.",
      "domain": "location",
      "type": "fact",
      "created_at": "2026-02-17T10:32:00"
    }
  ],
  "next_cursor": "abc123def"
}
```
`next_cursor` is `null` when there are no more pages.

**Pagination pattern (client):**
```js
// First page
GET /api/user/facts/browse?limit=100

// Next page
GET /api/user/facts/browse?limit=100&cursor=<next_cursor from previous response>
```

---

### Facts Search (Semantic mode)

#### `POST /api/user/facts/search`

Vector-based semantic search. Embeds the query text, runs `find_nearest` on Firestore, returns top-50 results ranked by cosine similarity.

**Body:**
```json
{ "query": "travel plans to Poland" }
```

**Response:**
```json
{
  "facts": [ ... ],
  "query": "travel plans to Poland"
}
```

**Latency note:** +200–500ms vs browse (embedding generation via Gemini API).

**Use case:** Free-text semantic queries — finds conceptually related facts even when exact words don't match ("Poland" finds facts about "Kraków").

---

### Fact Management

#### `POST /api/user/facts/{fact_id}/invalidate`

Directly marks a fact as `state = "invalidated"` in Firestore. Immediate effect — no LLM, no embedding, no ConsolidationAgent.

**Security:** `account_id` from the JWT is verified against the document's `account_id` server-side. Returns `403` if they don't match, `404` if fact doesn't exist.

**Effect on downstream systems:**

| System | Impact |
|--------|--------|
| Browse (`GET /browse`) | Fact disappears immediately (filtered by `state == "current"`) |
| Memory search (`search_facts`) | Fact excluded immediately |
| Enrichment service (`search_facts_by_domain`) | Fact excluded immediately |
| Biographical context **cache** | ⚠️ Cache stale until next consolidation refreshes it |

The biographical cache risk is limited in practice: the cache contains only CRITICAL/HIGH priority biographical facts. Most user-invalidated facts are health metrics, location, possession — not cached.

**Frontend behaviour:** On success, the fact row is removed from the DOM immediately without page reload (`allFacts.filter(f => f.id !== id)` → `renderFacts()`).

#### Fact Correction (UI-only, no write API)

For corrections, the cabinet generates a pre-formatted English message and copies it to clipboard:

```
I found this fact in my memory database and it needs correction.
Current (incorrect): "{old_text}"
Correct version: "{new_text}"
Please update it accordingly.
```

The user pastes this into chat with Alek. ConsolidationAgent processes it via negation/correction pattern detection → marks old fact `SUPERSEDED`, creates new fact with correct embedding and full SCD2 lineage.

**Why not a direct write for corrections:** corrections require re-embedding + lineage management — ConsolidationAgent is the right owner of this logic.

**UX note:** The cabinet describes that the old version remains visible until the next memory sync — expected and documented to the user.

---

### Team Invites (owner-only)

#### `GET /api/user/invite-codes` — list active codes
#### `POST /api/user/invite-codes` — generate new code
#### `POST /api/user/join-team` — consume invite code

---

## Domain Model

Facts in the cabinet have two write operations:
1. **Read path** (browse + search) — always `state == "current"` filtered
2. **Invalidate path** — direct `state = "invalidated"` write, bypassing ConsolidationAgent

All fact *creation* and *correction* goes through the ConsolidationAgent pipeline.

```
FactEntity
  - id: str                        ← used as pagination cursor + invalidate target
  - text: str
  - domain: FactDomain (enum)      ← health | location | biographical | ...
  - type: FactType (enum)
  - state: str                     ← "current" (read), "invalidated" (after Remove)
  - created_at: datetime
  - account_id: str                ← security: always == g.account_id from JWT
```

---

## Security Model

### Authentication
`@auth_required` decorator wraps every endpoint:
1. Extracts Bearer token from cookie or `Authorization` header
2. Calls `session_service.verify_access_token(token)` — validates HS256 signature + expiry
3. Sets `g.user_id`, `g.account_id`, `g.role` from verified JWT payload

### Data Isolation
`g.account_id` comes from the verified JWT — not from user input. All Firestore queries and writes are scoped to `account_id == g.account_id`.

```python
# Read: account_id from JWT, not from request
facts = await fact_repo.search_facts(
    query_vector=vector,
    account_id=g.account_id,
    user_id=g.user_id,
)

# Write: account_id verified server-side in the adapter
await fact_repo.invalidate_fact(
    fact_id=fact_id,
    account_id=g.account_id,  # ← adapter checks doc.account_id == this
)
```

---

## Frontend Architecture

**Stack:** Single HTML file (`cabinet.html`), Tailwind CSS (CDN), vanilla JS.

### Two Facts Modes

```
Browse mode (default):
  → GET /api/user/facts/browse
  → Domain chips filter server-side (new request per chip)
  → "Load more" button loads next cursor page
  → max-height: 520px, overflow-y: auto

Search mode (on "Search" button or Enter):
  → POST /api/user/facts/search
  → Returns top-50 semantic results
  → Mode bar shows query and result count
  → "← Browse" resets to browse mode
```

### Fact Action Buttons

Each fact row has two action buttons (subtle, muted until hover):

| Button | Hover colour | Action |
|--------|-------------|--------|
| **Edit** | indigo | Opens "Correct this fact" modal — copies pre-formatted message to clipboard |
| **Invalid** | red | Opens "Remove this fact" modal — direct API call on confirm |

**Remove modal flow:**
1. User clicks "Invalid" → modal shows fact text
2. User clicks "Remove" → `POST /api/user/facts/{id}/invalidate`
3. On success: fact removed from DOM immediately, toast shown

**Correct modal flow:**
1. User clicks "Edit" → modal shows old fact (read-only) + textarea for new version
2. User types correction, clicks "Copy message" (or ⌘↵)
3. Clipboard contains structured English message; user pastes into Slack/Telegram
4. ConsolidationAgent processes via correction detection → SUPERSEDED + new fact created
5. Old fact remains visible in cabinet until next consolidation (documented to user)

### Design
- Background: warm cream `#f0ece6`
- Header: dark brown `#2c2420`
- Cards: `#faf8f5` + warm border `#e8e2d9` + soft box-shadow
- Domain badges: color-coded per domain (red=health, green=location, blue=biographical, etc.)

---

## Dependencies

| Dependency | Type | Purpose |
|------------|------|---------|
| `FactRepository` | Port | Facts read (browse + search) + invalidate write |
| `EmbeddingService` | Port | Query vectorization for semantic search |
| `UserRepository` | Port | Platform identities read/write |
| `SessionService` | Service | JWT verification |
| `InviteCodeService` | Service | Invite code generation and consumption |

---

## Firestore Indexes Required

| Collection | Fields | Purpose |
|------------|--------|---------|
| `domain_facts_v2` | `account_id` ASC, `state` ASC, `created_at` DESC | Browse all facts |
| `domain_facts_v2` | `account_id` ASC, `state` ASC, `domain` ASC, `created_at` DESC | Browse with domain filter |
| `domain_facts_v2` | `account_id` ASC, `state` ASC, `vector` VECTOR 768 | Semantic search (existing) |

Vector index was already present. Pagination indexes added in `config/firestore.indexes.json` (2026-02-18).

---

## Cross-References

- **Authentication:** [OAuth Web API](../oauth_web_api/README.md) — JWT structure, session tokens
- **Facts data model:** [Database Schema](../../08_concepts/DATABASE_SCHEMA.md) — `domain_facts_v2` collection
- **Semantic search infrastructure:** [Search Enrichment](../search_enrichment/README.md) — `find_nearest`, RRF
- **Embedding generation:** [Embedding System](../embedding_system/README.md)
- **User identity:** [User Management System](../user_management_system/README.md)

---

**Last Updated:** 2026-02-18 (fact management added)
**Status:** ✅ Production Ready
