# RFC: Rich Content — Images, Files, and Document Memory

**Status:** Draft
**Author:** Solo dev
**Created:** 2026-02-25

---

## 1. Problem Statement

SmartAgent responses are text-only. The system cannot deliver:
- Visual content (weather cards, maps)
- Downloadable structured output (reports, summaries, decisions)
- Long-form documents that should be persistently accessible and searchable

The `rich_content` field already exists in the SmartAgent JSON output and is parsed by
`llm_response_parser.py`, but `OUTPUT_FORMAT_JSON.groovy` has no trigger conditions, and the
Slack adapter only handles `type="table"`. Everything else silently falls back to text.

---

## 2. Use Cases

| # | User request | Expected output |
|---|---|---|
| U1 | "What's the weather in Kyiv?" | Text response + weather image (wttr.in) |
| U2 | "Give me a map of how to get to X" | Text response + static map image |
| U3 | "Summarize our discussion and give me a file" | Text confirmation + downloadable .md file |
| U4 | "What did we decide about the bike last month?" | RAG search in user files → relevant excerpt |
| U5 | "Show directions from A to B" | Text + map image + optionally route details |
| U6 | "Create a plan for X and save it" | Downloadable file + stored in file memory |

U1–U3 are Milestone 1–2. U4 (RAG on files) is Milestone 3. U5–U6 extend those milestones.

---

## 3. Architecture Overview

Rich content follows the same hexagonal pattern as the rest of the system.
**Agents declare what to generate; infrastructure handles how.**

```
SmartAgent JSON output
  rich_content: [{type: "weather_image", data: {location: "Kyiv"}, fallback: "Weather for Kyiv"}]
  (array — multiple items per response allowed)

ConversationHandler
  └─ for each item in rich_content:
       RichContentService.process(item, channel_id)

RichContentService (Application layer)
  ├─ "weather_image"  → WttrFetcher → bytes → PlatformMediaPort.upload_image()
  ├─ "map_image"      → MapsStaticFetcher → bytes → PlatformMediaPort.upload_image()
  └─ "file"          → FileGenerator → FileRepository.store() → PlatformMediaPort.upload_file()

PlatformMediaPort (new port)
  ├─ SlackMediaAdapter  → files_upload_v2 / chat_postMessage with image block
  └─ TelegramMediaAdapter → sendPhoto / sendDocument
```

**Nothing new in agents.** SmartAgent only needs updated prompt instructions (token).
All generation and delivery logic stays in Application/Adapter layers.

---

## 4. Content Types

### 4.1 `weather_image`
```json
{"type": "weather_image", "data": {"location": "city name or address"}, "fallback": "Weather for X"}
```
- Source: `GET https://wttr.in/{location}_2.png` (3-day forecast PNG, no API key)
- Storage: ephemeral — send to platform, discard bytes
- Trigger: any weather/forecast/temperature/precipitation query

### 4.2 `map_image`
```json
{"type": "map_image", "data": {"address": "full address or place name", "zoom": 14}, "fallback": "Map of X"}
```
- Source: Google Maps Static API (`GOOGLE_MAPS_API_KEY`) → PNG
- Storage: ephemeral — send to platform, discard bytes
- Trigger: location queries, "show on map", "where is X"
- Note: Full routing/places via MapsAgent (separate milestone, not in this RFC)

### 4.3 `file`
```json
{
  "type": "file",
  "data": {
    "filename": "meeting-summary-2026-02-25.md",
    "title": "Meeting Summary: Architecture Discussion",
    "content": "# Meeting Summary\n\n## Decisions\n...",
    "tags": ["summary", "architecture", "decisions"]
  },
  "fallback": "File: meeting-summary-2026-02-25.md"
}
```
- Format: Markdown (`.md`) — renders in Slack preview, universally readable
- Storage: Firestore `user_files` collection + vector embedding (see §5)
- Trigger: explicit user request ("give me a file", "save this", "I want a document", "export")

---

## 5. File Storage Decision

### The Question
Is storing files in Firestore with vector search (RAG-capable) over-engineering?

### Analysis

Files are structurally different from biographical facts:
- Facts are **atomic** (one claim, one entity)
- Files are **composite** (multiple claims, a document with structure)

Two possible approaches:

**Option A — Ephemeral:** Generate → upload to Slack → forget.
Simple, but files are lost after Slack retention expires. No "find what I decided last month."

**Option B — Document Memory:** Generate → store in Firestore → upload to Slack.
Creates a third memory tier alongside session history and biographical facts.

### Decision: Option B

