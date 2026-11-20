<!-- living-doc
covers:
  src/services/file_conversion_service.py
  src/services/document_service.py
  src/composition/document_service_setup.py
  src/domain/document_open.py
  src/domain/document_open_texts.py
  src/domain/document_read.py
  src/domain/document_presentation.py
  src/domain/document_limits.py
  src/domain/active_kind.py
  src/domain/file_ref.py
  src/domain/untrusted_text.py
  src/domain/container_facts.py
  src/domain/audio_transcript.py
  src/utils/file_conversion.py
  src/utils/document_hardening.py
  src/ports/document_read_port.py
  src/ports/container_facts_port.py
  src/ports/pdf_splitter_port.py
  src/ports/audio_transcription_port.py
  src/ports/transcript_cache_port.py
  src/adapters/openai_transcription_adapter.py
  src/adapters/firestore_transcript_cache.py
-->
# File Conversion and the Open Path

## Purpose

This block turns a file ref into what a model can read, before any LLM adapter sees it: text, a note, or a native
file the consumer attaches. One path serves every interactive open (chat uploads, delivered documents, `workspace:`
files, `email:` attachments, and the file-reading half of drive opens) and the specialists' prefetch. It is what makes
file reading independent of the provider: the adapters never parse a file.

It also resolves a ref to plain bytes for the two actions no model reads (save to the drive, send to the chat), and
decides which email attachments may not be forwarded.

What it is not: it does not store uploads (see [file_storage](../file_storage/README.md)), it does not decide a
drive open's §29 bound, folders, recordings or video (`DriveOperations` does, see [user_drive](../user_drive/README.md)),
and it runs no parser itself (the sandbox's light service does). The spec is
[`EXTERNAL_FILES_RFC.md`](../../10_rfcs/EXTERNAL_FILES_RFC.md).

## Owned data

One collection: the transcript cache of chat audio, `EnvironmentConfig.audio_transcripts_collection`, written and read
only by `FirestoreTranscriptCache` (`src/adapters/firestore_transcript_cache.py`). Document id
`{account_id}:{sha256 of the bytes}:{transcriber model}`; fields `text`, `account_id`, `written_at`, `purge_after`
(`TRANSCRIPT_CACHE_DAYS` after the write; a read treats an expired entry as absent). Schema row:
[`DATABASE_SCHEMA.md`](../../08_concepts/DATABASE_SCHEMA.md) §3.3. The cache outlives the upload it was made from: the
bucket lifecycle deletes the upload sooner (see file_storage).

Nothing else is stored. Uploaded bytes live in the storage block; a read file's text is the tool result of its turn and
is never put in session history.

## Flows

### Upload turn

`ConversationHandler` calls `FileConversionService.process_attachment`: the bytes go to storage, and the message gets a
label-only `MessagePart` (`[File: "<ref>" (<size>)]`, plus `file_data` with ref, mime, size, original name, and the local
path for a native binary). Nothing is parsed and no content enters history; the model reads the file later with
`open_file`. With no file service the handler answers `storage_unavailable_alert` (`src/utils/file_conversion.py`).

### Open (one file, for a model)

Entry: `FileConversionService.open`. It takes the consuming model's native declaration (`consumer`; empty means text
only), the one absolute `deadline_at`, and a `caller` (`CALLER_OPEN`, `CALLER_PREFETCH` or `CALLER_CONNECTOR`). The email
index does not come through `open`: it builds its own `ReadRequest` with `CALLER_INDEX` for `DocumentService.read`
(`src/domain/document_read.py`).

1. `classify_ref` (`src/domain/file_ref.py::classify_ref`) picks the store: email, skill, workspace, delivered, drive,
   else chat. Trap: a skill, workspace or delivered ref whose name contains `drive:` keeps its own store, because those
   prefixes are decided first (`file_conversion_service.py::_is_drive_ref`); a chat ref never holds `:` (the upload's
   stored name goes through `sanitize_filename`, `src/adapters/gcs_file_storage_adapter.py`), so only a prefixed ref can
   carry `drive:` in a name.
