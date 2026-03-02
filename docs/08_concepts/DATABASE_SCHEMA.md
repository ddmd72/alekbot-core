# Firestore Database Schema (Production Audit)

**Last Updated:** 2026-03-02
**Status:** ✅ Production Validated
**Version:** 3.4 (Email Indexing + Notification State)

---

## ⚠️ CRITICAL: Database Configuration

**🎯 PRODUCTION DATABASE NAME:** `us-production`  
**🌍 REGION:** `us-central1` (US region)  
**📦 PROJECT:** `gen-lang-client-0554950952`

**❌ DO NOT USE DEFAULT (UNNAMED) DATABASE!**

### Environment Variable Configuration:

```bash
# REQUIRED for all production operations
export FIRESTORE_DATABASE=us-production

# Verify in code:
# src/config/environment.py → EnvironmentConfig.firestore_database_id
# Default value: os.getenv("FIRESTORE_DATABASE", "us-production")
```

### Why us-production?

1. ✅ **Performance:** US-CENTRAL1 region (lower latency for US users)
2. ✅ **Multi-Vector Search:** Full 3-vector RRF implementation
3. ✅ **Separation:** Isolated from default DB for safety
4. ✅ **Migration Complete:** All data migrated from default → us-production (2026-02-09)

**See:** `docs/SESSION_2026_02_09_US_PRODUCTION_MIGRATION.md`

---

## 📖 HowTo: Using This Document

### Purpose

Canonical reference for Firestore collections, document structures, and indexes.  
**Generated from code audit:** Includes exact attribute names, types, and logic.

### When to Update

- [ ] New collection added via `src/config/environment.py`
- [ ] Domain model changes (`src/domain/*.py`)
- [ ] Adapter logic changes (`src/adapters/*.py`)
- [ ] Index configuration update (`config/firestore.indexes.json`)

### Cross-References

- **OAuth Architecture:** [../10_rfcs/MULTI_TENANT_OAUTH_RFC.md](../10_rfcs/MULTI_TENANT_OAUTH_RFC.md)
- **Prompt System v3:** [../05_building_blocks/prompt_design_system_v3/README.md](../05_building_blocks/prompt_design_system_v3/README.md)
- **Environment Config:** `src/config/environment.py`

---

## 1. Environment Strategy (ADR-006: Semantic Separation)

### 1.1 Collection Naming Rules

Collections are separated into **Domain** (versioned) and **Infrastructure** (stable).

**Format:** `{prefix}{category}_{name}[_version]`

| Type       | Dev Example                       | Prod Example          | Description           |
| :--------- | :-------------------------------- | :-------------------- | :-------------------- |
| **Domain** | `development_domain_users_v2`     | `domain_users_v2`     | Identity/Config (v2)  |
| **Domain** | `development_domain_accounts_v2`  | `domain_accounts_v2`  | Billing/IAM (v2)      |
| **Domain** | `development_domain_facts_v2`     | `domain_facts_v2`     | Memory/Knowledge (v2) |
| **Domain** | `development_domain_prompt_*_v3`  | `domain_prompt_*_v3`  | Prompt System (v3)    |
| **Domain** | `development_domain_email_facts_v1` | `domain_email_facts_v1` | Indexed Email Archive (v1) |
| **Infra**  | `development_oauth_credentials`   | `oauth_credentials`   | Gmail OAuth Tokens    |
| **Infra**  | `development_email_indexing_state`| `email_indexing_state`| Indexing Cursor per User/Provider |
| **Infra**  | `development_email_indexing_jobs_v1` | `email_indexing_jobs_v1` | Indexing Job Journal |
| **Infra**  | `development_email_exclusions`    | `email_exclusions`    | Sender/Domain Skip Patterns |
| **Infra**  | `development_user_notification_state` | `user_notification_state` | Last Active Channel per User |
| **Infra**  | `development_sessions`            | `sessions`            | Sliding Window Cache  |
| **Infra**  | `development_consolidation_queue` | `consolidation_queue` | Async Queue           |
| **Infra**  | `development_event_dedup`         | `event_dedup`         | Idempotency Store     |

