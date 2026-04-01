# RFC: File Storage & Reference-Based File Handling

**Status:** IMPLEMENTED
**Date:** 2026-03-31
**Implemented:** 2026-04-01
**Owner:** AI Engineering
**Scope:** `conversation_handler.py`, `file_conversion.py`, new `FileStoragePort`, new `FileManagementAgent`, `agent_manifest.py`
**Goal:** Remove file content from conversation history and orchestrator context.
Files become lazy-loaded resources: stored in GCS, referenced by filename,
fetched on demand via uniform intent delegation.

---

## 1. Problem

### 1.1 Files inline in messages

When a user sends a file attachment, `ConversationHandler` converts it to text
(via `convert_file_to_text`) and embeds the full content in `MessagePart.text`.
This content — up to 30,000 characters (~7K tokens) per file — enters the
orchestrator's LLM context and persists in session history.

Current mitigation: `make_history_stub()` creates a 1,000-char stub in `text`,
full content in `full_text`. `_apply_history_tier()` swaps to stub after
`HISTORY_FULL_TURNS` (2 turns). But for 2 full turns the file content is present,
and the 1,000-char stub still accumulates across 30 messages.

### 1.2 Native binary files (images, PDFs)

These are passed as `file_data` with a local `path` key. For the current turn,
the LLM adapter encodes them to base64. The `path` entry is stripped before
history save, so binary content does NOT persist in Firestore.

### 1.3 Delegation-only files

When a user sends a file with "make a PDF from this" — the orchestrator doesn't
need the file content at all. It only needs to delegate to DocPlanner/PdfGenerator.
Currently, the full converted text (up to 30K chars) enters the orchestrator's
context regardless of whether the orchestrator will read it.

### 1.4 Summary

| Scenario | Current cost | Desired cost |
|---|---|---|
| File for delegation ("forward to DocPlanner") | ~7K tokens in orchestrator context | 0 tokens (reference only) |
| File for discussion ("let's discuss this") | ~7K tokens for 2 turns, 250 tok stub × 28 turns | 0 in history; content only in delegation loop |
| Image/PDF (native binary) | Current turn only | Same + fetchable on subsequent turns |

---

## 2. Design

### 2.1 Core principle

**File content is never stored in MessagePart or session history.**
All files go to GCS. Orchestrator sees a reference (filename + size).
Content is accessed on demand — either by the orchestrator via intent delegation,
or by specialists via infrastructure resolution.

### 2.2 File reference = filename

The orchestrator sees the **original filename** as the reference — nothing else:

```
report.docx
```

No UUIDs, no paths, no user IDs. The filename is semantically meaningful
to the LLM — it understands what `report.docx` is without parsing infrastructure paths.

**Duplicate name resolution** — macOS Finder convention:

```
report.docx          ← first upload
report (1).docx      ← second upload with same name
report (2).docx      ← third
```

`GcsFileStorageAdapter._deduplicate()` checks existing files for the user
and appends `(N)` if the name is taken.

**Storage key** (invisible to agents and orchestrators):

```
{user_id}/files/report.docx
{user_id}/files/report (1).docx
```

`FileStoragePort` adapter assembles the full key via `_key(filename, user_id)`.
Agents never see `gs://bucket/...` or any path beyond the filename.

**Filename sanitization:**

GCS prohibits `#`, `?`, `[`, `]`, `*`, `\n`, `\r`, `\t` in object keys.
Cyrillic and other UTF-8 are allowed.

`GcsFileStorageAdapter.sanitize_filename()` replaces prohibited chars → `_`.
Applied once at upload time. `file_data.original_name` preserves the original
when sanitization changed the name.

**Label format:**

```
[File: "report.docx" (24KB)]
```

Filename in quotes — prevents LLM from including size in `file_ref` context param.

### 2.3 Mime type inference

Orchestrator never passes mime type — it doesn't know and shouldn't care.
`FileManagementAgent` and `FileConversionService.resolve_content()` infer mime
type from the file extension via `mimetypes.guess_type()`. This determines
the conversion strategy:

- `image/*` → native binary (return as `file_data` for LLM vision)
- `application/pdf` → native binary (same)
- `text/*` → read as UTF-8
- `audio/*` → transcribe via AudioTranscriptionPort
- everything else → markitdown conversion

No mime type in `context_schemas` — agent resolves it internally.

### 2.4 File flow — upload

```
User sends file via Slack/Telegram
    |
    v
Platform adapter downloads to local temp path (unchanged)
    |
    v
ConversationHandler → FileConversionService.process_attachment():
    1. Upload file bytes to GCS (dedup handled by adapter)
    2. Return reference-only MessagePart:
       MessagePart(
           text='[File: "report.docx" (24KB)]',
           file_data={
               "ref": "report.docx",
               "mime_type": "application/vnd...",
               "size_bytes": 24576,
           }
       )
    3. For native binary (image/*, PDF) — ALSO keep local temp path
       in file_data for current turn's LLM call:
       file_data["path"] = "/tmp/photo.jpg"   # current turn only
    4. Delete local temp in finally block (unchanged)
```

### 2.5 File flow — history save

```
ConversationHandler (history save):
    if file_data has "ref" AND "path":
        strip "path", keep reference only
    elif file_data has "path" only (legacy):
        skip entirely
```

Result: history contains only the text label (~40 chars) and a reference dict
(~100 chars). No file content. No base64. No stubs.

### 2.6 File flow — orchestrator wants to read a file

Orchestrator sees `[File: "report.docx" (24KB)]` in user message. User says
"let's discuss this file". Orchestrator delegates via uniform protocol:

```
delegate_to_specialist(
    intent="open_file",
    query="Retrieve the contents of report.docx",
    context={"file_ref": "report.docx"}
)
```

`FileManagementAgent` receives the delegation and infers mime type from extension:

**Text files** (docx, txt, csv, md, xlsx):
1. Download from GCS → temp file → `convert_file_to_text()` → return full text
2. No truncation limit — file content lives only in delegation loop

**Native binary** (images, PDFs):
1. Download from GCS → temp file
2. Return `AgentResponse` with `metadata={"file_data": {"path": tmp, "mime_type": ...}}`
3. SmartAgent reads `file_data` from `ToolResponse` and attaches as extra
   `MessagePart(file_data=...)` in delegation loop history
4. LLM adapter encodes it natively (base64/File API) → LLM sees the image/PDF

Tool responses are **not saved** to session history (existing behavior).
File content dies with the delegation loop.

### 2.7 File flow — forward to specialist

User: "Make a PDF from this file". Orchestrator delegates directly:

```
delegate_to_specialist(
    intent="create_pdf",
    query="Create a professional PDF from the attached document",
    context={"file_ref": "report.docx"}
)
```

Document generators (`create_pdf`, `create_document`, `create_html_page`) accept
`file_ref` in `context_schemas`. The orchestrator passes it directly — no need
to fetch the file first.

**Infrastructure resolution:** `AgentCoordinator._resolve_file_refs()` runs before
dispatching to any specialist — both SYNC and ASYNC paths. If `file_ref` is in
params, it calls `FileConversionService.resolve_content(ref, user_id)` and injects
`file_content` into the specialist's payload. The specialist receives text, never
knows about GCS.

**Specialist consumption of `file_content`:**
- `DocPlannerAgent` — existing loop at lines 102-105 appends all extra string
  fields from payload to query. `file_content` is picked up automatically.
- `PdfGeneratorAgent` — explicit `payload.get("file_content")` injection into `raw_query`.
- `HtmlPageGeneratorAgent` — same explicit injection.

Orchestrator never sees file content — zero tokens for a pure forwarding operation.

### 2.8 File flow — native binary on current turn

For images and PDFs, the LLM sees the binary content natively on the **current turn**:
1. `file_data` contains both `ref` (for persistence) and `path` (local temp for adapter)
2. All LLM tiers support native binary input — Gemini ECO (Flash Lite) handles
   images/PDFs natively, same as Claude and OpenAI
