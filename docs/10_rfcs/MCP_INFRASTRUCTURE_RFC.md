# RFC: MCP Protocol Infrastructure + Maps Grounding Lite Pilot

**Status:** IMPLEMENTED
**Date:** 2026-03-08
**Owner:** AI Engineering
**Milestone:** Specialist Agents — MCP Integration

**Supersedes:** MAPS_SEARCH_RFC.md (§ 3–6 implementation; problem statement preserved)
**Related:** MAPS_SEARCH_RFC.md, HEXAGONAL_PROMPT_CACHING_RFC.md

---

## 1. Problem Statement

`MapsSearchAgent` is built on Gemini-native `types.GoogleMaps` grounding with three hard limits:

1. **Provider lock-in.** Pinned to `gemini-2.5-flash` — Gemini 3.x dropped Maps grounding support.
   Any model upgrade requires re-evaluating the entire agent.
2. **No routing, no weather.** Descriptor explicitly says "NOT for routing". Weather goes to
   `web_search` (unstructured, lower quality).
3. **Widget token non-functional.** `google_maps_widget_context_token` is extracted but the
   `<gmp-place-contextual>` HTML delivery is broken in practice. The only differentiated feature
   of the current backend does not work.

Google Maps AI Grounding Lite (`mapstools.googleapis.com/mcp`) is an MCP server that provides
places, route computation, and weather lookup — provider-agnostic, currently free in experimental.

This RFC has two goals:
- Define **generic MCP client infrastructure** reusable across future MCP servers.
- Replace the Gemini-native Maps backend with MCP as the first pilot.

---

## 2. MCP Protocol Overview

MCP (Model Context Protocol) is a transport standard for connecting LLM agents to external tools.
Google Maps AI Grounding Lite exposes an MCP endpoint at:

```
https://mapstools.googleapis.com/mcp
```

**Transport:** Streamable HTTP (POST only, stateless per call).
**Auth:** `X-Goog-Api-Key: <key>` header. Reuses existing `GOOGLE_SEARCH_API_KEY`.

### 2.1 Protocol Flow

```
1. POST /mcp  body: {jsonrpc: "2.0", method: "initialize", ...}
   → Server returns: {tools: [{name, description, inputSchema}, ...]}

2. POST /mcp  body: {jsonrpc: "2.0", method: "tools/call",
                     params: {name: "places_search", arguments: {query: "..."}}}
   → Server returns: {content: [{type: "text", text: "<JSON result>"}]}
```

No persistent session. Each call is independent.

### 2.2 Maps Grounding Lite Tools

| Tool | Key Arguments | Returns |
|------|--------------|---------|
| `places_search` | `query`, `location?` | name, address, rating, hours, Maps URL, Place ID |
| `route_computation` | `origin`, `destination`, `mode?` | distance, duration (no turn-by-turn, no traffic) |
| `weather_lookup` | `location` | current conditions, hourly forecast, daily forecast |

**Quotas (experimental):** `places_search` 100 req/min / 1,000/day; weather + routes 300 req/min.
**Cost:** free during experimental phase.

---

## 3. Hexagonal Placement

```
src/ports/
    maps_tools_port.py          ← domain port (ABC)

src/adapters/mcp/
    __init__.py
    mcp_client.py               ← generic MCP transport (private helper, not a port)
    mcp_maps_adapter.py         ← implements MapsToolsPort via MCPClient

src/agents/
    maps_search_agent.py        ← updated: MapsToolsPort injection + LLM tool loop
```

`MCPClient` lives in `adapters/mcp/` as a **private shared helper** — analogous to
`google.cloud.firestore.Client` used across all Firestore adapters. It is not a port because
it carries no domain meaning; it is the implementation detail of adapters.

**Pattern for future MCP servers:**
- New domain port → `src/ports/{capability}_port.py`
- New adapter → `src/adapters/mcp/{name}_adapter.py` (uses same `MCPClient`)
- `MCPClient` is the reuse point; adapters are the extension point.

---

## 4. MapsToolsPort

```python
# src/ports/maps_tools_port.py
from abc import ABC, abstractmethod


class MapsToolsPort(ABC):

    @abstractmethod
    async def get_tool_declarations(self) -> list[dict]:
        """Return tool schemas in LLMRequest-compatible FunctionDeclaration format."""

    @abstractmethod
    async def call_tool(self, name: str, arguments: dict) -> dict:
        """Execute a named tool call and return the parsed JSON result."""
```

**Port justification:** testable substitution in unit tests (mock avoids HTTP), system boundary
(external network call), and future alternative implementations (e.g., `GeminiMapsAdapter`
wrapping the old native grounding tool as a fallback backend).

---

## 5. MCPClient

```python
# src/adapters/mcp/mcp_client.py
class MCPClient:
    def __init__(self, base_url: str, api_key: str) -> None: ...

    async def initialize(self) -> list[dict]:
        """
        Call MCP initialize method. Returns list of tool schemas.
        Result cached — tool schemas are immutable per server version.
        Converts MCP inputSchema → LLMRequest-compatible FunctionDeclaration dicts.
        """

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """
        Call MCP tools/call method. Returns parsed JSON result.
        Raises MCPToolError on non-200 or error response.
        """
```

