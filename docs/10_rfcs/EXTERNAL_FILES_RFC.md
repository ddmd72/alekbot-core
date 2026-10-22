# RFC: One way to open external files — refs, native reading, shared security pieces

**Status:** Proposed (rev 1). Not built. *D* items are owner decisions (2026-10-22); *P* items are author proposals the
owner has not ruled on.
**Date:** 2026-10-22
**Owner decisions (2026-10-22):**
- **D1. Placement.** The document service is a component inside each runtime (main service, drive indexer). Its
  contract is designed as if it were remote: serializable input, closed-schema outcome, an explicit deadline and
  "still working" state, billing inside, no caller state shared. It can become its own deploy unit later without
  touching callers. Open check: can Logfire content capture be suppressed for the reader's calls in the main service?
- **D2. Native when effective.** A file read in a conversation goes to the orchestrator's model natively when that
  model reads that kind well; otherwise through the pipeline (text layer → text-or-scan check → no-tool reader →
  text). The drive indexer keeps the pipeline (its task is different). Grok: images natively, PDFs not.
- **D3. Ref first.** The orchestrator always gets a ref, never the file, and opens it on demand through one open path.
  Every external file (chat upload, drive file, email attachment) becomes a ref. The LLM adapter declares what it
  reads natively and well (the pattern of `LLMPort.supports_caching()`); the open path asks and acts. Adapters never
  call services.
- **D4. Email refs.** `email:<message id>/<part id>`; nothing is copied; every open fetches from Gmail (message
  structure, size check, that part only). Any consumer (save to drive, a specialist) resolves the same ref to bytes at
  action time. A deleted email gets an honest "email deleted". A cache may come later inside the resolver, not now.
  `get_email_attachment` stops returning content and returns the ref, like a chat upload.
- **D5. No new limits or budgets** for non-drive reads; a clean place is left for them. Drive budgets are untouched.
- **D6. Security policies in one place.** Kind from bytes, caps, isolated parsing, text-or-scan routing, the no-tool
  reader, untrusted marking: each exists once, as shared pieces, used by every path. The indexer keeps its own loop and
  its money and record decisions and calls the same pieces. The document service is those pieces assembled behind
  "open a ref".
- **Contract (owner, 2026-10-22):** the sandboxes are not all built; they come later and they are the isolation layer.
  This design works today (in-process engines, the drive parser child) and names the adapter each sandbox slice swaps
  in, with no second migration of callers.

Related: `USER_DRIVE_INDEXING_RFC.md` §9, §16 (Slice 3), §24.3, Δ35; `USER_DRIVE_RFC.md` §4.3–§4.6; `FILE_STORAGE_RFC.md`;
file_storage README §1.1; `UNTRUSTED_CONTENT_SANDBOX_RFC.md` §1, §4.7, §4.10, §7; `GMAIL_EMAIL_INDEXING_RFC.md` §2.4;
`decisions/drive_{prefetch_wrapper_slice3,parser_sandbox_resource_isolation,extract_ingress_slice3}.md`.

## 0. In one paragraph

Three kinds of external file reach the bot: chat uploads, drive files, email attachments. Each is read a different
way today. Chat uploads and drive files are refs that `open_file` resolves; email attachments are not refs at all, and
`get_email_attachment` returns their text directly. Only the drive indexer checks a file's kind from its bytes, parses
in an isolated child process, notices a scan and sends it to a no-tool reader model. Every other read parses in a
thread of the main service and returns whatever text comes out. This RFC makes every external file a ref and gives
opening one path: resolve the ref to bytes, hand them to one **document service**. If the consuming model reads that
kind natively (its adapter says so), it gets the bytes; otherwise the indexer's own pieces run: sniff, parse in the
child, text-or-scan check, reader. Text that reaches a model is marked untrusted by one function. The indexer keeps its
loop and calls the same pieces. The sandbox slices later replace one adapter behind the parse port, with no caller change.

## 1. Today, with the recorded reasons

### 1.1 The incident that started this (2026-10-22)

The owner asked the bot to read an email's PDF attachment, save it to the drive and forward it. The PDF was a scan
without a text layer. All three steps failed:
1. **Read.** `get_email_attachment` converted the file with the text-layer converter in the main service
   (`src/services/email_search_service.py:191-253`, `convert_file_to_text` at `:244`). The result was table rules and
   bullets, not empty, so no alert fired; it came back as a normal result.
2. **Save.** `save_file_to_drive` got the attachment's file name as `file_ref`. The resolver knows chat uploads,
   delivered documents, workspace, skill and drive refs (`src/services/file_conversion_service.py:185-210`), and an
   email attachment is none of these, so the answer was "not found, ask the user to re-upload".
3. **Forward.** No capability returns an existing file's bytes to the user, except video (§8, OQ1).

The bot retried three times. Failure 1 is about reading, failure 2 about refs, failure 3 about delivery.

### 1.2 How each source is read, and why