**Prefixes:**

- Development: `development_`
- Test: `test_`
- Production: `""` (Empty string)

---

## 2. Core Identity & Billing (v2 Domain)

### 2.1 Users (`{prefix}domain_users_v2`)

**Purpose:** User identity, platform bindings, and configuration overrides.  
**Document ID:** `user_id` (UUID)  
**Code Reference:** `src/domain/user.py` → `UserProfile`

```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "external_user_id": "firebase|123456789", // OAuth Identity (Provider|ID)
  "account_id": "account-550e8400...", // Link to BillingAccount (Tenant)
  "email": "user@example.com",
  "display_name": "John Doe",

  // Platform Bindings
  "platform_identities": {
    "slack": "U123456",
    "telegram": "987654321"
  },

  // Auth Metadata (from provider)
  "auth_metadata": {
    "provider_id": "google.com",
    "photo_url": "https://..."
  },

  // Configuration Overrides (UserBotConfig)
  "config": {
    // Provider Configuration
    "default_tier": "eco", // eco | balanced | performance
    "provider_preference": "gemini", // gemini | claude | openai
    "model_overrides": {
      "smart": "claude-3-opus-20240229"
    },
    "agent_tiers": {
      "router": "eco",
      "smart": "performance"
    },
    "temperature": 0.7,

    // Prompt Preferences
    "prompt_preferences": {
      "language": "uk",
      "vibe": "friendly",
      "custom_instructions": "Be concise",
      "custom_kernel_id": null,
      "custom_anchors_id": null
    },

    // Features
    "tools_enabled": ["search_memory", "ask_web_search_agent"],
    "is_paranoid_mode": false,
    "consolidation_threshold": 10,
    "consolidation_batch_size": 50,

    // Search & Memory Limits (3-level resolution: USER > ACCOUNT > SYSTEM)
    "semantic_search_limit": null,         // enriched context cap (system default: 30)
    "biographical_cache_limit": null,      // biographical facts (system default: 50)
    "principles_cache_limit": null,        // anchors/principles (system default: 15)

    // History Optimization
    "history_recent_full_turns": null      // recent turns with full text (system default: 5)
  },

  "created_at": "timestamp",
  "updated_at": "timestamp",
  "is_active": true
}
```

**Indexes:**

- `external_user_id` (ASC) — Login lookup
- `platform_identities.slack` (ASC) — Slack lookup

---

### 2.2 Accounts (`{prefix}domain_accounts_v2`)

**Purpose:** Tenant billing, usage tracking, IAM roles, and shared defaults.  
**Document ID:** `account_id` (UUID)  
**Code Reference:** `src/domain/billing.py` → `BillingAccount`

```json
{
  "account_id": "account-550e8400...",
  "tier": "family", // free | family | pro | enterprise | admin

  // IAM Policy (User Role Assignment)
  "iam_policy": {
    "user_1_uuid": "owner",
    "user_2_uuid": "member",
    "user_3_uuid": "viewer"
  },

  // Shared Configuration (UserBotConfig)
  // Applied to all members unless overridden at user level
  "account_defaults": {
    "default_tier": "balanced",
    "prompt_preferences": {
      "language": "en"
    },
    // History optimization: how many recent model turns use full_text vs summary
    // Resolution: USER > ACCOUNT > SYSTEM (default: 5)
    "history_recent_full_turns": 5
  },

  // Usage Tracking (AccountUsageStats)
  "usage": {
    "total_requests": 1500,
    "total_tokens": 150000,
    "total_cost": 4.5,

    "daily_tokens": 5000,
    "daily_cost": 0.15,
    "daily_reset_at": "timestamp",

    "monthly_tokens": 100000,
    "monthly_cost": 3.0,
    "monthly_reset_at": "timestamp"
  },

  "daily_token_limit": 100000,
  "monthly_cost_limit": 50.0,

  "created_at": "timestamp",
  "updated_at": "timestamp",
  "is_active": true
}
```

