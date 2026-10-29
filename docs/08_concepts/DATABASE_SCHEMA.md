# Firestore Collections

A map of the Firestore collections: what each one holds, its document id, and where its shape is defined. Field-level
schemas live in the code (the domain model named in each row), and the building-block maps go deeper for memory and the
drive. This page says where to look, so it does not copy field lists that would drift.

---

## 1. Database and naming

- **One named database, `us-production`.** `EnvironmentConfig.firestore_database_id` reads `FIRESTORE_DATABASE` and
  defaults to it. The `(default)` database is not used. Every Firestore client passes `database=` (scripts use
  `scripts/_firestore.py`), and an AST test enforces it.
- **Name = `{prefix}{name}`.** The prefix comes from `APP_ENV` (`EnvironmentConfig.firestore_collection_prefix`):
  `development_` for development, `test_` for test, and empty for production.
- **There is one live environment: the `development_` collections.** The empty-prefix production path is a kept
  mechanism, not a second live dataset. The unprefixed collections were deleted, and the live ones keep their prefix by
  decision. See `../04_solution_strategy/decisions/collection_prefix_retained.md` and
  `../04_solution_strategy/decisions/dead_prod_collections_deletion.md`.
- **Domain collections carry a schema version** (`domain_users_v2`, `domain_facts_v2`, `domain_prompt_*_v3`, …).
  Infrastructure collections do not (`sessions`, `event_dedup`, …).
- **The names are defined in `src/config/environment.py`** (`EnvironmentConfig.*_collection`). The exceptions are four
  names their adapters build from the prefix: `channel_bindings`, `short_links`, `user_primary_channel` and
  `prompt_components`.

## 2. Indexes and TTL

- **Composite and vector indexes are declared in `config/firestore.indexes.json`.** That file is the only index list.
  An index that a new query needs must be `READY` before the code that runs the query is deployed. A missing index
  fails the query at run time, not at deploy time.
- **TTL policies are set by hand, per collection group, on the field `expires_at`.** The steps are in
  `../07_deployment/README.md`. A TTL policy deletes any document in that group whose `expires_at` has passed. So a
  collection whose documents must not expire never names a field `expires_at`: the memory-write lease uses
  `lease_until`. The facts collection never gets a TTL policy, and a test scans the tree for one.

## 3. Collections

`{p}` is the prefix. "Model" names the class that defines the stored shape.

### 3.1 Identity, access and billing

| Collection | Holds | Doc id | Model / code |
| :--- | :--- | :--- | :--- |
| `{p}domain_users_v2` | user identity, platform bindings, per-user config (`UserBotConfig`) | `user_id` | `UserProfile`, `src/domain/user.py` |
| `{p}domain_accounts_v2` | tenant: tier, IAM policy, account defaults, usage counters, cost alert limit | `account_id` | `BillingAccount`, `src/domain/billing.py` |
| `{p}domain_whitelist_v1` | the emails and domains allowed in (chat and Cabinet) | `config` (single document) | `src/adapters/firestore_whitelist_repo.py` |
| `{p}domain_invite_codes_v1` | invite and self-link codes | the code | `InviteCode`, `src/domain/invite_code.py` |
| `{p}oauth_credentials` | provider OAuth tokens (Gmail, Microsoft) | `{user_id}_{provider}` | `OAuthCredentials`, `src/domain/email.py` |
| `{p}channel_bindings` | a channel bound to an agent (companion channels) | `channel_id` | `ChannelBinding`, `src/domain/channel_binding.py` |

Account usage counters written before 2026-07-28 are inflated. For historical cost, use BigQuery `prompt_content`, not
`usage.*` (root `CLAUDE.md`, Token accounting).

### 3.2 Memory

