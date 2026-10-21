# Every Firestore client names its database

Date: 2026-10-22 (S1b F4). Status: decided.

## Decision
The live database is the named `us-production`; `(default)` is unused. Its id has one source,
`EnvironmentConfig.firestore_database_id` (`FIRESTORE_DATABASE`, default `us-production`). The three
`*_main.py` services read it (main used to fall back to `(default)`); scripts use `scripts/_firestore.py`.
`tests/unit/test_firestore_database_explicit.py` (AST scan of `src/`, `scripts/`, `firestore_utils/`, entry points)
fails any client without an explicit `database=`; `None`, `''` and bare `**kwargs` count as missing.

## Alternatives rejected
- Change only the env default: a client built with no argument still picks `(default)`.
- Global monkeypatch of the client: hides the choice and breaks the emulator branch.
- Reviewing scripts by hand: how the Feb-2026 legacy-fact migration hit the wrong database.

## Why
A forgotten argument must fail in CI. Scripts on retired schemas (`facts`, `*_oauth`, `prompt_components`) were deleted.
`clone_dev_to_prod.py --confirm` now targets the live database (guarded; fixed source list).

## Residuals (read-only facts, 2026-10-22)
- `(default)` still holds stray collections: `development_domain_users_v2`, `test_consolidation_queue`,
  `test_domain_users_v2`, `test_prompt_components`, `test_sessions`.
- `us-production` holds Feb-2026 backups: `development_domain_accounts_v2_backup_*`, `development_domain_facts_v2_backup_*` (x2).
  Deleting any of these is an optional owner step. No v1 legacy collections remain in `us-production`.
