<!-- living-doc
covers:
  config/
  scripts/_firestore.py
  src/config/environment.py
-->

# Firestore Collections

Which collection belongs to which subsystem, how its document id is built, and the rules every collection shares
(naming, indexes, expiry). Field lists are not copied here: the model or adapter named in each row defines the shape,
and most adapters state the document layout in their module docstring.

## 1. Database and naming

- **One named database, `us-production`.** `EnvironmentConfig.firestore_database_id` reads `FIRESTORE_DATABASE` and
  defaults to it; the `(default)` database is not used. Every Firestore client passes `database=` (scripts get theirs
  from `scripts/_firestore.py`); `tests/unit/test_firestore_database_explicit.py` enforces it by AST scan.
- **Name = `{prefix}{name}`.** `EnvironmentConfig.firestore_collection_prefix` is empty in production and
  `{env}_` otherwise (`development_`, `test_`).
- **The live data is the `development_` collections.** The empty-prefix production path is a kept mechanism, not a
  second dataset; the live collections keep their prefix by decision
  (`../04_solution_strategy/decisions/collection_prefix_retained.md`,
  `../04_solution_strategy/decisions/dead_prod_collections_deletion.md`).
- **Names live in `src/config/environment.py`** (`EnvironmentConfig.*_collection`). Exceptions: three adapters build
  their name from the prefix themselves, with no property (`channel_bindings`, `short_links`, `user_primary_channel`),
  and the prompt token collections get their `_system` / `_user` suffix in `src/composition/prompt_assembly_setup.py`
  (the property returns the base `domain_prompt_tokens_v3`).
- **Only two subcollections are prefixed** (`drive_transcript_windows`, `drive_transcript_parts`, from
  `EnvironmentConfig`), because Firestore collection-group ids are not scoped by parent. The others are unprefixed
  literals: the skills' `versions` and `files`, skill drafts' `draft_files`, drive notes' `versions`.
  **`draft_files` is a collection group with a TTL policy on `expires_at` across the whole database** (registry entry
  with `prefixed: false`): a subcollection named `draft_files` anywhere, in any environment, expires.
- **Schema versions** appear in the names of the domain collections (`domain_users_v2`, `domain_facts_v2`,
  `domain_prompt_*_v3`, `invite_codes_v2`); infrastructure collections have none. A replaced collection is never
  migrated in place and never read again: `oauth_credentials` (replaced by `credentials`) and `mcp_oauth_clients`
  (replaced by `mcp_clients`) stay in the database unread
  (`../04_solution_strategy/decisions/credential_store_cutover.md`,
  `../04_solution_strategy/decisions/secrets_at_rest_cipher_and_digest.md`).
- **Secret-bearing ids are digests, never the raw value.** Invite codes, MCP authorization codes, `$geo` links, connect
  flows and drive download links use the keyed digest of the secret as document id
  (`../04_solution_strategy/decisions/secrets_at_rest_cipher_and_digest.md`); an MCP refresh token uses its sha256.
  Integration credentials and MCP client secrets are one `CipherPort` ciphertext.

## 2. Indexes and expiry

- **Composite and vector indexes are declared in `config/firestore.indexes.json`**, the only index list. An index a
  new query needs must be `READY` before the code that runs the query is deployed: a missing index fails at query
  time, not at deploy time.
- **Two expiry field names carry meaning.** A Firestore TTL policy deletes every document whose policy field has
  passed, and policies are set on `expires_at` or `purge_after`. A collection whose documents must not be deleted gets no policy, and the janitor's state uses other names (the
  memory-write lease uses `lease_until`). Some collections still write `expires_at` with no policy on them
  (`sessions`, `invite_codes_v2`, `task_config`, `drive_links`): the field is checked in code, and the registry
  says `never`.
- **`config/ttl_registry.json` lists every writer of `expires_at` or `purge_after`** and what deletes the document
  (`deleted_by`: a TTL policy, the janitor, a provider, nobody, or not yet verified). The policy runbook in `../07_deployment/README.md` is generated from
  it by `scripts/generate_ttl_runbook.py`; `tests/unit/test_ttl_registry.py` fails when a writer is not registered or
  the runbook is stale.
- **Never a TTL policy on the facts collection** or on the janitor's collections (`fact_lifecycle_*`,
  `memory_write_leases`, `fact_review_state`): a fact's expiry is a decision, the janitor stales the fact and never
  deletes it. `tests/unit/test_facts_have_no_ttl_policy.py` scans the tree for such a command.
- **`sessions` writes `expires_at` but has no policy:** expiry is a lazy check in
  `FirestoreSessionStore.load_session` (`FirestoreSessionStore.ttl_hours`). Do not add a policy on the belief that the field is live.