Not over-engineering because:
1. The vector infrastructure already exists — adding a new collection reuses it fully
2. The use case is real for an exocortex: "find the plan I wrote in January"
3. Files are intentional, named artifacts — unlike facts (auto-extracted), files are created on explicit request, making them high-signal
4. Incremental: doesn't change the fact system at all

**What this is NOT:**
- Not RAG on uploaded files (no ingestion pipeline)
- Not document editing or versioning
- Not chunked search (whole-file vector only — files should be concise)

### `user_files` Collection Schema

```python
UserFileEntity:
    file_id: str           # UUID
    account_id: str        # multi-tenant key
    created_by_user_id: str
    title: str             # human-readable name
    filename: str          # e.g., "summary-2026-02-25.md"
    content: str           # full markdown text
    content_vector: List[float]  # 768-dim embedding of content
    tags: List[str]        # from rich_content data
    created_at: datetime
    source_context: str    # brief description of what generated this file
```

**New port:** `FileRepository(ABC)` with `add_file()`, `search_files(query_vector, limit)`.
**New memory specialist:** `FileSearchAgent` (similar to MemorySearchAgent, registered in AgentRegistry).

### Images: Ephemeral by Design

Weather and map images have no value beyond the moment they are sent.
They are NOT stored. Bytes are fetched → uploaded → discarded.

---

## 6. Prompt Token Changes

`OUTPUT_FORMAT_JSON.groovy` needs explicit trigger conditions added to `rich_content_rules`.
The current instruction ("If response contains serializable data") is too vague.

New rules define: **when** to use each type, **what fields** are required, and **one example** per type.

`rich_content` remains an array — multiple items allowed (e.g., text + file + image in one response).

---

## 7. Implementation Milestones

### M1 — Foundation + Weather (current scope)

**Goal:** End-to-end rich content pipeline working for the simplest case.

Changes:
1. `OUTPUT_FORMAT_JSON.groovy` — add trigger conditions + examples for `weather_image` and `file`
2. New port: `PlatformMediaPort` (`upload_image`, `upload_file`)
3. New adapter: `SlackMediaAdapter` (implements port via `files_upload_v2`)
4. New service: `RichContentService` with dispatcher + `WttrFetcher`
5. `ConversationHandler` — hook to call `RichContentService` after text delivery
6. `RichContent` domain type updated if needed

Deliverable: "What's the weather in Madrid?" → text + forecast image in Slack.

---

### M2 — File Generation + Document Memory

**Goal:** LLM can generate and store named documents. Files are searchable.

Changes:
1. `FileRepository` port + `FirestoreFileRepository` adapter
2. `FileGenerator` in `RichContentService` (embed content → store → upload)
3. `FileSearchAgent` registered in `AgentRegistry` (SmartAgent can delegate to it)
4. `OUTPUT_FORMAT_JSON.groovy` — trigger conditions for `file` type
5. `SlackMediaAdapter.upload_file()` — upload .md file with download button

Deliverable: "Summarize our discussion and give me a file" → .md download in Slack + stored in memory.
"Find the architecture decisions document" → FileSearchAgent returns the file.

---

### M3 — Map Images

**Goal:** Static map images delivered on location queries.

Changes:
1. `MapsStaticFetcher` in `RichContentService`
2. `OUTPUT_FORMAT_JSON.groovy` — trigger conditions for `map_image`
3. Requires `GOOGLE_MAPS_API_KEY` in secrets + Cloud Run env

Deliverable: "Show me where X is" → text + map PNG.

---

### M4 — Maps Agent (concept only, not detailed here)

Full Google Maps access via `delegate_to_specialist`:
- Geocoding, Places Search, Directions API
- New `MapsAgent` registered in `AgentRegistry`
- `GOOGLE_MAPS_API_KEY` already provisioned from M3
- Returns structured data + optionally triggers `map_image` via rich_content

Out of scope for this RFC — separate design needed.

---

## 8. What Is Out of Scope

- PDF generation (no identified use case)
- Image generation (Imagen/DALL-E) — no concrete trigger identified yet
- Telegram adapter for media (follows same port, deferred)
- File editing or versioning
- Chunked RAG on file content (whole-file vector sufficient for the scale)
- HTML file format (no benefit in messenger context)

---

## 9. Open Questions

1. Should `FileSearchAgent` be proactively queried (like MemorySearchAgent in every request), or only on explicit "find my file" intent? Recommendation: explicit only — files are intentional, not casual memory.

2. Slack `files_upload_v2` (new API) vs `files_upload` (deprecated but simpler). Use v2 from the start.

3. Should weather image replace the text weather response, or accompany it? Recommendation: accompany — fallback text must always be present for accessibility and Telegram.