| Source | Ref | Open | Native bytes to the model | Text path | Recorded reason |
|---|---|---|---|---|---|
| Chat upload | the stored, deduplicated name in `[File: "<ref>" (size)]` (`file_conversion_service.py:212-255`) | `open_file` → `FileManagementAgent._fetch` (`src/agents/file_management_agent.py:211-281`) | image or PDF by extension guess (`:237-240`), no size cap, temp file (`:317-346`) | `convert_file_to_text` in a thread of the main service | content never in history (FILE_STORAGE_RFC §2.1); the label carries the ref, not the original name (README §1.1) |
| Drive file | `drive:<item id>` in `[Drive: …]`, kept across turns by `drive_context` | `open_file_from_drive` or `open_file` (lenient) → `_open_drive_item` (`:701-760`) | image ≤ 5 MiB in four formats, PDF ≤ 20 MiB, on provider metadata (`src/domain/user_drive.py:22-24`, `:43`) | the same converter, then the nonce wrapper (`:756-760`) | caps "below the strictest provider limits, so a large scan cannot fail Smart's whole turn" (USER_DRIVE_RFC §4.6) |
| Email attachment | none; the model passes `email_id` + `filename` | `get_email_attachment` → `EmailSearchService.get_attachment` | never | the same converter; 3 MB cap; 30 000 characters | none recorded beyond the original RFC flow (GMAIL_EMAIL_INDEXING_RFC §2.4) |

Facts that shape the design:
- **The orchestrator is blind on the upload turn, by design.** `process_attachment` builds one part
  `MessagePart(text=label, file_data=…)` (`file_conversion_service.py:255`); every adapter takes the text branch first
  (`src/adapters/claude_adapter.py:922`, `gemini_adapter.py:329`, `openai_adapter.py:565`, `grok_adapter.py:599`), so
  only the label reaches the model. README §1.1 records why: no context bloat, forward by reference, open on demand.
  The `path` kept for images and PDFs (`file_conversion_service.py:248-249`) is read by nothing.
- **Native bytes reach the model only on the tool turn** that opened them (`src/infrastructure/delegation_engine.py:721-729`
  → a `file_data`-only part, `src/domain/llm.py:365-376`).
- **Whether a file is native is decided without the consumer.** `is_native_binary` is `image/*` or PDF
  (`src/utils/file_conversion.py:58-60`). The delegation context carries no provider
  (`delegation_engine.py:679-682`). `ProviderCapabilities.vision` is `True` in all four adapters and read by no code;
  `get_capabilities()` is read only for the cache strategy (`src/services/agent_context_builder.py:398`). The Grok
  adapter drops every non-image file part with a warning (`grok_adapter.py:642-649`).
- **Two parse engines.** The indexer's child reads by sniffed kind with pinned engines (pdfminer.six directly for
  PDF; `src/adapters/drive_parser/readers.py`), under rlimits and an unprivileged user
  (`decisions/drive_parser_sandbox_resource_isolation.md`). Every other read runs markitdown's own type detection
  in-process (`file_conversion.py:261-271`; pdfplumber first for PDF). No record calls the split intended; every
  record points to convergence in Slice 3 (`USER_DRIVE_INDEXING_RFC.md` §9 last bullet, §16).
- **The indexer's chain** (`src/services/drive_indexing_service.py:2022-2146`) is: fetch, sniff (`sniff_kind`,
  `src/domain/drive_sniff.py:57`), the chain table (`EXTRACTOR_CHAINS`, `src/domain/drive_extraction.py:30`), the
  router (`model_route`, `:190-205`: no text, under 200 characters per page, over 5 % garbage, parser error), then six
  record- and money-coupled decisions before the reader (`:2073-2088`).
- **Untrusted marking** exists for drive text only: `wrap_untrusted` and the two reminders
  (`src/domain/drive_search.py:133-142`, `:563-571`). Chat and email text reach the model unwrapped. Text prefetched
  for document specialists is unwrapped by an accepted deferral to Slice 3 (`decisions/drive_prefetch_wrapper_slice3.md`).
- **Prefetch.** `AgentCoordinator` turns a `file_ref` into text before DocPlanner, PdfGenerator or HtmlPageGenerator
  run (`src/infrastructure/agent_coordinator.py:576-613`; resolver wired at `main.py:309`). FileManagement opts out
  (`src/infrastructure/agent_manifest.py:543`; USER_DRIVE_RFC §4.4: waste, double fetch, a page per not-connected call).

## 2. Goals and non-goals

**Goals**
- Every external file is a ref; opening has one path; acting on a file (save, prefetch) resolves the same ref.
- A scan is read wherever it is opened: natively when the consuming model reads it well, by the reader otherwise.
- Each security piece exists once and every path uses it: kind from bytes, caps, isolated parsing, text-or-scan
  routing, the no-tool reader, untrusted marking.
- The document service's contract survives a later move to its own deploy unit (D1) and the sandbox swaps (§4.9).

