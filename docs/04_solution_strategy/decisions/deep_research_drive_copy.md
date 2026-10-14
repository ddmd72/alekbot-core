# Decision: Copy deep research reports to the user's drive (opt-in)

**Date:** 2026-10-14
**Status:** Accepted (owner decisions 2026-10-14)
**Context:** Bot artifacts live in the private media bucket, whose lifecycle deletes everything at 30 days
(`email_review/` at 5). A deep research is the one artifact that costs real money and time and that the user wants to
keep and find again; at 30 days its report link and its storage key (the history note's `open_file` ref) are gone.
Code: `src/domain/deep_research_drive_copy.py`, `src/services/deep_research_drive_copy_service.py`,
`AgentWorkerHandler._copy_deep_research_report`, `deliver_deep_research` (the context marker),
`GET/PUT /api/drive/deep-research-copy`, `UserBotConfig.drive_copy_deep_research`.

## Decision

- **Scope: deep research reports only.** Morning issues, email reviews and other generated pages are not kept. The
  media bucket and its lifecycle are unchanged.
- **Opt-in, per user.** A Cabinet switch in the Drive card, "Copy deep research reports to my drive", is the explicit
  consent; **off by default**. It is read uncached at each delivery.
- **What is copied: one file, the HTML report** the user received (the same bytes stored under `docs/`), not the
  round markdown or `meta.json`.
- **Where:** the user's drive App Folder, in a folder named in the user's UI language (`UIMessage.
  DEEP_RESEARCH_DRIVE_FOLDER`: "Deep research", "Глибокі дослідження", "Recherches approfondies", "Investigaciones";
  there is no `ru` UI language). Named `YYYY-MM-DD <title>.html`: the date is the user's local day, the title is the
  report's `<title>`, else the research question shortened at a word (≤ 80 chars), else "report"; sanitized with
  `sanitize_drive_filename` and capped at 200 UTF-8 bytes on a character boundary.
- **How:** `UserDriveService.save` — never overwrites; same bytes → the existing file; otherwise the drive's suffixed
  name; it nudges the indexer. Zero LLM, deterministic.
- **Where in the flow: one seam.** Every deep research entry point (the Claude runner via `agent_execution`, the Cloud
  Run Job, OpenAI's webhook, the polling path) ends in `deliver_deep_research`, which enqueues the HtmlPageGenerator
  task. That task's context now carries `deep_research_report_query` (the question, ≤ 500 chars). When
  `AgentWorkerHandler._deliver_document_result` has delivered that task's HTML link, it calls
  `DeepResearchDriveCopyService.copy_report`. Nothing else in the delivery changes.
- **Outcome: one short localized line after the link** (`notify_raw`): "📁 Copied to your drive: <folder>/<name>",
  "Not copied to your drive: the drive is not connected." (also when the grant needs a reconnect, or no drive is
  configured on the deployment), or "Not copied to your drive: saving failed." Toggle off → no line. **No retry, no
  queue**, and the copy can never fail the delivery: every error is caught and logged, and the report link has
  already been sent. The copy is bounded to 60 s (`DEEP_RESEARCH_COPY_TIMEOUT_S`): a slow Graph sequence never holds
  the delivery task open; on the timeout nothing more is posted.
- **Indexing:** the copy is an ordinary drive file. With drive indexing on, Search v2 indexes it like any HTML; the
  indexer is unchanged.

## Alternatives

- **A permanent bucket archive (no search):** keeps the bytes but gives the user nothing to find them with, and needs
  bucket, lifecycle and catalog work for every artifact kind. Rejected by the owner: the drive is where the user's
  long-term files already live, and it brings search for free.
- **A source-agnostic index over a bot-owned archive (the study's recommendation C):** one search over the drive and
  an archive, per-source purges, a split sync state. Rejected by the owner: an indexer refactor (M–L, right after
  Search v2) for one artifact kind; the drive copy reuses the existing index with no indexer change.
- **Copy the markdown (or md + meta) instead of the HTML:** the md is the raw research text, the HTML is what the user
  actually read and the indexer extracts both equally well. One file per research, the one the user recognises; no
  duplicate hits.
- **Retry or queue a failed copy:** a queue for a disconnected drive is a second store in disguise. The user is told
  once, in the chat, next to the report; they can save it themselves while the link lives.
- **Fold the line into the link message:** `send_document_link` renders the label as link text; a second line there
  would need a new channel contract on both platforms. A separate short line is the same "receipt" pattern the drive
  already uses.

## Consequences

- The folder follows the UI language at delivery time: a user who switches language gets a second folder.
- Turning the switch off leaves existing copies on the drive (they are the user's files).
- The "Copied" line goes to the chat but not to session history; Smart finds the copy through the drive intents
  (listing or search) like any file.
- The 200-byte cap is stricter than OneDrive's 255-character limit, leaving room for the drive's clash suffix.

**Trigger to revisit:** another artifact kind the owner wants kept, a request for retries, or a second drive provider
whose name rules differ.
