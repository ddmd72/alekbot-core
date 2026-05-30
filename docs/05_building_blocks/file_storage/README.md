# File Storage (Building Block)

## HowTo: Using This Document

### Purpose

Describes the GCS-backed file storage system: upload, fetch, specialist injection, and
history management for user file attachments.

### When to Read

- Before modifying file upload/download flows in ConversationHandler
- Before adding new file-aware agents or modifying FileManagementAgent
- Before changing AgentCoordinator file_ref resolution logic
- When debugging "file not found" or history bloat issues

### When to Update

- [ ] FileStoragePort interface changes (new methods)
- [ ] GcsFileStorageAdapter dedup or sanitization logic changes
- [ ] FileConversionService.process_attachment return format changes
- [ ] FileManagementAgent adds new intents (Phase 2: list_files, search_files)
- [ ] AgentCoordinator._resolve_file_refs flow changes
- [ ] ConversationHandler history cleaning logic changes
- [ ] GCS bucket lifecycle rules change

### Cross-References

- **File Conversion:** [../file_conversion/README.md](../file_conversion/README.md) — text extraction utilities
- **Document Generation:** [../document_generation/README.md](../document_generation/README.md) — file_ref support in generators
- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md) — FileManagementAgent in catalogue
- **RFC:** [../../10_rfcs/FILE_STORAGE_RFC.md](../../10_rfcs/FILE_STORAGE_RFC.md) — design decisions and rationale

---

## 1. Overview

### Problem

1. **File content bloats conversation history** — up to 30,000 chars (~7K tokens) per file persisted in Firestore
2. **Native binary files** (images, PDFs) only accessible on the current turn; cannot be re-fetched later
3. **Delegation-only files** — full content unnecessarily enters orchestrator context when forwarding to specialists

### Solution

**File content is never stored in MessagePart or session history.** All files go to GCS as
references (filename only). Content is fetched on-demand.

### Design Principle

| Before | After |
|--------|-------|
| MessagePart(text="30K chars of DOCX content") | MessagePart(text='[File: "report.docx" (45KB)]', file_data={"ref": "report.docx", ...}) |
| ~7K tokens per file in every history load | ~40 chars label + ~100 chars metadata |

### 1.1 Design Intent — read this before "fixing" image handling

**Files (including images) are deliberately NOT inlined into the orchestrator.** On upload they
go to GCS; the orchestrator (Smart/Quick) receives only a label `[File: "<ref>" (size)]` plus
`file_data` reference metadata — never the bytes. This is intentional, for three reasons:

1. **Don't bloat orchestrator context** with binary it usually doesn't need.
2. **Forward-by-reference:** the orchestrator can hand a file to a specialist by name
   (`file_ref`) without ever loading it (`AgentCoordinator._resolve_file_refs`).
3. **Tool-on-demand:** when the request is actually *about* the file, the orchestrator calls a
   tool (`open_file`) to read it (text → converted; image → temp file for vision). The image
   reaches the model on that tool turn, on a `file_data`-only `MessagePart` produced by
   `build_tool_turn`.

Consequence: the `if part.text: … elif part.file_data:` chain in **all** LLM adapters
(claude/openai/gemini) is **correct, not a bug.** The attachment part carries both a label
(`text`) and a reference (`file_data`); the adapter emitting only the label is exactly the
reference-only behavior above. The orchestrator is *meant* to be "blind" to the attachment on
the upload turn and fetch it via tool. Do not "fix" the adapters to co-emit the image — that
would defeat the design.

> **CRITICAL INVARIANT — the label must carry the unique GCS `ref`, never the original filename.**
> The orchestrator addresses files **by the name shown in the label** when it calls `open_file`
> or forwards `file_ref`. GCS download is an **exact-key** match. Slack names every pasted image
> `image.png`; dedup gives a unique `ref` (`image (4).png`), but if the label showed the original
> `image.png`, the tool call would land on whatever stale object squats the un-suffixed slot
> (e.g. a months-old `image.png`) → bot describes the *wrong/old* image. The debug logger only
> serializes `p.text` (never `file_data`), so dumps cannot show whether an image was inlined —
> do not diagnose vision from request dumps. Fixed 2026-05-30: `file_conversion_service.py`
> builds the label from `ref`, not `filename`.