**Indexes:**

- `account_id` (ASC) — Lookup

---

## 3. Knowledge & Memory

### 3.1 Facts (`{prefix}domain_facts_v2`)

**Purpose:** Long-term semantic memory (shared knowledge).  
**Document ID:** `fact_id` (UUID)  
**Code Reference:** `src/domain/entities.py` → `FactEntity`

```json
{
  "id": "fact_123",

  // Dual Ownership
  "account_id": "account-550e8400...",  // Owner (Tenant)
  "created_by_user_id": "user_1",       // Creator

  // Content
  "text": "User prefers concise answers",
  "vector": [0.12, -0.45, ...],          // 768-dim text embedding (main search)
  "tags_vector": [0.08, -0.32, ...],     // 768-dim domain keywords embedding (NEW: Session 2026-02-07)
  "metadata_vector": [0.15, -0.28, ...], // 768-dim structured data embedding (NEW: Session 2026-02-07)
  "tags": ["preference", "style"],
  "type": "principle",                   // state | event | principle | system | alert

  // Visibility
  "visibility": "account_shared",       // account_shared | user_private

  // Metadata & Lineage
  "metadata": {
    "source": "slack_message",
    "confidence": 0.95
  },
  "lineage_id": "lineage_abc...",       // Links versions of same fact

  // SCD Type 2
  "created_at": "timestamp",
  "valid_from": "timestamp",
  "valid_to": null,                     // null = current truth
  "is_current": true,

  // Biographical context ordering (adapter-internal — NOT in FactEntity domain model)
  // Written by FirestoreFactRepository on every add_fact/update_fact.
  // Values: critical=1, important=2, standard=3, contextual=4, archival=5
  "context_priority_rank": 2
}
```

**Backward Compatibility:**

- `_migrate_ownership_fields()` in `FirestoreFactRepository` handles legacy data.
- Maps old `owner_id` → `account_id` + `created_by_user_id`.
- Maps old `visibility: "private"` → `visibility: "user_private"`.

**Indexes (us-production - READY):**

- **Vector Search (Main):** `account_id` + `is_current` + `vector` (768-dim, COSINE)
- **Vector Search (Tags):** `account_id` + `is_current` + `tags_vector` (768-dim, COSINE)  
  ✨ **NEW:** Created 2026-02-09 for category/domain queries
- **Vector Search (Metadata):** `account_id` + `is_current` + `metadata_vector` (768-dim, COSINE)  
  ✨ **NEW:** Created 2026-02-09 for structured data queries
- **Lineage History:** `lineage_id` + `created_at` (DESC)
- **Legacy Fallback:** `owner_id` + `is_current` + `vector` (COSINE) - Deprecated

**Multi-Vector RRF Search:** See `docs/08_concepts/multi_vector_rrf_search.md`

---

### 3.2 Sessions (`{prefix}sessions`)

**Purpose:** Chat history, context, and sliding window storage.  
**Note:** Does NOT use `_oauth` suffix.  
**Document ID:** `session_id` (Slack channel ID or UUID)  
**Code Reference:** `src/domain/session.py` → `SessionState`

```json
{
  "session_id": "C123456", // Channel ID
  "owner_id": "user_1",    // Or account_id

  // Message History (Sliding Window)
  // MessagePart fields: text, full_text, tool_call, tool_response, file_data
  "history": [
    {
      "role": "user",
      "parts": [
        { "text": "Hello" }
      ],
      "created_at": 1234567890
    },
    {
      "role": "model",
      "parts": [
        {
          "text": "Hi! 👋",              // summary (≤300 chars) when ENABLE_HISTORY_OPTIMIZATION=true; full text otherwise
          "full_text": "Hi! How can I help you today? ..." // full response, always stored
        }
      ],
      "created_at": 1234567895
    }
  ],

  "created_at": "timestamp",
  "updated_at": "timestamp",
  "last_activity": 1234567890,
  "expires_at": "timestamp" // TTL: 90 days (2160 hours)
}
```

