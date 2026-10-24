# ADR 006: Semantic Firestore Collection Naming

**Status:** Accepted (2026-02-05) — still in force; the migration it planned is done.

## Context

Collection names had drifted: an `_oauth` suffix on core collections, no suffix on infrastructure collections, and a
hardcoded `dev_` prefix on prompt collections next to the configured `development_` prefix.

## Decision

Separate **domain** collections (business entities, schema-versioned) from **infrastructure** collections
(technical, versionless). Every name is built in `EnvironmentConfig` (`src/config/environment.py`); no adapter
hardcodes a name or a prefix.

Format: `{prefix}{category}{name}[_vN]`

1. **Prefix:** `EnvironmentConfig.firestore_collection_prefix` (environment-specific; see
   `docs/04_solution_strategy/decisions/collection_prefix_retained.md`).
2. **Category:** `domain_` for core business entities; empty for infrastructure.
3. **Version:** `_vN` on domain collections, bumped on a breaking schema change so old and new can live side by side.

Examples: `domain_users_v2`, `domain_accounts_v2`, `domain_facts_v2`, `domain_prompt_tokens_v3`,
`domain_whitelist_v1`, `domain_email_facts_v1` (domain); `sessions`, `consolidation_queue`, `event_dedup`,
`worker_task_dedup`, `long_turns` (infrastructure).

Collections added later for feature subsystems (drive index, skills, fact review, memory-write leases) use the plain
`{prefix}{name}` form.

## Consequences

- Business data and technical state are distinguishable by name; a versioned domain collection can be migrated by
  copying into `_vN+1`.
- Firestore has no rename, so changing a name is always a copy-all-documents migration plus re-creating its indexes.