## 3. Collections

`{p}` is the prefix. Model / code names the class or adapter that defines the stored shape.

### 3.1 Identity, access and billing

| Collection | Doc id | Model / code |
| :--- | :--- | :--- |
| `{p}domain_users_v2` | `user_id` | `UserProfile`, `src/domain/user.py` |
| `{p}domain_accounts_v2` | `account_id` | `BillingAccount`, `src/domain/billing.py` |
| `{p}domain_whitelist_v1` | `config` (one document: the emails and domains allowed in, chat and Cabinet) | `src/adapters/firestore_whitelist_repo.py` |
| `{p}invite_codes_v2` | keyed digest of the normalised code | `InviteCode`, `src/domain/invite_code.py`; `src/adapters/firestore_invite_code_repo.py` |
| `{p}credentials` | `{user_id}:{credential_id}` | `OAuthCredential`, `src/domain/oauth_credential.py`; `FirestoreCredentialStore` |
| `{p}oauth_flows` | `oauth_flow:<keyed digest of the state>`; single use | `src/services/oauth_connect_service.py` |
| `{p}auth_device_sessions_v1` (a Cabinet device session; expires on `purge_after`; stores the SHA-256 of the refresh secret, never the secret) | the random `sid` | `DeviceSession`, `src/domain/device_session.py`; `src/adapters/firestore_device_session_repo.py` |
| `{p}channel_bindings` | `channel_id` | `ChannelBinding`, `src/domain/channel_binding.py` |
| `{p}auth_device_sessions_v1` | the random `sid` | `DeviceSession`, `src/domain/device_session.py`; `src/adapters/firestore_device_session_repo.py`; `../10_rfcs/CABINET_DEVICE_SESSIONS_RFC.md` §7.1 |
| `{p}auth_passkeys_v1` | SHA-256 hex of the credential id | `Passkey`, `src/domain/passkey.py`; same adapter (its `revoke_all` covers sessions and passkeys in one transaction); RFC §7.3 |
| `{p}auth_passkey_challenges` | `passkey_challenge:<SHA-256 of the nonce>`; single use, 5 minutes | `src/services/passkey_service.py` |
| `{p}facts_pin_v1` (an opt-in facts PIN; keyed digests of the PIN and the duress code, never a code; no TTL) | `user_id` | `FactsPin`, `src/domain/facts_pin.py`; `src/adapters/firestore_facts_pin_repo.py` (its attempt transaction also writes `is_active` and `inactive_reason` on the user's `{p}domain_users_v2` document); RFC §7.7, §7.8 |

Old account usage counters are unreliable; for historical cost use BigQuery `prompt_content`, not `usage.*` (`../04_solution_strategy/decisions/billing_execution_scoped_ledger.md`).

### 3.2 Memory

What the fact fields mean (versions, `lineage_id`, `state`, expiry) is in `../05_building_blocks/memory/README.md`.

| Collection | Doc id | Model / code |
| :--- | :--- | :--- |
| `{p}domain_facts_v2` (one document per fact **version**) | fact id | `FactEntity`, `src/domain/entities.py` |
| `{p}user_context` (cached biographical context) | account id | `FirestoreFactRepository`, `src/adapters/firestore_repo.py` |
| `{p}memory_write_leases` | `account_id` | `src/adapters/firestore_memory_write_lease.py` |
| `{p}memory_fact_drafts` (awaiting `$memory save`) | `{user_id}:{code}` | `src/adapters/firestore_memory_fact_draft_repository.py` |
| `{p}fact_lifecycle_daily` | `{account_id}:{YYYY-MM-DD}` | `EnvironmentConfig.fact_lifecycle_daily_collection` |
| `{p}fact_lifecycle_lines` | `{YYYY-MM-DD}` | `EnvironmentConfig.fact_lifecycle_lines_collection` |
| `{p}fact_lifecycle_run` (the janitor's run guard) | `sweep` | `EnvironmentConfig.fact_lifecycle_run_collection` |
| `{p}fact_review_questions` | question id | `src/adapters/firestore_fact_review_store.py` |
| `{p}fact_review_messages` | `{user_id}:digest:{iso_week}`, `{account_id}:quote:{language}:{source_digest}` | same |
| `{p}fact_review_state` | `account_id` | same |
| `{p}person_cards` (retrieval index, never a fact) | `{account_id}:{card_key}` | `PersonCard`, `src/domain/person_cards.py` |
| `{p}confirmed_people` | `account_id` | `ConfirmedPeople`, `src/domain/confirmed_people.py` |
| `{p}profile_answers` | `account_id` | `ProfileAnswers`, `src/domain/profile_answers.py` |
| `{p}domain_email_facts_v1` (indexed emails, searched like facts) | `{user_id}_{email_id}` | `IndexedEmail`, `src/domain/email.py`; `src/adapters/firestore_indexed_email_repo.py` |

### 3.3 Conversation and background work

| Collection | Doc id | Model / code |
| :--- | :--- | :--- |
| `{p}sessions` | `session_id` | `Session`, `src/domain/session.py`; `FirestoreSessionStore` |
| `{p}consolidation_queue` | `batch_id` | `ConsolidationBatch`, `src/domain/consolidation.py` |
| `{p}companion_extraction_queue` (keyed by session, not user) | `batch_id` | `src/adapters/firestore_companion_extraction_queue.py` |
| `{p}event_dedup` (Slack/Telegram event idempotency) | platform event id | `src/adapters/firestore_dedup_store.py` |
| `{p}worker_task_dedup` | task name or mark key | `TaskClaimStore`, `src/ports/task_claim_store.py` |
| `{p}long_turns` | `turn_id` | `src/adapters/firestore_long_turn_registry.py` |
| `{p}user_notification_state` (last active channel) | `user_id` | `NotificationChannel`, `src/domain/notification.py` |
| `{p}user_primary_channel` | `user_id` | `src/adapters/firestore_notification_state_adapter.py` |
| `{p}audio_transcripts` (expires on `purge_after`) | `{account_id}:{key}` | `FirestoreTranscriptCache`, `src/adapters/firestore_transcript_cache.py` |
| `{p}short_links` | the code | `src/adapters/firestore_short_link_repository.py` |
| `{p}external_workers` | `worker_id` | `ExternalWorker`, `src/domain/external_worker.py`; `FirestoreExternalWorkerStore`; life cycle: `../05_building_blocks/external_task_queue/README.md` |
| `{p}external_worker_keys` (a key is stored as a keyed digest, never the raw key) | key digest | `ExternalWorkerKey`, `src/domain/external_worker_key.py`; same adapter |
| `{p}external_tasks` (expires on `purge_after`, set only once final) | `task_id` | `ExternalTask`, `src/domain/external_task.py`; `FirestoreExternalTaskStore` |
| `{p}external_task_results` (expires on `purge_after`) | `task_id` | the submitted result text; same adapter |

**Optional flags on stored records.** A saved model message in `{p}sessions` carries `execution_tainted: true` when
outside content entered the execution that wrote it (`Message.execution_tainted`, `src/domain/llm.py`); it is written
only when true and a record without it reads as false. An `{p}orchestrator_notes` document carries `untrusted: true`
the same way (`AgentNote.untrusted`, `src/domain/agent_note.py`).

**Sessions per channel.** `session_id = f"{user_id}:{channel_id}"` (`src/domain/messaging.py::session_id_for`) is
deterministic: each channel (a Slack channel or DM, a Telegram chat) has its own session, history and consolidation
stream. History is a sliding window; batches past it are handed to `{p}consolidation_queue`
(a channel bound to a companion hands them to `{p}companion_extraction_queue`:
`src/services/overflow_routing_service.py`).

### 3.4 Prompts

How these combine into a prompt: `../05_building_blocks/prompt_design_system_v3/README.md`.

| Collection | Doc id | Model / code |
| :--- | :--- | :--- |
| `{p}domain_prompt_tokens_v3_system` (looked up first) | `token_id` | `src/domain/prompt_v3/token.py` |
| `{p}domain_prompt_tokens_v3_user` (when the system collection has no match) | `token_id` | `src/adapters/prompt_v3/firestore_token_repository.py` |
| `{p}domain_prompt_blueprints_v3` | `blueprint_id` | `src/domain/prompt_v3/blueprint.py` |
| `{p}domain_prompt_profiles_v3` | `agent_id` | `src/domain/prompt_v3/agent_profile.py` |
| `{p}domain_prompt_overrides_v3` | `{OWNER_TYPE}_{owner_id}` | `src/adapters/prompt_v3/firestore_agent_profile_repository.py` |

### 3.5 Email, tasks, reminders and skills

| Collection | Doc id | Model / code |
| :--- | :--- | :--- |
| `{p}email_indexing_state` | `{user_id}_{provider}` | `IndexingState`, `src/domain/email.py` |
| `{p}email_indexing_jobs_v1` | `job_id` | `IndexingJob`, `src/domain/email.py` |
| `{p}email_reader_pages` (one document per user, overwritten each UTC day) | `user_id` | `src/adapters/firestore_email_reader_budget.py` |
| `{p}email_exclusions` | truncated sha256 of `{user_id}|{pattern_type}|{pattern}` | `EmailExclusion`, `src/domain/email.py` |
| `{p}task_search_index` (To Do is the source of truth) | `{user_id}_{task_id}` | `TaskSearchEntry`, `src/domain/task.py` |
| `{p}task_config` | `user_id` | `TaskUserConfig`, `src/domain/task.py` |
| `{p}orchestrator_notes` | `{epoch_ms}-{uuid4 hex}` | `AgentNote`, `src/domain/agent_note.py` |
| `{p}skills` (`versions/v<n>`, `files/{sha256}` below each) | `{user_id}:{name}` | `src/adapters/firestore_skill_repository.py` |
| `{p}skill_drafts` (awaiting `$skill save`) | `{user_id}:{code}` | same |

### 3.6 Voice, companions and the MCP server

| Collection | Doc id | Model / code |
| :--- | :--- | :--- |
| `{p}voice_tickets` | ticket or marker key | `src/adapters/firestore_ephemeral_store.py` |
| `{p}geo_mark_links` (one-time `$geo` links) | `geo_link:<keyed digest of the link id>` | `src/services/geo_mark_service.py`; `../10_rfcs/GEO_MARKS_RFC.md` |
| `{p}companion_records` (scoped by session) | record id | `src/adapters/firestore_companion_memory_repository.py` |
| `{p}companion_context_cache` | session id | `src/adapters/firestore_companion_cache_repository.py` |
| `{p}mcp_clients` | `client_id` | `src/adapters/firestore_mcp_client_repository.py` |
| `{p}mcp_auth_codes` | keyed digest of the code | same |
| `{p}mcp_refresh_tokens` | sha256 of the token | same |

`{p}voice_tickets`, `{p}geo_mark_links`, `{p}oauth_flows`, `{p}auth_passkey_challenges` and `{p}drive_note_erase_requests` are
written through `FirestoreEphemeralStore` (`src/adapters/firestore_ephemeral_store.py`), whose read re-checks the
expiry: a document the TTL sweep has not yet deleted still reads as absent.

### 3.7 User drive

Meaning, lifecycles and what each purge keeps: `../05_building_blocks/user_drive/README.md`. Index, usage and transcript collections are written by
`FirestoreDriveIndexRepository` (`src/adapters/firestore_drive_index_repo.py`, module docstring); the mirror roots and
ledger by `FirestoreDriveMirrorRepository` (`src/adapters/firestore_drive_mirror_repo.py`); the doc-id builders are in
`src/domain/drive_index.py`, `drive_usage.py`, `drive_transcript.py`, `drive_mirror.py`, `drive_note.py` and
`drive_connection.py`. Records, chunks, hints, transcript jobs and batches and the
sync state are keyed by connection scope; usage, initial build, transcript store, mirror roots and ledger are user- or
drive-scoped on purpose (drive map, "Keys by scope").

| Collection | Doc id | Notes |
| :--- | :--- | :--- |
| `{p}drive_connections` | `{user_id}:{connection_id}` | never deleted: a DISCONNECTED one keeps its ref tag reserved; `FirestoreDriveConnectionRepository` |
| `{p}drive_user_settings` | `{user_id}` | the primary-connection pointer; same repository |
| `{p}drive_index_records`, `drive_index_chunks`, `drive_index_hints` | per-scope builders | |
| `{p}drive_sync_state` | the connection id | |
| `{p}drive_model_usage` | `usage_doc_id` | a purge keeps it |
| `{p}drive_transcript_jobs` (+ `drive_transcript_windows`), `drive_transcript_batches`, `drive_transcripts` (+ `drive_transcript_parts`) | per-scope builders | |
| `{p}drive_mirror_roots` | `{user_id}:{drive_identity}` | no purge deletes it |
| `{p}drive_transcript_mirror` | per mirrored file or folder | no purge deletes it |
| `{p}drive_notes` (`versions` below each) | `{user_id}:{note_id}` | durable: no purge deletes it, an account deletion must |
| `{p}drive_note_erase_requests` | `{user_id}:{code}` | short-lived |
| `{p}drive_links` | keyed digest of the link id | one-time download links, short-lived; `src/adapters/firestore_drive_link_store.py` |

## 4. Adding a collection

1. Add a property to `EnvironmentConfig` returning `f"{self.firestore_collection_prefix}<name>"`, with a docstring
   saying what the collection holds and its document id.
2. Declare every index it needs in `config/firestore.indexes.json` and have it `READY` before the code ships.
3. Decide on expiry. A field a TTL policy should act on is named `expires_at` or `purge_after` and registered in
   `config/ttl_registry.json`; any other field gets another name.
4. Add a row here.