**Implementation Notes:**

- **Sliding Window:** Max 200 messages. Older messages extracted to `ConsolidationQueue`.
- **Overflow:** Triggered by `append_messages_batch()` transaction.
- **TTL:** `cleanup_expired_sessions()` deletes sessions older than `ttl_hours`.
- **Dual-field model response:** `text` = compressed summary (when `ENABLE_HISTORY_OPTIMIZATION=true`) or full text (when false). `full_text` = complete response, always stored. On history load, SmartAgent applies tiered logic: last `history_recent_full_turns` model messages use `full_text`, older ones use `text`. Backward compatible — old records without `full_text` fall back to `text`.

**Indexes:**

- `owner_id` (ASC) + `last_activity` (DESC) — Latest session lookup
- `last_activity` (ASC) — Cleanup queries

---

### 3.3 User Context (`{prefix}user_context{suffix}`)

**Purpose:** Cached biographical summary (100x read optimization).  
**Document ID:** `account_id` (Owner ID)  
**Code Reference:** `src/adapters/firestore_repo.py` → `refresh_biographical_context_cache()`

```json
{
  "biographical_facts": [
    {
      "text": "Software engineer, lives in Kyiv...",
      "type": "event",
      "tags": ["bio", "location"],
      "created_at": "timestamp" // ISO string
    }
  ],
  "last_updated": "timestamp", // Server Timestamp
  "facts_count": 42,
  "version": 3,
  "note": "Generated without vector index" // Optional
}
```

---

## 4. Prompt Design System v3

**Naming Rule:** Part of Domain (`domain_prompt_*_v3`).

### 4.1 System Tokens (`{prefix}domain_prompt_tokens_v3_system`)

### 4.2 User Tokens (`{prefix}prompt_user_tokens`)

### 4.2 User Tokens (`{prefix}domain_prompt_tokens_v3_user`)

**Purpose:** Admin-managed prompt fragments.  
**Document ID:** `token_id` (e.g., `HUMOR_PRESET_RANEVSKAYA`)  
**Code Reference:** `src/domain/prompt_v3/token.py`

```json
{
  "token_id": "HUMOR_PRESET_RANEVSKAYA",
  "category": "humor_engine", // humor_engine | archetype | voice | ...
  "class": "properties",      // properties | instructions | ...
  "content": "humor_engine { ... }", // Groovy code block
  "metadata": {
    "version": "1.0",
    "author": "system",
    "description": "Ranevskaya humor style",
    "validation": { ... } // SecurityPort validation result
  },
  "created_at": "timestamp",
  "updated_at": "timestamp"
}
```

### 4.2 User Tokens (`{prefix}prompt_user_tokens`)

**Purpose:** User-customizable tokens (fallback lookup).  
**Schema:** Same as System Tokens.

**Dual-Collection Logic:**

1. Lookup in `system_tokens` first.
2. If not found, lookup in `user_tokens`.

---

### 4.3 Blueprints (`{prefix}domain_prompt_blueprints_v3`)

**Purpose:** Prompt templates with slot definitions.  
**Note:** v3 suffix REMOVED in main.py initialization.  
**Document ID:** `blueprint_id` (e.g., `smart_agent_v1`)  
**Code Reference:** `src/domain/prompt_v3/blueprint.py`

```json
{
  "blueprint_id": "smart_agent_v1",
  "template": "class Alek { {{HUMOR_ENGINE}} {{VOICE}} }",
  "classes": {
    "HUMOR_ENGINE": {
      "allowed_token_categories": ["humor_engine"],
      "overridable_by": ["USER"],
      "default_token": "HUMOR_PRESET_RANEVSKAYA"
    },
    "VOICE": {
      "allowed_token_categories": ["voice", "tone"],
      "overridable_by": ["ACCOUNT", "USER"],
      "default_token": "VOICE_CONVERSATIONAL"
    }
  }
}
```

---

### 4.4 Agent Profiles (`{prefix}domain_prompt_profiles_v3`)

