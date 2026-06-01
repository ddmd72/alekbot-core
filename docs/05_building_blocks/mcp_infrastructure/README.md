# MCP Infrastructure (Building Block)

> **Direction note:** This block describes alekbot as an **MCP client** — it connects
> OUT to external MCP servers (Google Maps AI Grounding Lite). The inverse direction —
> alekbot as an **MCP server** consumed by claude.ai Custom Connectors — lives in
> [`../remote_mcp_server/`](../remote_mcp_server/README.md). Don't confuse the two;
> they share the name "MCP" but are fundamentally different surfaces.

## 1. Overview

MCP (Model Context Protocol) is a transport standard for connecting LLM agents to external tool
servers over HTTP. This building block provides:

1. **`MCPClient`** — generic JSON-RPC 2.0 HTTP transport, reusable across MCP servers.
2. **`MCPMapsAdapter`** — `MapsToolsPort` implementation for Google Maps AI Grounding Lite.

**Why a generic client?** Each new MCP server needs only a new adapter file.
`MCPClient` handles the protocol; the adapter handles domain semantics and error mapping.

---

## 2. Hexagonal Placement

```
src/ports/
    maps_tools_port.py          ← domain port (ABC) for location services

src/adapters/mcp/
    __init__.py
    mcp_client.py               ← generic MCP transport (private shared helper)
    mcp_maps_adapter.py         ← implements MapsToolsPort via MCPClient

src/agents/
    maps_search_agent.py        ← injects MapsToolsPort, runs LLM ↔ tool loop
```

`MCPClient` is analogous to `google.cloud.firestore.Client` — shared infrastructure detail
used by multiple adapters. It is **not a port** because it carries no domain meaning.

---

## 3. MCPClient

**File:** `src/adapters/mcp/mcp_client.py`

Generic MCP transport using `aiohttp` (no new dependencies).

### 3.1 Interface

```python
class MCPClient:
    def __init__(self, base_url: str, api_key: str) -> None: ...

    async def get_tool_declarations(self) -> list[dict]:
        """
        Call MCP tools/list. Returns tool schemas in LLMRequest-compatible format
        (inputSchema renamed to parameters). Cached after first call.
        """

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """
        Call MCP tools/call. Returns parsed JSON result.
        Raises MCPToolError on HTTP error or JSON-RPC error response.
        """
```

### 3.2 Protocol

- **Transport:** Streamable HTTP, POST to base URL.
- **Auth:** `X-Goog-Api-Key: <key>` header. Reuses `GOOGLE_SEARCH_API_KEY`.
- **Envelope:** JSON-RPC 2.0.

```
POST /mcp
Body: {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
→ {"result": {"tools": [{name, description, inputSchema}, ...]}}

POST /mcp
Body: {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
       "params": {"name": "places_search", "arguments": {"query": "..."}}}
→ {"result": {"content": [{"type": "text", "text": "<JSON or plain text>"}]}}
```

### 3.3 Caching

`get_tool_declarations()` caches the result after the first call. Tool schemas are
immutable per server version — no re-initialization needed within an agent lifecycle.

### 3.4 Error Handling

- **HTTP non-200** → `MCPToolError("HTTP {status}: {body}")`
- **JSON-RPC error field** → `MCPToolError("{message}")`
- **Non-JSON text result** → wrapped as `{"text": "<raw text>"}` (graceful degradation)

`MCPToolError` is adapter-internal. `MCPMapsAdapter` maps it to domain `MapsToolError`.

---

## 4. MapsToolsPort

**File:** `src/ports/maps_tools_port.py`

Domain port for location services. Provider-agnostic — any LLM with `native_tools`
capability can drive the tool loop.

```python
class MapsToolsPort(ABC):

    @abstractmethod
    async def get_tool_declarations(self) -> list[dict]:
        """Return tool schemas in LLMRequest-compatible FunctionDeclaration format."""

    @abstractmethod
    async def call_tool(self, name: str, arguments: dict) -> dict:
        """Execute a named tool call. Raises MapsToolError on failure."""
```

**Port justification:** testable substitution (`AsyncMock(spec=MapsToolsPort)` in agent unit
tests — no network), system boundary (external HTTP call), and backend switchability
(swap `MCPMapsAdapter` ↔ `GeminiMapsAdapter` without touching agent code).

---

## 5. MCPMapsAdapter

**File:** `src/adapters/mcp/mcp_maps_adapter.py`

Thin adapter implementing `MapsToolsPort`. Constructor: `(mcp_client: MCPClient)`.

```python
class MCPMapsAdapter(MapsToolsPort):

    CAPABILITY_DESCRIPTION = (
        "Place search and discovery, route computation (distance and duration, "
        "not turn-by-turn directions), and current weather lookup via Google Maps "
        "AI Grounding Lite. Input: natural language task."
    )
```

`CAPABILITY_DESCRIPTION` drives the LLM routing description in `agent_manifest.py`.
When switching backends, update this constant and the manifest entry — always together.

### 5.1 Maps Grounding Lite Tools

| Tool | Key Arguments | Returns |
|------|--------------|---------|
| `places_search` | `query`, `location?` | name, address, rating, hours, Maps URL, Place ID |
| `route_computation` | `origin`, `destination`, `mode?` | distance, duration (no turn-by-turn, no traffic) |
| `weather_lookup` | `location` | current conditions, hourly forecast, daily forecast |

**Endpoint:** `https://mapstools.googleapis.com/mcp`
**Auth key:** `GOOGLE_SEARCH_API_KEY` env var
**Quotas (experimental):** `places_search` 100 req/min / 1 000/day; routes + weather 300 req/min.
**Cost:** free during experimental phase.