| Collection | Holds | Doc id | Model / code |
| :--- | :--- | :--- | :--- |
| `{p}domain_facts_v2` | long-term facts: one document per fact **version**, three 768-dim vectors | fact id | `FactEntity`, `src/domain/entities.py` |
| `{p}user_context` | cached biographical context: facts, principles, directives | owner (account) id | `FirestoreFactRepository`, `src/adapters/firestore_repo.py` |
| `{p}memory_write_leases` | one memory-writing run per account at a time | `account_id` | `src/adapters/firestore_memory_write_lease.py` |
| `{p}memory_fact_drafts` | facts proposed by Smart and waiting for `$memory save` | `{user_id}:{code}` | `src/adapters/firestore_memory_fact_draft_repository.py` |
| `{p}fact_lifecycle_daily` | fact-lifecycle numbers per account and UTC day | `{account_id}:{YYYY-MM-DD}` | `src/config/environment.py` |
| `{p}fact_lifecycle_lines` | one mark per UTC day: the daily ops line was claimed | `{YYYY-MM-DD}` | `src/config/environment.py` |
| `{p}fact_review_questions` | memory-review questions, one per fact | question id | `src/adapters/firestore_fact_review_store.py` |
| `{p}fact_review_messages` | memory-review message claims (the weekly digest) and quote translations (a stored fact's text as shown in a message language) | `{user_id}:digest:{iso_week}`, `{account_id}:quote:{language}:{source_digest}` | same |
| `{p}fact_review_state` | memory-review state per account | `account_id` | same |
| `{p}domain_email_facts_v1` | valuable emails as facts, four vectors, the same search path as facts | `{user_id}_{email_id}` | `IndexedEmail`, `src/domain/email.py` |

What the fact fields mean (versions, `lineage_id`, `state`, expiry) is explained in the memory map,
`../05_building_blocks/memory/README.md`.

### 3.3 Conversation and background work

| Collection | Holds | Doc id | Model / code |
| :--- | :--- | :--- | :--- |
| `{p}sessions` | per-channel chat history (sliding window, at most 200 messages, 90-day `expires_at`) | `session_id` = `{user_id}:{channel_id}` | `Session`, `src/domain/session.py`; `src/adapters/firestore_session_store.py` |
| `{p}consolidation_queue` | session overflow batches waiting for consolidation | `batch_id` | `ConsolidationBatch`, `src/domain/consolidation.py` |
| `{p}companion_extraction_queue` | companion-session batches waiting for the extractor | `batch_id` | `src/adapters/firestore_companion_extraction_queue.py` |
| `{p}event_dedup` | Slack/Telegram event idempotency (TTL on `expires_at`) | platform event id | `src/adapters/firestore_dedup_store.py` |
| `{p}worker_task_dedup` | Cloud Tasks delivery claims, daily-review marks, deep-research webhook claims | task name or mark key | `TaskClaimStore`, `src/adapters/firestore_task_claim_store.py` |
| `{p}long_turns` | chat turns that outlived the 90 s mark | `turn_id` | `src/adapters/firestore_long_turn_registry.py` |
| `{p}user_notification_state` | the user's last active channel, for background notifications | `user_id` | `NotificationChannel`, `src/domain/notification.py` |
| `{p}user_primary_channel` | the user's primary channel | `user_id` | `src/adapters/firestore_notification_state_adapter.py` |
| `{p}short_links` | short code to URL mappings (TTL on `expires_at`, re-checked on read) | the code | `src/adapters/firestore_short_link_repository.py` |

### 3.4 Prompts

| Collection | Holds | Doc id | Model / code |
| :--- | :--- | :--- | :--- |
| `{p}domain_prompt_tokens_v3_system` | system prompt tokens (looked up first) | `token_id` | `src/domain/prompt_v3/token.py` |
| `{p}domain_prompt_tokens_v3_user` | user tokens (looked up when the system collection has no match) | `token_id` | `src/adapters/prompt_v3/firestore_token_repository.py` |
| `{p}domain_prompt_blueprints_v3` | blueprints: outer class and section order | `blueprint_id` | `src/domain/prompt_v3/blueprint.py` |
| `{p}domain_prompt_profiles_v3` | agent profiles: the blueprint and the tokens of one agent | `agent_id` | `src/domain/prompt_v3/agent_profile.py` |
| `{p}domain_prompt_overrides_v3` | account and user token overrides | `{OWNER_TYPE}_{owner_id}` | `src/adapters/prompt_v3/firestore_agent_profile_repository.py` |
| `{p}prompt_components` | the v2 prompt component store, still wired in `ServiceContainer` | auto id | `src/adapters/firestore_prompt_repository.py` |

How the v3 collections combine into a prompt: `prompt_assembly_guide.md`.

### 3.5 Email, tasks, reminders and skills

| Collection | Holds | Doc id | Model / code |
| :--- | :--- | :--- | :--- |
| `{p}email_indexing_state` | indexing cursor per user and provider | `{user_id}_{provider}` | `IndexingState`, `src/domain/email.py` |
| `{p}email_indexing_jobs_v1` | one record per indexing run (resume, Cabinet history) | `job_id` | `IndexingJob`, `src/domain/email.py` |
| `{p}email_exclusions` | sender, domain and subject patterns skipped before classification | `{user_id}_{pattern_type}_{pattern}` | `EmailExclusion`, `src/domain/email.py` |
| `{p}task_search_index` | vectors and display fields for Microsoft To Do tasks (To Do is the source of truth) | `{user_id}_{task_id}` | `TaskSearchEntry`, `src/domain/task.py` |
| `{p}task_config` | primary list and Graph webhook subscriptions per user | `user_id` | `TaskUserConfig`, `src/domain/task.py` |
| `{p}orchestrator_notes` | self-reminders that fire on schedule | epoch milliseconds | `AgentNote`, `src/domain/agent_note.py` |
| `{p}skills` | a user's custom skills (`versions/v<n>` and `files/{sha256}` below each) | `{user_id}:{name}` | `src/adapters/firestore_skill_repository.py` |
| `{p}skill_drafts` | skill drafts waiting for `$skill save` | `{user_id}:{code}` | `src/adapters/firestore_skill_repository.py` |

### 3.6 Voice, companions and the MCP server

| Collection | Holds | Doc id | Model / code |
| :--- | :--- | :--- | :--- |
| `{p}voice_tickets` | call tickets and the one-call-per-user marker (TTL on `expires_at`) | ticket or marker key | `src/adapters/firestore_ephemeral_store.py` |
| `{p}companion_records` | companion memory records, scoped by session | record id | `src/adapters/firestore_companion_memory_repository.py` |
| `{p}companion_context_cache` | cached context summary per companion session | session id | `src/adapters/firestore_companion_cache_repository.py` |
| `{p}mcp_clients` | MCP OAuth clients (dynamic registration); `client_secret` is KMS ciphertext | `client_id` | `src/adapters/firestore_mcp_client_repository.py` |
| `{p}mcp_auth_codes` | short-lived MCP authorization codes | keyed digest of the code | same |
| `{p}mcp_refresh_tokens` | MCP refresh tokens, rotated on use | sha256 of the token | same |

### 3.7 User drive

All are written by `FirestoreDriveIndexRepository` (`src/adapters/firestore_drive_index_repo.py`). Their meaning,
lifecycles and which purge keeps what are in the drive map, `../05_building_blocks/user_drive/README.md`.

| Collection | Holds |
| :--- | :--- |
| `{p}drive_index_records` | one record per drive item (doc id `{user_id}:{item_id}`), with a card vector and no stored path |
| `{p}drive_index_chunks` | text chunks, children of a record |
| `{p}drive_index_hints` | passage hints, one document per chunk that has hints |
| `{p}drive_sync_state` | sync state per user: delta cursor, epoch, lease, applied versions |
| `{p}drive_model_usage` | model spend per class and month (a purge keeps it) |
| `{p}drive_transcript_jobs` | transcript jobs, one per media file in flight; window results in `{p}drive_transcript_windows` below each |
| `{p}drive_transcript_batches` | provider batches, one per submitted batch |
| `{p}drive_transcripts` | the transcript store under the content key; large ones are split into `{p}drive_transcript_parts` |
| `{p}drive_mirror_roots` | the transcript mirror's root per user and drive (`{user_id}:{drive_id}`); no purge deletes it |
| `{p}drive_transcript_mirror` | the mirror's ledger, one document per mirrored file and folder; no purge deletes it |

The two subcollections carry the prefix too, because Firestore collection-group ids are not scoped by parent.

---

## 4. Adding a collection

1. Add a property to `EnvironmentConfig` that returns `f"{self.firestore_collection_prefix}<name>"`, with a docstring
   that says what the collection holds and its document id.
2. Declare every index it needs in `config/firestore.indexes.json`, and have it `READY` before the code ships.
3. Decide on expiry. Only a field that a TTL policy should act on is named `expires_at`, and the policy is added in the
   deployment README.
4. Add a row here.
