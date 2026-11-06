# RFC: One way to open external files — refs, native reading, shared security pieces

**Status:** Proposed (rev 6); U0–U5 built (U4b, email save and send with D13's macro check in the sandbox, 2026-11-05; U5, one parse engine, on the branch `feat/u5-one-engine`, shipped with the parser `VERSION` bump and the drive upgrade pass held off until the owner OKs the probe's number). *D* items are owner decisions (2026-10-22 to 2026-11-03); *P* items are author
proposals the owner has not ruled on, each separable; *A* items are author decisions on engineering points.
**Date:** 2026-10-22 (rev 2: 2026-10-23, after four reviews; rev 3: owner decisions D7–D11; rev 4: re-review; rev 5: 2026-10-23, owner decisions D12–D13; rev 6: 2026-11-03, owner decisions D14–D15, code re-read; owner decisions D16–D20 of the same day)
Code cites are `main` @ `2280ca78`.
**Owner decisions (2026-10-22):**
- **D1. Placement.** The document service is a component inside each runtime (main service, drive indexer). Its
  contract is designed as if it were remote: serializable input, closed-schema outcome, an explicit deadline and
  "still working" state, billing inside, no caller state shared. It can become its own deploy unit later without
  touching callers. Open check: can Logfire content capture be suppressed for the reader's calls in the main service?
- **D2. Native when effective.** A file read in a conversation goes to the orchestrator's model natively when that
  model reads that kind well; otherwise through the pipeline (text layer → text-or-scan check → no-tool reader →
  text). The drive indexer keeps the pipeline (its task is different). Grok: images natively, PDFs not.
- **D3. Ref first.** The orchestrator always gets a ref, never the file, and opens it on demand through one open path.
  Every external file (chat upload, drive file, email attachment) becomes a ref. Email breaks this today:
  `get_email_attachment` returns content, not a ref. The LLM adapter declares what it reads natively and well (the
  pattern of `LLMPort.supports_caching()`); the open path asks and acts. Adapters never call services.
- **D4. Email refs.** As recorded: the ref is `email:<message id>/<part id>`. Open = read the message structure, check
  the part's size, download only that part. "Save to drive / send to the owner resolve the same ref to bytes at action
  time; the bot keeps nothing." A deleted email gets an honest "email deleted". A cache may come later inside the same
  resolver branch, without changing the ref.
- **D5. No new limits or budgets** for non-drive reads; a clean place is left for them. Drive budgets are untouched.
- **D6. Security policies in one place.** Kind from bytes, caps, isolated parsing, text-or-scan routing, the no-tool
  reader, untrusted marking: each exists once, as shared pieces, used by every path. The indexer keeps its own loop and
  its money and record decisions and calls the same pieces. The document service is those pieces assembled behind
  "open a ref".
- **D7 (2026-10-23). Long scans: a simple cut, no async.** No "still working" mechanism. An interactive open reads
  only as many pages as can be shown (aligned with the presentation cap) within today's open timeout, and tells the
  model "first N of M pages".
- **D8. A separate intent sends a file to the user in chat.** It takes any ref kind, resolves the bytes through the
  shared resolver and delivers them through `UserNotificationService.notify_file_bytes` with the origin channel as
  override; a failed upload is reported as a failure. Other agents may reuse it later (not now).
- **D9. No file contents in Logfire, from any model call** (orchestrator, reader, any agent). Calls, tokens and text
  stay; file parts are dropped from captured spans.
- **D10. Email has no cap of its own:** email attachments use the shared caps.
- **D11. One open verb per store**, as recorded: "Store stays in the intent name: `open_file` (chat, delivered,
  workspace, skill), `open_file_from_drive` (drive), `open_email_attachment` (email attachments). … `get_email_attachment`
  is removed: attachment refs come from `search_emails` results and `get_email_details` …; `open_email_attachment`
  takes the ref and opens it through the shared open path. Email intents stay three: search, details, open attachment.
  Save/send act on the same ref." Recorded correction (2026-10-23): the email index stores attachment names only, so
  search results show names, not refs. The drive's verb is now the drive specialist's local tool `open_drive_file`
  (Smart's intent `work_with_drive_files`, `DRIVE_SPECIALIST_RFC.md`); `open_file_from_drive`, `save_file_to_drive` and
  `update_file_in_drive` no longer exist (`src/infrastructure/agent_manifest.py:78-82`). The store is still in the name.
- **D12 (2026-10-23). Open an email attachment by email id + attachment name.** `open_email_attachment` takes an
  `email:` ref or an email id + attachment name (the part resolved at open time); duplicate names → no guess, the refs
  are listed.
- **D13. `send_file_to_user` does not forward "active" kinds from email attachments.** For email attachments only, by
  kind sniffed from the bytes (not the name): HTML/HTM, SVG, HTA, Office with macros, shortcuts (lnk), disk images
  (iso/img), executables and scripts. The bot says what kind it is and that it does not forward such files; the owner
  can open it in Gmail. Drive and chat files are sent without this restriction. Owner 2026-11-05: legacy OLE2 Office
  files (.doc, .xls, .ppt, any name) with a VBA project count as Office with macros, and saving an email attachment to
  the drive (`copy_file_to_drive` from an `email:` ref) is checked like sending it.
- **D14 (2026-11-03). Order: the document service first, the sandbox after it.** "Risk is not scary; do it the most
  logical and hexagonal way." U0 → U1 → U2 → U3 → U4 are built first, then U5. The sandbox's parse slices come after,
  as adapter swaps under the document service's ports (`ContentExtractorPort`, `PdfSplitterPort`): S3 in the indexer,
  S4 in the main service, with no caller change (§4.9). Until they run, the parser child in each runtime is the accepted
  residual of `decisions/drive_parser_sandbox_resource_isolation.md`, including for Google full-drive grants
  (`GOOGLE_DRIVE_RFC.md` §10). Recorded in the sandbox RFC as O12. By code dependency S3 may start once U1 and U3 are
  in, before U4/U5; S4 waits for U5.
- **D15 (2026-11-03). P4 accepted in principle: one parse engine.** The child's own readers serve every source;
  markitdown's auto-detect path is retired in U5. The final go was given in D16; the U0-b real-file report is
  not a gate (§8). Consequences: no `DocumentConverterPort`, `LocalDocumentConverter`, `SandboxDocumentConverter` and no
  `chat.convert` op; one parse op family, `parse.*` (A1), serves both runtimes; sandbox S4 is "admit the main service's
  identity to that family and flip its knob". Sandbox S-Δ1 (audio stays with the caller) and S-Δ2 (plain text stays
  with the caller, §4.4) hold. Recorded in the sandbox RFC as O13.
- **D16 (2026-11-03). D15's final go is given.** The U5 re-extract and re-embed cost is approved up front; the measured
  estimate is still reported before U5 ships.
