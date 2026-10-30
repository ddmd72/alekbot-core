# Composition — traps

- **MCP tool schema: accept liberally, advertise strictly.** FastMCP builds the schema from type hints and validates with
  pydantic: `Optional[T]` emits `anyOf:[T,null]`, and any advertised constraint turns bad input into an `isError` round-trip.
  Optionality through defaults, a `description` per param, constraints in `BeforeValidator` normalizers (`domain/mcp.py`).
- **claude.ai caches `tools/list` at registration:** an MCP schema change needs the connector removed and re-added
  (`decisions/mcp_tool_schema_liberal_input.md`).
- **MCP endpoints sit at the server root**, routed by the plain ASGI dispatcher in `main.py`, never a Starlette `Mount`
  (it strips the prefix). `main.py` swallows MCP build errors, so anything that must fail closed is checked in `build_application_graph`.
