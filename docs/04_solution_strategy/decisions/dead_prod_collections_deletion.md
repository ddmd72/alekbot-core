# Decision: Delete dead prod + legacy Firestore collections

**Date:** 2026-05-31
**Status:** Accepted — executed
**Context:** Followup tracker X.1 / ADR-006 cleanup step 4.

The prod deployment is dead: Cloud Run service `alek-bot` no longer exists, zero request logs
in 90 days, `main` branch abandoned. Prod data lived as **unprefixed** collections in the
shared `us-production` database (dev uses the `development_` prefix). 17 collections — 14 prod
(`domain_*_v2`, `domain_prompt_*_v3`, `sessions`, `event_dedup`, `consolidation_queue`,
`user_context`, `prod_whitelist`, …) + 3 legacy (`facts`, `observations`, `users` per
DATABASE_SCHEMA §7) — held 5534 stale docs.

**Decision:** Export all 17 to a gitignored backup (`scripts/memory/prod_backup_*`), then
delete. Also deleted orphan Cloud Scheduler `alek-bot-keep-alive` (pinged the nonexistent prod
`/health` every 10 min). Safe because the dev runtime is name-isolated — all collection access
flows through `EnvironmentConfig.firestore_collection_prefix` → `development_*`; verified no
bare unprefixed literal (`sessions` / `event_dedup` / `user_context` / whitelist) is read
directly in `src/` (the literals found are base-names fed to a prefixer, an OAuth scope string,
a payload dict key, or a docstring example).

**Rejected alternatives:**
- *Full X.1 (two physical Firestore DBs, unprefixed names):* the real clean end-state, but a
  copy-migration; stays deferred to X.2 release-DB work. Deleting dead data is the slice that
  needs no migration.
- *Rename live `development_*` → semantic/unprefixed names now:* Firestore has no native
  rename; any rename is a copy-all-docs migration (ADR-006 §Consequences). Standing rule —
  "if it needs migration, skip." Not done.
- *Delete without backup:* prod is dead, but a one-time export is near-free reversibility.
- *Drop the prod no-prefix branch in `environment.py`:* the prefix mechanism is the correct
  env-isolation primitive a future fresh prod would reuse; only the prod *instance* is dead,
  not the abstraction.

**Not closed:** dead prod composite indexes remain (harmless — no collection to index);
GitHub default-branch flip to `develop` + `main` deletion (X.3) pending GitHub auth.
