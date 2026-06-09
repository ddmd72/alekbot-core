# RFC: Rich Content — Files and Media Delivery

**Status:** Implemented (V1)
**Author:** Solo dev
**Created:** 2026-02-25
**Updated:** 2026-02-26

---

## 1. Problem Statement

SmartAgent responses were text-only. The system could not deliver:
- Visual content (weather cards, maps)
- Downloadable structured output (reports, summaries, tables, documents)

The `rich_content` field already existed in the SmartAgent JSON output and was parsed by
`llm_response_parser.py`, but `OUTPUT_FORMAT_JSON.groovy` had no trigger conditions, and the
Slack adapter's `send_rich_content` only handled `type="table"`. Everything else fell back to text.

**Important:** The existing `type="table"` handler in `response_channel.py` contains
non-trivial Block Kit rendering logic for structured data tables. It must not be removed —
only extended with new type handlers alongside it.

---

## 2. Use Cases

| # | User request | Expected output |
|---|---|---|
| U1 | "Give me a summary as a file" | Text confirmation + downloadable .md file |
| U2 | "Create a plan and export as Word" | Downloadable .docx file |
| U3 | "Give me this data as Excel" | Downloadable .xlsx spreadsheet |
| U4 | "Create an HTML report" | Downloadable .html file |
| U5 | "What's the weather in Kyiv?" | Text response (no image — weather_image removed, see §6) |
| U6 | "What did we decide about X?" | *Future: RAG search in file memory (M4+)* |

U1–U4 delivered in V1. U5 text-only. U6 future branch.

---

## 3. Architecture (V1)

Rich content follows the hexagonal pattern.
**Agents declare what to generate; infrastructure handles how.**

```
SmartAgent JSON output
  rich_content: {"type": "file", "data": {...}, "fallback": "File: name.ext"}
  (single object or null — one item per response)

ConversationHandler
  └─ _deliver_rich_content(content, response_channel, thread_id)
       ├─ content_type in _MEDIA_CONTENT_TYPES ("file", "map_image") AND rich_content_service present
       │     → RichContentService.process(content, channel_id)
       └─ otherwise (type="table", no service, Telegram)
             → response_channel.send_rich_content()  [Block Kit / fallback text]

RichContentService (Application layer)
  └─ "file" → detect extension → convert if needed → PlatformMediaPort.upload_file()
       ├─ .md / .html / .txt  → encode UTF-8 → bytes
       ├─ .xlsx               → CSV string → openpyxl → bytes
       └─ .docx               → Markdown string → python-docx → bytes

PlatformMediaPort (port)
  ├─ SlackMediaAdapter  → files_upload_v2
  └─ TelegramMediaAdapter → sendDocument (deferred — see §7)
```

**Nothing new in agents.** SmartAgent only needs updated prompt instructions (token).
All conversion and delivery logic stays in Application/Adapter layers.

---

## 4. Content Types (V1)

### 4.1 `file`

```json
{
  "type": "file",
  "data": {
    "filename": "meeting-summary-2026-02-26.md",
    "title": "Meeting Summary: Architecture Discussion",
    "content": "# Meeting Summary\n\n## Decisions\n..."
  },
  "fallback": "File: meeting-summary-2026-02-26.md"
}
```

**Trigger:** User explicitly asks for a file, document, export, spreadsheet, or to "save" something.
Never proactive — only on explicit request.

**Supported formats:**

| Extension | LLM generates in `content` | Server converts via |
|---|---|---|
| `.md` | Markdown string | UTF-8 encode (no conversion) |
| `.html` | Full HTML document (`<!DOCTYPE html>...`) | UTF-8 encode (no conversion) |
| `.xlsx` | CSV string (first row = headers) | `openpyxl` CSV→xlsx |
| `.docx` | Markdown string | `python-docx` Markdown→docx |

**Error handling:** If conversion fails → fallback to `.txt` (raw content, UTF-8 encoded).
No crash, logged as ERROR.

**Storage:** Direct Slack upload via `files_upload_v2`. No GCS in V1 — see §5.

### 4.2 `table` (existing, unchanged)

Handled by `response_channel.py` Block Kit renderer. Not routed through `RichContentService`.
Renders structured comparative data as an in-chat Slack block.

### 4.3 `weather_image` (removed in V1)

Initially implemented in M1 (wttr.in PNG via `files_upload_v2`). Removed after testing:
- ASCII-art PNG output is poor quality ("it just didn't work out")
- No actionable improvement path within Slack's file upload model

Weather queries now return text-only responses.

---

## 5. File Storage Decision (V1)

### Decision: Direct Slack Upload, No GCS

Files are encoded to bytes and uploaded directly to Slack via `files_upload_v2`.
No GCS bucket, no TTL, no URL generation in V1.

**Why direct upload for V1:**
- Eliminates GCS infrastructure complexity (MediaStoragePort, GcsMediaAdapter, lifecycle policy)
- File lifecycle managed by Slack (permanent in workspace)
- No PII exposure risk (no public URL, no web crawler access — critical for files with biographical data)
- Satisfies the "get a file now" use case completely

