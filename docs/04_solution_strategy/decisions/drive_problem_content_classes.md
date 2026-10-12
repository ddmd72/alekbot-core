# Decision: Problem content in the drive is bounded first and classified second

**Date:** 2026-10-11
**Status:** Accepted (User Drive Step 2; Slice 1 applies the metadata-stage rules)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §5.2, §5.3, §5.5; owner decision D1 (§21).

**Decision:** Safety never depends on classifying a file correctly.
- **Layer 0, bounds that hold for any input:** per-user card and chunk caps with document-first admission, per-file
  caps, one hit per file before fusion, extraction in a killed-on-limit child process (Slice 2), the quiet period.
  A misclassified file costs quality, never stability.
- **Layer 1, seven classes for quality and cost**, each with a harm, an objective signal at the stage where it is
  read, and a visible `reason`: machine-generated trees (dot-folders, a `.git` or `pyvenv.cfg` child, the tool-made
  `node_modules`, `__pycache__` and `site-packages` anywhere (plan Δ16: tools name them, users do not), and density:
  `childCount` over `DENSE_FOLDER_CHILDREN` with mostly content class *other*, decided once from a probe), opaque
  containers, machine text, book-length files, image-only scans, flapping files, photographs vs. images with text.
- A wrong rule is fixed by a `policy_version` bump that the upgrade step applies in place, never by a migration.

**What Slice 1 builds** (`src/domain/drive_index_policy.py`, `src/domain/drive_index_planner.py`):
- `ADMITTED_KINDS` is empty, so no file is downloaded and every file is metadata-only. Chunks, the chunk caps,
  extraction, the content-stage signals (magic bytes, text quality, zero yield, thumbnail triage) and the quiet period
  arrive with Slice 2 (plan Δ12). Of Layer 0, Slice 1 has the per-user card cap
  (`MAX_CARD_RECORDS_PER_USER`), admitted by `card_rank_of`: documents, then media, then other files, then folders.
- Metadata rules: never-download item rules (dot names, secret-file names, OS artefacts, remote items, packages,
  malware) and kinds by extension, so archives, disk images, logs and data dumps stay metadata-only by kind.
- Class 1 (generated trees) is decided on the folder: a dot name, a `.git` or `pyvenv.cfg` child, a generated
  directory name (`is_generated_dir`, Δ16), or density. A marker that leaves its folder re-opens it; a folder that is
  itself being deleted is never re-opened by a late `.git` tombstone. Changed after review (fix R1): the plan's
  planner resurrected a deleted repository when its tombstone preceded the `.git` one.
- Density reads the first `PROBE_CHILDREN` (200) children of a folder over `DENSE_FOLDER_CHILDREN` (500): the folder
  is dense when files of content class *other* are more than half of the sampled files (subfolders excluded). The
  probe is one item call from the tick's shared `ITEM_CALLS_PER_TICK` budget (plan Δ9). Until it succeeds the folder
  waits as `probe_pending` and its children are held unresolved; a failed probe is a per-item failure, while a
  not-connected or throttled answer ends the tick (systemic).
- "Decided once" is a decision with hysteresis: it stands until `childCount` doubles or halves. When it no longer
  stands and the folder is at or below the threshold, its density anchor is cleared (`density_reset`), so a
  cleaned-and-refilled tree is probed again. Changed after review (fix R1): in the plan the anchor survived a
  clean-and-refill, and the refilled tree was never re-probed.

**Rejected alternatives:**
- *Shannon entropy as a container signal:* every DOCX, XLSX, PPTX and EPUB is a ZIP at ~7.9 bits/byte.
- *Timestamp clustering for generated trees:* copying, syncing or unzipping gives human folders identical times.
- *Density alone:* an Obsidian vault or a photo folder would turn opaque; the content-class condition keeps them.
- *Name and folder conventions (`Scan_*`, `Docs/`):* users rename, write in many languages and follow no convention.
- *Head, summary and tail sampling of long files:* loses the middle and needs an LLM call per file.

**Trigger to revisit:** an incident in the density probe (§12.4 names it the maintenance hotspot), or `reason`
counts in the Cabinet showing a rule misfire on real folders.

**See also:** [drive_indexer_lease_queue_service.md](drive_indexer_lease_queue_service.md) (the item-call budget the
probe spends), [drive_index_not_in_search_enrichment.md](drive_index_not_in_search_enrichment.md) (how cards are
searched).
