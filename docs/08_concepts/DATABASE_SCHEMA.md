# Firestore Collections

**Last Updated:** 2026-06-01
schemas live in the code (the domain model named in each row), and the building-block maps go deeper for memory and the
drive. This page says where to look, so it does not copy field lists that would drift.

---


## 1. Database and naming

- **One named database, `us-production`.** `EnvironmentConfig.firestore_database_id` reads `FIRESTORE_DATABASE` and
defaults to it. The `(default)` database is not used. Every Firestore client passes `database=` (scripts use
`scripts/_firestore.py`), and an AST test enforces it.
- **Name = `{prefix}{name}`.** The prefix comes from `APP_ENV` (`EnvironmentConfig.firestore_collection_prefix`):
`development_` for development, `test_` for test, and empty for production.

---

decision. See `../04_solution_strategy/decisions/collection_prefix_retained.md` and

- **Domain collections carry a schema version** (`domain_users_v2`, `domain_facts_v2`, `domain_prompt_*_v3`, …).  
Infrastructure collections do not (`sessions`, `event_dedup`, …).  
- **The names are defined in `src/config/environment.py`** (`EnvironmentConfig.*_collection`). The exceptions are four

`prompt_components`.

## 2. Indexes and TTL

```bash
An index that a new query needs must be `READY` before the code that runs the query is deployed. A missing index
fails the query at run time, not at deploy time.

`../07_deployment/README.md`. A TTL policy deletes any document in that group whose `expires_at` has passed. So a
collection whose documents must not expire never names a field `expires_at`: the memory-write lease uses
`lease_until`. The facts collection never gets a TTL policy, and a test scans the tree for one.
```




### 3.1 Identity, access and billing

| Collection | Holds | Doc id | Model / code |

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

| `{p}domain_accounts_v2` | tenant: tier, IAM policy, account defaults, usage counters, cost alert limit | `account_id` | `BillingAccount`, `src/domain/billing.py` |

| `{p}domain_invite_codes_v1` | invite and self-link codes | the code | `InviteCode`, `src/domain/invite_code.py` |

| `{p}channel_bindings` | a channel bound to an agent (companion channels) | `channel_id` | `ChannelBinding`, `src/domain/channel_binding.py` |

Account usage counters written before 2026-07-28 are inflated. For historical cost, use BigQuery `prompt_content`, not


| :--------- | :-------------------------------- | :-------------------- | :-------------------- |

| Collection | Holds | Doc id | Model / code |
| :--- | :--- | :--- | :--- |
| `{p}domain_facts_v2` | long-term facts: one document per fact **version**, three 768-dim vectors | fact id | `FactEntity`, `src/domain/entities.py` |
| `{p}user_context` | cached biographical context: facts, principles, directives | owner (account) id | `FirestoreFactRepository`, `src/adapters/firestore_repo.py` |
| `{p}memory_write_leases` | one memory-writing run per account at a time | `account_id` | `src/adapters/firestore_memory_write_lease.py` |
| `{p}memory_fact_drafts` | facts proposed by Smart and waiting for `$memory save` | `{user_id}:{code}` | `src/adapters/firestore_memory_fact_draft_repository.py` |
| `{p}fact_lifecycle_daily` | fact-lifecycle numbers per account and UTC day | `{account_id}:{YYYY-MM-DD}` | `src/config/environment.py` |
| `{p}fact_lifecycle_lines` | one mark per UTC day: the daily ops line was claimed | `{YYYY-MM-DD}` | `src/config/environment.py` |
| `{p}fact_review_questions` | memory-review questions, one per fact | question id | `src/adapters/firestore_fact_review_store.py` |
| `{p}fact_review_messages` | memory-review message claims (the weekly digest) | `{user_id}:digest:{iso_week}` | same |
| `{p}fact_review_state` | memory-review state per account | `account_id` | same |
| `{p}domain_email_facts_v1` | valuable emails as facts, four vectors, the same search path as facts | `{user_id}_{email_id}` | `IndexedEmail`, `src/domain/email.py` |

What the fact fields mean (versions, `lineage_id`, `state`, expiry) is explained in the memory map,
`../05_building_blocks/memory/README.md`.

### 3.3 Conversation and background work

| Collection | Holds | Doc id | Model / code |
| :--- | :--- | :--- | :--- |
| `{p}sessions` | per-channel chat history (sliding window, at most 200 messages, 90-day `expires_at`) | `session_id` = `{user_id}:{channel_id}` | `Session`, `src/domain/session.py`; `src/adapters/firestore_session_store.py` |

---

| `{p}event_dedup` | Slack/Telegram event idempotency (TTL on `expires_at`) | platform event id | `src/adapters/firestore_dedup_store.py` |

| `{p}long_turns` | chat turns that outlived the 90 s mark | `turn_id` | `src/adapters/firestore_long_turn_registry.py` |

| `{p}user_primary_channel` | the user's primary channel | `user_id` | `src/adapters/firestore_notification_state_adapter.py` |  
| `{p}short_links` | short code to URL mappings (TTL on `expires_at`, re-checked on read) | the code | `src/adapters/firestore_short_link_repository.py` |  