**Non-goals**
- Money: limits and budgets for non-drive reads (D5), and the drive's per-class budgets (untouched).
- Image triage, profiler, passages, drive transcription jobs: indexer work on its own state.
- Voice messages (they are the user's own words) and generator inputs (`edit_image`, video edits).
- Redesign of the daily email review (its payload placement and tool surface), and prompt-layer defences beyond the
  marking function.
- Delivering a file's original bytes to the user (the incident's third failure): not decided, §8 OQ1.
- Implementation. A plan follows acceptance.

## 3. Principles (D1–D6 in plain words)

1. **The model sees refs** and opens a file only when it asks; every ref kind opens through the same code (D3).
2. **The consumer decides native, by declaration.** The adapter of the model that will read the file says which kinds
   it reads natively and up to what size; the open path asks and never guesses a provider (D2, D3).
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
| Workspace / skill | `workspace:files/…` / `skill:<name>/<path>` (unchanged) | workspace bucket / Firestore | — |
| Drive | `drive:<item id>` (unchanged) | user drive | `[Drive: <path> (size) ref=drive:<id>]` |
| **Email attachment (new, D4)** | `email:<message id>/<part id>` | the user's Gmail, fetched on open | `[Email file: "<name>" (size) ref=email:<message id>/<part id>]` |

**P9. One ref classifier.** A pure function in `src/domain/file_ref.py` classifies a ref by prefix in a fixed order
(`email:`, `skill:`, `workspace:`, delivered prefixes, `drive:` anywhere, else a chat name). Today the order is
repeated in each resolver, and `_is_drive_ref` needs an exclusion list because `parse_drive_ref` matches `drive:`
anywhere (`file_conversion_service.py:57-66`); a new kind would add a branch to every copy.

**What counts as external.** Chat uploads, drive files, email attachments, workspace `files/` copies and delivered
documents (they can carry third-party text) go through the document service and are marked. `skill:` refs are the
product's own instruction text: resolved and returned as today, never read by the service or marked.

### 4.2 The open path

One entry, `FileConversionService.open(ref, user, account, consumer, deadline)`. It grows out of today's
`resolve_content`/`resolve_bytes`, and its steps are:
1. **Classify** the ref (P9) and **resolve** it in its store. Each store keeps the checks it has today: the ownership
   check on delivered keys, drive metadata before download (folder, recording, archived transcript, 404 = no access),
   the email part lookup (§4.7).
2. **Size check on metadata, before download**, against the caps table (§4.5).
3. **Download** the bytes.
4. **Read** them through the document service (§4.4), passing `consumer` (§4.3) and the deadline.
5. **Present** the outcome: native → a file part; text → marked with the one function (§4.6); a failure → the
   existing `[System: …]` note contract, worded for the source.
6. **Return refs that outlive the turn**, via the existing `*_context` mechanism
   (`src/handlers/conversation_handler.py:1383-1395`): `drive_context` as today, `email_file_context` for email.

Who calls it:
- `open_file` (every ref kind, email included) and `open_file_from_drive`.
- The coordinator's prefetch, with an empty `consumer`: specialists get marked text, and a scan reaches them as reader
  text. This closes Δ35.
- Actions that need bytes (`save_file_to_drive`, `update_file_in_drive` with `source_ref`) call
  `resolve_file(ref) → (bytes, name, mime)`: steps 1–3 only. The name comes from the store, not the ref string.

What stays outside the document service, in the open path: video goes to the user (`_fetch_video`, drive branch
`:741-752`); recordings open as transcripts; folders get the list hint. These are store or delivery rules, not reading.

**P12. Intents.** `open_file` accepts every ref kind. `open_file_from_drive` stays as a drive-named alias over the
same path. Drive mutations keep the store in the intent name (USER_DRIVE_RFC §4.4): the rule was written against
mutations called on the wrong store, and reading is already lenient there. Email has no mutation intents (the grant is
read-only), so it needs no intent of its own.

### 4.3 The adapter's declaration and how it reaches the open path

**P5. `LLMPort.native_formats() -> Tuple[NativeFormat, ...]`**, where `NativeFormat = (mime, max_bytes)` is a pure
domain value. It is abstract on the port, beside `supports_caching()`, and each adapter answers from its own
knowledge:

| Adapter | Declares (initial; numbers confirmed by spike U0-e) |
|---|---|
| Claude | JPEG, PNG, GIF, WebP up to 3.75 MiB raw (its 5 MB limit applies to the base64, `src/domain/drive_image_mime.py:12-16`); `application/pdf` |
| OpenAI | the four image formats; `application/pdf` |
| Gemini | the four image formats; `application/pdf`, bounded by the 20 MB inline request cap (P6) |
| Grok | the four image formats only (D2: xAI reads PDFs only through its Files API and a server-side search tool, neither implemented) |

**How it travels.** The orchestrator that runs a `DelegationEngine` puts its adapter's declaration into the context it
passes in (`context=message.context`, `src/agents/core/smart_response_agent.py:573`), under `native_formats`. The
engine already spreads that context into every delegation (`delegation_engine.py:679-682`), so it reaches the opening
agent without new plumbing. It stays valid for the whole transcript because a transcript runs on one provider:
cross-provider failover stops at the first tool turn, and provider rotation re-runs the turn from scratch, files
included (`src/agents/CLAUDE.md`, "Transcript integrity", "Cross-provider execution retry"). A caller without the key
(a specialist, the indexer, Lelik, background code) means text only. That default is safe.

