# RFC: MapsSearchAgent — Location-Aware Queries via Google Maps Grounding

**Status:** IMPLEMENTED
**Date:** 2026-03-03
**Implemented:** 2026-03-05
**Owner:** AI Engineering
**Milestone:** Specialist Agents — Phase 2

**Related:** RICH_CONTENT_RFC.md (rich_content pattern, `html_file` type)

---

## 1. Problem Statement

Location-based queries ("find a pharmacy nearby", "where to eat Japanese food downtown") are
answered from general knowledge — no real place data, no hours, no ratings. Google Search
grounding returns web snippets but not structured place information or interactive map context.

**Desired outcome:** Smart/Quick delegates location queries to `MapsSearchAgent` via the
`maps_query` intent, passing the full natural language task as-is. The agent returns structured
place/routing data and a `google_maps_widget_context_token`. Smart generates an HTML page
with the interactive widget embedded, uploads it to GCS, and sends the user a link. The user
opens a live interactive map in their browser.

---

## 2. Architecture

### 2.1 Full Flow

```
User: "find a cafe near my office that is open right now"
  │
  ↓ Smart/Quick → maps_query intent → MapsSearchAgent
      │    payload: {"query": "find a cafe near my office that is open right now"}
      │
      ├─ reads user location from biographical context
      │    (home address, office address → lat/lng)
      │
      ├─ LLM call:
      │    tools: [{"googleMaps": {}, "enableWidget": true}]
      │    toolConfig: {"retrievalConfig": {"latLng": {lat, lng}}}
      │    query: user query
      │
      └─ Gemini Maps grounding returns:
           response.text              — natural language answer
           google_maps_widget_context_token  — widget token

  → AgentResponse.result = {
        "text": "...",
        "maps_widget_token": "eyJ..."
    }

  ↓ Smart LLM receives tool result with token
      → generates HTML page on its own design judgment:
          <html>
            <head>
              <script src="https://maps.googleapis.com/maps/api/js
                ?key={MAPS_JS_KEY}&libraries=places&v=beta">
              </script>
              <style>/* Smart decides layout and styling */</style>
            </head>
            <body>
              <h2>Cafes near the office</h2>
              <gmp-place-contextual context-token="eyJ...">
              </gmp-place-contextual>
              <p>Text commentary from Smart</p>
            </body>
          </html>
      → deliver_response(
            full_response="text response for history",
            rich_content={
                "type": "html_file",
                "data": {"html": "<html>...", "filename": "map_{uuid}.html"},
                "fallback": "text response with addresses"
            }
        )

  ↓ ConversationHandler
      → upload HTML file to GCS (Content-Type: text/html, Cache-Control: no-store)
      → GCS public URL: https://storage.googleapis.com/{bucket}/maps/{uuid}.html
      → Slack/Telegram: sends the URL as a link ("📍 Open map")
      → user clicks → browser opens a live interactive map
      → fallback: if GCS upload is unavailable → text with addresses
```

### 2.2 Key Design Decisions

**Smart generates the HTML, not MapsSearchAgent.** MapsSearchAgent returns raw data (text +
token). Smart decides the layout, styling, and what context to add from the conversation.
This keeps MapsSearchAgent thin and gives Smart full presentation control.

**`html_file` content type — already exists in Smart's output schema.** Smart already knows
how to produce `rich_content` of type `html_file`. No output format changes needed.
Only the `PROTOCOL_SMART_AGENT_SELECTION` prompt needs to explain what to do when a tool
result contains `maps_widget_token` — Smart already knows the delivery mechanism.

**Why `html_file` and not `widget`:** `widget` renders via Playwright → screenshot →
static image. `html_file` uploads raw HTML to GCS → user gets a link to a live interactive
page. Maps widget requires interactivity (scroll, zoom, click places) — a screenshot is
insufficient.

**Location from biographical context.** No per-query location prompt. Smart passes coordinates
extracted from the user's biographical facts (home, office, current city) to the agent
payload. MapsSearchAgent reads `context["lat_lng"]` from the AgentMessage.

---

## 3. Agent Implementation

### 3.1 Class

```
src/agents/maps_search_agent.py
  class MapsSearchAgent(BaseAgent):
      agent_type = "maps_search"

      async def execute(message: AgentMessage) -> AgentResponse:
          # NL passthrough — query sent verbatim to Maps grounding
          query = message.payload.get("query", "")
          if not query:
              return AgentResponse.failure(error="No query provided in payload")
          return await self._call_maps(message, query)

      async def _call_maps(message, query):
          # Single Gemini Maps grounding call (model pinned to gemini-2.5-flash)
          request = LLMRequest(
              model_name=self.model_name,   # MAPS_SEARCH.model_name, not execution_context
              system_instruction="",
              messages=[Message(role="user", parts=[MessagePart(text=query)])],
              tools=[self._maps_tool],   # types.Tool(google_maps=types.GoogleMaps())
              temperature=self.TEMPERATURE,
          )
          response = await self._llm.generate_content(request=request)
          token = self._extract_widget_token(response.grounding_metadata)
          return AgentResponse.success(result={
              "text":              response.text,
              "maps_widget_token": token,   # None if Maps returned no widget
          })
```

