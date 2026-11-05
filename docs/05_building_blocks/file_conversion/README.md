# File Conversion and the Open Path

## Purpose

Turns a file ref into what a model can read, before any LLM adapter sees it: text, a note, or a native file the
consumer attaches. One path serves every interactive open (chat uploads, delivered documents, `workspace:` files, drive
files) and the specialists' prefetch. Spec: [`docs/10_rfcs/EXTERNAL_FILES_RFC.md`](../../10_rfcs/EXTERNAL_FILES_RFC.md)
(§4.2 the open path, §4.4 the document service, §4.5 caps, §4.6 marking; U3 built).

Goal — LLM-agnosticism: read once, works for Claude, Gemini, Grok, OpenAI without changes in adapters.

---

## Place in Architecture

```
upload turn:  ConversationHandler -> FileConversionService.process_attachment -> GCS + label-only MessagePart
              (no parsing, no content in history; no file service -> storage_unavailable_alert, see file_storage §2.1)

read turn:    opener (FileManagementAgent / DriveOperations, or the coordinator's prefetch)
                 |  open(ref, user_id, account_id, consumer=<native declaration>, deadline_at)
                 v
              FileConversionService.open        resolve the ref in its store, size check on metadata, stage bytes
                 |  ReadRequest(bytes, name, declared mime, store, caller, native formats, deadline_at)
                 v
              DocumentService.read              kind from bytes -> caps -> native or pipeline -> ReadOutcome
                 |
                 v
              OpenedFile(body | note | native file)  ->  the opener's presenter marks and cuts (present_text)
```

Layers: `FileConversionService` (`src/services/file_conversion_service.py`) resolves refs and stages bytes;
`DocumentService` (`src/services/document_service.py`) reads bytes behind `DocumentReadPort`
(`src/ports/document_read_port.py`); contracts and texts are pure domain modules (`src/domain/document_read.py`,
`document_open.py`, `document_limits.py`, `document_presentation.py`, `document_open_texts.py`);
`src/utils/file_conversion.py` keeps the alert texts and the markitdown helpers (no `src/` caller reads email through them after U4a).

> **See also:** [../file_storage/README.md](../file_storage/README.md) (GCS storage, the upload turn),
> [../user_drive/README.md](../user_drive/README.md) (drive opens, §29 bound).

---

## The open path

### `FileConversionService.open` / `prefetch`

`open(ref, user_id, account_id, *, consumer=(), deadline_at, caller=CALLER_OPEN) -> OpenedFile`
(`file_conversion_service.py:200`). It classifies the ref (`classify_ref`), resolves it in its store (delivered keys
carry the ownership check), refuses an oversize file on metadata before any download, stages the bytes for this call
only, reads them through the document service and answers exactly one of: `body` (today's `[File: <name>]` text, raw,
unmarked and uncut), `note` (a `[System: ...]` line), or a native file (`native_path` + `native_mime`). `consumer` is
the consuming model's native declaration (empty: text only). Store errors propagate; the callers map them. Folders,
recordings, video and the drive's §29 decision stay with the caller (`DriveOperations`). `email:` refs open through `open_email` (U4a): one listing of the message, a match by part id or name, one download, the document service with store `email`; a name that matches several parts returns candidate labels, never a guess.

`prefetch(ref, ..., deadline_at) -> Prefetched` (`:295`) is `open` as text only for a specialist
(DocPlanner, PdfGenerator, HtmlPageGenerator): the text as `content`, marked and cut at 60 000 characters, or a `note`
the specialist reports ("do not guess"). It never raises and never transcribes. The coordinator injects `file_content`
or `file_note` into the specialist's params (`agent_coordinator.py:609`).

### `DocumentService.read`

`read(ReadRequest) -> ReadOutcome` never raises; every failure path returns a status and logs one line (ERROR for the
systemic `error`, `memory`, `model_unavailable`, `parser_unavailable`; WARNING for the file's or the clock's), with the
request id, kind and a failure label, never text or a file name (`document_service.py`). In order:

1. **Kind from bytes** (`sniff_kind` over the first bytes, against the declared kind); never trusted from the name alone.
2. **Caps** (`domain/document_limits.py`, one table by store and kind): 5 MiB to parse as text; a text or text export up
   to 10 MiB before its inline `data:` images are dropped, then the 5 MiB cap on what is left; native files at most
   5 MiB (image) / 20 MiB (PDF), and never above the consumer's declaration.