```json
{
  | :--- | :--- | :--- | :--- |
  | `{p}domain_prompt_tokens_v3_system` | system prompt tokens (looked up first) | `token_id` | `src/domain/prompt_v3/token.py` |
  | `{p}domain_prompt_tokens_v3_user` | user tokens (looked up when the system collection has no match) | `token_id` | `src/adapters/prompt_v3/firestore_token_repository.py` |
  "email": "user@example.com",
  | `{p}domain_prompt_profiles_v3` | agent profiles: the blueprint and the tokens of one agent | `agent_id` | `src/domain/prompt_v3/agent_profile.py` |

  | `{p}prompt_components` | the v2 prompt component store, still wired in `ServiceContainer` | auto id | `src/adapters/firestore_prompt_repository.py` |
  
    "slack": "U123456",
    
  },

  | Collection | Holds | Doc id | Model / code |
  | :--- | :--- | :--- | :--- |
    | `{p}email_indexing_state` | indexing cursor per user and provider | `{user_id}_{provider}` | `IndexingState`, `src/domain/email.py` |
    | `{p}email_indexing_jobs_v1` | one record per indexing run (resume, Cabinet history) | `job_id` | `IndexingJob`, `src/domain/email.py` |
  },

  | `{p}task_config` | primary list and Graph webhook subscriptions per user | `user_id` | `TaskUserConfig`, `src/domain/task.py` |
  "config": {
    | `{p}skills` | a user's custom skills (`versions/v<n>` and `files/{sha256}` below each) | `{user_id}:{name}` | `src/adapters/firestore_skill_repository.py` |
    | `{p}skill_drafts` | skill drafts waiting for `$skill save` | `{user_id}:{code}` | `src/adapters/firestore_skill_repository.py` |
    
    ### 3.6 Voice, companions and the MCP server
      
    },
    | :--- | :--- | :--- | :--- |
      "router": "eco",
      | `{p}companion_records` | companion memory records, scoped by session | record id | `src/adapters/firestore_companion_memory_repository.py` |
    },
    | `{p}mcp_oauth_clients` | MCP OAuth clients (dynamic registration) | `client_id` | `src/adapters/firestore_mcp_client_repository.py` |
      | `{p}mcp_auth_codes` | short-lived MCP authorization codes | the code | same |
    },
    

    
    All are written by `FirestoreDriveIndexRepository` (`src/adapters/firestore_drive_index_repo.py`). Their meaning,
      "language": "uk",
      
      | Collection | Holds |
      | :--- | :--- |
      | `{p}drive_index_records` | one record per drive item (doc id `{user_id}:{item_id}`), with a card vector and no stored path |
    },

    // Features
    | `{p}drive_model_usage` | model spend per class and month (a purge keeps it) |
    | `{p}drive_transcript_jobs` | transcript jobs, one per media file in flight; window results in `{p}drive_transcript_windows` below each |
    | `{p}drive_transcript_batches` | provider batches, one per submitted batch |
    | `{p}drive_transcripts` | the transcript store under the content key; large ones are split into `{p}drive_transcript_parts` |

    | `{p}drive_transcript_mirror` | the mirror's ledger, one document per mirrored file and folder; no purge deletes it |
    
    The two subcollections carry the prefix too, because Firestore collection-group ids are not scoped by parent.
    

    
    ## 4. Adding a collection
  },

  that says what the collection holds and its document id.
  2. Declare every index it needs in `config/firestore.indexes.json`, and have it `READY` before the code ships.
  3. Decide on expiry. Only a field that a TTL policy should act on is named `expires_at`, and the policy is added in the
}
```






---



  
  


```json
{
  
  

  
  
    
    
    
  },

  
  
  
    
    All are written by `FirestoreDriveIndexRepository` (`src/adapters/firestore_drive_index_repo.py`). Their meaning,
      "language": "en"
    },
    
    
    
  },

  
  "usage": {
    
    
    

    
    
    

    
    

    
    
    
  },

  
  

  that says what the collection holds and its document id.
  2. Declare every index it needs in `config/firestore.indexes.json`, and have it `READY` before the code ships.
  3. Decide on expiry. Only a field that a TTL policy should act on is named `expires_at`, and the policy is added in the
}
```





---





  
  


```json
{
  

  
    // Owner (Tenant)
  

  // Content
  
  
  
  
  
  

  
  

  
  "metadata": {
    
    "confidence": 0.95
  },
  "lineage_id": "lineage_abc...",       // Links versions of same fact

  
  that says what the collection holds and its document id.
  
  "valid_to": null,                     // null = current truth
  "is_current": true,

  
  // Written by FirestoreFactRepository on every add_fact/update_fact.
  
  
}
```









- **Vector Search (Main):** `account_id` + `is_current` + `vector` (768-dim, COSINE)
- **Vector Search (Tags):** `account_id` + `is_current` + `tags_vector` (768-dim, COSINE)  
  