2. The ref is resolved in its store. Delivered keys carry an ownership check (the second path segment must be the
   requesting user; `_download_by_ref`). A drive ref goes through `DriveConnectionService` and the access policy's READ
   check before any download or any text that prints its path (`_drive_item`). Store errors propagate; the callers map
   them.
3. Size: a drive file and an email part are checked on metadata before download; chat and delivered bytes are
   downloaded first and capped by the document service. A workspace text file is capped by `OPEN_FILE_MAX_TEXT_BYTES`
   and decoded as UTF-8 in `_open` itself, and a skill ref is read by the skill resolver: neither reaches the document
   service. A drive text file is streamed up to
   `MAX_TEXT_EXPORT_BYTES`.
4. Every other file's bytes go to `DocumentService.read` as a `ReadRequest` (bytes and plain values only, `store` = the ref kind).
5. The answer is an `OpenedFile` (`src/domain/document_open.py`): `body` (`[File: <name>]` text, raw, unmarked, uncut),
   a `note`, or a native file (`native_path` + `native_mime`). The opener's presenter marks and cuts the text (see
   Presentation). Folders, recordings, video and the drive's §29 decision stay with the caller.

`email:` refs open through `FileConversionService.open_email`: one part listing, a match by part id (authoritative: a
part id that matches nothing is "missing", never a fall back to the name) or by name, one download, then the document
service with store `email`. A name matching several parts opens nothing and returns candidates plus the choose-one note;
that is a successful answer. Every expected failure is a note from `src/domain/email_open_texts.py`.

A native file is written to a temp file outside any staging directory (`_native_file`) because the adapter reads it later
in the turn; the consumer that attaches it owns it, and `DelegationEngine.execute` removes it when the run ends.

### Prefetch (a specialist's file)

`FileConversionService.prefetch` is `open` as text only: the text as `content` (marked and cut), or a `note` the
specialist reports instead of guessing. It never raises and never transcribes. `AgentCoordinator._resolve_file_refs`
(called by `prefetch_file_ref`) puts the result in the specialist's params as `file_content` or `file_note`; an agent
opts out with `AgentDescriptor.prefetch_file_ref=False` (the email, file-management and drive agents read for
themselves). The synchronous path uses `INTERACTIVE_OPEN_S`; the async worker (`AgentWorkerHandler`) uses
`PREFETCH_OPEN_S`.

### Document service read

Entry: `DocumentService.read` (`src/services/document_service.py`), behind `DocumentReadPort`. It never raises: every
failure is a `ReadOutcome` status, and every status except `TEXT`, `NATIVE` and `NO_TEXT` also gets one log line with the
request id, kind and a failure label, never text or a file name (the open path logs `NO_TEXT` itself). In order (`_read`, `_admit`):

1. **Kind from bytes.** `sniff_kind` over the first bytes against the declared kind; the name alone is never trusted.
2. **Audio** goes to the audio path below; PDF and image to step 3; the in-process text kinds
   (`IN_PROCESS_TEXT_KINDS`, plus `other`, `data_dump`, `mhtml` when the declared mime is `text/*`) are decoded as UTF-8
   in the service, since reading UTF-8 is not parsing; kinds in `PARSE_KINDS` go to step 4; anything else is
   `UNSUPPORTED`.
3. **Native decision.** An image, or a PDF, within the consumer's declared `max_bytes` goes native; a PDF also needs its
   page count (`_native_pdf`): encrypted or over `max_pages` falls to the pipeline. A text-only consumer always takes the
   pipeline.
4. **Caps** (`src/domain/document_limits.py`, one table by store and kind): `parse_input_cap` before parsing; plain text
   up to `text_export_cap`, then `MAX_FILE_BYTES` once inline `data:` images are dropped. A native declaration can never
   exceed `native_ceiling` (checked when `NativeFormat` is built).