Rejected: *reading `ProviderCapabilities.vision`* (one boolean, true everywhere, silent on PDFs and size); *the adapter
picking native or text while building the request* (the text would be produced for every open, reader included).

**P6. Native bytes travel as base64 in `file_data`, not as a temp-file path.** All four adapters accept `base64`
(Claude `claude_adapter.py:1063`, Gemini `gemini_adapter.py:374-380`, OpenAI `openai_adapter.py:665`, Grok
`grok_adapter.py:629`). This removes the temp files that native opens leave behind
(`file_management_agent.py:329`, `:733`; recorded as a leak in `decisions/memory_telemetry.md`). It also stops Gemini
re-uploading a `path` to its Files API on every call of the loop (`gemini_adapter.py:361-373`), and keeps nothing
local in the outcome, which D1 needs. Cost: Gemini's inline cap (20 MB per request) bounds its PDF declaration below
today's 20 MiB drive cap. Memory: the base64 string is held once for the loop, where today the temp file sits in
in-memory `/tmp` and is read and encoded again on every call.

### 4.4 The document service contract (D1)

```python
# src/domain/document_read.py — pure, mypy --strict
class ReadStatus(str, Enum):        # ExtractionStatus's per-file values, plus three
    TEXT; NO_TEXT; UNSUPPORTED; TOO_LARGE; ENCRYPTED; TIMEOUT; MEMORY; ERROR
    NATIVE                           # attach the bytes you sent, as `native_mime`
    MODEL_UNAVAILABLE                # the reader could not run now (429, 5xx, open breaker); nothing charged
    STILL_WORKING                    # reserved for an asynchronous adapter (D1); the in-process service never returns it

@dataclass(frozen=True)
class ReadRequest:
    content: bytes                   # staged by the open path; the service never fetches (P2)
    name: str                        # for log lines and the sandbox's `ext`; never addresses anything
    declared_mime: str
    source: str                      # "chat" | "delivered" | "workspace" | "drive" | "email" | "prefetch"
    native_formats: Tuple[NativeFormat, ...]   # empty: text only
    user_id: str; account_id: str    # the reader bills and configures by them
    deadline_s: float
    request_id: str

@dataclass(frozen=True)
class ReadOutcome:
    status: ReadStatus
    kind: str                        # sniffed
    native_mime: str                 # set when NATIVE
    text: str                        # raw, unmarked (§4.6)
    pages_total: int; pages_read: int; truncated: bool
    links: Tuple[str, ...]           # `extracted_by` of every link that ran, e.g. ("parser@3", "reader@2")
    model_calls: int; model_cost_usd: float    # already billed to the account; reported, not booked
    detail: str                      # a failure label, never document text

# src/ports/document_read_port.py
class DocumentReadPort(ABC):
    async def read(self, request: ReadRequest) -> ReadOutcome: ...   # every per-file result is an outcome
```

`src/services/document_service.py`: `DocumentService(DocumentReadPort)`. Its constructor takes the parse port
(`ContentExtractorPort`), the reader (`ContentExtractorPort`), and the PDF splitter.

**`read()` in order:**
1. **Kind from bytes.** `sniff_kind` over the first 8 KiB. For images, `image_mime` on the head; the declared mime
   only breaks ties.
2. **Cap check.** The source's caps (§4.5); over the cap → `TOO_LARGE`.
3. **Native.** If the sniffed format is in `native_formats` and within its `max_bytes` → `NATIVE`. Nothing is parsed.
4. **Pipeline.** Otherwise: `chain_for(kind)` → the parser in its child → `model_route` → the reader when routed, for
   at most `ReaderAllowance` pages (P13), stopping at the deadline. Plain text is read in-process (sandbox S-Δ2).
   Audio keeps today's inline transcription branch, which sandbox S-Δ1 keeps out of the parser.
5. **Return.** The outcome, with `links` and the spend (already billed per call inside the reader runner).

**P1. A port with one implementation today.** The repo asks for a port at a boundary with two implementations; D1
names the second (a remote adapter). Callers sit in three layers (an agent, a service, the coordinator), and services
may not import services (REQ-ARCH-22). Precedent: `SearchEnrichmentService(SearchEnrichmentPort)`,
`FactWriteService(FactWritePort)`. Without the port, moving the service out changes the callers' types, which D1 rules
out. Trade-off: one more port before its second adapter exists.

**P2. Bytes in, never a ref.** The service holds no store credentials: no Gmail, no Graph, no GCS. If it moves out
(D1), it is a bytes-in unit, and no third process refreshes users' OAuth tokens. The sandbox has the same rule:
"the caller fetches" (`UNTRUSTED_CONTENT_SANDBOX_RFC.md` §1).

