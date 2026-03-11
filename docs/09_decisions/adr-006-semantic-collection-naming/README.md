# ADR 006: Semantic Firestore Collection Naming

**Status:** Accepted  
  
**Context:**  


hardcoded `dev_` prefix on prompt collections next to the configured `development_` prefix.

## Decision


**Decision:**  
hardcodes a name or a prefix.

Format: `{prefix}{category}{name}[_vN]`

1. **Prefix:** `EnvironmentConfig.firestore_collection_prefix` (environment-specific; see

2. **Category:** `domain_` for core business entities; empty for infrastructure.
3. **Version:** `_vN` on domain collections, bumped on a breaking schema change so old and new can live side by side.

Examples: `domain_users_v2`, `domain_accounts_v2`, `domain_facts_v2`, `domain_prompt_tokens_v3`,

`worker_task_dedup`, `long_turns` (infrastructure).

Collections added later for feature subsystems (drive index, skills, fact review, memory-write leases) use the plain
| :--------- | :-------------------------------- | :-------------------------------- | :----------------- |

## Consequences

- Business data and technical state are distinguishable by name; a versioned domain collection can be migrated by
copying into `_vN+1`.
- Firestore has no rename, so changing a name is always a copy-all-documents migration plus re-creating its indexes.



**Consequences:**












