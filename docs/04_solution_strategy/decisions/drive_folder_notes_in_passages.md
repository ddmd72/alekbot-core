# Decision: Passages carry folder notes under one cap; a file's own note is never capped

**Date:** 2026-10-28
**Status:** Accepted (owner, OQ-A3, 2026-10-28); built on `feat/drive-answer-from-content`, not deployed.
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §28.3 A18, §28.6 OQ-A3, §28.9;
[DRIVE_FILE_NOTES_RFC.md](../../10_rfcs/DRIVE_FILE_NOTES_RFC.md) D20; code `src/domain/drive_excerpt_context.py`
(`plan_context`), `EXCERPT_FOLDER_NOTES_CHARS_MAX` in `src/domain/drive_index_params.py`.

**Decision:**
- A passage alone does not say what its file is, so each file's block carries its own owner note and its
  "what it is" description, and the owner notes of its ancestor folders (display only; D8's no-inheritance stands).
- **The cap covers folder notes only**, as one setting, `EXCERPT_FOLDER_NOTES_CHARS_MAX` (4 000). A file's own note and
  description are always shown, outside it. Raise the setting to remove the cap in practice.
- Over the cap, folders are dropped whole (farthest first, never cut) and named on a `Folder notes not shown (limit)` line.
- Results stay in **flat relevance order** with each folder's note printed once, before its first file; they are not
  regrouped as a tree.

**Rejected alternatives:**
- *A cap on every note and description:* a file's own note is what the owner said it is; cutting it defeats the feature.
- *Two settings (notes, descriptions):* the tree is aggregated per answer, so the cap is a guard, not a budget; the owner
  asked for one simple setting.
- *Regrouping results by folder:* loses relevance order, the order the answer is read in.
- *Folder descriptions written by a model:* deferred (RFC §28.7).

**Trigger to revisit:** logs show `Folder notes not shown (limit)` on answers where the dropped note told same-named files
apart.