**What D1's remote-ready contract asks, and how this contract meets it:**

| Requirement | How it is met |
|---|---|
| Serializable input, closed-schema outcome | bytes and plain values; no paths, callbacks or ContextVars; a model outage is a status, not an exception |
| Explicit deadline, "still working" state | `deadline_s` in the request; `STILL_WORKING` in the schema, returned only by a future asynchronous adapter |
| Billing inside, no shared caller state | the reader runner bills the account per call, as on the indexer; the service keeps nothing between calls |

**P7. Interactive deadline 90 s.** FileManagement times out at 120 s (`src/composition/user_agent_factory.py:864`)
and its reads are not shielded (USER_DRIVE_RFC §4.13). At 90 s the reader starts no new window and returns the pages
it has read (`truncated`, `pages_read` of `pages_total`); the open path tells the model which pages those are. The
measured reader time on the owner's drive (30 days, at most 17 pages per file) is 12.6 s at p50, 23 s at p90 and
62 s at most. Prefetch for specialists takes the deadline of its async task.

**P13. `ReaderAllowance`, the place D5 left open.** One method on the service,
`allowance(user, account, source) -> pages`, called before the reader. Today it returns the per-file ceiling
`MODEL_EXTRACT_MAX_PAGES` (200) for every non-drive source. A later budget replaces this one method. The drive indexer
does not call the service: it computes its own `max_model_pages` as today.

### 4.5 Shared pieces and where each lives

| Piece | Today | Under this RFC | Used by |
|---|---|---|---|
| Kind from bytes | `drive_sniff.sniff_kind`, `drive_image_mime.image_mime` | `src/domain/document_kind.py` (moved, P10) | indexer `_fetch`, document service |
| Caps | 9 constants in 7 modules (chat 5 MiB, email 3 MB, drive 45/10 MiB, vision 5/20 MiB, reader image 3.75 MiB, video 45 MiB) | one table `src/domain/document_limits.py`, by source and kind; values unchanged except as noted in §4.7 and P5 | open path (metadata), document service, parser adapter (`size_cap`) |
| Isolated parsing | `SubprocessContentExtractor` (indexer only) | the same adapter in both runtimes (P4) | indexer chain, document service |
| Text-or-scan routing | `chain_for`, `model_route`, `garbage_share` in `drive_extraction.py` | `src/domain/document_extraction.py` (moved, with `ExtractionRequest`/`Outcome`/`Status`, `failure_label`) | indexer `_extract`, document service |
| No-tool reader | `DriveReaderRunner` + `DriveDocumentReaderAgent` (indexer) | unchanged class, also built in the main service | indexer chain, document service |
| Untrusted marking | `wrap_untrusted` + reminders in `drive_search.py` | `src/domain/untrusted_text.py` (§4.6) | open path, prefetch, drive search and transcripts, email details and search lines |
| Container caps, converter allowlist | `src/utils/document_hardening.py` | unchanged | parser child |

**P10. Move the pure pieces, without shims.** Source-neutral code gets source-neutral modules. Callers' import lines
change (a test import fixed after a module move is self-authorized by the reviewer rule). The reader keeps its class
names, agent type `drive_reader`, prompt profile and `STRATEGIES` key. Renaming them would touch Firestore prompts, the
`agent_providers` user keys and billing labels; the drift in the name is accepted and recorded here.

**The indexer's use (D6), at the seams that exist today:**
- `DriveIndexingService(…, extractors=…)` (`drive_indexing_service.py:563`, `:584-586`): unchanged, the same
  `PARSER`/`READER` registry.
- `build_drive_models(parser=…)` (`src/composition/drive_model_setup.py:115-126`, called at `indexer_main.py:124-131`):
  unchanged; the main service gets its own `build_document_service(parser=…)` beside it.
- `ContentExtractorPort` (`src/ports/content_extractor_port.py`): unchanged; both runtimes put the same adapters
  behind it.
- `_fetch`, `_extract`, `_model_hold`, `_affordable`, `_spend_usd`, record states, versions, the upgrade pass:
  untouched. The indexer imports the moved pure pieces from their new modules. No indexer decision changes:
  whether the indexer goes through the service or through the pieces is settled by D6 as "the pieces".

### 4.6 Untrusted marking: one function

`wrap_untrusted(text, *, nonce, source, ref, label)` in `src/domain/untrusted_text.py` is today's function: the nonce
is the defence, escaping is minimal (Δ34), look-alike tags are neutralised. Two things change:
- **P11. The tag word becomes source-neutral:** `<external_content nonce=… source=… ref=… label=…>`. The reminders
  say "external files and results", not "the user's drive". `drive_document` around an email attachment would tell
  the model something false. The rename is one change across drive search, transcripts, `open_file_from_drive`, the
  `untrusted` line of `PROTOCOL_AGENT_SELECTION` (Firestore) and the `drive-search` skill. Tool results never enter
  history, so no stored text carries the old tag.
