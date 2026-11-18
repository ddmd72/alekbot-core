# Composition — traps

- **A new subsystem gets one `*_setup.py` returning one frozen bundle; a service is built where its bundle is built;
  nothing is attached after construction** (the named exceptions are listed in
  `tests/unit/architecture/late_wiring_calls_allowlist.txt` as `permanent`).
- **Five helpers stay private in `application_graph.py`** (`_build_coordinator`, `_build_generation_registries`,
  `_build_slack`, `_build_worker_handler`, `_build_telegram`): tests patch those names there and the allowlists key on the
  path, so moving one breaks them. `coordinator.set_agent_factory` stays in `build_application_graph` (the one real cycle).
- **Agents get their runtime through `BaseAgent.bind_runtime(AgentRuntime)`,** never by writing `_quota_service` /
  `_prompt_content_store` on an instance; the factory takes `AgentServices`, not a dict.
- **A new `/worker` task is a runner in `services/` (a `WorkerTask`), registered in `build_worker_tasks` (`worker_setup.py`);**
  `DisabledTask` stands in only where an absent feature's honest answer is a refusal.

- **MCP tool schema: accept liberally, advertise strictly.** FastMCP builds the schema from type hints and validates with
  pydantic: `Optional[T]` emits `anyOf:[T,null]`, and any advertised constraint turns bad input into an `isError` round-trip.
  Optionality through defaults, a `description` per param, constraints in `BeforeValidator` normalizers (`domain/mcp.py`).
- **claude.ai caches `tools/list` at registration:** an MCP schema change needs the connector removed and re-added
  (`decisions/mcp_tool_schema_liberal_input.md`).
- **MCP endpoints sit at the server root**, routed by the plain ASGI dispatcher in `src/web/mcp_dispatcher.py`, never a Starlette `Mount`
  (it strips the prefix). `main.py` swallows MCP build errors, so anything that must fail closed is checked in `build_application_graph`.
- **Every eviction goes through `UserAgentFactory._evict_user`** (it takes no lock and advances the user's generation; the
  locked build redoes a build an eviction overtook). A new eviction path that pops `_cache` directly reopens P15-S1b.