3. LLM adapter encodes from `path` as today (base64 for Claude, File API for Gemini)
4. After the turn, `path` is stripped; only `ref` persists in history
5. On subsequent turns, LLM sees only `[File: "photo.jpg" (1.2MB)]` — not the image
6. If orchestrator needs to re-see the file: delegates to `open_file` →
   agent returns binary as `file_data` in metadata → SmartAgent attaches as
   `MessagePart` → LLM sees it natively again

---

## 3. Components

### 3.1 `FileStoragePort` — `src/ports/file_storage_port.py`

```python
class FileStoragePort(ABC):
    async def upload(self, data: bytes, filename: str, user_id: str, content_type: str) -> str:
        """Upload. Returns deduplicated filename."""
    async def download(self, filename: str, user_id: str) -> bytes:
    async def delete(self, filename: str, user_id: str) -> None:
    async def exists(self, filename: str, user_id: str) -> bool:
    async def get_url(self, filename: str, user_id: str) -> str:
```

All methods take `(filename, user_id)` separately. Adapter assembles the full key internally.

### 3.2 `GcsFileStorageAdapter` — `src/adapters/gcs_file_storage_adapter.py`

- `_key(filename, user_id)` → `{user_id}/files/{filename}`
- `_deduplicate()` — Finder-style: `report.docx` → `report (1).docx`
- `sanitize_filename()` — replaces GCS-prohibited chars
- Lazy GCS client initialization
- All sync GCS ops wrapped in `run_in_executor`

### 3.3 `FileConversionService` — `src/services/file_conversion_service.py`

- `process_attachment(local_path, filename, mime_type, user_id)` → reference-only `MessagePart`
- `resolve_content(ref, user_id)` → download + infer mime from extension + convert to text.
  No mime_type parameter — always inferred internally via `mimetypes.guess_type()`.
- `resolve_bytes(ref, user_id)` → raw bytes for specialists needing originals
- No truncation limit on `resolve_content` — content is ephemeral (delegation loop only)

### 3.4 `FileManagementAgent` — `src/agents/file_management_agent.py`

Zero-LLM agent. Two intents:

- `open_file` — infer mime from extension; text files → `resolve_content()`;
  native binary (image/PDF) → `_fetch_binary()`: download to temp, return `file_data`
  in `AgentResponse.metadata` so SmartAgent attaches it as `MessagePart` for LLM vision.
  Result text for binary: `"File '{ref}' ({mime_type}) is attached. You can see and analyse it directly."`
- `delete_file` — remove from GCS

Reads params from `message.payload` directly (not `payload["context"]`) —
coordinator spreads `context_schemas` params into payload via `**extra_payload`.