**Purpose:** SYSTEM/AGENT level defaults.  
**Document ID:** `{blueprint_id}_{owner_type}_{owner_value}`  
**Code Reference:** `src/adapters/prompt_v3/firestore_agent_profile_repository.py`

```json
{
  // ID: smart_agent_v1_SYSTEM_smart
  "owner_type": "SYSTEM",
  "owner_value": "smart",
  "slots": [
    {
      "type": "token",
      "value": "HUMOR_PRESET_RANEVSKAYA",
      "non_overridable": false
    }
  ]
}
```

### 4.5 User Overrides (`{prefix}domain_prompt_overrides_v3`)

**Purpose:** USER/ACCOUNT level overrides.  
**Document ID:** `{blueprint_id}_{owner_type}_{owner_value}`  
**Schema:** Same as Agent Profiles.

**4-Level Resolution:**

1. **USER:** Check `user_token_overrides` for `USER`
2. **ACCOUNT:** Check `user_token_overrides` for `ACCOUNT`
3. **AGENT:** Check `agent_profiles` for `AGENT`
4. **SYSTEM:** Check `agent_profiles` for `SYSTEM`

---

## 5. Infrastructure Queues

### 5.1 Consolidation Queue (`{prefix}consolidation_queue`)

**Purpose:** Async background processing of session overflow.  
**Document ID:** `batch_id` (UUID)  
**Code Reference:** `src/domain/consolidation.py` → `ConsolidationBatch`

```json
{
  "batch_id": "batch_123",
  "user_id": "user_1",
  "session_id": "C123456",
  "messages": [ ... ], // Serialized messages
  "status": "pending", // pending | processing | completed | retry_pending | failed
  "attempts": 0,
  "last_error": "Timeout error",
  "facts_extracted": 5,
  "created_at": 1234567890.0,
  "processed_at": 1234567900.0
}
```

### 5.2 Event Dedup (`{prefix}event_dedup`)

**Purpose:** Slack event deduplication (idempotency).  
**Document ID:** `event_id` (Slack event ID)  
**Code Reference:** `src/adapters/firestore_dedup_store.py`

```json
{
  "created_at": 1234567890.0 // TTL managed by Firestore policy
}
```

---

## 6. Email Indexing Collections

### 6.1 Indexed Email Archive (`{prefix}domain_email_facts_v1`)

**Purpose:** Indexed email facts — mirrors FactEntity structure to enable identical RRF search.
**Document ID:** `email_id` (idempotent upsert on retry)
**Code Reference:** `src/domain/email.py` → `IndexedEmail`

```json
{
  "email_id": "gmail_msg_123",     // = Firestore document ID
  "user_id": "user_1",
  "account_id": "account-550e8400...",
  "source": "gmail",

  // Fact sentence (extracted by EmailClassificationAgent)
  "text": "Booked flight to Berlin for April 12, booking ref XYZABC",
  "vector": [0.12, -0.45, ...],           // embed(text)
  "tags_vector": [0.08, -0.32, ...],      // embed(tags joined)
  "metadata_vector": [0.15, -0.28, ...],  // embed(structured values: amounts, dates, refs)
  "attachments_vector": null,             // embed(attachment filenames); null if no attachments

  // Classification
  "tags": ["travel", "flight"],
  "category": "travel",
  "valuable_type": "confirmed_event",    // "confirmed_event" | "biographical_signal"
  "metadata": {
    "subject": "Your booking confirmation: XYZABC",
    "from_address": "noreply@airline.com",
    "snippet": "Booking confirmed..."
    // + structured entities extracted by classifier
  },

  // Email-specific top-level fields
  "subject": "Your booking confirmation: XYZABC",
  "from_address": "noreply@airline.com",
  "email_date": "timestamp",        // original email date
  "attachments": ["boarding_pass.pdf"],

  // Lifecycle
  "state": "current",               // "current" | "archived"
  "indexed_at": "timestamp",
  "embedding_pending": false,       // true if vectors not yet computed
  "consolidated_at": null           // set when batch sent to ConsolidationAgent
}
```