---

## 6. MapsSearchAgent Tool Loop

`MapsSearchAgent` runs a multi-turn LLM ↔ MCP loop (`_MAX_TURNS = 10`):

```
0. asyncio.gather(build_system_instruction(), get_tool_declarations())  ← parallel
1. system_instruction = PromptBuilderPort.build_for_agent("maps_search", account_id, user_id)
2. get_tool_declarations() → list[FunctionDeclaration]  (cached after first call)
3. LLMRequest(system_instruction, messages=[user_msg], tools=declarations)
4. LLM responds with tool_calls → [places_search | route_computation | weather_lookup]
5. for each tool_call:
     result = await maps_port.call_tool(name, args)  # MapsToolError → error dict, loop continues
     append MessagePart(tool_response=...) to history
6. Append model message with raw_content + parts (see § 6.1)
7. Repeat from step 3 until no tool_calls or max turns (_MAX_TURNS = 10)
8. After max turns → forced format call with "Please summarize the results above."
9. Return AgentResponse.success(result={"text": final_text})
```

### 6.2 System Prompt & Biographical Facts

`MapsSearchAgent` builds its system prompt via `PromptBuilderPort.build_for_agent()` with
`include_biographical=True`. This injects the user's biographical facts into the prompt
the same way every other specialist agent does — no hardcoded instruction string.

The system prompt is assembled from Firestore tokens at runtime:

| Token | Class | Purpose |
|-------|-------|---------|
| `MAPS_PROPERTIES` | `properties` | Archetype: "Primary Location Intelligence Specialist" |
| `MAPS_COGNITIVE_PROCESS` | `cognitive_process` | 3-angle orthogonal decomposition + location anchor rule |
| `MAPS_OUTPUT_FORMAT` | `output_format` | Place / route / weather block structure, URL style |

Blueprint: `maps_search_agent_v1`. Profile: `maps_search`.

**Location anchor rule** (in `MAPS_COGNITIVE_PROCESS`): the agent must pass the user's
address or location name to the tool exactly as given — no geocoding, no substitution.
Google Maps resolves addresses; the LLM must not.

**3-angle orthogonal decomposition:** every query is decomposed into exactly 3 maximally
independent search angles. Each angle gets its own tool call, possibly the same tool with a
different query formulation. `_MAX_TURNS = 10` gives headroom for 3 required angles plus retries.

### 6.1 Dual-Storage: raw_content + parts

Gemini's `types.Part.thought_signature` (bytes) must round-trip intact for multi-turn
tool-calling with thinking enabled. The signature lives on `types.Part`, not on
`types.FunctionCall` — it cannot be extracted without losing the bytes type.

Model messages store both representations:
```python
Message(
    role="model",
    raw_content=response.raw_content,  # Gemini: types.Content with signatures
    parts=[MessagePart(tool_call=tc) for tc in response.tool_calls],  # other providers
)
```

GeminiAdapter uses `raw_content` directly (bypasses part reconstruction).
Claude/OpenAI/Grok use `parts` (protected by `hasattr` type guards).
Domain model stays clean — no Gemini-specific bytes in `ToolCall`.

---

## 7. Backend Switchability

To revert to Gemini native grounding or add a new Maps backend:

1. Create `GeminiMapsAdapter(MapsToolsPort)` wrapping `types.GoogleMaps`.
2. In `user_agent_factory.py`: swap `MCPMapsAdapter` → `GeminiMapsAdapter`.
3. In `agent_manifest.py`: update `capability_descriptions[Intent.MAPS_QUERY]`.
4. In `agent_context_builder.py`: adjust `allowed_providers` if needed.

`MapsSearchAgent` itself does not change. Port contract is stable.

---

## 8. Adding a New MCP Server

Each new MCP server requires:

1. **Domain port** `src/ports/{capability}_port.py` (if capability is new).
2. **Adapter** `src/adapters/mcp/{name}_adapter.py` — implements the port, uses `MCPClient`.
3. **Agent** — injects the port via constructor, runs tool loop.
4. **Composition** `user_agent_factory.py` — wire `MCPClient(url, key)` → adapter → agent.

Shared `MCPClient` handles protocol. New adapter = ~30 lines of delegation + error mapping.

---

## 9. Tests

| File | What it tests | Mock boundary |
|------|--------------|---------------|
| `tests/unit/ports/test_maps_tools_port.py` | Port contract, MapsToolError | — |
| `tests/unit/adapters/test_mcp_maps_adapter.py` | MCPClient JSON-RPC, schema conversion, MCPMapsAdapter delegation | `aiohttp.ClientSession` |
| `tests/unit/agents/test_maps_search_agent.py` | Tool loop (single/multi/error/max turns), can_handle | `AsyncMock(spec=MapsToolsPort)` |

---

## 10. Composition

```python
# src/composition/user_agent_factory.py
mcp_client = MCPClient(
    base_url="https://mapstools.googleapis.com/mcp",
    api_key=self.config.get("GOOGLE_SEARCH_API_KEY", ""),
)
maps_port = MCPMapsAdapter(mcp_client)
maps_agent = MapsSearchAgent(
    config=AgentConfig(...),
    execution_context=maps_search_context,
    maps_port=maps_port,
    prompt_builder=prompt_builder,   # PromptBuilderPort — assembles system prompt + bio facts
    account_id=account_id,           # needed for 4-level override resolution
    user_id=user_id,
)
```

---

## 11. Status

**Status:** ✅ Production Ready
**Last Updated:** 2026-03-08
**RFC:** [MCP_INFRASTRUCTURE_RFC.md](../../10_rfcs/MCP_INFRASTRUCTURE_RFC.md) — IMPLEMENTED