5. **Parser.** Every parsed kind is read by the sandbox's light service through `SandboxContentExtractor`, one request
   per call; the main service runs no parser child. An outage answers `PARSER_UNAVAILABLE`, never the file's outcome.
6. **Router and reader.** `model_route` (`src/domain/document_extraction.py`) decides from the parser's outcome whether
   the text is enough or the file is a scan or low-density. Those go to the reader (`DriveReaderRunner`, no tools): at
   most `MODEL_EXTRACT_MAX_PAGES` pages per file for every store (`DocumentService.allowance`), a text budget equal to
   the store's presentation cap (`reader_text_budget`), under the open's deadline. If the reader is unavailable for a
   low-density file, the parser's text comes back flagged `may_be_incomplete`.
7. **Outcome** (`ReadOutcome`): raw, unmarked text with the page and cost facts of the read. Model cost is already billed;
   it is reported, not booked again.

Who may run the reader (`ReadRequest.caller`): open and prefetch always; `CALLER_CONNECTOR` (the drive connector) never,
what the router would hand the reader answers `NO_TEXT` with a `READER_SKIPPED` detail; `CALLER_INDEX` (the email index)
only when the parser's page count is known and fits `ReadRequest.reader_pages` (`reader_admits`, all or nothing), so the
index reads a scan in two passes (parser only, then with `reader_pages`).

### Parser service

There is no parser slot in the main service. The light service takes one request at a time per instance; HTTP 429, or a
`recycling` 503 that repeats after its one retry, ends as `ParserUnavailable("busy")` and the "converter busy" note; a
503 the sandbox never sent (a lost instance) is `unavailable` after the same one retry
(`src/adapters/sandbox_client/sandbox_http_client.py`). One adapter object serves as the parser, the PDF splitter
(`PdfSplitterPort`) and the container-facts reader (`ContainerFactsPort`). A caller that reads many files in a row backs
off on `busy`: the email index (`EmailAttachmentTextService`, `PARSE_CONCURRENCY` in flight, `BUSY_RETRY_DELAYS_S`, then
the mail stays pending; on the reader pass a busy answer is retried only while nothing was spent).

### Audio

Only a chat upload opened interactively is transcribed (`DocumentService._audio`): any other store, a prefetch, or no
transcriber (no `OPENAI_API_KEY`, `build_document_backends`) answers `UNSUPPORTED` and the audio note. Drive recordings
keep their own transcript-first rule in `DriveOperations`. The call is metered (`OpenAITranscriptionAdapter.
transcribe_metered`): model and usage are priced (`src/domain/billing.py`) and booked to the account through the quota
service. A cached transcript is returned before any call, so a second open costs nothing.

### Deadlines

One absolute `deadline_at` (epoch seconds) from the open's start; every wait and the child's wall clock are lowered to the
time left. `DocumentService.read` wraps the whole read in it and answers `TIMEOUT` / `deadline`.

| Opener | Deadline |
|---|---|
| `open_file` (`FileManagementAgent._open_budget_s`) | the agent's timeout minus `OPEN_MARGIN_S`; `INTERACTIVE_OPEN_S` with no timeout |
| Drive and stored opens through `DriveOperations` without an explicit deadline | `INTERACTIVE_OPEN_S` |
| Specialist prefetch, synchronous path | `INTERACTIVE_OPEN_S` |
| Specialist prefetch, async worker | `PREFETCH_OPEN_S`, under Cloud Tasks' dispatch deadline |

`STILL_WORKING` exists in the contract for a remote adapter and is never returned in-process.

### Resolve bytes for save and send