**Indexes:**
- `account_id` ASC, `state` ASC, `vector` VECTOR 768 — semantic search
- `account_id` ASC, `state` ASC, `tags_vector` VECTOR 768 — tag-based search
- `account_id` ASC, `state` ASC, `metadata_vector` VECTOR 768 — structured entity search
- `account_id` ASC, `state` ASC, `attachments_vector` VECTOR 768 — attachment search

---

### 6.2 OAuth Credentials (`{prefix}oauth_credentials`)

**Purpose:** Gmail OAuth tokens per user.
**Document ID:** `{user_id}_{provider}` (e.g., `user_1_gmail`)
**Code Reference:** `src/domain/email.py` → `OAuthCredentials`, `src/adapters/firestore_oauth_credentials_adapter.py`

```json
{
  "user_id": "user_1",
  "provider": "gmail",
  "access_token": "...",
  "refresh_token": "...",
  "token_expiry": "timestamp",
  "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
  "email_address": "user@gmail.com"   // provider account email (display only)
}
```

---

### 6.3 Email Indexing State (`{prefix}email_indexing_state`)

**Purpose:** Cursor tracking per user per provider.
**Document ID:** `{user_id}_{provider}`
**Code Reference:** `src/domain/email.py` → `IndexingState`

```json
{
  "user_id": "user_1",
  "provider": "gmail",
  "indexed_through": "timestamp",          // incremental — newest email date seen
  "oldest_indexed_through": "timestamp",   // backfill — oldest date queried
  "cursor_reindex": "timestamp"            // reindex — oldest date queried (~now-3yr)
}
```

**Cursor semantics:** `indexed_through` written ONLY at job completion using the max email date
seen across all pages. Incremental bootstrap: `date_from = max(oldest_indexed_through, cursor_reindex)`.

---

### 6.4 Email Indexing Jobs (`{prefix}email_indexing_jobs_v1`)

**Purpose:** One record per indexing run. Used for resume-on-retry, Cabinet job history, error reporting.
**Document ID:** `job_id` (UUID)
**Code Reference:** `src/domain/email.py` → `IndexingJob`

```json
{
  "job_id": "job_abc123",
  "user_id": "user_1",
  "account_id": "account-550e8400...",
  "provider": "gmail",
  "triggered_by": "cabinet",             // "cabinet" | "scheduler" | "script"
  "status": "running",                   // "running"|"completed"|"failed"|"failed_auth"
  "mode": "incremental",                 // "incremental" | "reindex" | "backfill"
  "next_page_token": null,               // primary resume cursor; null = job complete
  "last_email_date": "timestamp",        // fallback cursor if page token expired
  "backfill_until": null,                // stop date for backfill
  "max_email_date": "timestamp",         // newest email date seen across all pages
  "min_email_date": "timestamp",         // oldest email date seen across all pages
  "emails_fetched": 150,
  "emails_stored": 42,
  "emails_failed": 3,
  "embedding_pending": 42,
  "errors": [],                          // capped at 100: {email_id, stage, error}
  "started_at": "timestamp",
  "updated_at": "timestamp",
  "completed_at": null
}
```

---

### 6.5 Email Exclusions (`{prefix}email_exclusions`)

**Purpose:** Sender/domain/subject patterns to skip before LLM classification.
**Document ID:** `exclusion_id` (auto-generated by adapter)
**Code Reference:** `src/domain/email.py` → `EmailExclusion`

```json
{
  "exclusion_id": "excl_abc",
  "user_id": "user_1",
  "pattern_type": "sender_email",    // "sender_email" | "sender_domain" | "subject_pattern"
  "pattern": "noreply@newsletter.com",
  "reason": "recurring low-value sender (auto-detected)",
  "created_at": "timestamp"
}
```

---

### 6.6 User Notification State (`{prefix}user_notification_state`)