---

## 2. Architecture

### 2.1 Upload Flow

```
Platform Adapter (Slack/Telegram)
        | FileAttachment(url, mime_type, filename)
        v
ConversationHandler
        |
        |-- FileConversionService present?
        |       YES --> process_attachment()
        |               |-- Read bytes from local_path
        |               |-- Upload to GCS (sanitize + dedup)
        |               |-- Return reference-only MessagePart:
        |               |     text = [File: "report.docx" (45KB)]
        |               |     file_data = {ref, mime_type, size_bytes, path?}
        |               |-- Native binary: file_data includes "path" for current turn
        |       NO  --> Legacy path (inline content)
        v
LLM adapters encode file_data.path for current turn if present
```

### 2.2 Fetch Flow (orchestrator-driven)

```
Orchestrator (Smart/Quick)
        | delegate_to_specialist(intent="open_file", context={file_ref})
        v
AgentCoordinator --> FileManagementAgent
        |
        |-- Infer MIME type from extension
        |-- is_native_binary?
        |       YES --> download from GCS --> temp file
        |               Return file_data in metadata (LLM vision)
        |       NO  --> resolve_content (download + convert)
        |               Return full text (no truncation)
        v
SmartAgent reads file_data from metadata --> attaches as MessagePart
Tool response NOT saved to history (content is ephemeral)
```

### 2.3 Specialist File Injection

```
Orchestrator delegates: delegate_to_specialist(intent="create_pdf", context={file_ref})
        |
        v
AgentCoordinator._resolve_file_refs()
        |-- Downloads file from GCS
        |-- Converts to text via FileConversionService.resolve_content()
        |-- Injects "file_content" into specialist params
        v
Specialist receives pre-resolved text content
Orchestrator never sees raw file content
```

### 2.3b Bound Channel File Handling

For bound channels (`SessionMode.is_bound`), `ConversationHandler` strips the `path` field from
`file_data` before passing `message_parts` to the agent. This prevents LLM adapters from inlining
the binary file content. The GCS `ref` and label text (`[File: name (size)]`) are preserved.

The bound agent sees the file label in its platform history (Slack conversations.history) and
accesses the content on demand via `open_file` delegation (DelegationEngine → FileManagementAgent).

```
ConversationHandler (mode.is_bound):
  process_attachment() → GCS upload → MessagePart with file_data={ref, path, mime_type}
        |
        v
  Strip "path" from file_data → MessagePart with file_data={ref, mime_type, size_bytes}
        |
        v
  Agent receives text label only: "[File: report.docx (2.3MB)]"
  Agent calls open_file delegation when it needs the content
```

### 2.4 History Save Flow

```
message_parts cleanup (before saving to Firestore):

file_data has "ref" + "path"?
    --> Strip "path", keep "ref" (GCS-backed, persist reference)

file_data has only "path"?
    --> Skip entirely (legacy temp file, won't exist on next request)

Converted text file (id in file_part_stubs)?
    --> Replace with stub/full_text pair (tiered history)

Everything else:
    --> Keep as-is
```

---

## 3. Components

### 3.1 FileStoragePort

**Location:** `src/ports/file_storage_port.py`

| Method | Signature | Description |
|--------|-----------|-------------|
| `upload` | `(data, filename, user_id, content_type) -> str` | Upload with Finder-style dedup; returns deduplicated name |
| `download` | `(filename, user_id) -> bytes` | Download raw bytes |
| `delete` | `(filename, user_id) -> None` | Remove file |
| `exists` | `(filename, user_id) -> bool` | Check existence (for dedup) |
| `get_url` | `(filename, user_id) -> str` | Assemble public URL |

All methods are async. Callers never see storage keys — adapter assembles `{user_id}/files/{filename}` internally.

**Distinction from MediaStoragePort:** MediaStoragePort stores public non-PII content (HTML pages, widgets)
with public URLs. FileStoragePort stores private user file attachments with TTL and dedup.

### 3.2 GcsFileStorageAdapter

**Location:** `src/adapters/gcs_file_storage_adapter.py`

- **Finder-style dedup:** `report.docx` -> `report (1).docx` -> `report (2).docx`
- **Filename sanitization:** GCS-prohibited chars (`#?[]*\n\r\t`) replaced with underscore; UTF-8 preserved
- **Lazy client init:** GCS client created on first use, cached
- **Async/sync separation:** All GCS I/O via `run_in_executor`