- **Who marks.** The presenter marks: the open path for opened text, each renderer for content built from stored
  data (drive cards and transcripts already; email search lines, `delegation_engine.py:125-150`, and the body
  returned by `get_email_details`, `email_search_service.py:158-185`). Stored-derived content never passes through
  `read()`, so marking cannot live inside it; it lives in one function instead.
- Native bytes cannot be wrapped: they travel with `REFERENCE_REMINDER_ATTACHED`, worded per source, the label escaped
  like a wrapper attribute. The daily review's `email_for_triage {}` block in the system prompt is out of scope (§2).

### 4.7 Email specifics (D4)

- **Port.** `EmailProviderPort` gains `list_parts(creds, message_id) → [EmailPart(part_id, filename, mime, size)]`
  and `get_part(creds, message_id, part_id, max_bytes) → bytes`. The Gmail adapter walks the whole MIME tree. Today
  it downloads only top-level parts that have an `attachmentId` (`src/adapters/gmail_provider_adapter.py:389-393`)
  while it lists nested names (`:297-316`). It reads a part's inline `body.data` when there is no `attachmentId`, and
  downloads only the part asked for.
- **Open** = `list_parts` → find `part_id` → check the size against the cap → `get_part` → the document service.
  The mime is the part's own `mimeType`, used only as the declared hint; the bytes decide (§4.4 step 1). A message
  the mailbox no longer returns → "That email is no longer in the mailbox." Not connected or needs reconnect → the
  existing notes.
- **`get_email_attachment(email_id, filename)`** returns labels with refs (D4): the part or parts whose name matches,
  with no content. **P8:** `get_email_details` also lists its attachments as labels with refs, since it already
  fetches the message. Both return `history_context["email_file_context"] = [{name, ref}]`, so "open the second
  attachment" works on the next turn. `search_emails` keeps names: the index stores names only.
- **Credentials.** The open path gets Gmail access by constructor injection of `EmailSearchService` (it owns token
  refresh), under a TYPE_CHECKING import, as `DeepResearchDriveCopyService` injects `UserDriveService`.
- **Cap.** P8 also proposes that email use the shared per-kind caps instead of 3 MB. The 3 MB guarded a download of
  every attachment of the message into memory at once (`email_search_service.py:40-41`, `:212`), and per-part fetching
  removes that. The 10 MB "total" guard is unreachable today (`:229-233`).
- **Save to drive** with an `email:` ref: `resolve_file` → the part's name → the existing save rules (never
  overwrite, `Inbox/`, receipt-free). Workspace `copy` keeps its source allowlist; email is not added.

### 4.8 The parse engine (P4)

**P4. One engine: the indexer's child readers, for every source.** markitdown's auto-detect path in the main service
(`file_conversion.py:261-271`) is retired.

| Evidence | Source |
|---|---|
| Only the child bounds memory, and parsers amplify: a 5 KB XLSX reached ~1.75 GB, a 408 KB PDF ~955 MiB | USER_DRIVE_INDEXING_RFC §9 |
| Only the child drops privileges: environment, source code and other files' directories are out of reach | `decisions/drive_parser_sandbox_resource_isolation.md` |
| Its engines are pinned and versioned (`VERSION`), chosen by sniffed kind; markitdown picks a converter at runtime and falls back by exception | `readers.py:53-62`; commit "A-M pins" (2026-10-13), `requirements.txt:39-42` |
| The router needs its outcome: statuses and `pages_total`. The converter returns a string, and an error is a string too | `drive_extraction.py:63-91`; `file_conversion.py:147-155` |
| The sandbox already plans this engine behind `ContentExtractorPort` (S3) | sandbox RFC §4.10 |
| No record calls two engines the intended end state; every one defers convergence to Slice 3 | USER_DRIVE_INDEXING_RFC §9, §16, Δ35 |

**Costs, named.**
- Chat and email text changes for some files: pdfminer instead of pdfplumber layout, the child's own XLSX/XLS readers,
  HTML converted rather than raw (the "chat decision for HTML" Slice 3 named). Spike U0-b measures the differences
  before acceptance.
- The main service gains a child process with `RLIMIT_AS` 1 GiB, one at a time per process. The measured 7-day
  maximum is 803 MiB of 2 GiB, but the main service is the only chat instance. Spike U0-c measures the headroom.
- The child's uid switch needs root and the `driveparser` user. The image already has both (`Dockerfile:38-39`), and
  the main service's execution environment is not pinned.

Rejected: *keep markitdown in the main service* (two engines remain, and the main service keeps parsing in a thread
with nothing bounding it); *move every read to the indexer* (`/extract`; the indexer has internal ingress and
concurrency 1, so a chat read would wait behind a tick; `decisions/drive_extract_ingress_slice3.md`).

### 4.9 Relation to the sandbox

The sandbox is the isolation layer; this RFC fixes where it plugs in.

| Sandbox slice | What it swaps under this RFC | Callers touched |
|---|---|---|
| S3 drive parsers | `SubprocessContentExtractor` → `SandboxContentExtractor`, `PdfSplitter` → `PdfSplitterPort` HTTP adapter, in the indexer | none |
| S4 chat and on-demand reads | the same swap in the main service's `build_document_service`, behind its knob | none |

