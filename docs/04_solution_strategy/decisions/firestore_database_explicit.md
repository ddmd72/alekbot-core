# Every Firestore client names its database

Date: 2026-10-22 (S1b F4). Status: decided.

## Decision
The live database is the named `us-production`; `(default)` is unused. The id has one source,
`EnvironmentConfig.firestore_database_id` (`FIRESTORE_DATABASE`, default `us-production`).
`main.py`, `job_main.py` and `indexer_main.py` read it (main used to fall back to `(default)`).
Scripts use `scripts/_firestore.py` or pass the same property. `tests/unit/test_firestore_database_explicit.py`
(AST scan of `src/`, `scripts/`, `firestore_utils/` and the root entry points) fails any `Client`/`AsyncClient`
without an explicit `database=`; `database=None` and bare `**kwargs` count as violations outside the helper.

## Alternatives rejected
- Change only the env default: a client built with no argument still silently picks `(default)`.
- Global monkeypatch of the Firestore client: hides the choice, surprises tests, breaks the emulator branch.
- Reviewing scripts one by one: this is how the Feb-2026 legacy-fact migration wrote to the wrong database.

## Why
A forgotten argument must fail in CI, not write to the wrong database. Scripts on retired schemas
(`{prefix}facts`, `development_facts`, `*_oauth`, `prompt_components`) were deleted rather than converted.

## Residuals (read-only facts, 2026-10-22)
- `(default)` still holds stray collections from old scripts: `development_domain_users_v2`,
  `test_consolidation_queue`, `test_domain_users_v2`, `test_prompt_components`, `test_sessions`.
- `us-production` holds Feb-2026 backups: `development_domain_accounts_v2_backup_*` and
  `development_domain_facts_v2_backup_*` (two). Deleting any of these is an optional owner step.
- No v1 legacy collections remain in `us-production`.
- Scripts under `scripts/memory/` are not scanned (gitignored, owner-local).