- **D17 (2026-11-03). A4 accepted:** the reader's text budget for a drive scan opened in conversation is 60 000
  characters (§29's open bound).
- **D18 (2026-11-03). P13 accepted:** the reader's 200-page allowance per open applies to every store. Chat and email
  opens are not in the drive's monthly ledger.
- **D19 (2026-11-03). P14 and P15 accepted.** P14 (mark email bodies and search lines) is built in its own PR; P15
  (workspace and delivered files count as external) follows with U3.
- **D20 (2026-11-03). P19 accepted in a changed form:** ONE presentation cut for parser text on every open path, 60 000
  characters with a note naming the cut. Email attachments move from 30 000 to 60 000 (`truncate_with_alert`,
  `email_search_service.py:260`; `MAX_CONVERTED_CHARS`, `file_conversion.py:32`); chat, workspace and delivered opens,
  which have no cut today, get it. Realised in U3 (§4.5, §7).
- **Contract (owner, 2026-10-22):** the sandboxes are not all built; they come later and they are the isolation layer.
  This design works today (in-process engines, the drive parser child) and names the adapter each sandbox slice swaps
  in, with no second migration of callers. D14 fixes when.

Related: `USER_DRIVE_INDEXING_RFC.md` §9, §16 (Slice 3), §24.3, Δ35; `USER_DRIVE_RFC.md` §4.3–§4.6; `FILE_STORAGE_RFC.md`;
file_storage README §1.1; `UNTRUSTED_CONTENT_SANDBOX_RFC.md` §1, §4.4–§4.12, §7; `GMAIL_EMAIL_INDEXING_RFC.md` §2.4;
decision records `drive_prefetch_wrapper_slice3.md`, `drive_parser_sandbox_resource_isolation.md`,
`drive_extract_ingress_slice3.md`; `DRIVE_SPECIALIST_RFC.md` (the drive specialist and `DriveOperations`);
`MULTI_DRIVE_RFC.md` §9, §11.3 (tagged refs, access policy); `GOOGLE_DRIVE_RFC.md` §3.5, G1, G1b, G2, G10 (exports);
`USER_DRIVE_INDEXING_RFC.md` §27 (transcripts), §29 (the drive open bound).

**Changes in rev 6** (owner decisions D14–D20 and a re-read of the code at `2280ca78`):
- **Owner decisions 2026-11-03** (D16–D20): D15's final go and the U5 cost approval (D16); A4 (D17), P13 (D18), P14
  and P15 (D19) accepted; P19 accepted as one 60 000-character cut on every path, email included (D20). §4.4, §4.5, §5
  and §7 follow them.
- D14 (order) and D15 (P4 in principle) recorded; §4.8, §4.9, §5 and §7 follow them. P4 is no longer an open proposal.
- Drive: the drive specialist (`DriveAgent`, intent `work_with_drive_files`) and the shared `DriveOperations` replace
  FileManagement's drive intents; refs are `drive:<tag>:<item>` (`MULTI_DRIVE_RFC.md` §9); the §29 open bound governs
  drive text opens. §1.2, §4.1, §4.2 and the resolver table describe this.
- Audio: no production converter transcribes; U3 carries the audio requirement (§4.4) and removes the `defect:D3`
  ledger line.
- U0-b ran on a synthetic corpus (§8); U5's done-criteria name the four places where the child engine reads worse.
- Logfire is pinned at 5.2.0; its scrubbing still skips the message attributes (§4.3).
- §9 and §10 re-verified: every item still holds; citations moved; new items 9–12.
- **Author decisions new in rev 6** (each minimal, each listed in §5):
  - **A1.** The sandbox parse op family is named `parse.*` (§4.9).
  - **A2.** Native provider exports (a Google Doc as Markdown, Slides as text, Sheets as xlsx, Drawings as PDF) enter
    the open path as bytes the caller downloaded, with the export mime declared; no special path (§4.2).
  - **A3.** After U5, "plain text read in-process" (sandbox S-Δ2) means the kinds with no converter: `text`,
    `markdown`, `json`, `code`, `log`. `csv`, `html`, `eml`, `rtf` and `subtitles` go to the child's readers (§4.4).
  - **A4** (accepted, D17; the budget is 60 000 everywhere by D20). D7's text budget follows the store's presentation cap: 30 000 characters, except drive opens, where §29's
    60 000-character bound is the presentation cap (§4.4, §4.5).
  - **A5.** The native declaration reaches the drive specialist through `work_with_drive_files`. The specialist passes
    it on to the open unchanged, because its own model reads stand-ins and the bytes go to the delegating engine (§4.3).
  - **A6.** U5's PDF-table fix is a row-aware text pass in the child, not a reader route for table-heavy pages (§7).
- **Review round 1** (2026-11-03): `PdfSplitterPort` gains an optional deadline and a batch cut, and the service takes
  it explicitly (§4.4); the audio requirement is P21, with §27's opt-in and cap for drive recordings only (D5); U5's one
  parser `VERSION` bump is gated on a measured upgrade-pass cost and the owner's OK (§7); one adapter object serves both
  parse ports (§4.9); P5 names its deviation from `src/ports/CLAUDE.md`; the D13 macro check runs in the sandbox (O2, 2026-11-05; see §4.2).

**Changes since rev 1** (rev 2, four reviews, every change arbitrated): the native declaration travels per engine
attempt on a context copy and carries `max_pages`; one absolute deadline per open; prefetch reads in the worker (P18);
the open path is split between `FileConversionService.open` and the agents' store branches; `PARSER_UNAVAILABLE` added;
the parser slot wait is bounded; Logfire and build-check gates; P4 separable (U5); P6 dropped to follow-ups; one marking
function with a tag per store (P11 optional, P14 separate); P8 split; P15–P17 labelled; presentation caps labelled; an
interactive admission table; email listing rule and notes; sandbox deltas completed.

**Changes in rev 3** (owner decisions D7–D11): D7 truncation by pages; D8 `send_file_to_user`; D9 no file contents in
Logfire; D10 shared caps for email; D11 one open verb per store; OQ1–OQ5 closed.

**Changes in rev 5:** P20 accepted as D12; OQ6 closed by D13 (email-only active-kind block on `send_file_to_user`,
§4.2, in U4); no owner questions remain.

**Changes in rev 4** (re-review of rev 3, all items arbitrated):
- D7 realised exactly: with a text budget the reader reads window 1 alone, then one window at a time, stopping at the
  budget; the note says what is shown ("pages 1–N in full; text cut within pages N+1–N+10", holes listed).
- D11 quoted as recorded; the email id + attachment name input is P20, for the owner to confirm.
- The cut on parser text of non-email opens is back as P19 (D20 later made it 60 000 on every path); D7 covers reader text only.
- D9: logfire scrubbing cannot reach the message attributes; three candidates listed; U0-a has an exit to the owner;
  today's native drive opens named as an existing gap.
- Minors: `STILL_WORKING` maps to a timeout-style note (tested with a fake adapter); each open keeps its agent's
  timeout (email 300 s); the outage and prefix changes apply only when the new fields are set; `open_email_attachment`
  takes `file_ref` and EmailSearch opts out of prefetch; one part listing per open; send gets an action cap, a
  kind-from-bytes check and a `capabilities.py` entry; the removal list is complete; the old attachment converter is
  removed in U4; "not configured" without a file service.

## 0. In one paragraph

Three kinds of external file reach the bot: chat uploads, drive files, email attachments. Each is read a different
way today. Chat uploads and drive files are refs that `open_file` resolves; email attachments are not refs at all, and
`get_email_attachment` returns their text directly. Only the drive indexer checks a file's kind from its bytes, parses
in an isolated child process, notices a scan and sends it to a no-tool reader model. Every other read parses in a
thread of the main service and returns whatever text comes out. This RFC makes every external file a ref and gives
opening one path in code, behind one open verb per store: resolve the ref to bytes, hand them to one **document service**. If the consuming model reads that
kind natively (its adapter says so, within today's caps), it gets the bytes; otherwise the indexer's own pieces run:
sniff, parse in the child, text-or-scan check, reader. Text that reaches a model is marked untrusted by one function.
The indexer keeps its loop and calls the same pieces. The sandbox slices, built after this service (D14), replace one
adapter behind each parse port, with no caller change.

## 1. Today, with the recorded reasons

### 1.1 The incident that started this (2026-10-22)

The owner asked the bot to read an email's PDF attachment, save it to the drive and forward it. The PDF was a scan
without a text layer. All three steps failed:
1. **Read.** `get_email_attachment` converted the file with the text-layer converter in the main service
   (`src/services/email_search_service.py:198-260`, `convert_file_to_text` at `:251`). The result was table rules and
   bullets, not empty, so no alert fired; it came back as a normal result.
2. **Save.** The save to the drive (the same resolver serves the drive specialist's `copy_file_to_drive` today) got the attachment's file name as `file_ref`. The resolver knows delivered documents, workspace, skill and
   chat refs (`src/services/file_conversion_service.py:257-282`) and drive refs (`:351-352`, `:383-384`); an email
   attachment is none of these, so the answer was "not found, ask the user to re-upload".
3. **Forward.** No capability returns an existing file's bytes to the user, except video (D8 adds one).

The bot retried three times. Failure 1 is about reading, failure 2 about refs, failure 3 about delivery.

### 1.2 How each source is read, and why

| Source | Ref | Open | Native bytes to the model | Text path | Recorded reason |
|---|---|---|---|---|---|
| Chat upload | the stored, deduplicated name in `[File: "<ref>" (size)]` (`file_conversion_service.py:284-327`, label `:309`) | `open_file` (FileManagement) → `DriveOperations.fetch_stored` (`src/infrastructure/drive_operations.py:650-695`) | image or PDF by extension guess (`:665-667`), no size cap, temp file (`_fetch_binary`, `:722-733`) | `convert_file_to_text` in a thread of the main service | content never in history (FILE_STORAGE_RFC §2.1); the label carries the ref, not the original name (README §1.1) |
| Drive file | `drive:<tag>:<item id>` in `[Drive: <path> (size) ref=drive:<tag>:<id>]` (`src/domain/drive_ref.py:57-75`, `src/domain/user_drive.py:264-276`), kept across turns by `drive_context` | the drive specialist's `open_drive_file` (`src/infrastructure/drive_tools.py:225-233`), or `open_file` on a `drive:` ref (lenient, `src/agents/file_management_agent.py:99-102`) → `DriveOperations.open` (`drive_operations.py:437-449`) → `_open_drive_item` (`:991-1096`) | image ≤ 5 MiB in four formats, PDF ≤ 20 MiB, on provider metadata (`user_drive.py:29-30`, `:49`; `drive_operations.py:1003-1004`, `:1043-1057`); the specialist's model reads a stand-in, the bytes go to the delegating engine (`drive_tools.py:176-178`, `src/agents/drive_agent.py:331-332`) | the same converter, then §29's open bound and the nonce wrapper (`drive_operations.py:1077-1096`) | caps "below the strictest provider limits, so a large scan cannot fail Smart's whole turn" (USER_DRIVE_RFC §4.6); the bound: "a text file from the indexed drive above the cap is never handed to a model to read whole" (USER_DRIVE_INDEXING_RFC §29.2) |
| Email attachment | none; the model passes `email_id` + `filename` | `get_email_attachment` → `EmailSearchService.get_attachment` (`email_search_service.py:198-260`) | never | the same converter; 3 MB cap; 30 000 characters (60 000 after D20) | none recorded beyond the original RFC flow (GMAIL_EMAIL_INDEXING_RFC §2.4) |

Facts that shape the design:
- **The orchestrator is blind on the upload turn, by design.** `process_attachment` builds one part
  `MessagePart(text=label, file_data=…)` (`file_conversion_service.py:327`); every adapter takes the text branch first
  (`src/adapters/claude_adapter.py:983`, `gemini_adapter.py:329`, `openai_adapter.py:565`, `grok_adapter.py:599`), so
  only the label reaches the model. README §1.1 records why: no context bloat, forward by reference, open on demand.
  The legacy branch that runs only without `GCS_MEDIA_BUCKET` still sends bytes on the upload turn
  (`src/handlers/conversation_handler.py:888-913`).
- **Native bytes reach the model only on the tool turn** that opened them (`src/infrastructure/delegation_engine.py:809-823`
  → a `file_data`-only part, `src/domain/llm.py:365-377`), and stay in the transcript for the rest of the loop.
- **Whether a file is native is decided without the consumer.** `is_native_binary` is `image/*` or PDF
  (`src/utils/file_conversion.py:59-61`). The delegation context carries no provider
  (`delegation_engine.py:767-770`). `ProviderCapabilities.vision` is `True` in all four adapters and read by no code;
  `get_capabilities()` is read only for the cache strategy (`src/services/agent_context_builder.py:423`, `:430`). The Grok
  adapter drops every non-image file part with a warning (`grok_adapter.py:641-649`).
- **Two parse engines (before U5; now one).** The indexer's child reads by sniffed kind with pinned engines (pdfminer.six
  directly for PDF; `src/adapters/drive_parser/readers.py`), under rlimits and an unprivileged user
  (`decisions/drive_parser_sandbox_resource_isolation.md`). Before U5 every other read ran markitdown's own type detection
  in-process; U5 retired that path (§4.8).
- **The indexer's chain** (`src/services/drive_indexing_service.py:2851-2986`) is: fetch, sniff (`sniff_kind`,
  `src/domain/document_kind.py`, `sniff_kind`), the chain table (`EXTRACTOR_CHAINS`, `src/domain/drive_extraction.py:30`), the
  router (`model_route`, `:190-205`: no text, under 200 characters per page, over 5 % garbage, parser error), then six
  record- and money-coupled decisions before the reader (`:2912-2927`).
- **Untrusted marking** exists for drive text only: `wrap_untrusted` and the two reminders
  (`src/domain/drive_search.py:584-593`, `:138-147`). Chat and email text reach the model unwrapped. Text prefetched
  for document specialists is unwrapped by an accepted deferral to Slice 3 (`decisions/drive_prefetch_wrapper_slice3.md`).
- **Prefetch.** `AgentCoordinator` turns a `file_ref` into text before dispatch, and for ASYNC intents before enqueue,
  so the text rides in the Cloud Task body (`src/infrastructure/agent_coordinator.py:522-524`, `:544-546`,
  `:576-620`; resolver `FileConversionService.resolve_content(ref, user_id)` wired at
  `src/composition/application_graph.py:362-368`). All three consumers (DocPlanner, PdfGenerator, HtmlPageGenerator)
  are ASYNC. FileManagement and the drive specialist opt out (`src/infrastructure/agent_manifest.py:528`, `:572`;
  USER_DRIVE_RFC §4.4: waste, double fetch, a page per not-connected call).
- **Audio is not transcribed on open.** The production `FileConversionService` gets no transcriber
  (`src/composition/service_container.py:449-459`; `audio_service` stays `None`, `file_conversion_service.py:117`,
  `:127`), so an opened audio file answers "Audio transcription is not available" (`file_conversion.py:240-246`).
  Only voice messages are transcribed (the handler's own `audio_service`, `application_graph.py:936-943`). A drive
  recording the indexer has transcribed opens as its transcript (`drive_operations.py:1031-1041`, USER_DRIVE_INDEXING_RFC
  §27). The gap is a recorded defect: `tests/unit/composition/wiring_none_ledger.txt:72` (`defect:D3`, deferred to U3).
- **The drive's store branches live in `DriveOperations`** (`src/infrastructure/drive_operations.py`), shared by the
  drive specialist and FileManagement: folders, the archived and live transcripts, the owner's note line, the §29 open
  bound (refusal before download for an indexed text above 60 000 characters, head and cut line otherwise), the vision
  caps, video sent to the user, the access note. Untagged or disconnected refs are refused with a code-built line
  (`src/services/drive_connection_service.py:280-296`).

## 2. Goals and non-goals

**Goals**
- Every external file is a ref; opening has one path; acting on a file (save, send, prefetch) resolves the same ref.
- A scan is read wherever it is opened: natively when the consuming model reads it well, by the reader otherwise.
- Each security piece exists once and every path uses it: kind from bytes, caps, isolated parsing, text-or-scan
  routing, the no-tool reader, untrusted marking.
- The document service's contract survives a later move to its own deploy unit (D1) and the sandbox swaps (§4.9).
- No existing limit is raised or removed silently; every change of a limit is a labelled P-n.

**Non-goals**
- Money: limits and budgets for non-drive reads (D5), and the drive's per-class budgets (untouched).
- Image triage, profiler, passages, drive transcription jobs: indexer work on its own state.
- Voice messages (they are the user's own words) and generator inputs (`edit_image`, video edits).
- Redesign of the daily email review (payload placement, tool surface); prompt-layer defences beyond the marking
  function. Its tool texts change only where D4 forces it (§4.7).
- Small defects found during the study (§10 follow-ups). Implementation; a plan follows acceptance.

## 3. Principles (D1–D6 in plain words)

1. **The model sees refs** and opens a file only when it asks, through the verb of its store; every verb runs the
   same code (D3, D11).
2. **The consumer decides native, by declaration.** The adapter of the model that will read the file says which kinds
   it reads natively and up to what size; the open path passes that on and acts on the answer (D2, D3).
3. **Otherwise the pipeline:** kind from bytes → isolated parse → text-or-scan check → no-tool reader → text (D2, D6).
4. **Pieces, not copies.** The indexer and the document service import the same pieces; the indexer keeps its loop (D6).
5. **Remote-ready, not remote:** the contract carries nothing a remote call could not (D1). **No new money rules,** one
   marked place to add them (D5).

## 4. Design

### 4.1 Ref kinds

| Kind | Shape | Store | Label |
|---|---|---|---|
| Chat upload | stored name (unchanged) | GCS uploads | `[File: "<ref>" (size)]` |
| Delivered document | `docs/…`, `email_review/…`, `deep_research/…`, `video_generation/…` + `/{user}/{uuid}-{name}` (unchanged) | GCS media | the delivery note |
| Workspace / skill | `workspace:binary_files/…` / `skill:<name>/<path>` (unchanged) | workspace bucket / Firestore | — |
| Drive | `drive:<tag>:<item id>` (unchanged; the tag names the connection, `MULTI_DRIVE_RFC.md` §9) | the user's drive connections | `[Drive: <path> (size) ref=drive:<tag>:<id>]`; the path starts with the mount name |
| **Email attachment (new, D4)** | `email:<message id>/<part id>` | the user's Gmail, fetched on open | `[Email file: "<name>" (size) ref=email:<message id>/<part id>]`, name escaped like a wrapper attribute |

**P9. One ref classifier.** A pure function in `src/domain/file_ref.py` classifies a ref by prefix in a fixed order
(`email:`, `skill:`, `workspace:`, delivered prefixes, `drive:` anywhere, else a chat name); its drive branch calls the
tagged-ref codec `src/domain/drive_ref.py` (MULTI_DRIVE_RFC §9 names this hook and leaves P9 to this RFC). Today the
order is repeated in each resolver, and `_is_drive_ref` needs an exclusion list because `drive_ref.parse` matches
`drive:` anywhere (`file_conversion_service.py:90-99`, `drive_ref.py:35`, `:61-75`); a new kind would add a branch to
every copy. Resolving a drive ref to its connection stays in `DriveConnectionService.resolve_ref`; the classifier only
names the kind.

**P15. Workspace copies and delivered documents count as external.** Chat uploads, drive files and email attachments
are external by D3. Workspace `files/` copies and delivered documents can carry third-party text, so this proposal sends
them through the document service and marks them too; it changes how Smart sees its own delivered reports. Declined,
they open as today (unmarked). `skill:` refs are the product's own instruction text: resolved and returned as today,
never read by the service or marked.

### 4.2 The open path

**Who does what.** `FileConversionService.open(ref, user, account, consumer, deadline)` does only the generic steps:
1. **Classify** the ref (P9) and **resolve** it in its store (the ownership check on delivered keys; for email one part
   listing per open, from a ref or an email id plus name (D12), §4.7).
2. **Size check on metadata, before download**, against the caps table (§4.5).
3. **Download** the bytes into a per-call staging directory, removed in `finally`.
4. **Read** them through the document service (§4.4), passing the consumer's declaration (§4.3) and the deadline.
5. **Present** the outcome per §4.4's presentation table: native → a file part with the reminder; text → marked
   (§4.6) and capped (§4.5); otherwise a `[System: …]` note worded for the store.
6. **Return refs that outlive the turn** via the existing `*_context` mechanism
   (`conversation_handler.py:1439-1448`): `drive_context` as today, `email_file_context` for email (P8b).

The store-specific branches stay before `open`, where they are today. For chat, delivered, workspace and drive refs
that is `DriveOperations` (`src/infrastructure/drive_operations.py`), shared by FileManagement and the drive
specialist: folders (the list hint), recordings opened as transcripts (`:1031-1041`), archived transcripts
(`:955-975`), the owner's note line, the §29 decision taken before any download (an indexed text above the cap is
refused and read through passages, `:1008-1014`), the drive access note, and video sent to the user (`_fetch_video`
`:735-754`; drive branch `:1058-1073`). For a drive ref, `DriveOperations` passes the §29 extent it already looked up
into `open`, and step 5 applies §29's head-and-cut bound and its cut line (the pure `src/domain/drive_open_bound.py`,
today at `:1082-1096`) before marking, so the cut line stays outside the wrapper as today. `EmailSearchAgent` only
routes. These branches use `DriveSearchService` and `UserNotificationService`, which the conversion service should not
depend on.

**The bytes always come from the caller (P2).** Step 3 calls the store's own download: GCS for chat, delivered and
workspace refs; `UserDrivePort.download` through the connection's handle for drive refs; `get_part` for email. Google
has no pre-signed download URL (`GOOGLE_DRIVE_RFC.md` G2: every content URL needs the `Authorization` header), so a
Google file is downloaded by the caller like any other; nothing in this design hands a URL to the service or the child.

**Provider exports (A2).** A Google native has no file of its own: the adapter downloads an export (Docs as
`text/markdown`, Slides as `text/plain`, Sheets as xlsx, Drawings as PDF; `src/adapters/google/google_drive_mapping.py:30-35`,
`GOOGLE_DRIVE_RFC.md` §3.5). The export enters `open` as bytes with the export mime declared, and the kind follows
G1's rule (the mime wins over a disagreeing name extension). Step 2 skips the metadata size check for a `text/*` item,
as chat does today (a native's storage size says nothing of its text, G1b). From there it is an ordinary file: Markdown and text
exports take the plain-text path in the caller (with G1b's order: up to 10 MiB read, `data:` images dropped, then the
5 MiB cap, `src/domain/text_data_uris.py:11`, `:91`; `file_conversion.py:225-264`); xlsx and PDF exports go through
the parse path, and a Drawing's PDF may go native. Why no special case: an export's text is as untrusted as any file
(a shared Doc's text is third-party), and the provider's rendering adds no guarantee a later step could rely on;
treating it as "text the caller already has" would skip the caps and the marking. Rejected: reading natives through the
indexer's stored text (stale between ticks; absent for out-of-scope folders).

**Intents: one open verb per store (D11).** The store stays in the intent name for reading as well as for mutations
(USER_DRIVE_RFC §4.4):

| Verb | Agent | Opens |
|---|---|---|
| `open_file` (intent) | FileManagement | chat uploads, delivered documents, workspace `binary_files/` copies, skill files |
| `open_drive_file` (local tool; Smart's intent is `work_with_drive_files`) | the drive specialist (`src/agents/drive_agent.py`; tool `src/infrastructure/drive_tools.py:225-233`) | `drive:<tag>:<item>` refs |
| `open_email_attachment` (new intent; replaces `get_email_attachment`) | EmailSearch | `email:` refs in `file_ref`, or an email id plus attachment name (D12, §4.7) |

All three reach the same `FileConversionService.open` (the first two through `DriveOperations`). Each verb's
description names only its own store. A read given another store's ref is still served through the same path, as
`open_file` serves a `drive:` ref today (`file_management_agent.py:99-102`; USER_DRIVE_RFC §4.4, "reading is
lenient").

**`send_file_to_user` (D8).** A new SYNC intent on FileManagement.
- It takes `file_ref` (any ref kind) and an optional `name`, and resolves the bytes with `resolve_file(ref,
  action="send")` under the action cap (§4.5). A drive folder ref is refused with the list hint; a drive ref goes
  through the connection's access policy like any read (`MULTI_DRIVE_RFC.md` §11.3).
- The kind is sniffed with the shared piece. The delivery title names the store and the sniffed kind, so a renamed
  file is not sent under a false kind in the bot's voice.
- **Active kinds from email are not sent (D13).** For an `email:` ref only, these kinds are refused before upload:

| Kind | How the bytes show it |
|---|---|
| HTML / HTM, HTA | markup head (`<!doctype html`, `<html`, `<hta:application`), as the sniffer's HTML rule |
| SVG | `<svg` in a markup head |
| Office with macros (docm, xlsm, pptm; doc, xls, ppt) | an OOXML ZIP with a `vbaProject.bin` part or a macro-enabled content type; an OLE2 file (magic `D0 CF 11 E0 A1 B1 1A E1`, any name) with a `Macros`, `_VBA_PROJECT_CUR` or `VBA` storage (owner 2026-11-05). A plain OLE2 file, and an encrypted OOXML package, is not active |
| Shortcut (lnk) | the shell-link header magic |
| Disk image (iso, img) | `CD001` at offset 32 769 (ISO); a raw image has no magic, so only its name shows it |
| Executable | the sniffer's existing `executable` kind (ELF, Mach-O, MZ; `src/domain/document_kind.py`, `sniff_kind`) |
| Script | a `#!` first line; scripts without one (js, vbs, ps1, bat, cmd) only by name |

  The bytes decide. A name or part mime naming one of these kinds also blocks, never unblocks; this is stricter than
  the bytes alone and covers the kinds without magic. The check reads up to 64 KiB of the head, enough for the ISO
  marker. The head checks are byte compares in the caller (`src/domain/active_kind.py`); the macro check, which must
  list a ZIP's parts, runs in the sandbox as the light op `parse.container_facts` (owner decision O2, 2026-11-05;
  `ContainerFactsPort`, `src/adapters/drive_parser/container_facts.py: container_facts`, sandbox side only: the ZIP facts, or for an OLE2 file the directory's
  `vba_storage`), so no ZIP or OLE2 container is read in the main service.
  When the check cannot run (outage, timeout, a ZIP above the member cap) the file is refused as "could not be
  checked": the rule fails closed.
- The answer is a refusal note, not a failure of the turn: "This attachment is a <kind> file from an email; such
  files are not forwarded or saved. The user can open it in Gmail." Drive and chat refs are sent without this
  check; an HTML report on the drive is sent as any file. The same check, with the same note, runs before an `email:`
  source is saved to the drive (`DriveOperations.copy`, on the file as resolved and under its drive name); a refused
  save uploads nothing. Drive-to-drive and chat-file copies stay unchecked (owner 2026-11-05).
- Delivery goes through `UserNotificationService.notify_file_bytes` (`src/services/user_notification_service.py:692`),
  with `origin_channel_id` and `origin_platform` from the message context as overrides, as the video branch does today
  (`drive_operations.py:1063-1068`).
- That method returns whether the file was uploaded. `False` is answered as a failure ("the file could not be
  delivered"), never as success, as the video branch already does (`VIDEO_NOT_SENT`, `drive_operations.py:1069-1070`).
- No model reads the bytes, so nothing is marked. U4 adds it to `src/utils/capabilities.py`. Other agents may reuse
  the intent later; none does in this RFC.

**Interactive admission** (by the declared kind from name and declared mime, §4.5; today's behaviour kept):

| Kind | Chat / workspace / delivered / drive | Email |
|---|---|---|
| Document kinds the parser reads; plain text, CSV, JSON, Markdown, code | parse (plain text read in-process, sandbox S-Δ2; A3) | parse |
| PDF, the four image formats | native or pipeline (§4.3, §4.4) | native or pipeline |
| Audio | today a note, "transcription is not available" (`file_conversion.py:240-246`); a drive recording with a transcript opens as the transcript (`drive_operations.py:1031-1041`). U3 decides per path under the audio requirement (§4.4) | today a note (`audio_service=None`, `email_search_service.py:255`); U3 decides under the same requirement |
| Video | sent to the user, as today | a note, as today (the model can use `send_file_to_user`) |
| Anything else (archives, executables, other images) | refused with a note | refused with a note |

**Resolver callers.** Every caller resolves through `open` (content for a model) or `resolve_file(ref) →
(bytes, name, mime)` (steps 1–3, bytes for an action):

| Caller | Goes through | Accepts `email:` |
|---|---|---|
| `open_file`, `open_drive_file` via `DriveOperations` (`drive_operations.py:719`, `:725`, `:738`, `:964`, `:1046`, `:1059`, `:1077`) | `open` / `resolve_file` | served if given one (leniency) |
| `open_email_attachment` (EmailSearchAgent) | `open` | yes |
| `send_file_to_user` (new) | `resolve_file` | yes |
| The drive specialist's `copy_file_to_drive` and `replace_file_content` (`drive_tools.py:323-332`, `:381-389` → `DriveOperations.copy` `:455-469`, `.replace` `:538-562` → `_source_bytes` `:909-921`) | `resolve_file` | yes (`copy` adds `email:` to its accepted sources, `:457`) |
| Prefetch for specialists (`application_graph.py:362-368`, moved by P18) | `open`, text only | yes |
| `edit_image` (`src/agents/image_generation_agent.py:214`), video edits (`video_generation_agent.py:269`, `:324`) | `resolve_file` | yes, bytes only |
| Skill file reads (`src/services/skill_service.py:200`) | `resolve_file` | no (skill refs only) |
| Workspace `copy` (`src/services/workspace_service.py:627`) | `resolve_file` | no (its source allowlist is unchanged) |

**Naming on save.** `copy_file_to_drive` takes an explicit `file_name`; without one, an `email:` ref's name comes from
the part (the ref carries none), and every other ref kind keeps today's naming from the ref string (`drive_filename`,
`src/domain/user_drive.py:308-321`, called at `drive_operations.py:465`). Where the copy lands is the connection's
access policy, unchanged: inside its write folder, the primary's when no folder is named (`MULTI_DRIVE_RFC.md` §11.3;
`drive_operations.py:1141-1142`).

**Legacy upload branch.** The handler's branch without `FileConversionService` (`conversation_handler.py:888-913`)
is retired in U3: the upload path always stores a ref.

**P18. Prefetch reads in the async worker.** For ASYNC intents the `file_ref` travels in the Cloud Task payload and
`AgentWorkerHandler` opens it just before the specialist runs, under the task's own time; no reader text rides in a
task body, which suits D3. A SYNC prefetch (none today) would use the interactive deadline. The resolver gains
`account_id`: `open(ref, user_id, account_id, consumer=(), deadline)` replaces `resolve_content(ref, user_id)` at
`application_graph.py:362-368` (the coordinator's docstring still names a third `mime_type` argument the call never
passes, `agent_coordinator.py:64`, `:602`). On a failure the specialist gets no `file_content` and a separate `file_note` it is told to report (never
rendered into the document). A recording, folder or video ref gets a `file_note` too. Declined, prefetch keeps running
in the caller, under the interactive deadline.

### 4.3 The adapter's declaration and how it reaches the open path

**P5. `LLMPort.native_formats(model_name) -> Tuple[NativeFormat, ...]`**, where
`NativeFormat(mime, max_bytes, max_pages)` is a pure domain value. What a model reads is a property of the model, not
the adapter (`grok_adapter.py:107` sets one `vision=True` whose comment names the models that see). The port gives a **concrete text-only default `()`**: each
of the four adapters overrides it, the proxies forward it (`AlertingLLMProxy`, `CachingLLMProxy`, as they forward
`get_capabilities`, `src/services/alerting_llm_proxy.py:93`, `caching_llm_proxy.py:47`), and the six `LLMPort` test
fakes in five test files need no edit. An abstract method would force an edit to each fake through the reviewer.
This deviates from `src/ports/CLAUDE.md` ("All methods — `@abstractmethod async def`") on purpose, twice: the method
is concrete (a safe default exists: "reads nothing natively", so a forgotten override degrades to today's text path,
never to a crash) and synchronous (a static property of the model, no I/O). The precedent is
`LLMPort.supports_caching()`, which D3 names.

**Initial values never exceed today's drive caps** (`user_drive.py:29-30`); raising any of them is an owner decision.

| Adapter | Declares (U0-e confirms `max_pages` and checks the bytes) |
|---|---|
| Claude | the four image formats ≤ 3.75 MiB raw (its 5 MB limit applies to the base64, `src/domain/drive_image_mime.py:12-16`); PDF ≤ 20 MiB |
| OpenAI, Gemini | the four image formats ≤ 5 MiB; PDF ≤ 20 MiB |
| Grok | the four image formats ≤ 5 MiB only (D2: xAI reads PDFs only through its Files API and a server-side search tool, neither implemented) |

These same values now apply to chat, workspace, delivered and email opens, which have no native cap today.

**How it travels** (A):
- Each engine runner sets the key on a **copy** of its context, `{**message.context, "native_formats": [...]}`, from
  the adapter that serves the engine's calls, **per attempt**, always overwriting, never inheriting. It is never
  written into `message.context`.
- The value is JSON-plain (a list of dicts), so the Cloud Task encoder, which handles pydantic models, dates and
  enums but no dataclass (`src/adapters/gcp_task_queue.py:23-35`), never meets one.
- The coordinator drops the key before it builds a specialist's message (`agent_coordinator.py:531`) and before it
  enqueues (`:558-564`), except for the three SYNC intents that open files (`open_file`, `work_with_drive_files`,
  `open_email_attachment`), the only readers of the key.
- **The drive specialist passes the key on (A5).** It runs its own engine loop, but its model reads a stand-in for an
  opened file (`drive_tools.py:176-178`) and the bytes go back to the delegating engine in `metadata["file_data"]`
  (`drive_agent.py:331-332`). So the consumer is the engine that delegated `work_with_drive_files`, and the specialist
  hands the key it received to `DriveOperations.open` unchanged; it never sets one from its own adapter.

| Engine runner | Engine call | Adapter it declares from | Opens files today |
|---|---|---|---|
| Smart | `smart_response_agent.py:572` | `eff.ctx.provider` of the current `_run` attempt (`:568`) | yes |
| Quick (fallback and `notify` formatter) | `src/agents/core/quick_response_agent.py:242` | its own `self.llm` | yes, as a fallback |
| DomainResearcher | `src/agents/domain_researcher_agent.py:139` | its own `self.llm` | yes, `open_file` is its only intent (`agent_manifest.py:647-649`) |
| Tutor | `src/agents/tutor_agent.py:191` | its own `self.llm` | no (`search_web` only) |
| Drive specialist | its own loop | none: passes on the key it received (A5) | yes, every drive open |

Lelik's `dispatch` passes no key; its allowlist has no `open_file`. The declaration stays valid for the transcript:
cross-provider failover stops at the first tool turn, and provider rotation re-runs `_run` from scratch with a new
`eff`, files included (`src/agents/CLAUDE.md`, "Transcript integrity", "Cross-provider execution retry").

Rejected: *reading `ProviderCapabilities.vision`* (one boolean, true everywhere, silent on PDFs and size); *the adapter
picking native or text while building the request* (the text would be produced for every open, reader included).

**P16. The native decision sits inside the document service.** D3 records "the open path asks and either attaches the
file or sends it to processing". Here the open path asks (it passes the declaration) and acts on `NATIVE`, but the
service decides. The reason is that a PDF must be page-counted and checked for encryption before it is admitted, and
that is a parser op in the isolated child (later in the sandbox). The cost is that under a remote unit (D1) a native
open ships the file to the unit and back. Declined, the open path decides images itself, and for PDFs calls a
page-count op first.

**No file contents in Logfire (D9).** File parts (native bytes, the reader's windows, triage thumbnails) are dropped
from the captured spans of every model call: the orchestrator's, the reader's, any agent's. Calls, tokens and text
stay. Today content capture is on in the main service (`LOGFIRE_CAPTURE_CONTENT=true`, `cloudbuild-dev.yaml:35`), and
the provider SDKs are instrumented globally (`src/utils/telemetry.py:51-100`); the indexer has no Logfire.
- **Existing gap.** Today's native drive opens already put file bytes into captured spans
  (`drive_operations.py:1043-1057`).
- **Scrubbing cannot reach them.** The pinned Logfire (5.2.0, `requirements.txt:93`) treats `gen_ai.input.messages`
  and `gen_ai.output.messages` as safe keys and never walks them (`logfire/_internal/scrubbing.py:123`, `:166-167`,
  `SAFE_KEYS`). Its
  Anthropic and OpenAI integrations write image and document bytes into those attributes as `BlobPart`s.
- **The reader's output text is file content**, so for reader calls it goes too. Calls and tokens survive through our
  own metadata span `llm.call` (`src/agents/base_agent.py:1307`).
- U0-a finds the way (§8), with an exit to the owner if none works.

**Known limitation: totals per request.** Each file is checked against its own caps. Several native files in one loop
add up in every later call of that loop. That is today's behaviour, not introduced here. U0-e measures it; no tracking
mechanism is built now.

**Images the consumer cannot take natively** (R). The reader takes the four formats up to 3.75 MiB
(`src/composition/drive_reader_runner.py:174-179`). An image over the consumer's cap or in another format (HEIC) gets
a clear note, "too large to view" or "format not readable". Today such an image on Claude fails the turn, so the note
is an improvement. A text-only consumer's photo goes to the reader without triage, so its text is a verbatim reading,
not a description. **P17 (optional follow-up):** normalise images in the child (decode, downscale or convert to JPEG
under the cap) before the native check and the reader.

### 4.4 The document service contract (D1)

```python
# src/domain/document_read.py — pure, mypy --strict
class ReadStatus(str, Enum):        # ExtractionStatus's per-file values, plus four
    TEXT; NO_TEXT; UNSUPPORTED; TOO_LARGE; ENCRYPTED; TIMEOUT; MEMORY; ERROR
    NATIVE                           # attach the bytes you sent, as `native_mime`
    MODEL_UNAVAILABLE                # the reader cannot run now (429, 5xx, open breaker); spend so far reported
    PARSER_UNAVAILABLE               # detail: unavailable | misconfigured | busy; systemic, not charged to the file
    STILL_WORKING                    # reserved for a remote adapter (D1); the in-process service never returns it (D7)

@dataclass(frozen=True)
class ReadRequest:
    content: bytes                   # staged by the open path; the service never fetches (P2)
    name: str                        # for log lines and the sandbox's `ext`; never addresses anything
    declared_mime: str
    store: str                       # "chat" | "delivered" | "workspace" | "drive" | "email": keys the caps
    caller: str                      # "open" | "prefetch": keys the deadline and the presentation
    native_formats: Tuple[NativeFormat, ...]   # empty: text only
    user_id: str; account_id: str    # the reader bills and configures by them
    deadline_at: float               # one absolute deadline, from the start of the open
    request_id: str

@dataclass(frozen=True)
class ReadOutcome:
    status: ReadStatus
    kind: str                        # sniffed
    native_mime: str                 # set when NATIVE
    text: str                        # raw, unmarked (§4.6)
    pages_total: int; pages_read: int; skipped_pages: Tuple[int, ...]; truncated: bool; may_be_incomplete: bool
    links: Tuple[str, ...]           # `extracted_by` of every link that ran, e.g. ("parser@3", "reader@2")
    model_calls: int; model_cost_usd: float    # already billed to the account; reported, not booked
    detail: str                      # a failure label, never document text

# src/ports/document_read_port.py
class DocumentReadPort(ABC):
    async def read(self, request: ReadRequest) -> ReadOutcome: ...   # every result is an outcome; it never raises
```

**`DocumentService(DocumentReadPort)`** in `src/services/document_service.py`. Its constructor takes the parser and
the reader (both `ContentExtractorPort`), the splitter (`PdfSplitterPort`, explicitly: step 3's page count and
encryption check call it directly), and, if U3 wires audio on any path, a transcription port (P21, below). These two
ports are the seams the sandbox swaps under (D14): `ContentExtractorPort` for the parser, `PdfSplitterPort` for the
page ops.

**`PdfSplitterPort` (U1, sandbox S-Δ4 pulled forward).** Today's Protocol has two methods,
`pdf_page_count(path)` and `cut_pdf_window(path, first_page, page_count, out_path)` (`drive_reader_runner.py:73-78`);
the parser adapter serves both (`drive_model_setup.py:124` aliases it as the splitter). U1 promotes it to
`src/ports/pdf_splitter_port.py` with two additions: an optional `deadline_at` on every method (only lowering, like the
request limits below), and a batch `cut_pdf_windows(path, plan, out_dir, deadline_at=None)` that cuts several windows in
one child run (in process: one slot wait instead of one per window; in the sandbox: one upload, `parse.pdf_windows`).
The reader keeps holding the splitter; with a text budget it calls `cut_pdf_window` per window (one at a time, §4.4),
without one it may batch. The indexer passes no deadline and keeps today's calls.

**`read()` in order:**
1. **Kind from bytes.** The declared kind comes from `kind_of(name, declared_mime)`, then `sniff_kind` checks it on the
   first 8 KiB; for images `image_mime` reads the head. Admission per §4.2's table.
2. **Cap check** for the store and kind (§4.5); over the cap → `TOO_LARGE`.
3. **Native** (P16). If the sniffed format is in `native_formats` and within `max_bytes`: an image → `NATIVE`; a PDF →
   page count and encryption check through the splitter (the child's page op), then `NATIVE` within `max_pages`.
   Over the page limit or encrypted → the pipeline.
4. **Pipeline.** `chain_for(kind)` → the parser in its child → `model_route` → the reader when routed, for at most
   `allowance()` pages (P13), within the deadline. Plain text is read in-process (sandbox S-Δ2; A3 below). Audio
   follows P21 below.
5. **Return** the outcome, mapped per the status map (§4.5), with `links` and the spend.

**Plain text in the caller (sandbox S-Δ2, A3).** Reading UTF-8 is not parsing, so the service reads the kinds with no
converter in process: `text`, `markdown`, `json`, `code`, `log`, with chat's decode and G1b's order (`data:` images
dropped before the 5 MiB cap, `file_conversion.py:253-268`). Before U5, every other `text/*` upload was read the same way,
as today (CSV and HTML raw). Since U5, `csv`, `html`, `eml`, `rtf` and `subtitles` go to the child's readers
(`src/adapters/drive_parser/readers.py`, table `_READERS`), because each of them runs a real converter (a CSV dialect sniffer, an
HTML, MIME or RTF parser). `mhtml`, `other` and `data_dump` have no child reader and stay raw.

**Statuses, not strings (U3).** The child returns an `ExtractionOutcome` with a status; today's converter returns a
string, alerts included (`file_conversion.py:108-157`). The chat layer therefore rebuilds today's `[File: …]` wrapper
and `[System: …]` alert texts from `ReadOutcome.status` and `detail`, so a chat user sees the same notes as today for
the same failure. The 60 000-character cut (D20) sits in the chat and email presentation (§4.5); the child's own ceiling
(`MAX_TEXT_CHARS`, 2 000 000) is not a presentation cap.

**P21. Audio on open (U3 requirement).** Today no opened audio file is transcribed (§1.2), and the gap is the `defect:D3` line of
`tests/unit/composition/wiring_none_ledger.txt:72`. U3 decides per path (chat upload, drive file, workspace and
delivered file, specialist prefetch) whether that path transcribes, and any path that does meets all of these:
- **The drive's transcript first.** A drive recording reuses its stored transcript (USER_DRIVE_INDEXING_RFC §27, as
  `drive_operations.py:1031-1041` does today); a live transcription never duplicates one the indexer holds or will make.
- **Drive recordings: the §27 opt-in and cap hold.** A drive recording is transcribed live only where its connection's
  opt-in (`DriveConnection.transcribe_enabled`) and the global `DRIVE_TRANSCRIBE_ENABLED` allow it
  (USER_DRIVE_INDEXING_RFC §27.12, OQ-T3; sandbox O4), and every such call counts against the month's transcription cap
  (`TRANSCRIBE_USD_PER_MONTH`, `src/domain/drive_index_params.py:212`, D46); none bypasses it.
- **Chat and other non-drive audio: no new budget (D5).** The §27 cap applies to drive recordings only; a chat upload,
  workspace or delivered recording is metered and cached, not capped.
- **Metered.** Each call is billed to the account like the reader's calls, with its model and seconds.
- **Cached by content key.** The same bytes are transcribed once (a key over the content hash and the transcriber's
  version); a repeated open or a re-delegation reads the cached text.
- **Never on a prefetch by default.** A specialist prefetch (P18) does not transcribe unless U3 names the specialist
  that needs it.
- **Within the opener's deadline.** A transcription that cannot finish inside the open's remaining time (§4.4 "Long
  scans") answers the timeout-style note; it never outlives the agent's timeout.
U3 deletes the `defect:D3` line in the same PR as its decision. A path U3 leaves without transcription keeps today's
note.

**Two request limits, both only lowering (C, D7).** `ExtractionRequest` gains two optional fields, and neither may
raise an adapter's own ceilings:
- a **deadline**: the parser child's wall becomes min(120 s ceiling, remaining), the reader's budget min(its
  constructor value, remaining);
- a **text budget** in characters: the reader reads window 1 (pages 1–10) alone, then the remaining windows one at a
  time, in page order. Before cutting each window it checks the budget, and stops once the text read reaches it, so
  nothing beyond the budget is read or billed. With a budget, the run keeps one shared prefix counter, which changes
  the runner's "no state shared across tasks" invariant (`src/composition/drive_reader_runner.py:82-83`). Without one,
  windows run four in flight as today (`:207-217`).

The indexer passes neither and keeps today's behaviour. U3 proves it: with both fields unset, the reader's existing
tests pass unchanged. The sandbox adapter maps the deadline onto the request's
`limits`, which may only lower the sandbox's own (sandbox §4.8), so S3/S4 need no second port change. This is a change
to `ContentExtractorPort`'s request; §6 declares it.

**Long scans: a simple cut (D7).** An interactive scan read reads only as many pages as can be shown, and has no
"still working" path.
- **Text budget.** It equals the presentation cap for reader text (§4.5, A4/D17, D20): 60 000 characters on every path (today's
  30 000 outside the drive moves with U3), the drive's being §29's bound (`OPEN_MAX_CHARS`, `src/domain/drive_index_params.py:265`), so a drive scan is
  read as far as §29 would show it and no further.
- **Deadline.** Each open keeps its agent's existing timeout, minus a 30 s margin for presentation:
  - FileManagement: 120 s → 90 s (`src/composition/user_agent_factory.py:903`). Its reads are not shielded
    (USER_DRIVE_RFC §4.13).
  - The drive specialist: the time its run has left (`drive_tools.py:85`; soft deadline 150 s, hard 180 s,
    `src/infrastructure/agent_config.py:437-450`), minus the same margin.
  - EmailSearch: 300 s → 270 s (`src/infrastructure/agent_config.py:202`); the daily review inherits the same
    timeout as today. The value stays; only its markitdown comment (`:201`) is updated in U4.
- **The note says what is shown:** "pages 1–N in full", plus "text cut within pages N+1–N+10" when the cap falls inside
  a window, plus "page k skipped" for each page the provider refused or that hit the output limit (`:282-287`,
  `:309-313`). `ReadOutcome.pages_read` counts the pages shown in full, and `skipped_pages` lists the skipped ones.
  This is D7's "first N of M pages", stated without false precision: the reader's output has no page markers.
- **At the deadline** the window in flight is cancelled; what was read before it is shown. With nothing read, the
  outcome is an error (`:372`).
- **Trade-off.** Reading one window at a time is slower for a sparse long scan, so it reaches the deadline sooner. It
  still returns a prefix.
- **Measured reader time** on the owner's drive (30 days, at most 17 pages per file): 12.6 s at p50, 23 s at p90,
  62 s at most.

**P13. `allowance()`, the place D5 left open.** One method, `allowance(user, account, store) -> pages`, called before
the reader. It returns the reader's existing per-file ceiling `MODEL_EXTRACT_MAX_PAGES` (200,
`src/domain/drive_index_params.py:75`, used at `drive_reader_runner.py:194`) for **every** store, `drive` included.
An interactive drive open books nothing against the drive's monthly reader pages; only the indexer touches the drive
ledgers. `BUDGET_EXHAUSTED` (`drive_reader_runner.py:130-131`) is therefore unreachable from the service. A later
budget replaces this one method.

**Outage and weak text (F).** Only when the new request fields are set, a reader outage mid-file returns the contiguous
finished prefix as truncated text with a note, and the spend. Without them the runner raises as today (`:222`), so the
indexer's handling of `DriveModelUnavailable` is unchanged. When the reader is unavailable for a low-density parse,
the parser text comes back with `may_be_incomplete` and a note saying so.

**Parser slot (G; superseded in the deployed runtimes by the light service, UNTRUSTED_CONTENT_SANDBOX_RFC O14: no slot, busy = 429/503).** The child runs one at a time per adapter (`src/adapters/subprocess_content_extractor.py:101-102`, `max_children=1` at `:89`). In
the main service that slot is shared by every user's opens, page counts and window cuts. Every wait for it, the
splitter's included (its `deadline_at`), is bounded by the remaining deadline; past it the outcome is
`PARSER_UNAVAILABLE(busy)`, which is not a bomb. `max_children` for the
main service is sized from U0-c.

**Presentation** (in the open path):

| Outcome | What the model gets |
|---|---|
| `NATIVE` | the file part with `REFERENCE_REMINDER_ATTACHED`, worded per store |
| `TEXT` | marked text, capped per store (§4.5; drive: §29's bound and cut line), with the truncation or "may be incomplete" note when set |
| `TOO_LARGE`, `UNSUPPORTED`, `ENCRYPTED`, `NO_TEXT`, `TIMEOUT`, `MEMORY`, `ERROR` | the existing `[System: …]` note contract, worded for the store |
| `MODEL_UNAVAILABLE` | "the document reader is temporarily unavailable; try again later" |
| `PARSER_UNAVAILABLE` | "the file converter is temporarily unavailable; ask the user to send it again later" (sandbox §4.9) |
| `STILL_WORKING` | the timeout-style note "the file could not be read in time; try again" (no async path, D7) |

Both notes are defined and tested in U3, `STILL_WORKING` with a fake adapter, so S4 and a later remote unit (D1) only
swap an adapter.

**P1. A port with one implementation today.** The repo asks for a port at a boundary with two implementations. D1
names the second (a remote adapter), and the port gives test substitution now. Precedent:
`SearchEnrichmentService(SearchEnrichmentPort)`, `FactWriteService(FactWritePort)`. Trade-off: one more port before
its second adapter exists.

**P2. Bytes in, never a ref.** The service holds no store credentials: no Gmail, no Graph, no GCS. If it moves out
(D1), it is a bytes-in unit, and no third process refreshes users' OAuth tokens. The sandbox has the same rule:
"the caller fetches" (`UNTRUSTED_CONTENT_SANDBOX_RFC.md` §1).

| D1 requirement | How it is met |
|---|---|
| Serializable input, closed-schema outcome | bytes and plain values; no paths, callbacks or ContextVars; outages are statuses, not exceptions |
| Explicit deadline, "still working" state | `deadline_at` in the request; `STILL_WORKING` in the schema with a minimal presentation (D7: no async path) |
| Billing inside, no shared caller state | the reader runner bills the account per call, as on the indexer; the service keeps nothing between calls |

### 4.5 Shared pieces and where each lives

**What moves to source-neutral modules** (P10, no re-export shims; import lines change, which the reviewer rule
self-authorizes after a module move):
- `src/domain/document_kind.py`: `sniff_kind` (from `drive_sniff.py:58`), `image_mime` (from `drive_image_mime.py:19`),
  `kind_of` with `_EXT_KIND` and G1's mime table (from `drive_index_policy.py:18-37`, `:122`). A second, rendition-only
  `image_mime` lives in `src/domain/drive_index_run.py:107`; it stays there under its own name, so the moved one is the
  only `image_mime` in the neutral module.
- `src/domain/document_extraction.py`: `EXTRACTOR_CHAINS`, `chain_for`, `model_route`, `garbage_share`,
  `failure_label`, `ExtractionStatus`. The router's two thresholds stay in `drive_index_params.py` and are imported.

**What stays on the drive side:** `ExtractionRequest`/`ExtractionOutcome` and `DriveModelUnavailable` in
`drive_extraction.py` (they carry `DocumentBrief`, `CaptureSignals` and `model_fallback_cost`, which are drive
concepts), `TriageVerdict`, capture settling, the indexer's admission tables (`admission`, `ADMITTED_KINDS`) and its
parameters. The reader keeps its class names, agent type `drive_reader`, prompt profile and `STRATEGIES` key: renaming
them would touch Firestore prompts, users' `agent_providers` keys and billing labels. The drift in the name is
accepted and recorded here.

**Status map:** the eight shared `ExtractionStatus` values map one to one. `BUDGET_EXHAUSTED` is unreachable (P13).
`DriveModelUnavailable` maps to `MODEL_UNAVAILABLE` with its spend. `SandboxUnavailable`, `SandboxMisconfigured` and a
slot wait past the deadline map to `PARSER_UNAVAILABLE`.

| Piece | Today | Under this RFC | Used by |
|---|---|---|---|
| Kind from bytes | `sniff_kind`, `image_mime`, `kind_of` in drive modules | `document_kind.py` | indexer `_fetch`, document service |
| Caps | 9 constants in 7 modules | one table, `src/domain/document_limits.py`, by store and kind (below) | open path (metadata), document service, parser (`size_cap`) |
| Isolated parsing | `SubprocessContentExtractor` (indexer only) | the same adapter in both runtimes | indexer chain, document service |
| Text-or-scan routing | `drive_extraction.py` | `document_extraction.py` | indexer `_extract`, document service |
| No-tool reader | `DriveReaderRunner` + `DriveDocumentReaderAgent` (indexer) | unchanged, also built in the main service | indexer chain, document service |
| PDF page ops | `PdfSplitter` Protocol in `composition/` | `ports/pdf_splitter_port.py` | reader, document service |
| Untrusted marking | `wrap_untrusted` + reminders in `drive_search.py` | `src/domain/untrusted_text.py` (§4.6) | open path, prefetch, drive search and transcripts |
| Container caps, converter allowlist | `src/utils/document_hardening.py` | unchanged | parser child |

**Caps table** (values unchanged unless labelled):

| Cap | Value | Note |
|---|---|---|
| Input to text parsing, interactive | 5 MiB (`MAX_FILE_BYTES`, `file_conversion.py:31`) | every store; email too (D10; 3 MB today) |
| Plain text and text exports, before `data:` images are dropped | 10 MiB (`MAX_TEXT_EXPORT_BYTES`, `src/domain/text_data_uris.py:11`; GOOGLE_DRIVE_RFC G1b) | the 5 MiB cap applies to the text after the drop |
| Native, per file | the consumer's declaration, never above 5 MiB image / 20 MiB PDF | §4.3 |
| Drive download, any | 45 MiB (`MAX_DRIVE_DOWNLOAD_BYTES`, `user_drive.py:28`); a Google export 10 MiB (Google's cap, `GOOGLE_EXPORT_MAX_BYTES`, `src/adapters/google/google_drive_mapping.py:91`) | the sandbox-era bound is 32 MiB if sandbox S0-a fails (sandbox O6) |
| Presentation, email text | 30 000 characters today (`MAX_CONVERTED_CHARS`, `file_conversion.py:32`; `truncate_with_alert` at `email_search_service.py:260`) | **D20:** 60 000, realised in U3 |
| Presentation, drive text | 60 000 characters, §29's open bound (`OPEN_MAX_CHARS`, `drive_index_params.py:265`; `src/domain/drive_open_bound.py`): an indexed text above it is refused before download and read through passages, any other is cut at a paragraph with a cut line | kept (owner rule, USER_DRIVE_INDEXING_RFC §29.2); also the drive reader's text budget (A4) |
| Presentation, reader text, other stores | 30 000 characters today | **D7**: also the reader's text budget; **D20:** 60 000, realised in U3; the note says what is shown |
| Presentation, parser text, chat, workspace and delivered | none today below the 5 MiB input | **D20 (P19):** 60 000 characters with a note naming the cut, realised in U3; one cut on every open path |
| Bytes for an action (save, send) | 45 MiB (`MAX_VIDEO_ATTACH_BYTES`, `drive_operations.py:147`) | `resolve_file` takes the action; today's re-send cap, the platform margin |

**The indexer's use (D6), at the seams that exist today:**
- `DriveIndexingService(…, extractors=…)` (`drive_indexing_service.py:704-706`, `:732-734`): unchanged.
- `build_drive_models(parser=…)` (`src/composition/drive_model_setup.py:116-130`, called at `indexer_main.py:136-144`):
  unchanged; the main service gets its own `build_document_service(parser=…)` beside it.
- `ContentExtractorPort` (`src/ports/content_extractor_port.py`): one change, the two optional request limits (§4.4);
  the indexer passes neither.
- `_fetch`, `_extract`, `_model_hold`, `_affordable`, `_spend_usd`, record states, versions, the upgrade pass:
  untouched; the indexer imports the moved pure pieces. Whether the indexer goes through the service or through the
  pieces is settled by D6 as "the pieces".

### 4.6 Untrusted marking: one function

`wrap_untrusted(text, *, nonce, store, ref, label)` in `src/domain/untrusted_text.py` is today's function: the nonce is
the defence, escaping is minimal (Δ34), look-alike tags are neutralised. It takes its tag from the store: **the drive
keeps `drive_document`**; chat, email, workspace and delivered text get a neutral tag, `external_content`, and the
look-alike check covers both words. Drive output, the `drive-search` skill and the G3 bar do not change.
- **Owner step.** The standing `untrusted` line of `PROTOCOL_AGENT_SELECTION` (Firestore) names only the drive tag.
  It must also name the new tag before chat (U3) and email (U4) text is marked. That is a prompt upload by the owner.
- **P11 (optional, its own slice):** rename the drive tag to the neutral one as well. It touches drive search,
  transcripts, the standing line (owner upload) and the G3 bar.
- **P14 (separate):** mark email bodies from `get_email_details` (`email_search_service.py:165-192`) and the email
  search lines (`delegation_engine.py:143-168`) with the same function. They are not files; this changes two tool
  outputs, so it is outside U4's done-criterion. **Built (P14 PR):** the body with its attachment names is one
  `<external_content ref=<email id>>` block, the search lines one block around the list (a block per hit would double
  the lines); each block has a fresh nonce and `EXTERNAL_REMINDER` before it. The name attribute is empty: neither
  output carries a subject today. The standing `untrusted` line names email in the repo snapshot; the owner's upload
  ships it.
- Native bytes cannot be wrapped: they travel with `REFERENCE_REMINDER_ATTACHED`, worded per store, and the label is
  escaped like a wrapper attribute.
- Content built from stored data (search cards, transcripts) never passes through `read()`, so marking cannot live
  inside it; it lives in one function that every presenter calls.

### 4.7 Email specifics (D4)

- **P8a. Part-level fetch** (realises D4). `EmailProviderPort` gains `list_parts(creds, message_id) →
  [EmailPart(part_id, filename, mime, size)]` and `get_part(creds, message_id, part_id, max_bytes) → bytes`. The Gmail
  adapter walks the whole MIME tree. Today it downloads only top-level parts with an `attachmentId`
  (`src/adapters/gmail_provider_adapter.py:419-424`) while it lists nested names (`:343-356`). It reads a part's
  inline `body.data` when there is no `attachmentId`, and downloads only the part asked for.
- **One listing rule** in the adapter, checked by U0-d: a part with a filename is an attachment; inline images with a
  `Content-ID` are listed as attachments, as today; a forwarded `message/rfc822` is listed as one part, and its
  children are not listed.
- **`open_email_attachment` (D11)** takes the ref in `file_ref`, like the other open verbs. `EMAIL_SEARCH` sets
  `prefetch_file_ref=False` (the default is on, `src/infrastructure/agent_registry.py:125`), so the coordinator never
  pre-opens the ref as text. The agent only routes. `FileConversionService.open`'s email branch lists the parts once,
  finds the part, checks the size, calls `get_part` and reads. The part's own `mimeType` is the declared hint; the
  bytes decide. Without a `FileConversionService` (no file storage, `src/composition/service_container.py:449-460`)
  the intent answers "attachment opening is not configured", as FileManagement is not built then
  (`user_agent_factory.py:897-898`).
- **An email id plus attachment name as a second input (D12).** The index stores attachment names only, so `search_emails` results and the `email_search_context` kept in
  history (`src/agents/email_search_agent.py:313-340`) carry names, never part ids. `open_email_attachment` therefore
  also takes `email_id` + `name`. The email branch resolves the part from the same single listing. A name matching
  several parts opens none, and the answer lists their refs. Rejected: `search_emails` calling `list_parts` for every
  hit with attachments (one Gmail call per hit per search), and storing part ids in the index (a re-index).
- **Notes** (the same wording for `open` and `resolve_file`):

| Case | Note |
|---|---|
| The message is gone | "That email is no longer in the mailbox." |
| Message exists, part missing | "That attachment is no longer in this email." |
| Part with neither `attachmentId` nor data | "That attachment is empty." |
| Gmail 429 / 5xx / timeout | "The mailbox did not answer; try again shortly." |
| Not connected, needs reconnect | today's notes |

  A reconnect to a different mailbox makes old refs read as "no longer in the mailbox"; that is accepted.
- **Refs from `get_email_details` (D11).** It lists the message's attachments as labels with refs. That costs one
  `list_parts` call, because `deep=False` holds names only (`src/domain/email.py:51-64`). This was P8c in rev 2; D11
  makes it required.
- **P8b. `email_file_context`** (realises D4's "the ref in history stays valid"). `get_email_details` and
  `open_email_attachment` return `history_context["email_file_context"] = [{name, ref}]`, so "open the second
  attachment" works on the next turn.
- **Caps (D10).** Email uses the shared caps (§4.5). The 3 MB guarded a download of every attachment of the message
  into memory at once (`email_search_service.py:43-44`, `:219`); per-part fetching removes that. The 10 MB "total"
  guard is unreachable today (`:236-240`).
- **Wiring.** `EmailSearchAgent` gets `FileConversionService` by constructor injection (built at
  `src/composition/user_agent_factory.py:514-526`). The open path gets Gmail by constructor injection of `EmailSearchService`, under a TYPE_CHECKING
  import (as `DeepResearchDriveCopyService` injects `UserDriveService`). Two public methods are new on it:
  `list_attachment_parts(user_id, message_id)` and `get_attachment_part(user_id, message_id, part_id, max_bytes)`.
  Each wraps today's private refresh (`_maybe_refresh`, `email_search_service.py:295`) around the port call.
- **Intent and tool texts** (D11, in U4):
  - The manifest drops `GET_EMAIL_ATTACHMENT` and adds `OPEN_EMAIL_ATTACHMENT` (`agent_manifest.py:46`, the
    capability `:194`, the description `:208-212`, the context schema `:218-221`). The new context is `{"file_ref"}`,
    or `{"email_id", "name"}` (D12); the descriptor sets `prefetch_file_ref=False`.
  - `EmailSearchAgent` routes the new intent instead of `_handle_get_attachment` (`email_search_agent.py:85`, `:110`).
  - The hint under search results (`delegation_engine.py:164-167`) and the two daily-review lines
    (`src/services/email_review_service.py:176-177`, `:193`) name `open_email_attachment`.
  - The email workflow in `PROTOCOL_AGENT_SELECTION` names `get_email_attachment`; changing it is an owner prompt
    upload (mirror `prompts_snapshot/tokens/system/PROTOCOL_AGENT_SELECTION.groovy:60`, `:72-74`).
  - The drive specialist's `copy_file_to_drive` tool text (`src/domain/drive_tool_texts.py`) and `send_file_to_user`'s
    description name `email:` refs.
  - Every other mention of the removed intent changes in U4:
    - the read-only dry-run set in `scripts/validation/ab_agent_models.py:84` gains `open_email_attachment`;
      `send_file_to_user` stays out of it;
    - `src/agents/CLAUDE.md` names no email intent today (its roster row names EmailSearch only), so it changes only
      if U4 adds a trap;
    - the building-block READMEs `agent_registry`, `quick_agent_delegation`, `multi_agent_system`,
      `gmail_email_indexing`; `docs/04_solution_strategy/current_implementation/STRUCTURE.md:515`.
  - **Tests** that encode the removed intent, named for the reviewer, with this section as their spec:
    `tests/unit/agents/test_email_search_agent.py:312`, `:384-466`; `tests/unit/services/test_email_review_service.py:310`.
- **The old in-process attachment path is removed in U4.** It loses its only caller, and leaving it would keep a
  second, unisolated parse path one call away (D6):
  - `EmailSearchService.get_attachment` (`email_search_service.py:198-260`);
  - the adapter's `deep=True` download (`gmail_provider_adapter.py:188-199`, `:232-235`, `:391-429`) and its port doc
    (`src/ports/email_provider_port.py:43-44`);
  - `EmailFullContent.attachment_binaries` (`src/domain/email.py:64`).
- **Save to drive and send** with an `email:` ref: the drive specialist's `copy_file_to_drive` → `resolve_file` → the
  part's name → the connection's access policy (§4.2), or `send_file_to_user` (§4.2).
- **Daily review.** It reaches attachments through the same tools and the same 300 s agent timeout, so it gains scan
  reading; nothing else in it changes.

### 4.8 The parse engine (D6, D15)

**What D6 requires** is isolated parsing on every path. U3 runs the main service's parsing in the child; U5 makes the
child's own readers the only engine.

**One engine, the child's own readers, for every source (D15, built in U5).** `convert_file_to_text` and `build_markitdown`
are gone; markitdown's auto-detect (a converter picked at runtime, exception fallbacks) has no caller (the final go is D16; the U0-b real-file report is not a gate, §8). markitdown stays
only as the child's converters for DOCX, PPTX, EPUB and MSG, an allowlist fixed in `document_hardening.py`
(`MARKITDOWN_ALLOWED_CONVERTERS`); a guard test keeps any import of it inside `drive_parser/`. `.eml` HTML bodies no longer
use markitdown's HTML converter: they go through the child's `html_page_text`.

| Evidence | Source |
|---|---|
| Only the child bounds memory, and parsers amplify: a 5 KB XLSX reached ~1.75 GB, a 408 KB PDF ~955 MiB | USER_DRIVE_INDEXING_RFC §9 |
| Its engines are pinned and versioned (`PARSER_VERSION` in `document_extraction.py`, reported by the child), chosen by sniffed kind | `src/adapters/drive_parser/`; commit "A-M pins" (2026-10-13) |
| The router needs its outcome: statuses and `pages_total`; a converter that returns a string cannot give them | `drive_extraction.py` |
| The sandbox runs this engine behind `ContentExtractorPort` (S3, S4) | sandbox RFC §4.10 |
| U0-b (synthetic corpus): the child reads ODF and RTF (chat cannot), guesses legacy CSV code pages, refuses a corrupt file instead of passing it as text, and returns a status for an empty, encrypted or image-only file; it read worse in four places, fixed in U5 | §8 |

**The four U5 fixes** (one parser version bump, 3 to 4): PDF tables are row-aware (`drive_parser/pdf_rows.py`, pdfplumber per
page; a page without a detected table keeps pdfminer's text byte for byte); subtitle cues keep their start time as
`[hh:mm:ss]`; HTML keeps the full URL of an external link; `.eml` shows From, Sender, Reply-To, To, Cc, Bcc, Date and
Subject (RFC 2047 decoded), every text part, nested `message/rfc822` messages, and one line of attachment names, types and
sizes (contents are not read; a walk cap of 200 parts and depth 5).

Cost of the single engine: chat and email text changed for some files (pdfminer instead of pdfplumber layout, the child's
XLSX/XLS readers, CSV as a table, HTML converted rather than raw).

**Costs of the child in the main service, with either engine:**
- **Memory and slot.** A child with `RLIMIT_AS` 1 GiB, one at a time per adapter, in the only chat instance (measured
  7-day maximum 803 MiB of 2 GiB). The slot wait is bounded (§4.4); U0-c measures the headroom.
- **Privileges.** The uid switch needs root and the `driveparser` user; the image has both (`Dockerfile:42-43`), and on
  Cloud Run a missing parser user fails the start (GOOGLE_DRIVE_RFC G13, `subprocess_content_extractor.py:318-345`), so
  the main service inherits that guard. The build check that nothing loaded is writable by the parser user skips
  `node_modules` because the indexer never loads it (`Dockerfile:47-53`, prune at `:49`). The main service does load
  Node trees (its in-process DOCX and PDF runners). U3 removes the exemption, or adds those trees, with a test that the
  check covers every path the main service runs. The decision record gets a neutral status line: the child also runs in
  the main runtime, under its identity, and the record's trigger applies there too.
- **What the child could reach.** The main service's identity holds the KMS key grant and reads the credential
  collection, as the indexer's does (`decisions/secrets_at_rest_cipher_and_digest.md`; `docs/07_deployment/README.md:351-354`).
  A child exploited in the main service reaches what an exploited indexer child reaches (GOOGLE_DRIVE_RFC §10). D14
  accepts that residual in both runtimes until S3/S4; U3 is still a gain, since today the main service parses in a
  thread of the process that holds every token in memory.
- **Execution environment.** The main service does not pin one (`cloudbuild-dev.yaml:24-43`; the indexer pins gen2 at
  `:162`); the child's limits were measured on gen2. U3 pins gen2 or U0-c decides. The staged copy counts toward memory
  (`/tmp` is in memory).

Rejected: *keep parsing in a thread of the main service* (contradicts D6); *move every read to the indexer*
(`/extract`; the indexer has internal ingress and concurrency 1, so a chat read would wait behind a tick;
`decisions/drive_extract_ingress_slice3.md`).

### 4.9 Relation to the sandbox

Order (D14): the document service (U0–U4, then U5) is built first; the sandbox's parse slices follow as adapter swaps.

| Sandbox slice | What it swaps under this RFC | Needs | Callers touched |
|---|---|---|---|
| S3 drive parsers | in the indexer's composition line (`indexer_main.py:138`): `SubprocessContentExtractor` → `SandboxContentExtractor`, which implements both `ContentExtractorPort` and `PdfSplitterPort` | U1 (`PdfSplitterPort`), U3 (the request limits) | none |
| S4 chat and on-demand reads | the same swap in the main service's `build_document_service`, behind its knob | U3 (`PARSER_UNAVAILABLE` and its note), U5 (one engine, so the sandbox carries no converter op) | none |

- **One adapter object for both ports.** The in-process adapter already serves both (`drive_model_setup.py:124` aliases
  the parser as the splitter), so the HTTP adapter does too: each composition line swaps one object, the parser and the
  splitter can never point at different engines, and they share one `SandboxHttpClient` and one client-side slot budget
  (`SANDBOX_INDEXER_SLOTS`). Rejected: two HTTP classes (two objects to keep in step on every swap and rollback).

- **One op family, `parse.*` (A1).** With D15 there is no `DocumentConverterPort`, `LocalDocumentConverter`,
  `SandboxDocumentConverter` and no `chat.convert` op. Both runtimes call the same ops (`parse.extract`,
  `parse.pdf_page_count`, `parse.pdf_window`, `parse.pdf_windows`). The family is named for what it does, not for its
  first caller: `drive.*` would misname the main service's chat and email reads, and the sandbox's caller gate maps each
  runtime's identity to op prefixes (`src/web/sandbox_caller_gate.py:39-60`), so one family admitted to two identities
  costs one `GROUP_PREFIXES` row per group (`src/composition/sandbox_setup.py:37`; the RENDER-only start check
  `:87-91` widens with it) plus the identity and the knob. S4 = admit the main service's identity to `parse.*` and flip
  its knob.
- S-Δ1 (audio stays with the caller) and S-Δ2 (plain text stays with the caller, §4.4) hold.
- Until S3/S4, the child gives each runtime resource and privilege bounds (after U3's build-check fix). That is not
  containment, and the decision record says so in those words.
- The reader is never in the sandbox: it needs keys (sandbox §1). Media (sandbox S5) serves the transcription job,
  outside this RFC.

## 5. Owner decisions and author proposals

| # | Proposal | Status |
|---|---|---|
| D1–D6 | as in the header | owner, 2026-10-22 |
| D7 | long scans: the reader reads only the pages that fit the store's presentation cap (60 000 characters for drive opens, §29, A4/D17; 30 000 elsewhere until D20's 60 000 lands in U3), within each agent's existing timeout; the note says what is shown; no "still working" path (§4.4, §4.5) | owner, 2026-10-23 |
| D8 | `send_file_to_user`: any ref → `resolve_file` → `notify_file_bytes`, origin channel as override; a failed upload is a failure (§4.2) | owner, 2026-10-23 |
| D9 | no file contents in Logfire from any model call (§4.3) | owner, 2026-10-23 |
| D10 | email uses the shared caps (§4.5, §4.7) | owner, 2026-10-23 |
| D11 | one open verb per store; `open_email_attachment` replaces `get_email_attachment`; refs from `get_email_details` (§4.2, §4.7) | owner, 2026-10-23 |
| D12 | `open_email_attachment` also takes an email id + attachment name; duplicates list the refs (§4.7) | owner, 2026-10-23 |
| D13 | `send_file_to_user` refuses active kinds from email attachments, by kind from bytes; drive and chat unrestricted (§4.2) | owner, 2026-10-23 |
| O2 (U4b) | D13's macro check runs in the sandbox as `parse.container_facts`; no ZIP parsing in the main service; fail closed when it cannot run (§4.2; `decisions/email_send_macro_check_in_sandbox.md`) | owner, 2026-11-05 |
| D14 | order: U0–U4, then U5, first; sandbox S3/S4 after, as adapter swaps; the parser child is the accepted residual until then (§4.9, §7) | owner, 2026-11-03 |
| D15 | P4 in principle: one parse engine, the child's readers (§4.8, §8) | owner, 2026-11-03 |
| D16 | D15's final go given; the U5 re-extract/re-embed cost approved up front, the measured estimate still reported before shipping (§7) | owner, 2026-11-03 |
| D17 | = A4: the reader's text budget for a drive scan opened in conversation is 60 000 characters (§4.4, §4.5) | owner, 2026-11-03 |
| D18 | = P13: the 200-page allowance per open for every store; chat and email opens are not in the drive's monthly ledger (§4.4) | owner, 2026-11-03 |
| D19 | = P14 (built in its own PR) and P15 (§4.1, §4.6) | owner, 2026-11-03 |
| D20 | = P19 in a changed form: one 60 000-character presentation cut for parser text on every open path, with a note naming the cut; email attachments 30 000 → 60 000; realised in U3 (§4.5) | owner, 2026-11-03 |
| O-U5-2 | Link policy, chat and drive alike: an external link keeps its full URL without `utm_*` parameters and fragment, cut at 300 characters with an ellipsis; this supersedes D23 (domain only) of USER_DRIVE_INDEXING_RFC (§7 U5) | owner, 2026-11-05 |
| D21 | U5's drive re-read re-runs the free parser on parser-extracted files and re-embeds only the files whose extracted text changed; reader-extracted files are untouched; records store the reader family actually used (§7) | owner, 2026-11-05 |
| O-U5-3 | Detail of D21: an upgrade re-read never calls the paid reader; the parser's text stands (`decisions/drive_reread_by_text_change.md`) | owner, 2026-11-05 |
| O-U5-5 | `.eml` attachments are listed by name, type and size only; their contents are not read | owner, 2026-11-05 |
| O-U5-6 | Live Gmail bodies keep their links under O-U5-2's rule: the Gmail adapter's HTML-to-text writes an external link as `text (URL)`, the drive parser's form, through one shared pure rule (`src/domain/link_url.py`); the body stays parsed in the main service (BeautifulSoup, as before), a `text/plain` part is passed as written | owner, 2026-11-05 |
| P1 | `DocumentReadPort` + `DocumentService`, one implementation today (§4.4) | author |
| P2 | bytes in; the service never fetches (§4.4) | author |
| P5 | `LLMPort.native_formats(model_name)` with a text-only default; per-attempt key on a context copy (§4.3) | author, realises D3 |
| P8a | email part-level fetch incl. nested and inline parts (§4.7) | author, realises D4 |
| P8b | `email_file_context` (§4.7) | author, realises D4 |
| P9 | one ref classifier (§4.1) | author |
| P10 | pure pieces move to neutral modules; reader names kept (§4.5) | author |
| P11 | rename the drive tag to the neutral one (§4.6) | author, optional, own slice |
| P13 | `allowance()` = the existing 200-page ceiling for every store (§4.4) | accepted (D18); realises D5 |
| P14 | mark email bodies and search lines (§4.6) | accepted (D19); built in its own PR |
| P15 | workspace and delivered files count as external (§4.1) | accepted (D19) |
| P16 | the native decision sits inside the service (§4.3) | author |
| P17 | normalise images in the child (§4.3) | author, optional follow-up |
| P18 | prefetch reads in the async worker from the ref (§4.2) | author; moves a caller in U3 |
| P19 | one 60 000-character cut on parser text of every open path (§4.5) | accepted in a changed form (D20); U3 |
| P21 | audio on open: decided per path in U3; drive recordings under §27's opt-in and cap, chat audio metered and cached, not capped (D5); transcript first; within the deadline (§4.4) | author; U3 |
| A1 | the sandbox parse op family is `parse.*` (§4.9) | author decision, rev 6 |
| A2 | provider exports enter `open` as bytes with the export mime; no special path (§4.2) | author decision, rev 6 |
| A3 | after U5, plain text in the caller = `text`, `markdown`, `json`, `code`, `log`; `csv`, `html`, `eml`, `rtf`, `subtitles` to the child (§4.4) | author decision, rev 6 |
| A4 | D7's text budget = the store's presentation cap; drive: §29's 60 000 (§4.4, §4.5) | author decision, rev 6; accepted (D17) |
| A5 | the drive specialist passes the native declaration on unchanged (§4.3) | author decision, rev 6 |
| A6 | U5's PDF-table fix is a row-aware pass in the child, not a reader route (§7) | author decision, rev 6 |

Withdrawn: P4 (now D15), P7 (now D7), P8c (required by D11), P8d (now D10), P12 (replaced by D11), P20 (now D12).

## 6. Deltas to other specs

| Spec § | What changes | Why |
|---|---|---|
| USER_DRIVE_INDEXING_RFC §9, last bullet | No `/extract` on the indexer. Each runtime runs the same pieces; a remote unit may come later under D1 | D1 |
| USER_DRIVE_INDEXING_RFC §16 "Slice 3 — one extraction path" | Realised by U1–U4 (isolation and routing) and U5 (one engine, D15). Done-criterion "same text as the current path" becomes the U0-b report with accepted differences and U5's four fixes | D6, D15 |
| USER_DRIVE_INDEXING_RFC §24.3; `ContentExtractorPort` | `ExtractionRequest` gains an optional deadline and an optional text budget, both only lowering the adapter's ceilings. Only when one is set does the reader run windows one at a time against a shared prefix counter, return the finished prefix on a deadline, and return it on an outage instead of raising. The indexer passes neither, so its runs and its `DriveModelUnavailable` handling are unchanged | §4.4, D7 |
| USER_DRIVE_INDEXING_RFC Δ35; `decisions/drive_prefetch_wrapper_slice3.md` | **Done in U3.** Closed in U3: prefetch goes through `open` and is marked | D6 |
| `decisions/drive_extract_ingress_slice3.md` | Obsolete: no main → indexer call | D1 |
| `decisions/drive_parser_sandbox_resource_isolation.md` | **Done in U3.** A status line: the child also runs in the main runtime; the trigger applies there; the residual is accepted in both runtimes until S3/S4 (D14) | §4.8, D14 |
| USER_DRIVE_RFC §4.4; DRIVE_SPECIALIST_RFC | The store-in-the-name rule now covers reading too: `open_file`, the drive specialist's `open_drive_file`, `open_email_attachment`, one shared open path behind them. Reads stay lenient. Prefetch stays off for FileManagement and the drive specialist; the native declaration reaches the specialist through `work_with_drive_files` (A5) | D11 |
| USER_DRIVE_RFC §4.6, `_fetch_video` (`DriveOperations`) | Sending bytes to the user becomes the general `send_file_to_user`; the video branches stay as they are | D8 |
| USER_DRIVE_RFC §4.6 | Native caps come from the consuming adapter, never above today's 5/20 MiB; formats from the bytes; PDFs page-checked | D3, P5 |
| FILE_STORAGE_RFC §2.4 step 3, §2.8, §5; file_storage README §2.1 diagram | **Done in U3 for §2.4 step 3 (a note) and README §2.1.** Record that the upload turn is label-only (README §1.1 holds) and that the legacy branch is retired | §1.2 |
| UNTRUSTED_CONTENT_SANDBOX_RFC rev 4 (O12, O13; §1 C2, §4.4, §4.7, §4.8, §4.10, §4.12, §7, §12) | Applied in that RFC's rev 4: one `parse.*` family for both runtimes, no `DocumentConverterPort` or `chat.convert`; S3/S4 after the document service; S4 = identity + knob; the chat cap is this RFC's caps table; one knob per runtime governs its parser and splitter; S4's acceptance adds a scan opened in chat within D7's deadline, through the batched `parse.pdf_windows` | D14, D15 |
| GOOGLE_DRIVE_RFC §10 | Its "built right after the multi-drive programme" points at D14's order | D14 |
| GMAIL_EMAIL_INDEXING_RFC §2.4 | `get_email_attachment` is removed; `open_email_attachment` opens a ref or an id plus name; `get_email_details` lists refs; the download of every attachment per call ends | D3, D4, D11 |
| `DRIVE_FILE_NOTES_RFC`, `SYSTEM_TURN_FINGERPRINT_RFC` | Only if P11 is accepted: they name `<drive_document>` / `wrap_untrusted` and follow the rename | P11 |
| `decisions/grok_revival_2026_08.md` ("upstream converts non-images to text") | Becomes true: Grok declares no PDF, so PDFs reach it as text | D2 |
| `tests/unit/composition/wiring_none_ledger.txt:72` (`defect:D3`) | Audio decision made in U3 (chat only); the line itself (now `:75`) is still in the ledger, a test edit for the test reviewer | §4.4 |

## 7. Slices

**Status:** U1 built (shared pieces moved to source-neutral modules, no behaviour change); optional P14 built (§4.6);
U2 built (the native-format declaration, PDF page caps from provider documentation; the measured spike U0-e replaces
them); **U3 built** (2026-11-03; the map of the result is `docs/05_building_blocks/file_conversion/README.md`); U4a (read) and U4b (save, send) built (2026-11-05); **U5 built** (one parse engine; the drive upgrade pass is held off at deploy, see the U5 row). Deltas of U3 from this RFC as written:
- **One field added to the contract:** `ReadOutcome.cut_within_pages` (the 1-based pages of the one window shown in part),
  which D7's note needs; every other `ReadOutcome` field is as in §4.4.
- **The drive presentation stays in `DriveOperations`** (the §29 refusal, the cut line and the drive wrapper); the other
  stores use `present_text`. Both add D7's "what was shown" line outside the wrapper.
- **`resolve_file` is deferred to U4b**, with the email and send work that needs it; `FileConversionService.open` and
  `prefetch` are the only new service methods.
- **P21's per-path audio decision: chat only.** Only a chat upload opened interactively is transcribed (metered, cached
  90 days in `<prefix>audio_transcripts`); a prefetch, a workspace or delivered file, and a drive file give the audio note,
  and drive recordings keep their transcript-first rule. The shared converter still has no transcriber. The ledger line
  `FileConversionService._audio_service  # defect:D3` is still in `wiring_none_ledger.txt` on this branch: its removal is a
  test-file edit left to the test reviewer.
- **The parser child in the main service is an interim step** until the untrusted-content sandbox's parse service
  replaces it as an adapter behind the same port (UNTRUSTED_CONTENT_SANDBOX_RFC).

Deltas of U4a (read) from this RFC as written:
- **No parser child.** S4 is live: the email read parses through `DocumentService` and the sandbox light service, so it is
  isolated with no extra work. D13's macro check runs in the sandbox (owner decision O2, U4b; built).
- **Native email opens ship in U4a** (D9's `file_omitted` already protects the history); `open_email_attachment` joins
  `NATIVE_OPENING_INTENTS`. U0-d never ran: the adapter's wire tests on synthetic payloads stand in until the live check.
- **`get_part` takes the listed part**, not a part id: Gmail's `attachmentId` is not stable between `messages.get`
  calls, so the ref carries only the part id and one open does one listing and one download.
- **Typed errors** (`EmailMessageGone`, `EmailPartUnavailable`, `EmailNotConnected`, `EmailPartTooLarge`) replace the
  swallow-everything `{}` of the old deep fetch, so "gone" and "mailbox did not answer" read differently.
- **A choice is a success.** Several name matches, or no ref and no name with several attachments, return the full
  `[Email file: ...]` labels with a "pass one ref" note; a failure would make the model retry the same call.
- **`file_ref` has one neutral description** in every descriptor (the shared context schema keeps the first registered
  text); `name` is described as `open_email_attachment` only. One attachment per call.
- **Labels printed after the untrusted wrapper**, names escaped inside it, so a forged name cannot pass as a label.
- **`email:` never enters `resolve_bytes`** (there is no allowlist to leave unchanged); image and video edits from `email:`
  are deferred. `convert_file_to_text` was removed in U5.

Each slice: tests first, the reviewer rule for any existing test, `make check` green, docs in the same PR.

| Slice | Content | Done when | Order |
|---|---|---|---|
| **U0 spikes** | §8 U0-a … U0-e (U0-b: the real-file run) | each result written down | first |
| **U1 pieces** | P9 (its drive branch on the `drive_ref` codec); P10 moves (G1's mime table moves with `kind_of`); caps table (§4.5, the text-export and §29 rows included); `PdfSplitterPort` (with the optional `deadline_at` and the batch `cut_pdf_windows`, §4.4); the marking function with a tag per store (drive unchanged) | no behaviour change; drive output byte-identical; indexer tests change import lines only | after U0 |
| **U2 declaration** | P5 on four adapters (wire tests), the proxies forward, the four engine runners set the key per attempt, the coordinator drops it except for the three opening intents, the drive specialist passes it on (A5); `DriveOperations` decides native from it (still through today's converter) | a PDF opened under Grok arrives as text, not dropped, from `open_file` and from the drive specialist; the key never reaches any other specialist or a Cloud Task body; DomainResearcher and Quick keep native reading | after U1 |
| **U3 service** | `DocumentReadPort`, `DocumentService`, `build_document_service` (child parser, splitter, reader); `open` for chat, delivered, workspace and drive opens (drive with §29's extent, A4); the two request limits; D7's cut; statuses and presentation, the chat wrapper and alert texts rebuilt from statuses, the one 60 000-character cut (D20) on every open path, email attachments moved from 30 000; P21's per-path audio decision (§4.4), the `defect:D3` ledger line deleted; D9 per U0-a's result; P13; the build-check fix; the legacy branch retired; with P18, prefetch in the worker | parsing on every interactive path runs in the child (either engine); a scan opened by a text-only consumer returns reader text within the store's budget, with nothing read past it, and a note that says what is shown; a chat file that fails gives the same note as today; with both new fields unset, the reader's existing tests pass unchanged; the `PARSER_UNAVAILABLE` and `STILL_WORKING` notes tested (the second with a fake adapter); no open exceeds its agent's timeout in tests; every transcribing path is metered, cached and under the §27 cap in tests; no file part in a captured span (D9); chat text marked after the owner's prompt upload | after U2; gated on U0-a, U0-c |
| **U4 email and send** | P8a, P8b; `open_email_attachment` replacing `get_email_attachment` (by ref, or id plus name, D12); refs from `get_email_details`; the notes; the two service methods; the tool texts and the full removal list; the old attachment path removed; `copy_file_to_drive` from an `email:` ref; `send_file_to_user` (D8) with its kind check, the D13 active-kind block and `capabilities.py` entry | the incident's three steps (read, save, send) pass on a synthetic multipart fixture (nested, inline, scan), opened by ref; a failed upload answers as a failure; one test per D13 kind from an email ref is refused with the note (including a renamed file and a name-only kind), and an HTML file from a drive ref is still sent; no caller of the old attachment path remains; email text marked and the email workflow line updated after the owner's prompt upload; the owner's live check | **Built:** U4a (read, 2026-11-05) and U4b (`resolve_file`, `copy_file_to_drive` and `send_file_to_user` from an `email:` ref, D13 with the sandbox macro check). After U3; native email opens wait for U0-a |
| **U5 one engine** | **Built.** D15: markitdown's auto-detect path retired; `csv`, `html`, `eml`, `rtf`, `subtitles` to the child (A3); four fixes in the child (PDF tables row-aware, A6; subtitle `[hh:mm:ss]`; HTML full links under O-U5-2; `.eml` headers and parts, §4.8), shipped together under **one** parser version bump (3 to 4). The child reports its `parser_version` and a `reader_family`; an upgrade re-read compares text and commits an unchanged one as a stamp (`decisions/drive_reread_by_text_change.md`, `docs/05_building_blocks/user_drive/README.md`) | **Gate before the upgrade pass runs:** the fixes change the child the indexer shares, so the pass re-reads `parser@3` records and may re-embed changed chunks. The pass is held off at deploy (`DRIVE_UPGRADE_PASS`); the owner runs the read-only probe (`scripts/infrastructure/drive_parser_upgrade_probe.py`) and gives an OK on its number under the money rules (over ~$3), then switches the pass on. Each fix is tested on a fixture; no caller of `convert_file_to_text` remains | after U4, on D15's go |
| **Optional** | P11 rename; P14 email bodies and lines; P17 image normalisation; P19 parser-text cut | each its own PR | any time after U1 |
| **S3 / S4** (sandbox RFC) | adapter swaps per §4.9 | no caller diff in the PR | after the document service (D14); S3 needs U1 and U3, S4 also U5 |

## 8. Open questions and spikes

**Open questions (owner).** None remain: OQ1–OQ5 were closed by D7–D11, P20 became D12 and OQ6 was closed by D13
(2026-10-23); P4 became D15 (2026-11-03), whose final go was given in D16 (U0-b's real-file run is not a gate).

**Spikes**
- **U0-a (D1's check, D9). How to keep file contents out of Logfire.** D9 requires it for every model call; the
  spike finds how. Logfire's scrubbing cannot do it (§4.3). Candidates:
  - (a) `send_to_logfire=False` plus our own OTLP exporter wrapper that rewrites the two message attributes before
    export;
  - (b) per-instrumentor request hooks or content options (including the vendor Gemini instrumentor,
    `src/utils/telemetry.py:85-99`);
  - (c) referencing native files by provider file id, so no bytes enter the request (adjacent to the §10 follow-ups,
    and labelled as such if chosen);
  - for the reader alone, `suppress_instrumentation()` around its calls; check that our `llm.call` span still exports.

  Memory is measured separately from export. An export-time filter meets D9 but not D1's memory check, because the
  attribute is still built in process. A suppressed call's failure must stay visible as a structured log line
  (provider, error type, window). If no way keeps the reader's contents out, D1 reopens, as the owner stated.
  **Exit:** if no candidate works for the orchestrator's calls, D9 and native reading (D2) conflict. That goes to the
  owner (for example, native opens off until it is solved); U3 and native email opens do not wait indefinitely.
- **U0-b. Text equivalence of the two engines.** **Ran on a synthetic corpus** (2026-11-03; invented content, every
  kind the chat path takes; local, no cost). Findings that shape this RFC: identical text for DOCX, PPTX, Markdown,
  plain text and JSON; the child reads ODF and RTF, which chat cannot, guesses legacy CSV code pages, returns a status
  for empty, encrypted, image-only and corrupt files where chat returns a string or passes garbage on, and drops
  spreadsheet float noise; it reads worse in four places (PDF tables, subtitle timecodes, HTML link targets, `.eml`
  headers and parts), which U5 fixes (§7); its statuses mean the chat layer rebuilds today's notes (§4.4). **Not yet
  measured:** real-world PDFs (fonts, ligatures), real scans, `.msg`, files at the caps, large sheets, and the drive
  evaluation corpus. That run is not a gate (D16); its differences, when it is run, are listed per kind, then accepted or
  fixed.
- **U0-c. The child in the main service:** the uid switch in its execution environment; peak memory of one turn with
  two native PDFs at the caps, capture on, and a child at its limit; slot wait p95 under concurrent opens; the staged
  copy counted.
- **U0-d. Gmail part facts:** `partId` stable across fetches; inline `body.data` sizes; nested and `message/rfc822`
  parts against the listing rule; a message moved to trash.
- **U0-e. Native limits per adapter:** a synthetic image and PDF at each declared boundary (bytes and pages), and two
  or more native files in one loop. Its cost is estimated before running; over $3 needs the owner's OK.

## 9. Where current code disagrees with this design

Re-verified at `2280ca78`; items 1–8 held, items 9–12 were new. Items 4, 6, 9 and 12 are resolved by U3 (their text is kept as written for the record).

1. Email attachments have no ref; `get_email_attachment` returns converted text (`email_search_service.py:198-260`).
   No `email:` ref exists anywhere in the code.
2. `EmailFullContent` carries names and bytes keyed by name, with no part id and no mime (`src/domain/email.py:51-64`).
   The adapter fetches top-level parts only, so nested and small inline parts are listed but never fetched
   (`gmail_provider_adapter.py:419-424` against `:343-356`). Two parts with the same name collide (`:413`).
3. The MIME of an attachment comes from its file name (`email_search_service.py:242`). The 10 MB total guard is
   unreachable (`:236-240`): the 3 MB check (`:230`) runs first on the same size.
4. **Resolved in U3.** The consumer-blind native decision, the two parse engines and drive-only marking, as described in §1.2. None of
   `document_kind.py`, `document_limits.py`, `untrusted_text.py`, `pdf_splitter_port.py` exists; `wrap_untrusted` is
   drive-only (`src/domain/drive_search.py:584`); `PdfSplitter` is a Protocol in `drive_reader_runner.py:73`.
5. Two caps for one provider limit: drive opens allow images up to 5 MiB raw (`user_drive.py:29`, applied at
   `drive_operations.py:1003-1004`), while the reader holds images to 3.75 MiB because Anthropic's 5 MB limit applies
   to the base64 (`drive_image_mime.py:12-16`). Chat and workspace native opens have no cap at all
   (`drive_operations.py:722-733` → `file_conversion_service.py:377-385`).
6. **Resolved in U3** (`ExtractionRequest.deadline_at` and `text_budget`). `ContentExtractorPort` had no per-request limit: `ExtractionRequest` has no deadline or budget
   (`drive_extraction.py:50-59`); the child's wall and the reader's budget are constructor values
   (`subprocess_content_extractor.py:88`, `:96`; `drive_reader_runner.py:112`), and the reader starts every window at
   once (`drive_reader_runner.py:211-217`).
7. The drive copy derives the file name from the ref string when no `file_name` is given (`drive_operations.py:465` →
   `drive_filename`, `user_drive.py:308-321`); an `email:` ref carries none, and `copy` accepts chat, delivered,
   `workspace:` and `skill:` sources only (`drive_operations.py:457`).
8. `open_file` tests `is_drive_ref` first (`file_management_agent.py:101`, `:107`; `drive_operations.py:440`); the
   resolver excludes other prefixes first (`file_conversion_service.py:90-99`). `required_capabilities` in
   `STRATEGIES` is enforced nowhere (`agent_context_builder.py:27`; nothing reads the key). FILE_STORAGE_RFC §2.4/§2.8
   still describe native bytes on the upload turn.
9. **Resolved in U3** (chat audio is transcribed, metered and cached; §7). **No opened audio was transcribed**: the production `FileConversionService` has no
   transcriber (`service_container.py:449-459`), recorded as `defect:D3` (`wiring_none_ledger.txt:72`). U3 (§4.4).
10. **The drive's open path is shared infrastructure, not an agent branch.** `DriveOperations` serves both the drive
    specialist and FileManagement's lenient `open_file`; the drive specialist's model reads a stand-in for an opened
    file and returns the bytes in metadata (§1.2, §4.3 A5). The §29 open bound runs before and after the conversion
    (`drive_operations.py:1008-1014`, `:1082-1096`).
11. **Text files have a two-stage cap.** Plain text is read up to 10 MiB, `data:` images dropped, then the 5 MiB cap
    applies (`file_conversion.py:225-232`, `:261-264`; drive text streamed to 10 MiB, `file_conversion_service.py:194-200`).
    §4.5's caps table records it.
12. **Resolved in U3** (P18: the resolver is `(ref, user_id, account_id, deadline_at) -> Prefetched`). **The coordinator documented a resolver signature it did not use:** `(ref, user_id, mime_type)` in its docstring
    (`agent_coordinator.py:64`) against `(file_ref, user_id)` at the call (`:602`). P18 replaces both.

## 10. Follow-ups outside this RFC

These were found during the study. They are tracked on the defect list (items 7, 11 and 12) and are not part of this
design; all three still hold:
- Native opens leave temp files behind (`drive_operations.py:727`, `:1049`). Carrying native bytes as base64 instead
  would end this, but changes a provider's request path.
- `process_attachment` keeps a `path` for images and PDFs that no adapter reads (`file_conversion_service.py:320-321`).
- Gemini re-uploads a `path` file part to its Files API on every call of a loop (`gemini_adapter.py:361-373`).