- With P4, `DocumentConverterPort`, `LocalDocumentConverter`, `SandboxDocumentConverter` and the `chat.convert` op
  are not needed. S4 becomes: admit the main service's identity to the parse ops and flip its knob. S-Δ1 (audio stays
  with the caller) and S-Δ2 (plain text stays with the caller) hold as written.
- Until S3/S4, the child gives the main service the same resource and privilege bounds the indexer has today. That
  is not containment, and the decision record already says so in those words. Its trigger names "email attachments
  at scale"; the sandbox remains the answer to it.
- The reader is never in the sandbox: it needs keys (sandbox §1). S5 (media) serves the transcription job, outside this RFC.

## 5. Owner decisions and author proposals

| # | Decision | Status |
|---|---|---|
| D1–D6 | as in the header | owner, 2026-10-22 |
| P1 | `DocumentReadPort` + `DocumentService`, one implementation today (§4.4) | author |
| P2 | bytes in; the service never fetches (§4.4) | author |
| P4 | one parse engine: the child readers; markitdown auto-detect retired; `chat.convert` not built (§4.8, §4.9) | author; gated on U0-b |
| P5 | `LLMPort.native_formats()`; passed through the delegation context (§4.3) | author, realises D3 |
| P6 | native bytes as base64, no temp files (§4.3) | author |
| P7 | interactive deadline 90 s, partial pages named (§4.4) | author |
| P8 | email: part-level fetch, refs from `get_email_details` too, `email_file_context`, shared caps (§4.7) | author, realises D4 |
| P9 | one ref classifier in `domain/file_ref.py` (§4.1) | author |
| P10 | pure pieces move to neutral modules; reader names kept (§4.5) | author |
| P11 | source-neutral wrapper tag and reminders (§4.6) | author |
| P12 | `open_file` takes every ref; `open_file_from_drive` an alias; mutations keep the store in the name (§4.2) | author |
| P13 | `ReaderAllowance` returns the 200-page ceiling for non-drive sources (§4.4) | author, realises D5 |

## 6. Deltas to other specs

| Spec § | What changes | Why |
|---|---|---|
| USER_DRIVE_INDEXING_RFC §9, last bullet | No `/extract` on the indexer. Each runtime runs the same pieces; a remote unit may come later under D1 | D1, P4 |
| USER_DRIVE_INDEXING_RFC §16 "Slice 3 — one extraction path" | Realised by this RFC (U1–U4). Done-criterion "same text as the current path" becomes the U0-b equivalence report with accepted differences | P4 |
| USER_DRIVE_INDEXING_RFC Δ35; `decisions/drive_prefetch_wrapper_slice3.md` | Closed in U3: prefetch goes through the open path and is marked | D6 |
| `decisions/drive_extract_ingress_slice3.md` | Obsolete: no main → indexer call | D1 |
| USER_DRIVE_RFC §4.4 | The store stays in the name for mutations. Reading converges: `open_file` takes every ref, `open_file_from_drive` is an alias. Prefetch stays off for FileManagement | D3, P12 |
| USER_DRIVE_RFC §4.6 | Native caps come from the consuming adapter, formats from the bytes; the 5/20 MiB constants retire | D3, P5 |
| FILE_STORAGE_RFC §2.4 step 3, §2.8, §5; file_storage README §2.1 diagram | Record that the upload turn is label-only (README §1.1 holds); the unused `path` in `process_attachment` is removed | §1.2 |
| UNTRUSTED_CONTENT_SANDBOX_RFC §1 C2, §4.7 `chat.convert`, §4.10, §7 S4, §12.10 | One parse op family for both runtimes; no `DocumentConverterPort`; S4 = identity + knob. Five converter call sites today, not four, and all retire | P4 |
| GMAIL_EMAIL_INDEXING_RFC §2.4 | `get_email_attachment` returns refs; content comes through `open_file`; the per-message download of every attachment ends | D4 |
| `decisions/grok_revival_2026_08.md` ("upstream converts non-images to text") | Becomes true: Grok declares no PDF, so PDFs reach it as text | D2 |

## 7. Slices

Each slice: tests first, the reviewer rule for any existing test, `make check` green, docs in the same PR.