`FileConversionService.resolve_file` returns the bytes, name and declared mime, for the action `save` or `send`, of any ref for an action no model reads: nothing is parsed, marked or cut. The cap is `action_cap()`,
checked on metadata where the store has it (drive, email) and on the bytes elsewhere. A Google native file is named by
its export (`DriveItem.exported`, `export_file_name`). What the service decides itself raises `FileNotResolved`, whose
`line` is the honest note; callers answer with it. Callers: `DriveOperations.copy` (the `copy_file_to_drive` tool) and
`FileManagementAgent._send` (`send_file_to_user`, SYNC, delivery through `UserNotificationService.notify_file_bytes`).
`resolve_bytes` is the plain bytes path of other callers (video re-send, image and video generation inputs, skill and
workspace services); it refuses `email:` refs.

### Active kinds from email (D13)

For an `email:` ref only, both callers above call `FileConversionService.active_kind_of` on the file as resolved and
again under the name the model gave; a refusal uploads nothing (owner ruling 2026-11-05). The decision is the pure
function `src/domain/active_kind.py::active_kind`: head magic over `ACTIVE_HEAD_BYTES`, then the container's macros,
then the name's extension, then the mime. A name or mime naming an active kind blocks, never unblocks. The kind table is
in that module's docstring. A ZIP-family file or an OLE2 head (a legacy `.doc`/`.xls`/`.ppt` under any name) needs the
macro facts, read by the sandbox (`ContainerFactsPort`, op `parse.container_facts`, central directory and content types
only; the main service reads no container).

### Presentation

`src/domain/document_presentation.py` builds what the model gets from an outcome; the opener calls it, the service does
not.

- `present_text` wraps the text with `wrap_untrusted` under the store's tag (drive: `DRIVE_TAG` with the reference
  reminder; every other store: `EXTERNAL_TAG` with the external reminder), cuts at a paragraph, and puts the cut note and
  the "what was shown" line (`shown_line`) outside the wrapper. Skill files are the system's own text and stay unmarked.
- One cut: `MAX_CONVERTED_CHARS` on every non-drive store, email included (`presentation_cap`). A drive open keeps its own
  bound, `OPEN_MAX_CHARS`, and cut line in `DriveOperations`.
- `note_for` maps every status that carries no text to a note: per-file failures give the conversion alert, `TOO_LARGE`
  the size alert; the deadline, `MODEL_UNAVAILABLE` and `PARSER_UNAVAILABLE` have their own; an image the consumer cannot
  take and an unsupported audio file have theirs. The texts are in `src/domain/document_open_texts.py`.

## Invariants and traps

