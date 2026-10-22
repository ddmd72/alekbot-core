# RFC: One way to open external files — refs, native reading, shared security pieces

**Status:** Proposed (rev 2). Not built. *D* items are owner decisions (2026-10-22); *P* items are author proposals the
owner has not ruled on, each separable.
**Date:** 2026-10-22 (rev 2: 2026-10-23, after four reviews)
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
- **Contract (owner, 2026-10-22):** the sandboxes are not all built; they come later and they are the isolation layer.
  This design works today (in-process engines, the drive parser child) and names the adapter each sandbox slice swaps
  in, with no second migration of callers.

Related: `USER_DRIVE_INDEXING_RFC.md` §9, §16 (Slice 3), §24.3, Δ35; `USER_DRIVE_RFC.md` §4.3–§4.6; `FILE_STORAGE_RFC.md`;
file_storage README §1.1; `UNTRUSTED_CONTENT_SANDBOX_RFC.md` §1, §4.4–§4.12, §7; `GMAIL_EMAIL_INDEXING_RFC.md` §2.4;
decision records `drive_prefetch_wrapper_slice3.md`, `drive_parser_sandbox_resource_isolation.md`,
`drive_extract_ingress_slice3.md`.

**Changes since rev 1** (four reviews, no blocker; every change below was arbitrated):
- A. The native declaration is set per engine attempt on a copy of the context, JSON-plain, dropped by the coordinator
  before specialists and Cloud Tasks; every engine runner is listed; `native_formats(model_name)` with a text-only default.
- B. `NativeFormat` gains `max_pages`; declared values never exceed today's drive caps; PDFs get a page and encryption
  check before `NATIVE`; per-request totals are a stated limitation.
- C. One absolute deadline per open, reaching the parser and the reader through an optional `ExtractionRequest` field;
  truncate-or-still-working is owner question OQ5.
- D. Prefetch for specialists reads in the async worker from the ref, with `account_id`, failures as a `file_note` (P18).
- E. `FileConversionService.open` does the generic steps; store branches stay in `FileManagementAgent`.
- F. `PARSER_UNAVAILABLE` added; `MODEL_UNAVAILABLE` reports spend; drive opens get the same 200-page ceiling; outage
  and low-density fallbacks stated.
- G. The parser slot wait is bounded by the deadline; `max_children` for the main service comes from U0-c.
- H. U0-a also tests dropping file contents from captured spans and keeping failures visible; native email opens wait for it.
- I. U3 fixes the image build check that exempts Node trees the main service loads.
- J. P4 (one engine) is separable: U3 is engine-neutral; retiring markitdown is its own slice U5.
- K. P6 (base64 instead of temp files) and the unused-`path` removal are dropped from the design; they are follow-ups.
- L. One marking function with a tag per source; the drive keeps `drive_document`; the full rename is optional P11.
- M. Marking email bodies and search lines is a separate proposal, P14.
- N. D4 is quoted as recorded; OQ1 narrowed to the surface.
- O. New labels: P15 (workspace and delivered files as external), P16 (native decision inside the service), P8 split
  into P8a–d; the name from the store applies to `email:` refs only.
- P. Presentation caps: no existing output bound is removed; new ones are labelled.
- Q. `kind_of` moves with the kind module; an interactive admission table keeps today's behaviour.
- R. Images the reader refuses get a clear note; image normalisation is the optional follow-up P17.
- S. The four tool texts that say "content" are updated in U4.
- T. Email: one listing rule, a note per failure, named public service methods.
- U. Constructor, module moves, status map, P1's reason, resolver callers, legacy upload branch, `store`/`caller`
  split, three citation fixes.
- V. Sandbox deltas completed; `STILL_WORKING` presentation defined; staging and the main service's execution
  environment stated; the 32 MiB bound noted.

## 0. In one paragraph