| Slice | Content | Done when | Sandbox order |
|---|---|---|---|
| **U0 spikes** | §8 U0-a … U0-e | each result written down; U3 gated on U0-a and U0-c, P4 on U0-b | none needed |
| **U1 pieces** | P9 classifier; P10 moves; caps table; P11 wrapper with the Firestore token and skill updated in step | no behaviour change except the tag word; drive search G3 injected-case bar unchanged; indexer tests change import lines only | before S3 |
| **U2 declaration** | P5 `native_formats` on the four adapters plus wire tests; orchestrators pass it; P6 base64 parts; FileManagement decides native from it (still through today's converter) | a PDF opened under Grok arrives as text, not dropped; no temp file left by a native open; the adapter contract tests extended | independent |
| **U3 service** | `DocumentReadPort`, `DocumentService`, `build_document_service` in main (child parser, reader runner, splitter); the open path for chat, delivered, workspace and drive opens; prefetch through it; marking; P7, P13 | a scan opened by a text-only consumer returns reader text; a native-capable consumer gets `NATIVE`; prefetched text is marked; `convert_file_to_text`'s markitdown branch has no caller | before S4; S3 independent |
| **U4 email** | P8 port methods and adapter; `email:` branch; `get_email_attachment` / `get_email_details` refs; `email_file_context`; save from an `email:` ref | the incident's first two steps pass on a synthetic multipart fixture (nested, inline, scan); the owner's live check | after U3 |
| **S3 / S4** (sandbox RFC) | adapter swaps per §4.9 | no caller diff in the PR | as the sandbox RFC orders them |

## 8. Open questions and spikes

**Open questions (owner)**
- **OQ1. Delivering a file's original bytes to the user.** This was the incident's third failure, and it is not
  decided. What exists: `UserNotificationService.notify_file_bytes` (any bytes to the origin channel) and
  `resolve_file(ref)` (§4.2). It could be an option on `open_file`, its own intent, or not offered. Platform caps
  apply (45 MiB, today's video re-send cap).
- **OQ2. Native reading keeps file bytes inside the orchestrator's calls**, which Logfire instruments with content
  capture on. The same mechanism ran the main service out of memory on 2026-10-15. D2 keeps native reading. Should
  U0-a's suppression, if it works, also cover file parts of the orchestrator's calls?
- **OQ3. Retire `open_file_from_drive`** after P12 has run for a while, or keep the alias for good?
- **OQ4. The email cap** (P8): shared per-kind caps, or keep 3 MB?

**Spikes**
- **U0-a (D1's check). Logfire suppression per call in the main service.** The pinned Logfire (4.40.0) exposes
  `suppress_instrumentation()`. Check that the Anthropic, OpenAI and Google GenAI instrumentors honour it around one
  reader call, and that the orchestrator's spans in the same turn stay. If it does not, D1 reopens, as the owner
  stated: the reader in the main service becomes a memory risk again.
- **U0-b. Text equivalence of the two engines** on real chat and email files and on the drive evaluation corpus:
  differences listed per kind, then accepted or fixed before P4 is accepted.
- **U0-c. Parser child in the main service:** the uid switch in its execution environment; peak memory of one turn
  with a child at its limit plus a reader read.
- **U0-d. Gmail part facts:** `partId` stable across fetches; inline `body.data` sizes; nested parts; a message moved
  to trash.
- **U0-e. Per-adapter native limits:** a synthetic image and PDF at each boundary, through each adapter. Its cost is
  estimated before running; over $3 needs the owner's OK.

## 9. Where current code disagrees with this design

1. Email attachments have no ref; `get_email_attachment` returns converted text (`email_search_service.py:191-253`).
2. `EmailFullContent` carries names and bytes keyed by name, with no part id and no mime (`src/domain/email.py:62-74`).
   The adapter fetches top-level parts only, so nested and small inline parts are listed but never fetched
   (`gmail_provider_adapter.py:389-393` against `:297-316`). Two parts with the same name collide.
3. The MIME of an attachment comes from its file name (`email_search_service.py:235`). The 10 MB total guard is
   unreachable (`:229-233`).
4. The native decision ignores the consumer: `is_native_binary` (`file_conversion.py:58-60`). `vision` is declared and
   unread. Grok drops PDF parts (`grok_adapter.py:642-649`) that the open paths attach for every provider.
5. Two caps for one provider limit: drive opens allow images up to 5 MiB raw (`user_drive.py:23`), while the reader
   holds images to 3.75 MiB because Anthropic's 5 MB limit applies to the base64 (`drive_image_mime.py:12-16`). Chat
   opens have no cap at all (`file_management_agent.py:317-346`).
6. Native opens leave temp files (`file_management_agent.py:329`, `:733`); Gemini re-uploads a `path` on every call
   (`gemini_adapter.py:361-373`).
7. Chat, workspace, drive-open and email text is parsed in a thread of the main service by markitdown's auto-detect
   (`file_conversion.py:261-271`). The drive indexer uses its child readers.
8. The wrapper and reminders are drive-named (`drive_search.py:131-142`, `:563-571`); prefetched text is unwrapped
   (`agent_coordinator.py:601-603`).
9. `save_file_to_drive` derives the file name from the ref string (`file_management_agent.py:801`); an `email:` ref
   carries none.
10. `open_file` tests `is_drive_ref` first (`file_management_agent.py:169-171`); the resolver excludes other prefixes
    first (`file_conversion_service.py:57-66`). `required_capabilities` in `STRATEGIES` is enforced nowhere
    (`agent_context_builder.py:27`, `:203`). `process_attachment` keeps a `path` no adapter reads
    (`file_conversion_service.py:248-249`); FILE_STORAGE_RFC §2.4/§2.8 still describe native bytes on the upload turn.