### 3.2 Widget Token Extraction

`google_maps_widget_context_token` is returned in `response.candidates[0].grounding_metadata`.
Exact field path must be verified against the Gemini SDK response schema during implementation.

---

## 4. Agent Configuration

```python
# src/infrastructure/agent_config.py

@dataclass
class MapsSearchAgentConfig:
    # Maps grounding — $25 / 1,000 grounded prompts; 500 free/day.
    # Provider: Gemini 2.x only — Maps grounding not supported on Gemini 3.x.
    # model_name hardcoded: gemini-flash-latest resolves to 3.x → 400 INVALID_ARGUMENT.
    # Agent overrides execution_context.model_name with this value at init time.
    model_name: str = "gemini-2.5-flash"
    temperature: float = 0.7
    timeout_ms: int = 90_000

MAPS_SEARCH = MapsSearchAgentConfig()
```

Model: `gemini-2.5-flash` pinned directly in `MapsSearchAgentConfig` — not via tier resolution.
`gemini-flash-latest` resolves to Gemini 3.x which does not support the Maps grounding tool.

---

## 5. Registry

```python
# src/infrastructure/agent_manifest.py

MAPS_SEARCH = AgentDescriptor(
    agent_id="maps_search_agent",
    agent_type="maps_search",
    capabilities={Intent.MAPS_QUERY: ExecutionMode.SYNC},
    description="Expert geospatial agent powered by Google Maps",
    capability_descriptions={
        Intent.MAPS_QUERY: (
            "Expert geospatial agent powered by Google Maps. Can find places, "
            "analyze business details, and build complex routes. "
            "Capabilities: "
            "1. Search: Find addresses, businesses, ratings, and working hours. "
            "2. Navigation: Calculate routes (driving, transit, walking), ETAs, and distances. "
            "3. Discovery: Recommend best spots nearby or along a specific path. "
            "Input: A natural language task (e.g., 'Find a route from A to B with a stop at a cafe' "
            "or 'Is there a pharmacy open near me?'). "
            "payload: {\"query\": \"<natural language task>\"}"
        ),
    },
    internal=False,
)
```

Single intent — NL passthrough. The orchestrating LLM (Quick/Smart) passes the full
natural language task directly. Maps grounding is a natural language API — no
structured decomposition needed. Visible to both Smart and Quick LLMs.

---

## 6. Smart HTML Generation

Smart already produces `html_file` rich content — no output schema changes needed.
Only `PROTOCOL_SMART_AGENT_SELECTION` needs a new instruction describing `maps_widget_token`:

> When a tool result contains `maps_widget_token`, generate an HTML page embedding
> `<gmp-place-contextual context-token="{token}"></gmp-place-contextual>` with the
> Google Maps JS API script tag (key injected server-side). Add layout and text context
> at your discretion. Set `rich_content.type = "html_file"`, place the HTML in
> `rich_content.data.html`. Provide a plain-text `fallback` with addresses and a
> Google Maps link.

The exact prompt text belongs in a Firestore token update — not hardcoded here.

---

## 7. Dependencies

### 7.1 Google Maps JavaScript API Key

A separate `MAPS_JS_KEY` is required for the `<gmp-place-contextual>` web component to load
in the user's browser. This is distinct from the Gemini API key used for Maps grounding.

- Enable: **Maps JavaScript API** + **Places API (New)** in GCP Console
- Store: `MAPS_JS_KEY` in `.env` and GCP Secret Manager
- The key must allow the Cloud Run service origin (or be unrestricted for server-side use)

### 7.2 GCS HTML File Serving

The HTML file is uploaded to the existing GCS bucket under a `maps/` prefix with
`Content-Type: text/html`. No Playwright dependency — the file is served directly by GCS
as a web page. The user's browser loads the Google Maps JS API and renders the interactive
widget. GCS bucket must allow public read on the `maps/` prefix (same pattern as existing
Playwright screenshot uploads, different prefix).

---

## 8. Pricing

| Component | Cost |
|---|---|
| Gemini Maps grounding | $25 / 1,000 grounded prompts |
| Free tier | 500 prompts/day |
| Maps JavaScript API | Free up to 28,000 loads/month |
| GCS HTML file storage | Negligible |

At current usage scale (single user, occasional maps queries): free tier covers it.

---

## 9. Limitations

