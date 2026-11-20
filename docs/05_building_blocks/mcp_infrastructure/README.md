<!-- living-doc
covers:
  src/adapters/mcp/
  src/ports/maps_tools_port.py
  src/agents/maps_search_agent.py
-->
# MCP Infrastructure (outbound MCP client)

## Purpose

This block is how the bot calls tools on **external MCP servers**. Today there is one: Google Maps AI Grounding Lite
(places, routes, weather), driven by the Maps specialist agent. The generic part is a small JSON-RPC-over-HTTP client;
each server gets its own adapter that owns the domain semantics and error mapping.

What it is not: the inverse direction, alekbot as an MCP server consumed by claude.ai, is a separate block
([remote_mcp_server](../remote_mcp_server/README.md)). Do not confuse the two; the names `MCPClient` in
`src/domain/mcp.py` (an OAuth-registered *caller* of our server) and in `src/adapters/mcp/mcp_client.py` (our client of
*their* server) are unrelated classes.

## Owned data

None. The block stores nothing: tool schemas are cached in memory per `MCPClient` instance, and the API key is
`GOOGLE_SEARCH_API_KEY`, read in `src/composition/user_agent_factory.py` through `config.get` (listed in
`load_settings()`).

## Flows

**Wiring.** `UserAgentFactory` (`src/composition/user_agent_factory.py`) builds one `MCPClient` pointing at the Maps MCP
URL with the API key, wraps it in `MCPMapsAdapter`, and injects that as `maps_port` into `MapsSearchAgent`. The agent
sees only `MapsToolsPort`. The agent is declared as `MAPS_SEARCH` in `src/infrastructure/agent_manifest.py` (intent
`Intent.MAPS_QUERY`), and Quick, Smart and Lelik reach it through `SEARCH_WEB_MAPS_FANOUT` in the same file.

**Tool discovery.** `MCPClient.get_tool_declarations` sends `tools/list` (it skips the MCP `initialize` handshake; the
server is stateless), converts each tool with `MCPClient._convert_tool_schema` (`inputSchema` becomes `parameters`,
`$ref`/`$defs` are inlined and fields Gemini rejects are stripped by `MCPClient._deref_schema`), and caches the list for
the instance's life. Tool names therefore come from the server, not from this repo.

**Tool call.** `MCPClient.call_tool` sends `tools/call`. `MCPClient._parse_tool_result` returns the first text content
item parsed as JSON; a non-JSON text becomes `{"text": ...}`, a JSON non-object becomes `{"result": ...}`, an
unexpected shape `{"raw": ...}`. `MCPMapsAdapter.call_tool` first normalises the arguments (see Invariants), then
turns `MCPToolError` into `MapsToolError`.

**Agent loop** (`MapsSearchAgent._run_tool_loop`):
1. In parallel: build the system prompt (`MapsSearchAgent._build_system_instruction`) and fetch tool declarations.
2. Send the query with the current UTC time prepended and the declarations as `tools`.
3. If the model returns tool calls, append the model message, run all calls of that turn concurrently
   (`MapsSearchAgent._execute_tool_call`), append the results as one user message, and repeat.
4. Stop on a reply without tool calls. After `_MAX_TURNS` turns, make one last call without tools asking the model to
   summarise what it has.
5. An empty final text returns a `partial` response; an exception returns `AgentResponse.failure`.

## Invariants and traps

- **`MCPClient` is not a port.** It carries no domain meaning and is private to `src/adapters/mcp/`. The port is
  `MapsToolsPort`; it exists for substitution in agent tests and a replaceable backend. Its implementations are only
  `MCPMapsAdapter` (there is no Gemini-native adapter in the tree, despite the port docstring).
- **A tool failure does not fail the turn.** `MapsSearchAgent._execute_tool_call` turns `MapsToolError` into an
  `{"error": ...}` tool response so the model can retry or answer from the rest. Calls run concurrently, which is safe
  because `MCPClient._post` opens its own `aiohttp` session per call (30 s timeout, `X-Goog-Api-Key` header).
- **Placeholder zeros are removed before the wire** (`MCPMapsAdapter._normalize_arguments`). The Google tool schemas
  flatten protobuf `oneof` and "zero means unspecified" into plain objects, and weaker models fill them with `{0,0}`
  coordinates or a `{0,0,0}` date. The weather tool rejects a zero date, so it is dropped (with the hour) to get current
  weather; a location is collapsed to its best field (`latLng`, then `placeId`, then `address`). The tool names are
  matched by `MCPMapsAdapter._WEATHER_TOOL` and `_ROUTES_TOOL`; a renamed server tool silently skips normalisation.
  The input dict is never mutated. Enforced by `tests/unit/adapters/test_mcp_maps_adapter.py`.
- **Schema conversion is load-bearing for Gemini.** A leftover `$ref`, `format` or `x-google-enum-descriptions` causes
  `MALFORMED_FUNCTION_CALL`; that is why `_deref_schema` strips them.
- **Model messages carry both `raw_content` and `parts`.** Gemini thought signatures live on the SDK `Part` and must
  round-trip intact, so `GeminiAdapter` replays `raw_content` (`src/adapters/gemini_adapter.py`) while other providers
  read `parts`. Keep both when appending the model turn.
- **No biographical facts.** The prompt is built with `include_biographical=False`: spatial queries do not need them and
  a tool-calling agent should not leak them. Location comes from the user config, time from the prepended line.
- **Known gap against the no-fallback-prompt rule:** if prompt assembly fails, `_build_system_instruction` logs and uses
  an empty instruction instead of failing the response.
- **`MCPMapsAdapter.CAPABILITY_DESCRIPTION` and the manifest text are two copies.** The manifest description
  (`MAPS_SEARCH` in `src/infrastructure/agent_manifest.py`) is what routes to the agent and is not generated from the
  constant; change a backend and update both.

**Adding another MCP server:** a port in `src/ports/` if the capability is new, an adapter in `src/adapters/mcp/` that
wraps an `MCPClient` and maps `MCPToolError` to the port's error, an agent that runs a tool loop, and wiring in
`UserAgentFactory`. `MCPClient` hardcodes the `X-Goog-Api-Key` header, so a server with another auth scheme needs the
client generalised first.

## Links

- [`MCP_INFRASTRUCTURE_RFC.md`](../../10_rfcs/MCP_INFRASTRUCTURE_RFC.md) — design record.
- [remote_mcp_server](../remote_mcp_server/README.md) — the inbound direction.
- Agent roster and the web+maps fan-out: `src/agents/CLAUDE.md`.

## Where to start reading

- `src/adapters/mcp/mcp_client.py::MCPClient`
- `src/adapters/mcp/mcp_maps_adapter.py::MCPMapsAdapter`
- `src/ports/maps_tools_port.py::MapsToolsPort`
- `src/agents/maps_search_agent.py::MapsSearchAgent`
- `src/composition/user_agent_factory.py::UserAgentFactory`
- `src/infrastructure/agent_manifest.py::MAPS_SEARCH`