Three kinds of external file reach the bot: chat uploads, drive files, email attachments. Each is read a different
way today. Chat uploads and drive files are refs that `open_file` resolves; email attachments are not refs at all, and
`get_email_attachment` returns their text directly. Only the drive indexer checks a file's kind from its bytes, parses
in an isolated child process, notices a scan and sends it to a no-tool reader model. Every other read parses in a
thread of the main service and returns whatever text comes out. This RFC makes every external file a ref and gives
opening one path: resolve the ref to bytes, hand them to one **document service**. If the consuming model reads that
kind natively (its adapter says so, within today's caps), it gets the bytes; otherwise the indexer's own pieces run:
sniff, parse in the child, text-or-scan check, reader. Text that reaches a model is marked untrusted by one function.
The indexer keeps its loop and calls the same pieces. The sandbox slices later replace one adapter behind the parse
port, with no caller change.

## 1. Today, with the recorded reasons

### 1.1 The incident that started this (2026-10-22)

The owner asked the bot to read an email's PDF attachment, save it to the drive and forward it. The PDF was a scan
without a text layer. All three steps failed:
1. **Read.** `get_email_attachment` converted the file with the text-layer converter in the main service
   (`src/services/email_search_service.py:191-253`, `convert_file_to_text` at `:244`). The result was table rules and
   bullets, not empty, so no alert fired; it came back as a normal result.
2. **Save.** `save_file_to_drive` got the attachment's file name as `file_ref`. The resolver knows delivered documents,
   workspace, skill and chat refs (`src/services/file_conversion_service.py:185-210`) and drive refs (`:279-280`,
   `:311-312`); an email attachment is none of these, so the answer was "not found, ask the user to re-upload".
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
  The legacy branch that runs only without `GCS_MEDIA_BUCKET` still sends bytes on the upload turn
  (`src/handlers/conversation_handler.py:871-888`).
- **Native bytes reach the model only on the tool turn** that opened them (`src/infrastructure/delegation_engine.py:721-729`
  → a `file_data`-only part, `src/domain/llm.py:365-376`), and stay in the transcript for the rest of the loop.
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
- **Prefetch.** `AgentCoordinator` turns a `file_ref` into text before dispatch, and for ASYNC intents before enqueue,
  so the text rides in the Cloud Task body (`src/infrastructure/agent_coordinator.py:522-524`, `:544-546`,
  `:576-613`; resolver `(ref, user_id)` wired at `main.py:309`). All three consumers (DocPlanner, PdfGenerator,
  HtmlPageGenerator) are ASYNC. FileManagement opts out (`src/infrastructure/agent_manifest.py:543`; USER_DRIVE_RFC
  §4.4: waste, double fetch, a page per not-connected call).

## 2. Goals and non-goals

**Goals**
- Every external file is a ref; opening has one path; acting on a file (save, prefetch) resolves the same ref.
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
- The surface for sending a file's original bytes to the owner (D4 names the action; the surface is OQ1).
- Small defects found during the study (§10 follow-ups). Implementation; a plan follows acceptance.

## 3. Principles (D1–D6 in plain words)

1. **The model sees refs** and opens a file only when it asks; every ref kind opens through the same code (D3).
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
| Workspace / skill | `workspace:files/…` / `skill:<name>/<path>` (unchanged) | workspace bucket / Firestore | — |
| Drive | `drive:<item id>` (unchanged) | user drive | `[Drive: <path> (size) ref=drive:<id>]` |
| **Email attachment (new, D4)** | `email:<message id>/<part id>` | the user's Gmail, fetched on open | `[Email file: "<name>" (size) ref=email:<message id>/<part id>]`, name escaped like a wrapper attribute |

**P9. One ref classifier.** A pure function in `src/domain/file_ref.py` classifies a ref by prefix in a fixed order
(`email:`, `skill:`, `workspace:`, delivered prefixes, `drive:` anywhere, else a chat name). Today the order is
repeated in each resolver, and `_is_drive_ref` needs an exclusion list because `parse_drive_ref` matches `drive:`
anywhere (`file_conversion_service.py:57-66`); a new kind would add a branch to every copy.

**P15. Workspace copies and delivered documents count as external.** Chat uploads, drive files and email attachments
are external by D3. Workspace `files/` copies and delivered documents can carry third-party text, so this proposal sends
them through the document service and marks them too; it changes how Smart sees its own delivered reports. Declined,
they open as today (unmarked). `skill:` refs are the product's own instruction text: resolved and returned as today,
never read by the service or marked.

### 4.2 The open path

**Who does what.** `FileConversionService.open(ref, user, account, consumer, deadline)` does only the generic steps:
1. **Classify** the ref (P9) and **resolve** it in its store (the ownership check on delivered keys, the email part
   lookup, §4.7).
2. **Size check on metadata, before download**, against the caps table (§4.5).
3. **Download** the bytes into a per-call staging directory, removed in `finally`.
4. **Read** them through the document service (§4.4), passing the consumer's declaration (§4.3) and the deadline.
5. **Present** the outcome per §4.4's presentation table: native → a file part with the reminder; text → marked
   (§4.6) and capped (§4.5); otherwise a `[System: …]` note worded for the store.
6. **Return refs that outlive the turn** via the existing `*_context` mechanism
   (`conversation_handler.py:1383-1395`): `drive_context` as today, `email_file_context` for email (P8b).

The store-specific branches stay in `FileManagementAgent`, before it calls `open`: folders (the list hint), recordings
opened as transcripts, archived transcripts, the drive access note, and video sent to the user (`_fetch_video`; drive
branch `file_management_agent.py:741-752`). They use `DriveSearchService` and `UserNotificationService`, which the
conversion service should not depend on.

**P12. Intents.** `open_file` accepts every ref kind. `open_file_from_drive` stays as a drive-named alias over the
same path. Drive mutations keep the store in the intent name (USER_DRIVE_RFC §4.4): the rule was written against
mutations called on the wrong store, and reading is already lenient there. Email has no mutation intents (the grant is
read-only), so it needs no intent of its own.

**Interactive admission** (by the declared kind from name and declared mime, §4.5; today's behaviour kept):

| Kind | Chat / workspace / delivered / drive | Email |
|---|---|---|
| Document kinds the parser reads; plain text, CSV, JSON, Markdown, code | parse (plain text read in-process, sandbox S-Δ2) | parse |
| PDF, the four image formats | native or pipeline (§4.3, §4.4) | native or pipeline |
| Audio | transcribed inline, as today (`file_conversion.py:236-248`) | not transcribed, as today (`audio_service=None`, `email_search_service.py:244-249`): a note |
| Video | sent to the user, as today | sent to the user (the video branch, after resolve) |
| Anything else (archives, executables, other images) | refused with a note | refused with a note |

**Resolver callers** (U-m7). Every caller resolves through `open` (content for a model) or `resolve_file(ref) →
(bytes, name, mime)` (steps 1–3, bytes for an action):

| Caller | Goes through | Accepts `email:` |
|---|---|---|
| `open_file`, `open_file_from_drive` (`file_management_agent.py:308`, `:326`, `:689`, `:732`, `:742`, `:756`) | `open` / `resolve_file` | yes |
| `save_file_to_drive`, `update_file_in_drive` `source_ref` (`:632`) | `resolve_file` | yes |
| Prefetch for specialists (`main.py:309`, moved by P18) | `open`, text only | yes |
| `edit_image` (`src/agents/image_generation_agent.py:214`), video edits (`video_generation_agent.py:269`, `:324`) | `resolve_file` | yes, bytes only |
| Skill file reads (`src/services/skill_service.py:200`) | `resolve_file` | no (skill refs only) |
| Workspace `copy` (`src/services/workspace_service.py:480`) | `resolve_file` | no (its source allowlist is unchanged) |

**Naming on save.** For an `email:` ref the file name comes from the part (the ref carries none). Every other ref kind
keeps today's naming from the ref string (`drive_filename`, `file_management_agent.py:801`).

**Legacy upload branch.** The handler's branch without `FileConversionService` (`conversation_handler.py:871-888`)
is retired in U3: the upload path always stores a ref.

**P18. Prefetch reads in the async worker.** For ASYNC intents the `file_ref` travels in the Cloud Task payload and
`AgentWorkerHandler` opens it just before the specialist runs, under the task's own time; no reader text rides in a
task body, which suits D3. A SYNC prefetch (none today) would use the interactive deadline. The resolver gains
`account_id`: `open(ref, user_id, account_id, consumer=(), deadline)` replaces `resolve_content(ref, user_id)` at
`main.py:309`. On a failure the specialist gets no `file_content` and a separate `file_note` it is told to report (never
rendered into the document). A recording, folder or video ref gets a `file_note` too. Declined, prefetch keeps running
in the caller, under the interactive deadline.

### 4.3 The adapter's declaration and how it reaches the open path

**P5. `LLMPort.native_formats(model_name) -> Tuple[NativeFormat, ...]`**, where
`NativeFormat(mime, max_bytes, max_pages)` is a pure domain value. What a model reads is a property of the model, not
the adapter (`grok_adapter.py:107` lists vision per model). The port gives a **concrete text-only default `()`**: each
of the four adapters overrides it, the proxies forward it (`AlertingLLMProxy`, `CachingLLMProxy`, as they forward
`get_capabilities`, `src/services/alerting_llm_proxy.py:93`, `caching_llm_proxy.py:47`), and the six `LLMPort` test
fakes in five test files need no edit. An abstract method would force an edit to each fake through the reviewer.

**Initial values never exceed today's drive caps** (`user_drive.py:23-24`); raising any of them is an owner decision.

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
- The value is JSON-plain (a list of dicts), so the Cloud Task encoder, which handles only pydantic models
  (`src/adapters/gcp_task_queue.py:22-28`), never meets a dataclass.
- The coordinator drops the key before it builds a specialist's message (`agent_coordinator.py:531`) and before it
  enqueues (`:557-562`). The only reader is FileManagement's SYNC `open_file`.

| Engine runner | Engine call | Adapter it declares from | Opens files today |
|---|---|---|---|
| Smart | `smart_response_agent.py:573` | `eff.ctx.provider` of the current `_run` attempt (`:566`) | yes |
| Quick (fallback and `notify` formatter) | `src/agents/core/quick_response_agent.py:242` | its own `self.llm` | yes, as a fallback |
| DomainResearcher | `src/agents/domain_researcher_agent.py:141` | its own `self.llm` | yes, `open_file` is its only intent (`agent_manifest.py:691-693`) |
| Tutor | `src/agents/tutor_agent.py:192` | its own `self.llm` | no (`search_web` only) |

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
    STILL_WORKING                    # reserved for an asynchronous adapter (D1); the in-process service never returns it

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
    pages_total: int; pages_read: int; truncated: bool; may_be_incomplete: bool
    links: Tuple[str, ...]           # `extracted_by` of every link that ran, e.g. ("parser@3", "reader@2")
    model_calls: int; model_cost_usd: float    # already billed to the account; reported, not booked
    detail: str                      # a failure label, never document text

# src/ports/document_read_port.py
class DocumentReadPort(ABC):
    async def read(self, request: ReadRequest) -> ReadOutcome: ...   # every result is an outcome; it never raises
```

**`DocumentService(DocumentReadPort)`** in `src/services/document_service.py`. Its constructor takes the parser and
the reader (both `ContentExtractorPort`) and `AudioTranscriptionPort`. `PdfSplitter` is promoted to
`src/ports/pdf_splitter_port.py` in U1 (sandbox S-Δ4, pulled forward); the reader keeps holding it.

**`read()` in order:**
1. **Kind from bytes.** The declared kind comes from `kind_of(name, declared_mime)`, then `sniff_kind` checks it on the
   first 8 KiB; for images `image_mime` reads the head. Admission per §4.2's table.
2. **Cap check** for the store and kind (§4.5); over the cap → `TOO_LARGE`.
3. **Native** (P16). If the sniffed format is in `native_formats` and within `max_bytes`: an image → `NATIVE`; a PDF →
   page count and encryption check through the splitter (the child's page op), then `NATIVE` within `max_pages`.
   Over the page limit or encrypted → the pipeline.
4. **Pipeline.** `chain_for(kind)` → the parser in its child → `model_route` → the reader when routed, for at most
   `allowance()` pages (P13), within the deadline. Plain text is read in-process (sandbox S-Δ2). Audio uses the
   injected `AudioTranscriptionPort` only where §4.2's table says so.
5. **Return** the outcome, mapped per the status map (§4.5), with `links` and the spend.

**One deadline (C).** `deadline_at` is absolute. `ExtractionRequest` gains an optional deadline that may only **lower**
an adapter's own ceilings: the parser child's wall becomes min(120 s ceiling, remaining) and the reader's budget
min(its constructor value, remaining). The indexer passes none and keeps today's values. The sandbox adapter maps the
same field onto the request's `limits`, which may only lower the sandbox's own (sandbox §4.8), so S3/S4 need no second
port change. This is a change to `ContentExtractorPort`'s request; §6 declares it.

**P7. Interactive deadline: 90 s for an open**, so download + parse + reader + presentation stay inside FileManagement's
120 s timeout (`src/composition/user_agent_factory.py:864`) with a margin; FileManagement reads are not shielded (USER_DRIVE_RFC
§4.13). At the deadline the reader **cancels** windows in flight (`drive_reader_runner.py:213-217`); the finished
windows come back as `truncated` text and the note says "N of M pages read, not necessarily the first N". With no
window finished the outcome is an error (`:372`). Email attachment reads move from the email intent's 300 s to
`open_file`'s 120 s. The measured reader time on the owner's drive (30 days, at most 17 pages per file) is 12.6 s at
p50, 23 s at p90, 62 s at most. Whether a long scan should instead answer "still working" and post the rest later is
owner question OQ5.

**P13. `allowance()`, the place D5 left open.** One method, `allowance(user, account, store) -> pages`, called before
the reader. It returns the reader's existing per-file ceiling `MODEL_EXTRACT_MAX_PAGES` (200,
`src/domain/drive_index_params.py:70`, used at `drive_reader_runner.py:194`) for **every** store, `drive` included.
An interactive drive open books nothing against the drive's monthly reader pages; only the indexer touches the drive
ledgers. `BUDGET_EXHAUSTED` (`drive_reader_runner.py:130-131`) is therefore unreachable from the service. A later
budget replaces this one method.

**Outage and weak text (F).** A reader outage mid-file returns the finished windows as truncated text with a note, and
the spend (today the runner raises and drops them, `:222`). When the reader is unavailable for a low-density parse,
the parser text comes back with `may_be_incomplete` and a note saying so.

**Parser slot (G).** The child runs one at a time per adapter (`src/adapters/subprocess_content_extractor.py:99`). In
the main service that slot is shared by every user's opens and every window cut. The wait for it is bounded by the
remaining deadline; past it the outcome is `PARSER_UNAVAILABLE(busy)`, which is not a bomb. `max_children` for the
main service is sized from U0-c.

**Presentation** (in the open path):

| Outcome | What the model gets |
|---|---|
| `NATIVE` | the file part with `REFERENCE_REMINDER_ATTACHED`, worded per store |
| `TEXT` | marked text, capped (§4.5), with the truncation or "may be incomplete" note when set |
| `TOO_LARGE`, `UNSUPPORTED`, `ENCRYPTED`, `NO_TEXT`, `TIMEOUT`, `MEMORY`, `ERROR` | the existing `[System: …]` note contract, worded for the store |
| `MODEL_UNAVAILABLE` | "the document reader is temporarily unavailable; try again later" |
| `PARSER_UNAVAILABLE` | "the file converter is temporarily unavailable; ask the user to send it again later" (sandbox §4.9) |
| `STILL_WORKING` | "still reading; the result will be posted to this chat", the result delivered later through the notification path |

`PARSER_UNAVAILABLE` and `STILL_WORKING` are defined and tested in U3 (the second with a fake adapter), so S4 and a
later remote unit only swap an adapter.

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
| Explicit deadline, "still working" state | `deadline_at` in the request; `STILL_WORKING` in the schema with a defined presentation |
| Billing inside, no shared caller state | the reader runner bills the account per call, as on the indexer; the service keeps nothing between calls |

### 4.5 Shared pieces and where each lives

**What moves to source-neutral modules** (P10, no re-export shims; import lines change, which the reviewer rule
self-authorizes after a module move):
- `src/domain/document_kind.py`: `sniff_kind` (from `drive_sniff.py`), `image_mime` (from `drive_image_mime.py`),
  `kind_of` with `_EXT_KIND` (from `drive_index_policy.py:72-87`).
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
| Input to text parsing, interactive | 5 MiB (`file_conversion.py:30`) | every store; email: 3 MB today, P8d proposes 5 MiB (OQ4) |
| Native, per file | the consumer's declaration, never above 5 MiB image / 20 MiB PDF | §4.3 |
| Drive download, any | 45 MiB (`user_drive.py:22`) | the S4-era bound is 32 MiB if sandbox S0-a fails (sandbox O6) |
| Presentation, email | 30 000 characters (`file_conversion.py:31`, today's `truncate_with_alert`) | kept |
| Presentation, chat / workspace / delivered / drive opens | none today beyond the 5 MiB input | **P19:** 30 000 characters with a note, the same as email |
| Presentation, reader text | none today on the interactive path | covered by P19 |

**The indexer's use (D6), at the seams that exist today:**
- `DriveIndexingService(…, extractors=…)` (`drive_indexing_service.py:563`, `:584-586`): unchanged.
- `build_drive_models(parser=…)` (`src/composition/drive_model_setup.py:115-126`, called at `indexer_main.py:124-131`):
  unchanged; the main service gets its own `build_document_service(parser=…)` beside it.
- `ContentExtractorPort` (`src/ports/content_extractor_port.py`): one change, the optional deadline (§4.4); the
  indexer passes none.
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
- **P14 (separate):** mark email bodies from `get_email_details` (`email_search_service.py:158-185`) and the email
  search lines (`delegation_engine.py:125-150`) with the same function. They are not files; this changes two tool
  outputs, so it is outside U4's done-criterion.
- Native bytes cannot be wrapped: they travel with `REFERENCE_REMINDER_ATTACHED`, worded per store, and the label is
  escaped like a wrapper attribute.
- Content built from stored data (search cards, transcripts) never passes through `read()`, so marking cannot live
  inside it; it lives in one function that every presenter calls.

### 4.7 Email specifics (D4)

- **P8a. Part-level fetch** (realises D4). `EmailProviderPort` gains `list_parts(creds, message_id) →
  [EmailPart(part_id, filename, mime, size)]` and `get_part(creds, message_id, part_id, max_bytes) → bytes`. The Gmail
  adapter walks the whole MIME tree. Today it downloads only top-level parts with an `attachmentId`
  (`src/adapters/gmail_provider_adapter.py:389-393`) while it lists nested names (`:321-325`). It reads a part's
  inline `body.data` when there is no `attachmentId`, and downloads only the part asked for.
- **One listing rule** in the adapter, checked by U0-d: a part with a filename is an attachment; inline images with a
  `Content-ID` are listed as attachments, as today; a forwarded `message/rfc822` is listed as one part, and its
  children are not listed.
- **Open** = `list_parts` → find `part_id` → size check → `get_part` → the document service. The part's own
  `mimeType` is the declared hint; the bytes decide.
- **Notes** (the same wording for `open` and `resolve_file`):

| Case | Note |
|---|---|
| The message is gone | "That email is no longer in the mailbox." |
| Message exists, part missing | "That attachment is no longer in this email." |
| Part with neither `attachmentId` nor data | "That attachment is empty." |
| Gmail 429 / 5xx / timeout | "The mailbox did not answer; try again shortly." |
| Not connected, needs reconnect | today's notes |

  A reconnect to a different mailbox makes old refs read as "no longer in the mailbox"; that is accepted.
- **P8b. `email_file_context`** (realises D4's "the ref in history stays valid"). `get_email_attachment` returns
  labels with refs (the D3 note): the part or parts whose name matches, with no content. It also returns
  `history_context["email_file_context"] = [{name, ref}]`, so "open the second attachment" works on the next turn.
- **P8c (addition).** `get_email_details` also lists its attachments as labels with refs. That costs one `list_parts`
  call, because `deep=False` holds names only (`src/domain/email.py:62-74`).
- **P8d (limit change, OQ4).** Email uses the shared 5 MiB parse cap instead of 3 MB. The 3 MB guarded a download of
  every attachment of the message into memory at once (`email_search_service.py:40-41`, `:212`); per-part fetching
  removes that. The 10 MB "total" guard is unreachable today (`:229-233`).
- **Credentials.** The open path gets Gmail by constructor injection of `EmailSearchService`, under a TYPE_CHECKING
  import (as `DeepResearchDriveCopyService` injects `UserDriveService`). Two public methods are new on it:
  `list_attachment_parts(user_id, message_id)` and `get_attachment_part(user_id, message_id, part_id, max_bytes)`.
  Each wraps today's private refresh (`email_search_service.py:283`) around the port call.
- **Tool texts** (required by D4, in U4): the `get_email_attachment` description (`agent_manifest.py:215-219`) becomes
  "returns the attachment's ref; open it with `open_file`"; the same change goes into the hint under search results
  (`delegation_engine.py:147-148`) and the two daily-review lines (`src/services/email_review_service.py:169-170`,
  `:186`). `open_file`'s `file_ref` description (`agent_manifest.py:622-626`) names `email:` refs.
- **Save to drive** with an `email:` ref: `resolve_file` → the part's name → the existing save rules.
- **Daily review.** It reaches attachments through the same tools, so it gains scan reading; nothing else in it
  changes.

### 4.8 The parse engine (D6 and P4)

**What D6 requires** is isolated parsing on every path. That holds with either engine: U3 runs the main service's
parsing in the child. The child carries two engines today, its own readers and markitdown's converters (it imports
markitdown, `requirements.txt:39-42`), so the variant **"today's converters run inside the child"** is available.

**P4. One engine: the child's own readers, for every source.** Retiring markitdown's auto-detect path is its own slice
(U5), gated on U0-b and on the owner accepting P4.

| Evidence | Source |
|---|---|
| Only the child bounds memory, and parsers amplify: a 5 KB XLSX reached ~1.75 GB, a 408 KB PDF ~955 MiB | USER_DRIVE_INDEXING_RFC §9 |
| Its engines are pinned and versioned (`VERSION`, `subprocess_content_extractor.py:82`), chosen by sniffed kind; markitdown picks a converter at runtime and falls back by exception | `requirements.txt:39-42`; commit "A-M pins" (2026-10-13) |
| The router needs its outcome: statuses and `pages_total`; the converter returns a string, and an error is a string too | `drive_extraction.py:63-91`; `file_conversion.py:147-155` |
| The sandbox plans this engine behind `ContentExtractorPort` (S3) | sandbox RFC §4.10 |
| No record calls two engines the intended end state; every one defers convergence to Slice 3 | USER_DRIVE_INDEXING_RFC §9, §16, Δ35 |

Cost of P4: chat and email text changes for some files (pdfminer instead of pdfplumber layout, the child's XLSX/XLS
readers, HTML converted rather than raw). U0-b measures the differences.

**Costs of the child in the main service, with either engine:**
- **Memory and slot.** A child with `RLIMIT_AS` 1 GiB, one at a time per adapter, in the only chat instance (measured
  7-day maximum 803 MiB of 2 GiB). The slot wait is bounded (§4.4); U0-c measures the headroom.
- **Privileges.** The uid switch needs root and the `driveparser` user; the image has both (`Dockerfile:38-39`). The
  build check that nothing loaded is writable by the parser user skips `node_modules` because the indexer never loads
  it (`Dockerfile:42-48`). The main service does load Node trees (its in-process DOCX and PDF runners). U3 removes the
  exemption, or adds those trees, with a test that the check covers every path the main service runs. The decision
  record gets a neutral status line: the child also runs in the main runtime, under its identity, and the record's
  trigger applies there too.
- **Execution environment.** The main service does not pin one (`cloudbuild-dev.yaml:13-28`); the child's limits were
  measured on gen2. U3 pins gen2 or U0-c decides. The staged copy counts toward memory (`/tmp` is in memory).

Rejected: *keep parsing in a thread of the main service* (contradicts D6); *move every read to the indexer*
(`/extract`; the indexer has internal ingress and concurrency 1, so a chat read would wait behind a tick;
`decisions/drive_extract_ingress_slice3.md`).

### 4.9 Relation to the sandbox

| Sandbox slice | What it swaps under this RFC | Callers touched |
|---|---|---|
| S3 drive parsers | in the indexer: `SubprocessContentExtractor` → `SandboxContentExtractor`; the splitter → the `PdfSplitterPort` HTTP adapter | none |
| S4 chat and on-demand reads | the same two swaps in the main service's `build_document_service`, behind its knob | none: `PARSER_UNAVAILABLE` and its note already exist (U3) |

- With P4, `DocumentConverterPort`, `LocalDocumentConverter`, `SandboxDocumentConverter` and the `chat.convert` op
  are not needed. S4 becomes: admit the main service's identity to the parse op family and flip its knob. Without P4,
  `chat.convert` is the converter engine behind the same port. S-Δ1 (audio stays with the caller) and S-Δ2 (plain text
  stays with the caller) hold as written.
- Until S3/S4, the child gives the main service resource and privilege bounds (after U3's build-check fix). That is
  not containment, and the decision record says so in those words.
- The reader is never in the sandbox: it needs keys (sandbox §1). S5 (media) serves the transcription job, outside
  this RFC.

## 5. Owner decisions and author proposals

| # | Proposal | Status |
|---|---|---|
| D1–D6 | as in the header | owner, 2026-10-22 |
| P1 | `DocumentReadPort` + `DocumentService`, one implementation today (§4.4) | author |
| P2 | bytes in; the service never fetches (§4.4) | author |
| P4 | one parse engine, the child's readers; markitdown retired in U5 (§4.8) | author; gated on U0-b |
| P5 | `LLMPort.native_formats(model_name)` with a text-only default; per-attempt key on a context copy (§4.3) | author, realises D3 |
| P7 | interactive deadline 90 s; truncate and name the pages (§4.4) | author; OQ5 open |
| P8a | email part-level fetch incl. nested and inline parts (§4.7) | author, realises D4 |
| P8b | `email_file_context` (§4.7) | author, realises D4 |
| P8c | refs from `get_email_details` too (§4.7) | author, addition |
| P8d | email uses the shared 5 MiB cap instead of 3 MB (§4.7) | author, limit change; OQ4 |
| P9 | one ref classifier (§4.1) | author |
| P10 | pure pieces move to neutral modules; reader names kept (§4.5) | author |
| P11 | rename the drive tag to the neutral one (§4.6) | author, optional, own slice |
| P12 | `open_file` takes every ref; `open_file_from_drive` an alias (§4.2) | author |
| P13 | `allowance()` = the existing 200-page ceiling for every store (§4.4) | author, realises D5 |
| P14 | mark email bodies and search lines (§4.6) | author, separate |
| P15 | workspace and delivered files count as external (§4.1) | author |
| P16 | the native decision sits inside the service (§4.3) | author |
| P17 | normalise images in the child (§4.3) | author, optional follow-up |
| P18 | prefetch reads in the async worker from the ref (§4.2) | author; moves a caller in U3 |
| P19 | 30 000-character presentation cap on every interactive open (§4.5) | author, limit change |

## 6. Deltas to other specs

| Spec § | What changes | Why |
|---|---|---|
| USER_DRIVE_INDEXING_RFC §9, last bullet | No `/extract` on the indexer. Each runtime runs the same pieces; a remote unit may come later under D1 | D1 |
| USER_DRIVE_INDEXING_RFC §16 "Slice 3 — one extraction path" | Realised by U1–U4 (isolation and routing) and U5 (one engine, if P4). Done-criterion "same text as the current path" becomes the U0-b report with accepted differences | D6, P4 |
| USER_DRIVE_INDEXING_RFC §24.3; `ContentExtractorPort` | `ExtractionRequest` gains an optional deadline that only lowers the adapter's ceilings; the indexer passes none | §4.4 |
| USER_DRIVE_INDEXING_RFC Δ35; `decisions/drive_prefetch_wrapper_slice3.md` | Closed in U3: prefetch goes through `open` and is marked | D6 |
| `decisions/drive_extract_ingress_slice3.md` | Obsolete: no main → indexer call | D1 |
| `decisions/drive_parser_sandbox_resource_isolation.md` | A status line: the child also runs in the main runtime; the trigger applies there | §4.8 |
| USER_DRIVE_RFC §4.4 | The store stays in the name for mutations; reading converges (`open_file` takes every ref, `open_file_from_drive` an alias); prefetch stays off for FileManagement | D3, P12 |
| USER_DRIVE_RFC §4.6 | Native caps come from the consuming adapter, never above today's 5/20 MiB; formats from the bytes; PDFs page-checked | D3, P5 |
| FILE_STORAGE_RFC §2.4 step 3, §2.8, §5; file_storage README §2.1 diagram | Record that the upload turn is label-only (README §1.1 holds) and that the legacy branch is retired | §1.2 |
| UNTRUSTED_CONTENT_SANDBOX_RFC §1 C2, §4.7 `chat.convert`, §4.10, §7 S4, §12.10 | One parse op family for both runtimes; with P4 no `DocumentConverterPort`; S4 = identity + knob. Five converter call sites today, not four | P4 |
| UNTRUSTED_CONTENT_SANDBOX_RFC §4.4 | The main service calls the parse op family (today listed as `chat.*` only). Optional: rename the family `parse.*` before S3 builds it | P4 |
| UNTRUSTED_CONTENT_SANDBOX_RFC §4.8, §4.10, §4.12 | The chat cap is the caps table's; the main service also swaps the splitter; one knob governs the main service's parser and splitter | §4.5, §4.9 |
| UNTRUSTED_CONTENT_SANDBOX_RFC §7 S4 acceptance | Adds a scan opened in chat end to end under the P7 deadline; prefer the batched `drive.pdf_windows` op for the main service | P7 |
| GMAIL_EMAIL_INDEXING_RFC §2.4 | `get_email_attachment` returns refs; content comes through `open_file`; the download of every attachment per call ends | D3, D4 |
| `DRIVE_FILE_NOTES_RFC`, `SYSTEM_TURN_FINGERPRINT_RFC` | Only if P11 is accepted: they name `<drive_document>` / `wrap_untrusted` and follow the rename | P11 |
| `decisions/grok_revival_2026_08.md` ("upstream converts non-images to text") | Becomes true: Grok declares no PDF, so PDFs reach it as text | D2 |

## 7. Slices

Each slice: tests first, the reviewer rule for any existing test, `make check` green, docs in the same PR.

| Slice | Content | Done when | Order |
|---|---|---|---|
| **U0 spikes** | §8 U0-a … U0-e | each result written down | first |
| **U1 pieces** | P9; P10 moves; caps table; `PdfSplitterPort`; the marking function with a tag per store (drive unchanged) | no behaviour change; drive output byte-identical; indexer tests change import lines only | before S3 |
| **U2 declaration** | P5 on four adapters (wire tests), the proxies forward, the four engine runners set the key per attempt, the coordinator drops it; FileManagement decides native from it (still through today's converter) | a PDF opened under Grok arrives as text, not dropped; the key never reaches a specialist or a Cloud Task body; DomainResearcher and Quick keep native reading | after U1 |
| **U3 service** | `DocumentReadPort`, `DocumentService`, `build_document_service` (child parser, reader, audio); `open` for chat, delivered, workspace and drive opens; the deadline field; statuses and presentation; P13; the build-check fix; the legacy branch retired; with P18, prefetch in the worker | parsing on every interactive path runs in the child (either engine); a scan opened by a text-only consumer returns reader text; `PARSER_UNAVAILABLE` and `STILL_WORKING` presentations tested; no open exceeds FileManagement's timeout in tests; chat text marked after the owner's prompt upload | gated on U0-a, U0-c; before S4 |
| **U4 email** | P8a, P8b, the `email:` branch, notes, the two service methods, the tool texts, save from an `email:` ref | the incident's first two steps pass on a synthetic multipart fixture (nested, inline, scan); email text marked after the owner's prompt upload; the owner's live check | after U3; native email opens wait for U0-a |
| **U5 one engine** | P4: markitdown's auto-detect path retired | U0-b differences accepted by the owner | only if P4 is accepted |
| **Optional** | P11 rename; P14 email bodies and lines; P17 image normalisation; P8c; P8d; P19 | each its own PR | any time after U1 |
| **S3 / S4** (sandbox RFC) | adapter swaps per §4.9 | no caller diff in the PR | as the sandbox RFC orders them |

## 8. Open questions and spikes

**Open questions (owner)**
- **OQ1. The surface for sending the original file to the owner.** D4 names the action ("send to the owner resolves
  the same ref to bytes at action time"). Should it be an option on `open_file` or its own intent? It would build on
  `UserNotificationService.notify_file_bytes` and `resolve_file(ref)` (§4.2), within platform caps (45 MiB, today's
  video re-send cap).
- **OQ2. File contents in captured orchestrator calls.** Native reading keeps file bytes inside the orchestrator's
  calls, which Logfire captures with content on; the same mechanism ran the main service out of memory on 2026-10-15.
  Should captured spans drop file contents (less debug detail, less memory)? U0-a tests whether that is possible.
- **OQ3.** Retire `open_file_from_drive` after P12 has run for a while, or keep the alias for good? (low priority)
- **OQ4.** The email cap: the shared 5 MiB (P8d), or keep 3 MB? (low priority)
- **OQ5. A long scan opened in a conversation:** cut at the deadline and say which pages were read (P7), or answer
  "still working" and post the rest to the chat later, as drive mutations do (`file_management_agent.py:125-127`,
  USER_DRIVE_RFC §4.13)?

**Spikes**
- **U0-a (D1's check). Logfire in the main service.** The pinned Logfire (4.40.0) exposes `suppress_instrumentation()`.
  Check that the Anthropic, OpenAI and Google GenAI instrumentors honour it around one reader call, that the
  orchestrator's spans in the same turn stay, and that a suppressed call's failure stays visible as a structured log
  line (provider, error type, window). Also test dropping file-part contents from captured orchestrator spans (OQ2).
  If suppression does not work, D1 reopens, as the owner stated.
- **U0-b. Text equivalence of the two engines** on real chat and email files and on the drive evaluation corpus:
  differences listed per kind, then accepted or fixed before U5.
- **U0-c. The child in the main service:** the uid switch in its execution environment; peak memory of one turn with
  two native PDFs at the caps, capture on, and a child at its limit; slot wait p95 under concurrent opens; the staged
  copy counted.
- **U0-d. Gmail part facts:** `partId` stable across fetches; inline `body.data` sizes; nested and `message/rfc822`
  parts against the listing rule; a message moved to trash.
- **U0-e. Native limits per adapter:** a synthetic image and PDF at each declared boundary (bytes and pages), and two
  or more native files in one loop. Its cost is estimated before running; over $3 needs the owner's OK.

## 9. Where current code disagrees with this design

1. Email attachments have no ref; `get_email_attachment` returns converted text (`email_search_service.py:191-253`).
2. `EmailFullContent` carries names and bytes keyed by name, with no part id and no mime (`src/domain/email.py:62-74`).
   The adapter fetches top-level parts only, so nested and small inline parts are listed but never fetched
   (`gmail_provider_adapter.py:389-393` against `:321-325`). Two parts with the same name collide.
3. The MIME of an attachment comes from its file name (`email_search_service.py:235`). The 10 MB total guard is
   unreachable (`:229-233`).
4. The native decision ignores the consumer: `is_native_binary` (`file_conversion.py:58-60`). `vision` is declared and
   unread. Grok drops PDF parts (`grok_adapter.py:642-649`) that the open paths attach for every provider.
5. Two caps for one provider limit: drive opens allow images up to 5 MiB raw (`user_drive.py:23`), while the reader
   holds images to 3.75 MiB because Anthropic's 5 MB limit applies to the base64 (`drive_image_mime.py:12-16`). Chat
   opens have no cap at all (`file_management_agent.py:317-346`).
6. Chat, workspace, drive-open and email text is parsed in a thread of the main service by markitdown's auto-detect
   (`file_conversion.py:261-271`). The drive indexer uses its child.
7. The wrapper and reminders are drive-only (`drive_search.py:131-142`, `:563-571`); prefetched text is unwrapped and
   rides in Cloud Task bodies (`agent_coordinator.py:544-546`, `:601-603`).
8. `ContentExtractorPort` has no per-request deadline: the child's wall and the reader's budget are constructor values
   (`subprocess_content_extractor.py:85`, `drive_reader_runner.py:112`).
9. `save_file_to_drive` derives the file name from the ref string (`file_management_agent.py:801`); an `email:` ref
   carries none.
10. `open_file` tests `is_drive_ref` first (`file_management_agent.py:169-171`); the resolver excludes other prefixes
    first (`file_conversion_service.py:57-66`). `required_capabilities` in `STRATEGIES` is enforced nowhere
    (`agent_context_builder.py:27`, `:203`). FILE_STORAGE_RFC §2.4/§2.8 still describe native bytes on the upload turn.

## 10. Follow-ups outside this RFC

These were found during the study. They are tracked on the defect list (items 7, 11 and 12) and are not part of this
design:
- Native opens leave temp files behind (`file_management_agent.py:329`, `:733`). Carrying native bytes as base64
  instead would end this, but changes a provider's request path.
- `process_attachment` keeps a `path` for images and PDFs that no adapter reads (`file_conversion_service.py:248-249`).
- Gemini re-uploads a `path` file part to its Files API on every call of a loop (`gemini_adapter.py:361-373`).