- **Bytes decide the kind.** A file named `.pdf` that is not one is not read as a PDF (`sniff_kind`).
- **The service returns raw text; the presenter marks it.** Text from a file reaches a model only through
  `present_text` (or `DriveOperations`' own wrapper for the drive), under a per-call nonce; look-alike wrapper and
  system tags in the text are neutralised (`src/domain/untrusted_text.py::wrap_untrusted`). A new opener that skips the
  presenter hands the model unmarked file text.
- **Every failure is a note, never an exception, and never in the system's voice.** Notes read `[Tool note: ...]` and tell
  the model what to say; technical detail stays in logs. The rejected alternatives were an empty response, a bot-level
  error message and silent data loss (root `CLAUDE.md`, "Code-written text never claims the system's
  voice"). `DocumentService.read` and `prefetch` never raise; `open` lets store errors propagate by design.
- **An outage is not the file's outcome.** A parser or reader outage is `PARSER_UNAVAILABLE` / `MODEL_UNAVAILABLE`, so a
  caller never records "unreadable file" for an outage (the email index keeps the mail pending). Never add an in-process
  parser to a deployed composition (`src/services/CLAUDE.md`).
- **Log levels feed alerting.** One set decides: `src/domain/document_read.py::SYSTEM_FAULT_STATUSES` (`ERROR`,
  `MEMORY`, `MODEL_UNAVAILABLE`, `PARSER_UNAVAILABLE`) logs ERROR in `DocumentService`, the open path
  (`file_conversion_service.py::_log_unread`) and `DriveOperations`; other failures are WARNING, `NO_TEXT` logs nothing.
  A timeout (`TIMEOUT`, `STILL_WORKING`) is the clock's: WARNING everywhere.
- **Fail closed on D13.** No container-facts reader, an outage, a run failure, `error` or `too_large` leaves the container
  unchecked, and `active_kind` answers `UNCHECKED`: the file is refused with a "could not be checked" note, never sent.
  Drive and chat refs are not checked. Tests: `tests/unit/domain/test_active_kind.py`, `tests/unit/agents/test_file_management_send_file.py`, `tests/unit/infrastructure/test_drive_operations_copy_email_active.py`.
- **Caps live in one table.** `src/domain/document_limits.py`; `workspace.MAX_FILE_BYTES` is a different constant with the
  same name and is never merged with it. The module is imported by the parser child, so it stays stdlib only.
- **markitdown lives only in the parser child** (`src/adapters/drive_parser/readers.py`), through the converter classes
  named in `MARKITDOWN_ALLOWED_CONVERTERS` (`src/utils/document_hardening.py`), called directly (no `MarkItDown` object,
  so no builtins). Enforced by `tests/unit/test_markitdown_only_in_the_child.py` (no import elsewhere in `src/`) and
  `tests/unit/utils/test_markitdown_allowlist.py` (the unsafe converters stay off: zip inflates every member in RAM, audio
  uploads to a third party, image and URL converters call out).
- **ZIP bombs.** `check_zip_container` refuses a container declaring more than `MAX_ZIP_UNCOMPRESSED_BYTES` or
  `MAX_ZIP_MEMBERS`, reading only the central directory. `document_hardening.py` imports no logger or telemetry because the
  child imports it for every file. Memory amplification inside a parser (a tiny XLSX can inflate to gigabytes) is bounded
  by the child's resource limits, not by these checks.
- **File contents never leave in telemetry.** File parts of LLM calls are replaced by `file_omitted` in exported spans
  (`src/utils/logfire_redacting_exporter.py`, [`LOGGING.md`](../../07_deployment/LOGGING.md)).
- **Audio is interactive and chat-only.** Prefetch never transcribes, so a specialist cannot spend on audio unasked.
- **A native file is a temp file with an owner.** Whoever attaches `native_path` must remove it (`DelegationEngine`
  does); a failed write removes it in `_native_file`.
- **Dependencies.** The parser libraries, markitdown among them, are pinned in `requirements-sandbox-light.txt` (the
  image that parses). `requirements-dev.txt` carries them for tests and local scripts; the main image does not install
  them. markitdown needs its `[all]` extra.

## Links

- [`EXTERNAL_FILES_RFC.md`](../../10_rfcs/EXTERNAL_FILES_RFC.md): §4.2 the open path, §4.4 the document service, §4.5
  caps, §4.6 marking, D13 active kinds.
- [`UNTRUSTED_CONTENT_SANDBOX_RFC.md`](../../10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md): the light service the parser
  runs in.
- [file_storage](../file_storage/README.md) (the upload turn, bucket lifecycle),
  [user_drive](../user_drive/README.md) (drive opens, the §29 bound),
  [gmail_email_indexing](../gmail_email_indexing/README.md) (the `index` caller).

## Where to start reading

- `src/services/file_conversion_service.py::FileConversionService.open` and `.prefetch`: the open path.
- `src/services/document_service.py::DocumentService.read`: kind, caps, native, parser, router, reader.
- `src/domain/document_read.py::ReadRequest` and `ReadOutcome`: the contract and its deadlines.
- `src/domain/document_limits.py`: every cap.
- `src/domain/document_presentation.py::present_text`: marking and the cut.
- `src/services/file_conversion_service.py::FileConversionService.resolve_file` and `.active_kind_of`: save, send, D13.
