# Decision: A repository uploaded over several rounds is carded, then uncarded

**Date:** 2026-10-12
**Status:** Accepted (User Drive Step 2, Slice 1)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §5.2, §7.8, §16 (Slice 1 "done when"),
RFC Δ15, Δ16; [drive_problem_content_classes.md](drive_problem_content_classes.md).

**Decision:** A repository costs one card only when its `.git` (or `pyvenv.cfg`) arrives in the same delta round as
its files: work-off waits for a complete round (RFC Δ15), so the marker closes the folder before any file is carded.
When the files arrive in rounds that complete before the marker does (a slow sync client, an upload spread over
ticks), those files are carded like any first-seen file, and the marker's rescope later deletes their records and
cards. The cost is one card embedding per such file, spent once; documents keep their cards throughout, because the
card cap admits documents before code (§5.4). `node_modules`, `__pycache__` and `site-packages` are opaque by name
(RFC Δ16), so the bulk of a dependency tree is never carded either way.

**Rejected alternatives:**
- *Probe every first-seen folder for a marker before carding its files:* one Graph listing per folder on every
  onboarding, to save embeddings that cost cents.
- *A quiet period on first-seen files:* delays every new file's card, the common case, for a rare one (the quiet
  period arrives with content indexing in Slice 2, for edits).
- *No card for code-kind files:* loses "find my script" outside repositories, and a repository holds more than code.

**Trigger to revisit:** card-embedding cost or card-cap pressure from repository uploads seen in the tick logs
(`cards` followed by mass `rescoped`/deletes for one folder), or a sync client that routinely uploads `.git` last.