**Purpose:** Last active messaging channel per user for background alert delivery.
**Document ID:** `user_id`
**Code Reference:** `src/domain/notification.py` → `NotificationChannel`, `src/adapters/firestore_notification_state_adapter.py`

```json
{
  "user_id": "user_1",
  "platform": "slack",          // "slack" | "telegram"
  "channel_id": "D123456",      // Slack channel_id or str(Telegram chat_id)
  "updated_at": "timestamp"
}
```

Updated on every message received via `ConversationHandler` → `UserNotificationService`.

---

## 7. Deprecated / Legacy Collections

| Collection             | Status           | Note                                                |
| :--------------------- | :--------------- | :-------------------------------------------------- |
| `observations{suffix}` | ⚠️ **DEAD CODE** | Defined in repo but `add_observation` never called. |
| `observations_archive` | ⚠️ **DEAD CODE** | Never populated.                                    |
| `users` (no suffix)    | 🛑 **MIGRATED**  | Use `users_oauth` in Dev/Test.                      |
| `accounts` (no suffix) | 🛑 **MIGRATED**  | Use `accounts_oauth` in Dev/Test.                   |
| `facts` (no suffix)    | 🛑 **MIGRATED**  | Use `facts_oauth` in Dev/Test.                      |

---

## 7. Firestore Indexes (us-production)

**Database:** `us-production` | **Region:** us-central1
**Defined in:** `config/firestore.indexes.json`
**Verified via:** `gcloud firestore indexes composite list --database=us-production`
**Last audited:** 2026-02-18

> **Collection naming reminder:** `development_` prefix = dev environment, no prefix = production. Same physical `us-production` database, isolated by collection name.

---

### 7.1 Vector Search Indexes — `development_domain_facts_v2` / `domain_facts_v2`

Used by `SearchEnrichmentService` and `FirestoreFactRepository.search_facts()`. Each filter field must be included in the index for Firestore to accept the query.

| Fields | Used by | Purpose | Dev | Prod |
| :----- | :------ | :------ | :-- | :--- |
| `account_id` ASC, `state` ASC, `vector` VECTOR 768 | `search_facts(vector_field="vector")` | Semantic search on fact text (main channel) | ✅ | ⚠️ missing¹ |
| `account_id` ASC, `state` ASC, `tags_vector` VECTOR 768 | `search_facts(vector_field="tags_vector")` | Semantic search on tags — best for domain/category queries | ✅ | ⚠️ missing¹ |
| `account_id` ASC, `state` ASC, `metadata_vector` VECTOR 768 | `search_facts(vector_field="metadata_vector")` | Semantic search on structured metadata | ✅ | ⚠️ missing¹ |
| `account_id` ASC, `is_current` ASC, `vector` VECTOR 768 | — | LEGACY: pre-migration field `is_current` | ✅ | ✅ |
| `account_id` ASC, `is_current` ASC, `tags_vector` VECTOR 768 | — | LEGACY: pre-migration field `is_current` | ✅ | ✅ |
| `account_id` ASC, `is_current` ASC, `metadata_vector` VECTOR 768 | — | LEGACY: pre-migration field `is_current` | ✅ | ✅ |

> ¹ Production collection `domain_facts_v2` only has `is_current`-based vector indexes. Needs migration to `state`-based indexes when `is_current` field is fully retired.

---

### 7.2 Lineage Index — `development_domain_facts_v2` / `domain_facts_v2`

| Fields | Used by | Purpose | Dev | Prod |
| :----- | :------ | :------ | :-- | :--- |
| `lineage_id` ASC, `created_at` DESC | `get_lineage()`, `get_latest_fact_by_lineage()` | Retrieve full SCD2 version history of a fact. ORDER BY created_at DESC = latest version first. | ✅ | ✅ |

---

### 7.3 Domain Routing Indexes — `development_domain_facts_v2`

Used by `SearchEnrichmentService.build_enriched_context()` for router enrichment — fetching ALL current facts in specific domains (not a vector search, a direct WHERE query).

