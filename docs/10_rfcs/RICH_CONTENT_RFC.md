# RFC: Rich Content — Images, Files, and Document Memory

**Status:** Draft
**Author:** Solo dev
**Created:** 2026-02-25

---

## 1. Problem Statement

SmartAgent responses are text-only. The system cannot deliver:
- Visual content (weather cards, maps)
- Downloadable structured output (reports, summaries, decisions)

The `rich_content` field already exists in the SmartAgent JSON output and is parsed by
`llm_response_parser.py`, but `OUTPUT_FORMAT_JSON.groovy` has no trigger conditions, and the
Slack adapter's `send_rich_content` only handles `type="table"`. Everything else falls back to text.

**Important:** The existing `type="table"` handler in `response_channel.py` contains
non-trivial Block Kit rendering logic for structured data tables. It must not be removed —
only extended with new type handlers alongside it.

---

## 2. Use Cases

| # | User request | Expected output |
|---|---|---|
| U1 | "What's the weather in Kyiv?" | Text response + weather image (wttr.in) |
| U2 | "Give me a map of how to get to X" | Text response + static map image |
| U3 | "Summarize our discussion and give me a file" | Text confirmation + downloadable .md file |
| U4 | "Create a plan for X and save it" | Downloadable file (GCS, 30–60 day TTL) |
| U5 | "What did we decide about the bike last month?" | *Future: RAG search in file memory* |

U1 is M1. U3–U4 are M2. U2 is M3. U5 is a future branch (M4+), not in this RFC.

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
  └─ "file"          → bytes → GcsMediaStorage.store(ttl_days=30) → PlatformMediaPort.upload_file()

PlatformMediaPort (new port)
  ├─ SlackMediaAdapter  → files_upload_v2
  └─ TelegramMediaAdapter → sendPhoto / sendDocument (deferred)
```

**Nothing new in agents.** SmartAgent only needs updated prompt instructions (token).
All generation and delivery logic stays in Application/Adapter layers.

---

## 4. Content Types

### 4.1 `weather_image`
```json
{"type": "weather_image", "data": {"location": "city name or address"}, "fallback": "Weather for X"}
```
- Source: `GET https://wttr.in/{location}_2.png` (3-day forecast PNG, no API key needed)
- Storage: **ephemeral** — fetch bytes → upload to platform → discard
- Trigger: any weather/forecast/temperature/precipitation query

### 4.2 `map_image`
```json
{"type": "map_image", "data": {"address": "full address or place name", "zoom": 14}, "fallback": "Map of X"}
```
- Source: Google Maps Static API (`GOOGLE_MAPS_API_KEY`) → PNG
- Storage: **ephemeral** — fetch bytes → upload to platform → discard
- Trigger: location queries, "show on map", "where is X"
- Note: Full routing/places via MapsAgent is a separate future milestone

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
- Storage: **GCS bucket with TTL 30–60 days** (see §5)
- Trigger: explicit user request ("give me a file", "save this", "I want a document", "export")

---

## 5. File Storage Decision

### Decision: GCS bucket with TTL, no Firestore

Files are uploaded to a GCS bucket with a 30–60 day object lifecycle rule.
The bot sends the download URL to Slack via `files_upload_v2`.
No Firestore collection, no vector embedding, no RAG at this stage.

**Why not Firestore + vectors yet:**
- The "find my old files" use case is real but secondary to "get a file now"
- Building document memory (Firestore + `FileSearchAgent`) risks polluting memory
  with generated artifacts that may not deserve long-term retention
- GCS TTL gives a natural retention window without manual cleanup

**Future branch (not in this RFC): Document Memory**
When the use case "search in my files" is confirmed as frequent, promote to:
- Firestore `{env}_user_files` collection with `content_vector` (768-dim)
- New port: `FileRepository` with `add_file()`, `search_files()`
- New specialist: `FileSearchAgent` (registered in AgentRegistry, explicit-only trigger)
- Migration: backfill GCS objects into Firestore