3. **Native decision.** An image within the declaration goes native; a PDF within the declared bytes goes native after
   the page count (encrypted or too many pages goes to the pipeline). A text-only consumer always takes the pipeline.
4. **Parser service.** Every parsed kind is read by the sandbox's light service through `SandboxContentExtractor`, one
   request per call (`UNTRUSTED_CONTENT_SANDBOX_RFC`); the main service runs no parser child. An outage of the
   service answers `PARSER_UNAVAILABLE`, never the file's outcome.
5. **Router.** `model_route` decides from the parser's outcome whether the text is enough or the file is a scan or
   low-density, which goes to the reader.
6. **Reader** (`DriveReaderRunner`, no tools): at most `MODEL_EXTRACT_MAX_PAGES` = 200 pages per file (the same for
   every store; `DocumentService.allowance`), a 60 000-character text budget (`reader_text_budget`, the store's
   presentation cap, so nothing is read past what can be shown), under the open's deadline. When the reader is
   unavailable for a low-density file the parser's text is returned flagged incomplete.
7. **Outcome** (`ReadOutcome`): status, raw text, pages total / read / skipped / `cut_within_pages`, links
   (`parser@N`, `reader@N`), model calls and cost (already billed; reported, not booked again).

Plain text kinds (`text`, `markdown`, `json`, `code`, `log`, and a `text/*` csv or html until U5) are read as UTF-8 in the
service, not in the child (reading UTF-8 is not parsing).

**Chat audio (P21).** Only a chat upload opened interactively is transcribed (`OpenAITranscriptionAdapter.
transcribe_metered`); a prefetch never is, and drive recordings keep their own transcript-first rule. The call is
metered: its model and usage (tokens or audio seconds) are priced (`domain/billing.py`) and booked to the account
through the quota service. The transcript is cached per account for 90 days, the life of the upload
(`<prefix>audio_transcripts`, doc id `<account_id>:<sha256 of the bytes>:<transcriber model>`;
`firestore_transcript_cache.py`, `DATABASE_SCHEMA.md` §3.3), so a second open costs nothing.

### Parser service

There is no parser slot. The light service takes one request at a time per instance; when it is busy (HTTP 429 or 503)
the open answers `PARSER_UNAVAILABLE` / `busy` (the "converter busy" note), and one retry follows a `recycling` answer
(an instance replaced after a request). The same adapter object is the parser, the PDF splitter and the reader's window
cutter.

### Deadlines

One absolute `deadline_at` (epoch seconds) from the open's start; every wait and the child's wall clock are lowered to
the time left (`domain/document_read.py`).

| Opener | Deadline |
|---|---|
| `open_file` (FileManagement) | the agent's timeout minus `OPEN_MARGIN_S` (30 s) = 90 s (`INTERACTIVE_OPEN_S`) |
| Drive and stored opens through `DriveOperations` without an explicit deadline | `INTERACTIVE_OPEN_S` |
| Specialist prefetch on the synchronous path | `INTERACTIVE_OPEN_S` |
| Specialist prefetch in the async worker | `PREFETCH_OPEN_S` = 270 s, under Cloud Tasks' 600 s dispatch deadline |

A read past it answers `TIMEOUT` / `deadline` and the "could not be read in time; try again" note. `STILL_WORKING`
exists in the contract for a remote adapter and is never returned in-process.

---

## Presentation

`document_presentation.py` builds what the model gets from an outcome; the opener calls it, the service does not.

- **Marking per store.** `present_text` wraps the text with `wrap_untrusted` under the store's tag (drive: the
  reference reminder; every other store: the external reminder), then puts the cut note and the "what was shown" line
  OUTSIDE the wrapper. Skill files are the system's own text and stay unmarked.
- **The one cut.** 60 000 characters (`MAX_CONVERTED_CHARS`, D20) on every non-drive open path, email included; a drive
  open keeps its own §29 bound (`OPEN_MAX_CHARS` = 60 000) and cut line in `DriveOperations`. The cut falls at a
  paragraph.