| Fields | Used by | Purpose | Dev | Prod |
| :----- | :------ | :------ | :-- | :--- |
| `account_id` ASC, `domain` ASC, `state` ASC, `created_at` ASC | `search_facts_by_domain()` | Retrieve all current facts in a domain set (e.g. `["health", "medical_records"]`) for router context injection | ✅ | ⚠️ missing |
| `account_id` ASC, `domain` ASC, `created_at` ASC, `state` ASC | `search_facts_by_domain()` | Variant with different field order (Firestore requires matching order) | ✅ | ⚠️ missing |
| `account_id` ASC, `domain` ASC, `created_at` ASC | — | Older variant, superseded by state-filtered queries | ✅ | — |

---

### 7.4 Basic Filter Index — `development_domain_facts_v2`

| Fields | Used by | Purpose | Dev | Prod |
| :----- | :------ | :------ | :-- | :--- |
| `account_id` ASC, `state` ASC | `get_active_facts()` (no ORDER BY) | Simple multi-tenant filter: all current facts for an account without ordering. Used by biographical context refresh. | ✅ | ⚠️ missing |

---

### 7.5 Pagination Indexes — `development_domain_facts_v2` / `domain_facts_v2`

Added 2026-02-18 for User Cabinet facts browser (`GET /api/user/facts/browse`). Cursor-based pagination requires `ORDER BY created_at DESC` + `__name__ DESC` as tiebreaker.

| Fields | Used by | Purpose | Dev | Prod |
| :----- | :------ | :------ | :-- | :--- |
| `account_id` ASC, `state` ASC, `created_at` DESC, `__name__` DESC | `get_paginated_facts()` (no domain filter) | Browse all current facts, newest first. `__name__` DESC is Firestore's required tiebreaker for cursor pagination. | ✅ | ⚠️ pending deploy |
| `account_id` ASC, `state` ASC, `domain` ASC, `created_at` DESC, `__name__` DESC | `get_paginated_facts(domain=...)` | Same but filtered by domain chip (e.g. `?domain=health`). | ✅ | ⚠️ pending deploy |

---

### 7.6 Session Indexes — `sessions` / `development_sessions`

| Collection | Fields | Used by | Purpose | Status |
| :--------- | :----- | :------ | :------ | :----- |
| `sessions` | `owner_id` ASC, `last_activity` DESC | `SessionStore.get_session()` | Latest active session for a user. ORDER BY DESC = most recently active first. | ✅ READY |
| `development_sessions` | `owner_id` ASC, `last_activity` DESC | Same (dev) | Dev environment equivalent | ✅ READY |

---

### 7.7 Legacy Indexes (Deprecated)

| Collection | Fields | Status | Note |
| :--------- | :----- | :----- | :--- |
| `facts` (old) | `owner_id` ASC, `is_current` ASC, `vector` VECTOR | 🛑 LEGACY | Pre-migration collection. Use `domain_facts_v2`. |
| `facts` (old) | `account_id` ASC, `is_current` ASC, `vector` VECTOR | 🛑 LEGACY | Pre-migration collection. Use `domain_facts_v2`. |

---

### 7.8 Index Coverage Summary

| Query pattern | Index type | Missing in prod? |
| :------------ | :--------- | :--------------- |
| Vector search (`state`-filtered) | Vector | ⚠️ Yes — production uses `is_current` indexes only |
| Domain routing (`WHERE domain IN [...]`) | Composite | ⚠️ Yes |
| Cursor pagination (`ORDER BY created_at DESC`) | Composite | ⚠️ Yes — pending next deploy |
| Biographical context (no ORDER BY) | Composite | ⚠️ Yes |
| Lineage history | Composite | ✅ |
| Session lookup | Composite | ✅ |

**Action required:** Production `domain_facts_v2` needs state-based indexes deployed. Track in `config/firestore.indexes.json` — run `deploy_firestore_indexes.py` or `make deploy-prod`.

---

**Migration reference:** `docs/SESSION_2026_02_09_US_PRODUCTION_MIGRATION.md`
