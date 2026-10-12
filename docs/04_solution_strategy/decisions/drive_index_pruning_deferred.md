# Decision: The unprefixed index entries stay in `config/firestore.indexes.json`

**Date:** 2026-10-12
**Status:** Accepted (User Drive Step 2, Slice 1)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §12.5, RFC Δ22;
[collection_prefix_retained.md](collection_prefix_retained.md), [dead_prod_collections_deletion.md](dead_prod_collections_deletion.md).

**Decision:** The drive indexes are declared for the `development_` collections only (RFC Δ22), and the file's older
entries for unprefixed collections — 24 of its 64 entries, for collections deleted on 2026-05-31 — are kept, and
no live index is deleted for them. Pruning is not part of this feature.

**Why:**
- `collection_prefix_retained.md` keeps the prefix mechanism for a future real production environment, which would
  use the unprefixed names; those entries are what such an environment needs, and the file is the only record of it.
- They cost nothing that matters: 64 entries are far below Firestore's per-database composite-index limit, and an
  index over an empty collection holds no entries. `deploy_firestore_indexes.py` reports an existing index as "already exists".

**Rejected alternatives:**
- *Delete the 24 entries and their live indexes now:* saves nothing measurable and throws away the declaration a new
  environment would rebuild from.
- *Declare the drive indexes for both name forms, as the file's convention was:* adds dead indexes for collections that do
  not exist (RFC Δ22).

**Trigger to revisit:** the index count nears the database limit, or a separate production database is stood up
(then its indexes are declared for it, and this file's dead entries can go).
