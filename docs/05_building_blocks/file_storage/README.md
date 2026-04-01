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
MessagePart(
    text='[File: "report.docx" (45KB)]',
    file_data={
        "ref": "report.docx",        # deduplicated filename
        "mime_type": "application/...",
        "size_bytes": 45000,
        "original_name": "report.docx",  # only if sanitization changed it
        "path": "/tmp/...",              # only for native binary, current turn only
    }
)
```

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
