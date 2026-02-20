# ADR 006: Semantic Firestore Collection Naming

**Status:** Accepted  
**Date:** 2026-02-05  
**Context:**  
The current Firestore collection naming convention is inconsistent in development environments:

- Core collections use `_oauth` suffix (e.g., `development_users_oauth`).
- Infrastructure collections have no suffix (e.g., `development_sessions`).
- Prompt v3 collections use a hardcoded `dev_` prefix (e.g., `dev_prompt_system_tokens`).
- Prefixes vary between `development_` (from `environment.py`) and `dev_` (from legacy code).

**Decision:**  
Adopt a **Semantic Separation** strategy for collection naming, distinguishing between **Domain** (business logic, versioned) and **Infrastructure** (technical, stable) collections.

### Naming Convention

Format: `{prefix}{category}_{name}[_version]`

1. **Prefix:** Environment-specific (e.g., `development_`, `test_`, or empty for production).
2. **Category:** `domain_` for business entities, empty for infrastructure.
3. **Name:** Entity name (e.g., `users`, `sessions`).
4. **Version:** `_v2`, `_v3` etc. for domain entities. Infrastructure collections are versionless.

### Schema Matrix

| Type       | Old Dev Name                      | New Dev Name                      | Notes              |
| :--------- | :-------------------------------- | :-------------------------------- | :----------------- |
| **Domain** | `development_users_oauth`         | `development_domain_users_v2`     | OAuth Schema v2    |
| **Domain** | `development_accounts_oauth`      | `development_domain_accounts_v2`  | Billing v2         |
| **Domain** | `development_facts_oauth`         | `development_domain_facts_v2`     | Memory v2          |
| **Domain** | `dev_prompt_*`                    | `development_domain_prompt_*_v3`  | Prompt System v3   |
| **Infra**  | `development_sessions`            | `development_sessions`            | No change (stable) |
| **Infra**  | `development_consolidation_queue` | `development_consolidation_queue` | No change (stable) |
| **Infra**  | `development_event_dedup`         | `development_event_dedup`         | No change (stable) |
| **Infra**  | `development_user_context`        | `development_user_context`        | No change (stable) |

**Consequences:**

- **Positive:** Clear distinction between business data and technical implementation. Explicit versioning allows side-by-side migration. Eliminates hardcoded `dev_` prefixes.
- **Negative:** Requires data migration for domain collections. Longer collection names.
- **Risks:** Migration script must ensure data integrity. Code updates required across all adapters.

**Migration Plan:**

1. Update code to use new names.
2. Run migration script to copy data (Dev first).
3. Validate (UAT).
4. Deprecate and delete old collections.
5. Repeat for Production (future phase).