- **Notes by status** (`note_for`): every per-file failure (`no_text`, `unsupported`, `encrypted`, `error`, `memory`, a
  timeout that is not the deadline) gives today's conversion alert, `too_large` today's size alert; the deadline,
  `model_unavailable` and `parser_unavailable` (unavailable / misconfigured / busy) have their own; an image the
  consumer cannot take gets the too-large or unreadable-format image note; an unsupported audio file today's audio note.
- **Shown line (D7).** A page-wise read says what was shown ("first N of M pages", pages cut within, pages skipped).

---

## Untrusted input

Files are untrusted (chat uploads, drive files, workspace and delivered files). Markitdown's builtins are off; only
local document converters are registered (`build_markitdown()` in `file_conversion.py`). Left out on purpose:
`ZipConverter` (inflates every member in RAM), `AudioConverter` (uploads audio to Google), `ImageConverter` and the URL
converters. `check_zip_container()` refuses a ZIP container declaring more than `MAX_ZIP_UNCOMPRESSED_BYTES` (100 MiB)
or `MAX_ZIP_MEMBERS` (10 000); both helpers live in `src/utils/document_hardening.py`, free of logger and telemetry
imports because the parser child imports them for every file. Memory amplification inside a parser (a 5 KB XLSX
measured near 1.75 GB) is bounded by the child's limits, not by these checks.

File contents never leave in telemetry: file parts of LLM calls are replaced by `file_omitted` in exported spans
(`docs/07_deployment/LOGGING.md`).

---

## Not built yet

- **Email save and send (U4b).** `copy_file_to_drive` and `send_file_to_user` from an `email:` ref; until then a save or
  send of an `email:` ref answers with an honest "not available yet" line.
- **One parse engine (U5).** Until then a `text/*` upload of csv or html is read raw as UTF-8 in the service (A3), and
  markitdown's auto-detect path in `convert_file_to_text` is kept only for the U0-b report script; U5 moves it to the
  child's readers with the four fixes of RFC §7.
- `resolve_file` (the shared resolver for actions) is deferred to U4b.

---

## Alert texts (graceful degradation)

Any failure becomes a `[System: ...]` note in the conversation, not an exception: the model sees a directive and tells
the user in natural language what happened and what to try. Technical detail (exception type, path) stays in logs. The
rejected alternatives were an empty response, a bot-level error message and silent data loss.

| Scenario | Text | What the model will say |
|---|---|---|
| File over the parse cap | `size_alert` | Ask for a smaller file or pasted text |
| Unsupported format / conversion error | `conversion_alert` | Explain supported formats |
| Audio without a transcriber (or a prefetch) | `audio_unavailable_note` | Ask for text instead |
| Text cut | `cut_note` | The file was too large for full analysis; suggest splitting |
| Converter or reader unavailable, or busy | `parser_unavailable_note`, `model_unavailable_note` | Try again later |
| Not read in time | `deadline_note` | Try again |

---

## History Storage (tiered history)

The upload turn stores only a label (`[File: "<ref>" (<size>)]`) plus `file_data` metadata; a file's content is never in
session history, and the model reads it with `open_file` when it needs it. The tiered `full_text`/stub mechanism
(`make_history_stub`, `BaseAgent._apply_history_tier`) remains for text that has both forms; a read file's text is the
tool result of that turn, not history.

---

## Supported Formats

| Kind | Path |
|---|---|
| `image/*` | native when the consumer declares it and the file is within the cap; else the reader |
| `application/pdf` | native within the declared bytes and pages; else parser, router, reader |
| text kinds | UTF-8 in the service |
| docx, pptx, xlsx, xls, odt, ods, odp, epub, msg, rtf, eml, subtitles, html, csv | parser child |
| `audio/*` | chat upload, interactive open only: transcribed, metered, cached; else the audio note |
| anything else | `unsupported` -> conversion alert |

---

## Dependencies

```
requirements-dev.txt (tests, CI, local scripts; the main image does not install it):
markitdown[all]   # [all] required: onnxruntime describes images inside DOCX/XLSX
```

---

## History

Added: 2026-02-19 (Claude API returned HTTP 400 for unsupported MIME types; graceful degradation with a system alert
chosen over silent skip). Rebuilt around one open path with the document service in U3 (EXTERNAL_FILES_RFC); the legacy
handler branch that parsed uploads in the conversation handler is gone.