| Limitation | Notes |
|---|---|
| Paywalled / unlisted places | Not returned by Maps grounding |
| Gemini 3.x incompatible | Pin provider to Gemini 2.x in AgentProviderStrategy |
| Token expiry | Not documented — treat as single-use per response |
| No function calling + Maps | Same constraint as grounding + function calling — MapsSearchAgent makes a single grounding call, no delegation loop |
| Territories | Unavailable in CN, CU, IR, KP, VN |

---

## 10. Implementation Phases

### Phase 1 — Core ✅
- [x] `MapsSearchAgent` class (`src/agents/maps_search_agent.py`) — single `maps_query` NL passthrough
- [x] `AgentProviderStrategy` entry for `"maps_search"` — Gemini only, no fallback
- [x] `AgentDescriptor` in `agent_manifest.py` — single `maps_query` capability
- [x] Model pinned to `gemini-2.5-flash` in `MapsSearchAgentConfig` (3.x doesn't support Maps grounding)
- [x] `GOOGLE_SEARCH_API_KEY` reused as Maps JS API key (already in `.env` and Secret Manager)
- [x] Unit tests — 21 tests passing

### Phase 2 — Delivery ✅ (redesigned — see §11)

Original plan had Smart generating HTML. **Actual implementation uses DeliveryItems instead:**

- [x] `MapsSearchAgent` generates HTML with `<gmp-place-contextual>` and wraps it as
      `DeliveryItem(type="html_gcs_link")` — no Smart prompt changes needed
- [x] `enable_widget=True` passed to `types.GoogleMaps()` in `UserAgentFactory` — required for token
- [x] HTML delivered via existing `DELIVERY_ITEMS_RFC.md` pipeline: Quick/Smart aggregate
      `delivery_items` from sub-agent responses; `ConversationHandler` uploads to GCS and
      sends "📍 Open Map" link to Slack
- [x] GCS upload uses existing `html_gcs_link` handler in `ConversationHandler` — no new code

Remaining:
- [ ] E2E test: maps query → token returned → HTML uploaded → link in chat
- [ ] `PROTOCOL_SMART_AGENT_SELECTION` / `PROTOCOL_QUICK_AGENT_SELECTION` Firestore tokens:
      add `maps_query` `when` / `how` / `anti_patterns` instruction

### Phase 3 — Location Context
- [ ] Smart extracts lat/lng from biographical context before delegating
- [ ] Fallback: if no location in bio context → pass `lat_lng=None` → Maps searches by
      city/country name only (less precise but functional)

---

## 11. Implementation Notes (Deviations from Original Design)

### 11.1 HTML generation: MapsSearchAgent, not Smart

Original §2.2 proposed Smart generating the HTML page with `<gmp-place-contextual>` and delivering
it via `rich_content.type = "html_file"`. **Actual implementation:** MapsSearchAgent generates the
HTML itself and wraps it in `DeliveryItem(type="html_gcs_link")`. Smart receives a plain text
result from the delegation — no HTML generation prompt needed.

**Why:** The `DeliveryItems` delivery layer (`DELIVERY_ITEMS_RFC.md`) was implemented in parallel.
It provides a cleaner, typed transport for arbitrary deliverables — no orchestrator needs to know
the HTML format, and no prompt token needed on the Smart side.

### 11.2 `enable_widget=True` is required

`types.GoogleMaps()` without `enable_widget=True` returns text results but no
`google_maps_widget_context_token`. The token is only populated when the widget is explicitly
requested. This must be set in `UserAgentFactory`:

```python
maps_tool = types.Tool(google_maps=types.GoogleMaps(enable_widget=True))
```

### 11.3 Maps JS API: `v=alpha` required

`gmp-place-contextual` is only available in the alpha channel of the Maps JavaScript API.
`v=beta` renders an empty widget. The HTML template uses:

```html
<script src="https://maps.googleapis.com/maps/api/js?key=...&libraries=places&v=alpha"></script>
```

The "Using the alpha channel — for development purposes only" banner appears in the browser.
This is acceptable for solo-dev usage. Monitor Google's release schedule for promotion to beta.

### 11.4 `contextToken` via JS property, not HTML attribute

The `<gmp-place-contextual>` component requires the token to be set via the JavaScript property
`contextToken`, not via the HTML attribute `context-token`. The script tag must be synchronous
(no `async`) so the element is registered before the inline script sets the property:

```html
<gmp-place-contextual id="map"></gmp-place-contextual>
<script>
  document.getElementById('map').contextToken = '...';
</script>
```

### 11.5 API key reuse

`MAPS_JS_KEY` mentioned in §7.1 maps to `GOOGLE_SEARCH_API_KEY` in practice — the existing
Google Search API key (already in Secret Manager) also has access to the Maps JavaScript API
and Places API. No new secret needed.
