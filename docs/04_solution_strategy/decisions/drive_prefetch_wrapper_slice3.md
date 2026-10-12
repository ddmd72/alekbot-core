# Decision: Drive files prefetched for other specialists get the untrusted wrapper in Slice 3

**Date:** 2026-10-12
**Status:** Accepted, deferred (User Drive Step 2; D2 scope)
**Context:** [USER_DRIVE_INDEXING_RFC.md](../../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §10.4, owner decision D2 (§21),
RFC Δ34, Δ35; `AgentCoordinator._resolve_file_refs` in `src/infrastructure/agent_coordinator.py`.

**Decision:** D2 says every delivery of file text carries the reference-material reminder inside the nonce-tagged
wrapper. Slice 1 applies it to search results and to `open_file_from_drive`. A `drive:` ref passed as `file_ref` to
DocPlanner, PdfGenerator or HtmlPageGenerator is still downloaded and converted by the coordinator's file-ref
resolver and injected as `file_content`, raw, as chat uploads are. That path gets the wrapper when Slice 3 moves
every file-to-text conversion onto one extraction path.

**Rejected alternatives:**
- *Wrap in the coordinator now:* the resolver serves chat uploads and drive files alike, and each specialist's prompt
  builds its own document context; wrapping one source changes three agents' inputs ahead of the rework that
  replaces the resolver.
- *Refuse `drive:` refs for those intents:* breaks "make a PDF from my contract on the drive", a Step 1 capability.

**Residual risk:** a hostile document on the drive reaches a document-generating specialist without the reminder.
Their LLM calls carry no tools (DocPlanner's one hand-off is the DOCX code generator), so injected text can at worst
shape what they produce: DocGenerator's output is code that runs (sandboxed, no network or file system), and
HtmlPageGenerator's output is a page with JS that the user opens.

**Trigger to revisit:** Slice 3, or any specialist with tools beyond producing a document gaining `prefetch_file_ref`.