**Implementation notes:**
- Uses `aiohttp` (already in `requirements.txt`) — no new dependencies.
- JSON-RPC 2.0 envelope constructed inline (no jsonrpc library needed).
- `initialize()` result cached in instance — call once per agent lifecycle.
- `MCPToolError(Exception)` defined in same file for adapter-level error handling.

---

## 6. MCPMapsAdapter

```python
# src/adapters/mcp/mcp_maps_adapter.py
class MCPMapsAdapter(MapsToolsPort):

    CAPABILITY_DESCRIPTION = (
        "Place search and discovery, route computation (distance and duration, "
        "not turn-by-turn directions), and current weather lookup via Google Maps "
        "AI Grounding Lite. Input: natural language task. "
        "Examples: 'pharmacy near Khreschatyk open now', "
        "'route from Kyiv to Lviv by car', 'weather in Odesa today'."
    )

    def __init__(self, mcp_client: MCPClient) -> None: ...

    async def get_tool_declarations(self) -> list[dict]:
        return await self._mcp_client.initialize()

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return await self._mcp_client.call_tool(name, arguments)
```

`CAPABILITY_DESCRIPTION` is a class-level constant. When switching backends, update this
constant on the replacement adapter class and update `agent_manifest.py` accordingly.
These two changes always happen together — no dynamic mapping needed.

---

## 7. Updated MapsSearchAgent

Replaces the single Gemini native call with a multi-turn LLM ↔ MCP tool loop,
following the same pattern as `EmailSearchAgent`.

### 7.1 Constructor

```python
def __init__(
    self,
    config: AgentConfig,
    execution_context: AgentExecutionContext,
    maps_port: MapsToolsPort,
    user_id: Optional[str] = None,
) -> None:
```

`prompt_builder` was planned but deferred — see § 7.4 tech debt note.

### 7.2 Execute Flow

```
execute(message):
  1. tool_declarations = await maps_port.get_tool_declarations()
  2. LLMRequest(system_instruction=_SYSTEM_INSTRUCTION, tools=tool_declarations, messages=[user_msg])
  3. LLM responds with tool_calls → [places_search | route_computation | weather_lookup]
  4. for each tool_call:
       result = await maps_port.call_tool(name, arguments)
       append ToolResultMessage to history (model raw_content + user tool_response)
  5. LLM final call (no tools) → formats response text
  6. Return AgentResponse.success(text=...)
```

Max turns: `_MAX_TURNS = 4`. Model message carries both `raw_content` (Gemini: preserves
`thought_signature` bytes on `types.Part`) and `parts` (Claude/OpenAI/Grok: tool call list).
GeminiAdapter uses `raw_content` directly; other adapters use `parts`. See § 7.5.

### 7.3 Delivery

- **Places / weather / routes:** plain text response (LLM formats naturally).
- No `rich_content` table for routes in this implementation — the LLM produces a
  well-structured text response without requiring structured data extraction.
- No `html_gcs_link` — widget token delivery removed (was non-functional).

### 7.4 Tech Debt: System Prompt

`_SYSTEM_INSTRUCTION` remains a hardcoded module-level constant in `maps_search_agent.py`.
The plan to move it to PromptBuilderPort (Firestore profile `universal_agent_v1_SYSTEM_maps`)
was deferred due to time constraints. No functional impact — the prompt is minimal and stable.

**To resolve:** create `universal_agent_v1_SYSTEM_maps` Firestore profile → inject
`PromptBuilderPort` via constructor → call `prompt_builder.build(...)` in `_run_tool_loop`.

### 7.5 Dual-Storage: raw_content + parts (thought_signature fix)

Gemini `types.Part.thought_signature` (bytes) is required by the API when continuing a
conversation that included tool calls with thinking enabled. The signature lives on
`types.Part`, not on `types.FunctionCall` — it cannot be extracted and re-attached
without keeping the original SDK object.

Solution: model messages store both:
```python
messages.append(Message(
    role="model",
    raw_content=response.raw_content,   # Gemini: original types.Content with signatures intact
    parts=[MessagePart(tool_call=tc) for tc in response.tool_calls],  # Claude/OpenAI/Grok
))
```

GeminiAdapter's `_convert_messages` already has `if msg.raw_content: append directly; continue`.
Other adapters use `parts` (protected by `hasattr` type guards). Domain model stays clean —
`thought_signature: Optional[str]` on `ToolCall` is unused in the Gemini path.

---

## 8. Intent Strategy — No Dynamic Intents

Single intent `Intent.MAPS_QUERY` is unchanged. No new intents added.

**Why:** orchestrator intents are a contract between Quick/Smart and the specialist agent.
Backend capabilities are an implementation detail. When the backend changes:
- Swap adapter in `user_agent_factory.py`
- Update `capability_descriptions` text in `agent_manifest.py`
- No manifest structure changes, no intent additions/removals

`capability_descriptions[Intent.MAPS_QUERY]` updated to `MCPMapsAdapter.CAPABILITY_DESCRIPTION`.