- **Vector Search (Metadata):** `account_id` + `is_current` + `metadata_vector` (768-dim, COSINE)  
  
- **Lineage History:** `lineage_id` + `created_at` (DESC)




---



  
  
  


```json
{
  
  

  
  
  "history": [
    {
      "role": "user",
      "parts": [
        
      ],
      
    },
    {
      "role": "model",
      "parts": [
        {
          "text": "Hi! 👋",              // summary (≤300 chars) when ENABLE_HISTORY_OPTIMIZATION=true; full text otherwise
          
        }
      ],
      
    }
  ],

  that says what the collection holds and its document id.
  2. Declare every index it needs in `config/firestore.indexes.json`, and have it `READY` before the code ships.
  
  
}
```






- **Dual-field model response:** `text` = compressed summary (when `ENABLE_HISTORY_OPTIMIZATION=true`) or full text (when false). `full_text` = complete response, always stored. On history load, SmartAgent applies tiered logic: last `history_recent_full_turns` model messages use `full_text`, older ones use `text`. Backward compatible — old records without `full_text` fall back to `text`.






---



  
  


```json
{
  "biographical_facts": [
    {
      "text": "Software engineer, lives in Kyiv...",
      "type": "event",
      
      
    }
  ],
  
  
  "version": 3,
  
}
```

---











  
  


```json
{
  "token_id": "HUMOR_PRESET_RANEVSKAYA",
  
  
  
  "metadata": {
    "version": "1.0",
    "author": "system",
    "description": "Ranevskaya humor style",
    
  },
  that says what the collection holds and its document id.
  
}
```



  







---



  
  
  


```json
{
  
  
  "classes": {
    "HUMOR_ENGINE": {
      "allowed_token_categories": ["humor_engine"],
      
      "default_token": "HUMOR_PRESET_RANEVSKAYA"
    },
    "VOICE": {
      
      
      
    }
  }
}
```

---



  
  


```json
{
  
  "owner_type": "SYSTEM",
  "owner_value": "smart",
  "slots": [
    {
      "type": "token",
      
      "non_overridable": false
    }
  ]
}
```



  
  









---





  
  


```json
{
  
  
  
  
  
  "attempts": 0,
  
  
  .0,
  
}
```








```json
{
  .0,
  
}
```

---









```json
{
  
  
  
  

  
  
  
  
  
  

  
  "tags": ["travel", "flight"],
  "category": "travel",
  
  "metadata": {
    
    
    
    
  },

  
  
  
  
  

  
  
  
  
  
}
```







---







```json
{
  
  "provider": "gmail",
  
  
  
  
  
}
```

---







```json
{
  
  "provider": "gmail",
  
  
  
}
```




---







```json
{
  
  
  
  "provider": "gmail",
  
  
  
  
  
  
  
  
  
  
  
  
  
  
  2. Declare every index it needs in `config/firestore.indexes.json`, and have it `READY` before the code ships.
  
}
```

---







```json
{
  
  
  
  
  
  
}
```

---







```json
{
  
  
  
  
}
```



---








| :--------------------- | :-------------------- | :-------------------------------------------------- |
| `observations`         | 🗑️ **DELETED**        | Was dead code (`add_observation` never called).     |



---










---



Used by `SearchEnrichmentService` and `FirestoreFactRepository.search_facts()`. Each filter field must be included in the index for Firestore to accept the query.


| :----- | :------ | :------ | :-- | :--- |









---




| :----- | :------ | :------ | :-- | :--- |
| `lineage_id` ASC, `created_at` DESC | `get_lineage()`, `get_latest_fact_by_lineage()` | Retrieve full SCD2 version history of a fact. ORDER BY created_at DESC = latest version first. | ✅ | ✅ |

---






| :----- | :------ | :------ | :-- | :--- |
| `account_id` ASC, `domain` ASC, `state` ASC, `created_at` ASC | `search_facts_by_domain()` | Retrieve all current facts in a domain set (e.g. `["health", "medical_records"]`) for router context injection | ✅ | ⚠️ missing |
| `account_id` ASC, `domain` ASC, `created_at` ASC, `state` ASC | `search_facts_by_domain()` | Variant with different field order (Firestore requires matching order) | ✅ | ⚠️ missing |


---




| :----- | :------ | :------ | :-- | :--- |


---






| :----- | :------ | :------ | :-- | :--- |



---




| :--------- | :----- | :------ | :------ | :----- |



---




| :--------- | :----- | :----- | :--- |



---




| :------------ | :--------- | :--------------- |









---









```json
{
  
  
  
  
  "title": "Buy milk",
  
  
  
  
  
  
  
}
```













---







```json
{
  
  "subscriptions": [
    {
      
      
      
    }
  ]
}
```





---







```json
{
  
  
  "text": "Send Valencia morning news", // short display label ≤15 words
  "instruction": "The user asked for a daily morning news briefing about Valencia...", // full execution context, no limit
  
  
    
    
  },
  
  
}
```






|-----------|-------|--------|













---

**Migration reference:** `docs/SESSION_2026_02_09_US_PRODUCTION_MIGRATION.md`