### Images: Ephemeral by Design

Weather and map images have no value beyond the moment they are sent.
NOT stored anywhere. Bytes are fetched → uploaded → discarded.

---

## 6. Prompt Token Changes

`OUTPUT_FORMAT_JSON.groovy` needs explicit trigger conditions added to `rich_content_rules`.
The current instruction ("If response contains serializable data") is too vague.

New rules define: **when** to use each type, **what fields** are required, and **one example** per type.

`rich_content` is already an array — multiple items allowed per response (e.g., text + weather image).

---

## 7. Implementation Milestones

### M1 — Foundation + Weather Image

**Goal:** End-to-end rich content pipeline for the simplest case (weather image).

Changes:
1. `OUTPUT_FORMAT_JSON.groovy` — add trigger conditions + examples for `weather_image`
2. New port: `PlatformMediaPort` (`upload_image(bytes, alt_text, channel_id)`)
3. New adapter: `SlackMediaAdapter` implementing `PlatformMediaPort` via `files_upload_v2`
4. New service: `RichContentService` with type dispatcher + `WttrFetcher`
5. `ConversationHandler` — call `RichContentService` for each `rich_content` item after text delivery
6. Preserve existing `type="table"` handler in `response_channel.py` — add new types alongside it

Deliverable: "What's the weather in Madrid?" → text + 3-day forecast PNG in Slack.

---

### M2 — File Generation (GCS)

**Goal:** LLM generates downloadable .md files, stored in GCS with TTL.

Changes:
1. New port: `MediaStoragePort` (`store_file(bytes, filename, ttl_days) → url`)
2. New adapter: `GcsMediaAdapter` (GCS bucket upload with object lifecycle TTL)
3. `RichContentService` — `file` handler: encode content → GCS → get URL → upload to Slack
4. `OUTPUT_FORMAT_JSON.groovy` — trigger conditions for `file` type
5. `PlatformMediaPort.upload_file(bytes, filename, channel_id)`

Deliverable: "Summarize and give me a file" → .md download link in Slack, available for 30 days.

---

### M3 — Map Images

**Goal:** Static map images on location queries.

Changes:
1. `MapsStaticFetcher` in `RichContentService`
2. `OUTPUT_FORMAT_JSON.groovy` — trigger conditions for `map_image`
3. `GOOGLE_MAPS_API_KEY` added to `.env`, GCP secrets, and `cloudbuild-prod.yaml`

Deliverable: "Show me where X is" → text + map PNG in Slack.

---

### M4 — Document Memory (future branch, concept only)

Promotes GCS files to searchable memory tier:
- Firestore `{env}_user_files` collection + vector embedding
- `FileRepository` port + `FirestoreFileRepository` adapter
- `FileSearchAgent` registered in AgentRegistry (explicit-only trigger)

Separate design session required. Not blocking M1–M3.

---

### M5 — Maps Agent (concept only)

Full Google Maps access via `delegate_to_specialist`:
- Geocoding, Places Search, Directions API
- New `MapsAgent` in AgentRegistry
- `GOOGLE_MAPS_API_KEY` already provisioned from M3

Separate design session required.

---

## 8. What Is Out of Scope

- PDF generation
- Image generation (Imagen/DALL-E) — no concrete trigger identified
- Telegram adapter for media (deferred — same port, different adapter)
- File editing or versioning
- HTML file format
- Chunked RAG on file content

---

## 9. Resolved Decisions

1. **FileSearchAgent trigger:** Explicit only — never proactive. Files are intentional artifacts.
2. **Slack file upload API:** `files_upload_v2` (new API, not deprecated `files_upload`).
3. **Weather image accompanies text:** Both delivered — fallback text always present for accessibility.
4. **Existing table handler:** Preserved as-is. New type handlers are additive, not replacing.