Error messages are semantic — tell the orchestrator what happened, why, and
what to suggest to the user (e.g. "file not found, may have expired, ask user
to re-upload").

### 3.5 Agent Manifest — `src/infrastructure/agent_manifest.py`

```python
Intent.OPEN_FILE = "open_file"
Intent.DELETE_FILE = "delete_file"
```

`FILE_MANAGEMENT` descriptor: `internal=False`, both intents `SYNC`.

`context_schemas` for `open_file`: `file_ref` only. No `mime_type`.

Document generators (`CREATE_DOCUMENT`, `CREATE_PDF`, `CREATE_HTML_PAGE`) also
accept `file_ref` in `context_schemas` — orchestrator can forward files directly
without fetching content first. All three describe async delivery in
`capability_descriptions`.

### 3.6 Infrastructure resolution — `AgentCoordinator`

`_resolve_file_refs(params, user_id)` — async method called in both `_execute_sync`
and `_execute_async` before message creation / enqueue. If `file_ref` in params and
`file_ref_resolver` callback is configured, resolves content and injects `file_content`
into params.

`file_ref_resolver` is injected in `main.py` as
`container.file_conversion_service.resolve_content` after `ServiceContainer` is created.

### 3.7 SmartAgent tool response with file_data

`ToolResponse` dataclass has `file_data: Optional[Dict]` field. When
`AgentResponse.metadata` contains `file_data`, SmartAgent propagates it to
`ToolResponse`. When building delegation loop history, if `tool_response.file_data`
is set, an extra `MessagePart(file_data=...)` is appended alongside the
tool_response part. LLM adapter encodes it natively — orchestrator sees the image/PDF.

---

## 4. Wiring

- `ServiceContainer`: creates `GcsFileStorageAdapter` + `FileConversionService`
  from `GCS_MEDIA_BUCKET` env var. Passes both to `UserAgentFactory` via `agent_services()`.
- `UserAgentFactory`: creates `FileManagementAgent` per user if services available.
  Optional — if `file_conversion_service` is None, agent is not created.
- `ConversationHandler`: receives `file_conversion_service` via constructor.
  If set, uses new GCS path; otherwise falls back to legacy inline conversion.
- `SlackAdapterFactory` / `TelegramAdapterFactory`: pass `file_conversion_service`
  through to `ConversationHandler`.
- `AgentCoordinator`: receives `file_ref_resolver` callback. Injected in `main.py`
  after container creation.

---

## 5. LLM adapter changes

All four adapters (Claude, Gemini, OpenAI, Grok) handle `file_data` with three patterns:

| Key present | Behavior |
|---|---|
| `"base64"` | Use stored base64 from history (legacy) |
| `"path"` | Read local file, encode for provider (current turn) |
| `"ref"` only | Skip — no content to display, text label already in message |

### Router vision detection

After RFC, `file_data` is present on all files (not just native binary).
Router checks `file_data.mime_type.startswith("image/")` to detect vision,
not just `file_data` presence.

---

## 6. GCS bucket configuration

- Bucket: `alek-media-{env}` (existing, shared with deep research / documents)
- Prefix: `{user_id}/files/`
- Lifecycle rule: delete objects matching `*/files/*` older than 90 days
- Key format: `{user_id}/files/{filename}` (may include `(N)` dedup suffix)

**Bucket structure convention:**

```
alek-media-{env}/
  {user_id}/
    files/           ← user file attachments (this RFC)
    deep_research/   ← research reports (existing, to be migrated)
    documents/       ← generated PDFs/HTML (existing, to be migrated)
```

---

## 7. Evolution path

### Phase 1 (this RFC) — IMPLEMENTED
- Zero-LLM `FileManagementAgent`: `open_file`, `delete_file`
- Upload in `ConversationHandler` via `FileConversionService`
- Infrastructure resolution for specialists (SYNC + ASYNC paths)
- Native binary fetch returns `file_data` for LLM vision
- Document generators accept `file_ref` and receive `file_content` automatically

### Phase 2 (future)
- LLM-powered file management: "find my PDFs from last month", "list all documents"
- File metadata indexing (name, type, date, size) in Firestore
- `list_files`, `search_files` intents
- Per-user storage quota tracking

---

## 8. Known limitations / tech debt

- **Generated documents not fetchable.** Files created by PdfGenerator/HtmlPageGenerator
  are stored via `MediaStoragePort` under `docs/{uuid}-{filename}`, not under
  `{user_id}/files/`. If orchestrator tries to `open_file` on a generated
  document name, it gets 404. Fix: either store generated docs in `FileStoragePort`
  too, or teach orchestrator that generated documents are delivered directly and
  don't need fetching.

- **Input validation for generators.** Document generators (PDF, DOCX, HTML) will
  create content from general knowledge if the delegated query lacks specific source
  material. No mechanism to detect "insufficient input" and fail early. Requires
  either a system alert in the user message or a validation step in the agent.

---

## 9. Out of scope

- Copy-paste content compression (separate concern; may be addressed later)
- File search/listing (Phase 2)
- File sharing between users (not needed for solo dev)
- Encryption at rest (GCS default encryption is sufficient)
