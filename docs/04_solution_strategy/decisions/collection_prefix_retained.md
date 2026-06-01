# Decision: Keep the `development_` collection prefix (no rename migration)

**Date:** 2026-06-01
**Status:** Accepted
**Context:** Followup tracker X.1 closure; ADR-006 step 4 ("deprecate and delete old collections").

After the dead prod deployment was retired and its unprefixed collections deleted
([`dead_prod_collections_deletion.md`](dead_prod_collections_deletion.md)), the only live
data lives in `development_*`-prefixed collections in the `us-production` database. The prefix
is now a historical artifact of the abandoned dev/prod-in-one-database model — semantically it
would read cleaner as unprefixed (`domain_facts_v2` rather than `development_domain_facts_v2`).

**Decision:** Keep the `development_` prefix as-is. Do not rename.

**Why:**
- Firestore has **no native rename.** Renaming = copy every doc of ~28 collections (~8800 docs)
  into new collection names **plus** recreating all 58 composite + vector indexes against the
  new names, then deleting the originals. A real migration with real risk.
- The payoff is zero. The only consumer of collection names is the system itself (and the
  developer). No reviewer browses Firestore — portfolio access is read-only on the default git
  branch, nothing to see in the console. The cosmetic win does not justify a migration.
- The prefix **mechanism** in `EnvironmentConfig.firestore_collection_prefix` is correct and
  retained: it is the right env-isolation primitive a future real prod (separate database)
  would reuse. Only the prod *instance* is dead, not the abstraction.

**Rejected alternatives:**
- *Rename live collections to unprefixed now:* migration cost (above), no benefit.
- *Set the prefix default to `""` and migrate dev data into unprefixed names:* same migration,
  same lack of payoff; also throws away the isolation primitive.
- *Leave it undocumented ("historically happened"):* drift-by-silence. This record makes it a
  decision, not an accident.

**Trigger to revisit:** standing up a genuinely separate production environment (own Firestore
database). At that point the new env gets unprefixed names natively and this prefix stays on the
dev/lab database — which is X.1's full target shape, deferred until there is a reason to build it.