**GCS option re-evaluated and deferred:**
The original RFC planned GCS with 30-day TTL and a public URL for link preview.
This was rejected because:
1. Slack/Telegram unfurl fetches the URL → content goes to their infrastructure (PII leak)
2. Signed URLs are capability-based — anyone with the link can read the file
3. Web crawlers can index files if the URL appears anywhere public

GCS remains the right approach only for non-PII, explicitly public content (e.g., future map images).

**Future branch (M4): Document Memory**
When "search in my files" is confirmed as a frequent use case, promote to:
- Firestore `{env}_user_files` collection with `content_vector` (768-dim)
- New port: `FileRepository` with `add_file()`, `search_files()`
- New specialist: `FileSearchAgent` (explicit-only trigger)

---

## 6. Prompt Token Changes

`OUTPUT_FORMAT_JSON.groovy` updated in V1:
- Removed `weather_image` trigger entirely
- Expanded `file` trigger with format-specific instructions for md / html / xlsx / docx
- Added server-side conversion note: LLM generates CSV for xlsx, Markdown for docx — server converts

---

## 7. Implementation Milestones

### M1 — Weather Image (delivered, then removed)

Implemented: `PlatformMediaPort`, `SlackMediaAdapter`, `RichContentService` with WttrFetcher.
Weather PNG fetched from wttr.in and uploaded to Slack via `files_upload_v2`.

**Removed in V1:** ASCII-art output quality was unacceptable. `weather_image` trigger removed from
`OUTPUT_FORMAT_JSON.groovy`. WttrFetcher removed from `RichContentService`.
The hexagonal infrastructure (port + adapter + service) was retained — used for file delivery.

### M2 — File Delivery (delivered as V1)

**Delivered scope:**
1. `OUTPUT_FORMAT_JSON.groovy` — trigger conditions + format instructions for md / html / xlsx / docx
2. `RichContentService._handle_file()` — extension-based dispatch + conversion
3. `_csv_to_xlsx()` — CSV string → xlsx bytes via `openpyxl`
4. `_markdown_to_docx()` + `_apply_inline()` — Markdown string → docx bytes via `python-docx`
5. `ConversationHandler._deliver_rich_content()` — routes media types to service vs Block Kit
6. `requirements.txt` — `openpyxl>=3.1.0`, `python-docx>=1.1.0`

**Not in V1 (deferred):** GCS storage, MediaStoragePort, public URL delivery.

### M3 — Map Images (planned)

Static map images on location queries.

Changes:
1. `MapsStaticFetcher` in `RichContentService` → `map_image` handler
2. `OUTPUT_FORMAT_JSON.groovy` — trigger conditions for `map_image`
3. `GOOGLE_MAPS_API_KEY` added to `.env`, GCP secrets, `cloudbuild-prod.yaml`
4. GCS storage appropriate here (non-PII public map tiles)

### M4 — Document Memory (concept)

Promotes files to searchable memory tier. Separate design session required.

### M5 — PDF (planned, deferred)

LLM generates HTML → server converts via `weasyprint` (HTML→PDF).
Blocked on: Dockerfile system dependencies (`libpango`, `libcairo`) for Cloud Run.

### M6 — Telegram Media (deferred)

`TelegramMediaAdapter(PlatformMediaPort)` implementing `sendDocument` / `sendPhoto`.
Port and wiring pattern already defined. Telegram `ConversationHandler` currently
uses `send_rich_content()` fallback → delivers `fallback_text` as plain text.

---

## 8. What Is Out of Scope (V1)

- PDF generation (M5, needs Dockerfile changes for weasyprint)
- GCS file storage (M4, deferred — PII safety rationale in §5)
- Image generation (Imagen/DALL-E) — no concrete trigger identified
- Telegram media adapter (M6, deferred)
- File editing or versioning
- Chunked RAG on file content (M4+)

---

## 9. Resolved Decisions

1. **FileSearchAgent trigger:** Explicit only — never proactive. Files are intentional artifacts.
2. **Slack file upload API:** `files_upload_v2` (new API, not deprecated `files_upload`).
3. **Direct upload vs GCS URL:** Direct upload wins for PII-containing files. GCS only for public content (maps).
4. **weather_image removed:** wttr.in ASCII PNG is poor quality. Text-only weather responses are sufficient.
5. **Conversion responsibility:** Server-side (RichContentService), not LLM. LLM generates text formats (CSV, Markdown, HTML); server converts to binary.
6. **Fallback on conversion error:** Rename to `.txt`, upload raw content. No crash, no silent data loss.
7. **Existing table handler:** Preserved as-is. New type handlers are additive via `_deliver_rich_content()` routing.
8. **`rich_content` is a single object:** Not an array. `OUTPUT_FORMAT_JSON.groovy` and parser aligned.
