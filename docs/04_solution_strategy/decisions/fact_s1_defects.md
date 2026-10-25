# Fact S1 defects: vectors follow content, SCD2 flags follow state (2026-10-21)

**Decision.** Slice S1 of the fact lifecycle fixes defects in place, without changing the storage model:
- `update_fact` recomputes all three vectors (text, tags, metadata) whenever text, tags or metadata change,
  through `domain/fact_embedding.fact_embedding_texts`, the same recipe `FactWriteService` uses at creation
  (it leaves out the system tags `consolidated`/`anchor`, so a re-embed matches creation). Before, the old
  vectors were written back and ~159 current facts were found by the meaning of their first text.
- `update_fact` accepts `metadata` (merged key by key); the CONSOLIDATION_TOOLS token already promised it.
- `domain/fact_lifecycle.lifecycle_fields` maps state to flags: superseded -> `is_current=False` + `valid_to`;
  archived/invalidated -> `valid_to`, `is_current` stays True; current/stale -> True, no `valid_to`. So
  `is_current` means "latest document of its lineage", and only a superseded document has a successor.
  Used by `update_fact`, `merge_facts` and Cabinet `invalidate_fact` (which now also sets `last_updated`).
- A state-only update changes neither `version` nor `reported_date`; `version` bumps only on a content change
  (text, tags, metadata, temporal_class).
- `FactManagementPort.update_fact(fact_id, updates, account_id)`: a fact of another account is `not_found`;
  `merge_facts` skips foreign sources.
- `search_facts_by_domain` returns the newest facts first (was oldest-first, limit 10); dedup in
  `add_fact_if_unique` compares only `state == "current"`; vector-search projection includes `context`.
- `scripts/migration/fact_s1_backfill.py` (dry run by default, `--apply` writes) fixes flags, three string
  `reported_date` values and re-embeds every document with version > 1 in any state. It runs **after the S1 deploy** (otherwise later
  updates re-stale the vectors) and writes each document with an `update_time` precondition from the scan, so a
  fact the live consolidator changed meanwhile is skipped and counted ("changed since scan"; re-run). The dry
  run also reports the other desync classes as `[audit]` lines. Measured 2026-10-21: 78 superseded with
  `is_current=True`, 182 flag candidates, 354 vector candidates, 3 string dates.

**Rejected.**
- Real versions now (new document per content change, TD-4) - a storage-model change; belongs to S2 of the
  fact lifecycle RFC (`docs/10_rfcs/FACT_LIFECYCLE_RFC.md`, draft, separate branch).
- `is_current = False` for every non-current state - archived/invalidated facts have no successor, and the
  flag is read as lineage-latest; S2 decides what lineage reads mean.
- Expiry fields/janitor and a HISTORICAL vs ARCHIVAL prompt vocabulary - S3 (0 of 87 `context_priority`
  values in 30 days were historical; S3 rewrites those tokens anyway).
- Changing `get_latest_fact_by_lineage` - no memory caller uses it; its meaning belongs to S2 (done there: highest version that is not superseded).

**Known, not in S1** (all four closed by Facts S2, 2026-10-23; see `fact_versions_one_writer.md`, memory map §2.4):
- `merge_facts` supersedes the sources before creating the merged fact; a failed create leaves them without a
  successor. **Closed:** the merged fact and the closing of every source are one transaction; a failure writes nothing.
- `updates.tags` replaces tags wholesale (an explicit null writes None); the `anchor` tag can be dropped although
  anchors are looked up by it. **Closed:** `null`, `[]` and a non-list are refused; `consolidated` and `anchor` are kept.
- `_TrackingFactManagement.update_fact` records content even for not-found or failed updates. **Closed:** the tracker
  records the result id, and only a successful write.
- The per-fact lock in the adapter is process-local; S2 replaces it with a precondition batch. **Closed:** the lock is
  deleted; writes carry the `update_time` precondition, and a per-account lease serialises consolidation runs.

**Revise when.** A second embedding writer appears (it must use the shared recipe), or `is_current` gains a reader that
needs per-state meaning. S2 has landed (2026-10-23): versions are documents and `update_fact` writes a new one on a
content change; the flag mapping above is unchanged.
