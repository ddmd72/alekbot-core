# Decision: The drive index is not part of SearchEnrichmentService

**Date:** 2026-10-11
**Status:** Accepted (User Drive Step 2, Slice 1)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §10.3; owner decision D4 (§21).

**Since DRIVE_SPECIALIST_RFC:** Smart no longer holds the drive intents. `search_files_in_drive` is gone; the drive specialist (`work_with_drive_files`, [drive_specialist.md](drive_specialist.md)) searches with `search_drive`, and `drive_deleted` became its tool `search_deleted_recordings_and_notes`. The text below is kept as decided.

**Decision:** The drive is reached only through the `search_files_in_drive` intent, which Smart calls when the
answer may be in the user's documents. `SearchEnrichmentService` keeps injecting validated facts about the user and
never reads the drive index.

**Why:** Enrichment means "facts about the user, on every request". The drive is an orthogonal knowledge base: it
may hold facts about the user, or nothing about them (a contract, a manual, a repository). The same service backs the
remote MCP server and the companions, so enrichment would hand file content to claude.ai and the companions too.

**Where it holds in the code:**
- `Intent.SEARCH_FILES_IN_DRIVE` is declared in `src/infrastructure/agent_manifest.py` and served by
  `FileManagementAgent._search_drive`, which calls `DriveSearchService` (`src/services/drive_search_service.py`).
  The intent's description tells Smart to use it "when the answer may be in the user's files, and before saying a
  file is not there".
- `SearchEnrichmentService` has no dependency on the drive index repository or on `DriveSearchService`. The remote
  MCP tool `get_user_context` and the companion context assembler go through enrichment, so neither sees drive
  content unless an agent on its path delegates the intent.
- Search results (cards with `drive:<id>` refs) and the converted file text from `open_file_from_drive` are wrapped
  in the escaped, nonce-tagged reference-material wrapper (D2, RFC §10.4), never presented as memory. A PDF or
  image the model reads natively travels with the reference-material reminder and an escaped label instead; a video
  is sent to the user as a file, and the model gets only that reminder and label (`FileManagementAgent._open_drive`).

**Consequences:**
- A request that could be answered from a document costs one tool round-trip when Smart decides to search; a request
  that has nothing to do with the drive costs nothing.
- Smart may fail to search when it should. That is a prompt and intent-description problem, measured by the eval,
  not a reason to inject the drive into every prompt.
- Answers Smart derives from documents still reach memory through normal consolidation of the conversation (D4,
  accepted as a trade-off); the drive itself is never a consolidation source.
- The remote MCP connector is unchanged by the drive index: `get_user_context` still returns facts only, so claude.ai
  never receives file names or file text through it.

**Rejected alternatives:**
- *A drive stream in enrichment's RRF:* every prompt pays a kNN and carries document text presented as memory.
- *Enrichment behind a relevance threshold:* the RFC decides against cosine thresholds (§10.1); one tuned for facts
  does not transfer to documents.
- *Documents as a source for consolidation:* telling a fact about the user from third-party text is OQ-1.

**Trigger to revisit:** OQ-1 is designed, or the eval's G3 set (Slice 2) shows Smart not searching the drive when
the answer is there.

**See also:** [drive_index_opt_in_access_loss_grace.md](drive_index_opt_in_access_loss_grace.md) (the search gate
and the opt-in), [drive_problem_content_classes.md](drive_problem_content_classes.md) (what gets a card), and
[USER_DRIVE_RFC.md](../../10_rfcs/USER_DRIVE_RFC.md) (Step 1: refs, labels and `open_file_from_drive`).