**GCS Bucket Structure:**

```
alek-media-{env}/
  {user_id}/
    files/           <-- user attachments (this system)
    deep_research/   <-- research reports (existing)
    documents/       <-- generated PDFs/HTML (existing)
```

Lifecycle rule: delete objects matching `*/files/*` older than 90 days.

### 3.3 FileConversionService

**Location:** `src/services/file_conversion_service.py`

| Method | Purpose |
|--------|---------|
| `process_attachment(local_path, filename, mime_type, user_id)` | Upload to GCS, return reference-only MessagePart |
| `resolve_content(ref, user_id)` | Download from GCS + convert to text |
| `resolve_bytes(ref, user_id)` | Download raw bytes (for specialists needing original) |

**Reference-only MessagePart format:**

```python
# Dedup example: Slack sent "image.png", but that name was taken → ref = "image (4).png".
MessagePart(
    text='[File: "image (4).png" (365KB)]',  # label uses REF (see §1.1 invariant), not original
    file_data={
        "ref": "image (4).png",          # deduplicated filename — the addressable name
        "mime_type": "image/png",
        "size_bytes": 365000,
        "original_name": "image.png",    # only set when ref != original (dedup/sanitization)
        "path": "/tmp/...",              # only for native binary, current turn only
    }
)
```

**The label is built from `ref`, never `filename`** (`file_conversion_service.py`). `original_name`
is metadata only — it is *never* used to address files. See §1.1 for why this matters.

### 3.4 FileManagementAgent

**Location:** `src/agents/file_management_agent.py`

| Intent | Mode | Description |
|--------|------|-------------|
| `open_file` | SYNC | Download + convert (text) or download + temp file (binary) |
| `delete_file` | SYNC | Remove file from GCS |

**Zero-LLM agent** — no LLM calls, direct port operations. Context schema: `file_ref` (filename
from `[File: name (size)]` label in conversation).

Binary files returned as `file_data` in `AgentResponse.metadata` — SmartAgent propagates to
MessagePart for LLM vision.

---

## 4. Configuration

| Env var | Purpose | Required |
|---------|---------|----------|
| `GCS_MEDIA_BUCKET` | GCS bucket name | Yes (conditional — file storage disabled when absent) |

**ServiceContainer wiring:** `GcsFileStorageAdapter` and `FileConversionService` are created
only when `GCS_MEDIA_BUCKET` is configured. FileManagementAgent is registered only when both
services exist.

---

## 5. LLM Adapter Compatibility

All 4 adapters (Claude, Gemini, Grok, OpenAI) handle `file_data` with `"ref"` key gracefully:
debug log emitted, no error, no content appended. The text label `[File: ...]` in the same
message provides context to the LLM.

---

## 6. Router Vision Detection

Vision override (force complexity >= 7) uses refined logic:

- `file_data` with `"ref"` key AND `image/*` mime_type -> vision detected
- `file_data` with `"ref"` key AND non-image mime_type -> NOT vision (just a file label)
- `file_data` without `"ref"` (legacy path/base64) -> vision detected (backward compat)

---

## 7. Known Limitations (Phase 1)

1. **Generated documents not fetchable.** PdfGenerator/HtmlPageGenerator store files via
   `MediaStoragePort` under `docs/{uuid}-{filename}`, not under `{user_id}/files/`.
   `open_file` on a generated document returns 404.

2. **No input validation for generators.** Document generators will create content from general
   knowledge if the delegated query lacks source material.

---

## 8. Evolution Path (Phase 2)

- LLM-powered file management: "find my PDFs from last month", "list all documents"
- File metadata indexing (name, type, date, size) in Firestore
- `list_files`, `search_files` intents
- Per-user storage quota tracking

---

## History

Added: 2026-04-01
RFC: `docs/10_rfcs/FILE_STORAGE_RFC.md`
2026-05-30: Label now built from the unique GCS `ref` instead of the original filename
(`file_conversion_service.py`). Fixes "bot describes a different/old photo" when Slack-pasted
images all arrive named `image.png` and an exact-key download landed on a stale squatting object.
See §1.1 (Design Intent + critical invariant).