---

## 9. Composition Changes

### 9.1 user_agent_factory.py

```python
# Before
maps_tool = create_google_maps_tool(enable_widget=True)
maps_agent = MapsSearchAgent(..., maps_tool=maps_tool, maps_js_api_key=key)

# After
mcp_client = MCPClient(
    base_url="https://mapstools.googleapis.com/mcp",
    api_key=self.config.get("GOOGLE_SEARCH_API_KEY", ""),
)
maps_port = MCPMapsAdapter(mcp_client)
maps_agent = MapsSearchAgent(..., maps_port=maps_port, prompt_builder=prompt_builder)
```

### 9.2 agent_context_builder.py

```python
"maps_search": {
    "default_provider": "gemini",
    "allowed_providers": ["gemini", "claude"],  # MCP is provider-agnostic
    "required_capabilities": ["native_tools"],
}
```

### 9.3 agent_config.py

Remove `model_name` pin from `MapsSearchAgentConfig` — no longer needed, provider selected
via `AgentProviderStrategy`.

---

## 10. Backend Switchability

To revert to Gemini native grounding:

1. Create `GeminiMapsAdapter(MapsToolsPort)` wrapping `types.GoogleMaps` (old implementation).
2. In `user_agent_factory.py`: swap `MCPMapsAdapter` → `GeminiMapsAdapter`.
3. In `agent_manifest.py`: update `capability_descriptions` text (remove routes/weather mention).
4. In `agent_context_builder.py`: revert `allowed_providers` to `["gemini"]`.

The agent itself (`maps_search_agent.py`) does not change. Port contract is stable.

---

## 11. Migration Delta

| Aspect | Current | After RFC |
|--------|---------|-----------|
| Backend | Gemini `types.GoogleMaps` | MCP `mapstools.googleapis.com` |
| Provider | Gemini 2.5-flash only | Any (Gemini default) |
| Capabilities | Places only | Places + Routes + Weather |
| Delivery | `html_gcs_link` (broken widget) | Text + `rich_content` table |
| System prompt | Hardcoded class constant | PromptBuilderPort |
| Tool execution | Single Gemini grounding call | Multi-turn LLM ↔ MCP loop |
| Port | None | `MapsToolsPort` |
| Intent | `maps_query` | `maps_query` (unchanged) |
| Model pin | `gemini-2.5-flash` (hardcoded) | Resolved via ProviderStrategy |
| Cost | $25/1k prompts (after 500/day free) | Free (experimental quota) |

---

## 12. Files

**New:**
- `src/ports/maps_tools_port.py`
- `src/adapters/mcp/__init__.py`
- `src/adapters/mcp/mcp_client.py`
- `src/adapters/mcp/mcp_maps_adapter.py`
- `tests/unit/adapters/test_mcp_maps_adapter.py` (wire test, mock at aiohttp boundary)

**Modified:**
- `src/agents/maps_search_agent.py` — new constructor, LLM tool loop, drop HTML generation
- `src/composition/user_agent_factory.py` — swap maps DI
- `src/services/agent_context_builder.py` — add claude to allowed_providers
- `src/infrastructure/agent_manifest.py` — update capability_descriptions only
- `src/infrastructure/agent_config.py` — remove model_name pin
- `tests/unit/agents/test_maps_search_agent.py` — rewrite for new pattern

**Unchanged:**
- `src/infrastructure/agent_manifest.py` intent structure
- `src/infrastructure/agent_coordinator.py`
- All other agents

---

## 13. Pre-Implementation: Diagnostic Script

Before writing any production code, run `scripts/debug/test_mcp_maps.py` to resolve:

1. **Auth confirmation** — does `X-Goog-Api-Key` work or is OAuth required?
2. **Tool schema format** — what does `initialize` actually return? Does it map cleanly to
   `FunctionDeclaration`?
3. **`route_computation` response shape** — enough data for a `rich_content` table?
4. **`weather_lookup` response shape** — structured enough vs just raw text?

The script should: initialize MCPClient → call each of the 3 tools with sample args → print
raw JSON responses. Implementation details of `MCPClient` finalized after seeing real responses.

---

## 14. Tests

- **`test_mcp_maps_adapter.py`** — wire test per `ADAPTER_WIRE_TESTING.md`: mock `aiohttp.ClientSession`,
  assert correct JSON-RPC envelope, assert result parsing. Not a port-level mock.
- **`test_maps_search_agent.py`** — rewrite: `AsyncMock(spec=MapsToolsPort)`, test tool loop
  (single tool call, multi-tool, LLM error, tool error, max turns exceeded).
- HTML generation tests removed (delivery type changed).

---

## 15. Verification

1. `scripts/debug/test_mcp_maps.py` — raw MCP responses (prerequisite)
2. `make test-unit` — all tests pass
3. `make test-e2e-all` — `maps_query` intent works end-to-end
4. Manual Slack: "find a route from Khreshchatyk to Bessarabka" → routes table
5. Manual Slack: "what's the weather in Kyiv right now" → weather response
6. Manual Slack: "where to eat sushi in central Kyiv" → places response
