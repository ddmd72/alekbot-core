# Agents

Multi-agent system with ActorModel-like coordination — specialists, not one LLM for everything.
Tiers: ECO/BALANCED/PERFORMANCE (tier→model resolution + capability gates live in
[`src/adapters/CLAUDE.md`](../adapters/CLAUDE.md)).

## Structure

- `base_agent.py` — ABC. All agents inherit `BaseAgent`.
- `core/` — orchestrators: `RouterAgent`, `SmartResponseAgent`, `QuickResponseAgent` (fallback/formatter).
- `infrastructure/` — system agents: `BillingAgent`, `LoggerAgent`.
- All other files — specialist agents (roster below).

## Agent Roster (Multi-Agent Network)

- Router (Gemini) — LLM triage on every request: complexity, tone, semantic lens, search intent;
  triggers memory/web enrichment. **Always routes to Smart** (`_apply_routing_rules`); complexity
  drives Smart's per-request tier, not a Quick-vs-Smart split. `_classify_request` = rule-based
  fallback only. Vision (attachments) forces `task_complexity=DEEP_REASONING` (enum, not a numeric score).
- Smart — provider-agnostic (tier/model from execution context per user config). Primary path for
  every request; re-evaluates for follow-up delegation after tool results. Thinking via
  `UserBotConfig.agent_thinking["smart"]`; `message.context["thinking_effort"]` overrides.
- Quick — no longer primary-path (Router never routes to it). Two roles: (1) emergency fallback when
  Smart fails/times out (`AgentFallbackService.try_quick_fallback`); (2) default formatter for
  `UserNotificationService.notify` — caller may override `agent_id_override` (reminders and
  daily-email-review override to Smart). Functionally ≈ Smart minus post-tool re-evaluation.
  Deferred-deletion tech debt — see `docs/04_solution_strategy/decisions/quick_agent_deferred_deletion.md`.
- WebSearch — provider-native grounded search (`use_grounding=True`; each adapter injects its own:
  Gemini Google Search, OpenAI `web_search`, Claude `web_search_20250305`+`web_fetch_20250910`),
  called by Smart. QUICK/RESEARCH cognitive triage. Intents: `search_web`, `fetch_url`.
  - **JSON output is prompt-only, NOT schema-enforced.** `use_grounding` makes the OpenAI adapter
    suppress `text.format` (Web Search + JSON mode → 400), so the findings/source/url shape comes
    from the `WEBSEARCH_OUTPUT_FORMAT` token alone. The agent has **no `_parse_response`** either —
    `_call_grounded_llm` hands `response.text` to the orchestrator verbatim. Malformed JSON
    therefore never raises; it just degrades what Smart can compose. `confidence` is
    `len(text)/500`, so a terse answer reads as low-confidence downstream.
  - **Per-intent tier.** `search_web` runs on the agent's resolved tier (BALANCED); `fetch_url`
    runs on `WebSearchAgentConfig.fetch_url_tier` (ECO), resolved to a model by the provider via
    `LLMPort.get_model_for_tier` — the agent names a tier, never a model. `None` disables the
    downgrade. Measured 2026-07-29, `scripts/websearch/`. See
    `decisions/websearch_per_intent_tier.md`.
  - `fetch_url`'s system prompt is an inline constant (`_FALLBACK_FETCH_SYSTEM`), not a Firestore
    prompt — tracked exception, IMPLEMENTATION_ROADMAP.md TD-6. Its wording is tuned and
    load-bearing; read the comment before editing it.
- Memory — MemorySearchAgent (ECO): LLM extracts search keys → multi-vector RRF. Intents:
  `search_memory`; `save_to_memory` (zero LLM — orchestrator fills `context.text` via `context_schemas`;
  agent attaches `consolidation_text` on `MessagePart` → picked up in the normal consolidation batch).
- EmailSearch — EmailSearchAgent (ECO, `internal=False`). Intents: `search_emails`,
  `get_email_details`, `get_email_attachment` (markitdown).
- EmailClassification — shared ServiceContainer singleton, called by EmailIndexingService (not agents);
  tool-calling triage, extracts fact sentences. OUTPUT_FORMAT exception: markdown-block extraction in
  `_parse_response()` (see inline comment).
- DocPlanner (ASYNC, PERFORMANCE, intent `create_document`) — LLM → JSON layout spec →
  fire-and-forget delegate to DocGenerator → DeliveryItem("file_upload").
- DocGenerator (internal, BALANCED, intent `generate_docx_code`) — LLM writes Node.js script →
  DocxRunnerPort subprocess → DOCX bytes.
- PdfGenerator (ASYNC, BALANCED, intent `create_pdf`, `internal=False`) — one LLM call → HTML+CSS →
  NodePuppeteerRunner → PDF; two DeliveryItem("document"): HTML (GCS) + PDF (GCS + Slack upload).
  Filename from `<title>`; PromptBuilder required; LLM picks from a 12-style catalogue.
- HtmlPageGenerator (ASYNC, PERFORMANCE, intent `create_html_page`, `internal=False`) — one LLM call →
  complete HTML+CSS+JS → DeliveryItem("document") (GCS public link, no subprocess). PromptBuilder
  mandatory; design enforced by `COGNITIVE_PROCESS_HTML_PAGE`. **Unsplash:** LLM writes
  `source.unsplash.com/WxH/?keywords` placeholders → `_resolve_unsplash_placeholders` swaps real photos
  via `UnsplashAdapter` (`ImageSearchPort`); needs `UNSPLASH_ACCESS_KEY`, graceful no-op when absent.
- FileManagement (SYNC, zero-LLM) — intents `open_file` (GCS download + text/vision conversion via
  `FileConversionService`/`FileStoragePort`) and `delete_file`. `context_schemas`: `file_ref` (from the
  `[File: name (size)]` label). Binary → temp file + metadata for vision.
- Notes / Proactive Self-Reminders (PERFORMANCE, OpenAI, intent `manage_self_reminders`) — deferred
  instructions the system writes to itself that fire autonomously as new conversations. Two-field model:
  `text` (≤15-word label) + `instruction` (self-contained execution context). Tools: create/update/delete
  + `delegate_to_specialist` (multi-turn, max 3). Recurrence enum incl. `once` (default). Firing:
  Cloud Scheduler every 15 min → `fire_due_reminders` → per-fire `_build_reminder_alert` →
  `notify(agent_id_override=smart_response_agent_…)` → **Smart** runs it. One-time deleted after firing;
  recurrent (`hourly/daily/weekly/monthly`) → `reschedule()` (DST-safe, user timezone). Idempotency:
  `last_fired` 4-min guard. Soft cap 20. Every CRUD → `notify_raw()` to channel. See
  `docs/10_rfcs/PROACTIVE_SELF_REMINDERS_RFC.md`.
- Tasks (intent `manage_user_tasks`) — TasksAgent over `TasksProviderPort`. Tools: list/search/create/
  update/delete (max 6 turns). Search-before-mutate → `task_ref` (8-char `md5(task_id)[:8]`). Recurrence:
  5 patterns. Active provider: `MicrosoftToDoAdapter` (Graph API CRUD + webhook subscriptions; implements
  TasksProviderPort+TaskLifecyclePort; worker tasks `setup_microsoft_todo`/`reindex_task_list`/
  `renew_task_subscriptions`). `GoogleTasksAdapter` is frozen/deactivated. `TaskIndexingService`
  (embed→index + `resolve_short_id`), `TaskSearchIndex` (2-vector RRF). See `docs/05_building_blocks/tasks_integration/`.
- Consolidation (PERFORMANCE, Claude; Cloud Tasks) — background long-term memory formation
  (mechanism in root `CLAUDE.md` → Consolidation). **Stage 2b `_review_directives`** (unconditional,
  every pass) curates the `agent_directive` rulebook → `standing_directives` block; hard cap 15 via
  prompt + code backstop `_enforce_directive_cap`. See root `CLAUDE.md` → Standing Directives +
  `decisions/standing_directives.md`.
- DeepResearch (async, provider-agnostic, intent `deep_research`) — `create_interaction()` returns
  ACK (job_id); result delivered by adapter. **Default Claude** (`ClaudeDeepResearchRunnerAgent`,
  `NO_RETRY`) runs as a **Cloud Run Job** (`job_main.py`, task-timeout 18000s) via `JobRunnerPort`+
  `CloudRunJobsAdapter`; OpenAI backend = webhook. Gemini backend removed 2026-05-29. Two-pass critic via
  `UserBotConfig.deep_research_second_pass`. Job logs: `make logs-job` / `make fetch-logs-job [K]` →
  `alek_debug_job.log`; single run: `make logs-execution EXECUTION=<name>`.
- MapsSearch (SYNC, BALANCED, OpenAI `gpt-5.4-mini` default, intent `maps_query`, **`internal=True`**) — place
  search, routes, weather via Google Maps AI Grounding (MCP, `MapsToolsPort`). Not shown to LLMs;
  auto-triggered via `intent_fanout` when the orchestrator dispatches `search_web`, results merged
  under labeled sections. Latency-tuned: `thinking="low"` on every turn (all allowed providers reason
  at model default otherwise) + same-turn tool calls run concurrently via `asyncio.gather`.
- Compute (SYNC, ECO) — intents `compute_math`/`compute_datetime`/`compute_finance`/`compute`; runs
  Python in Gemini `code_execution` sandbox (`use_code_execution=True`). No external data — compute-only.

## Orchestration Patterns

- **AgentConfig** — central registry of tunable behavior parameters in `src/infrastructure/agent_config.py`.
  Agents read typed `@dataclass` values as class-level constants at definition time
  (`CONTEXT_WINDOW = QUICK.context_window`). Structured for Level 2 upgrade: replace class-level
  assignments with constructor-injected `self._cfg = get_agent_config(type, user_id)` backed by
  an `AgentConfigPort` + Firestore adapter — agents don't change. Provider selection is a separate
  concern — see `AgentProviderStrategy` in `src/services/agent_context_builder.py`.
- **AgentDescriptor** (`agent_registry.py`; instances in `agent_manifest.py`) — one per agent
  (specialist + orchestrator). Three parts: (A) `capabilities` — intents it exposes (`internal=True`
  hides it from LLM tool declarations); (B) `requirements` — `allowed_intents` (`None` = all
  non-internal) + `intent_remap` (dispatch-time intent substitution; currently unused — Quick's is `{}`);
  (C) `context_schemas` — per-intent typed param contracts; when present the orchestrator fills
  structured `context` instead of a bare `query` (used by `save_to_memory`, `get_email_details`,
  `get_email_attachment`). `eager: bool` (default True): eager → created in `ensure_agents_for_user()`;
  lazy (`eager=False`) → created on first delegation via `AgentFactoryPort.create_agent_on_demand()`
  (DocGenerator/DocPlanner/Pdf/Html/DeepResearch/ClaudeDeepResearchRunner/FileManagement). Specialists
  registered via `ALL_DESCRIPTORS` in `main.py`; orchestrators set a class-level `_descriptor`
  (coordinator never routes TO them via registry).
  See **Creating a New Agent** below for the complete checklist.
- **Intent** — typed string constants for all agent intent names. Defined in `agent_manifest.py`
  as `class Intent`. Import `Intent.SEARCH_MEMORY` etc. instead of raw string literals everywhere.
- **Specialist delegation** — the LLM has a single delegation tool: `delegate_to_specialist(intent, query, context?)`.
  Semantically it operates as **commissioning**: the LLM issues an assignment to a specialist
  who owns that capability. The LLM selects the specialist by matching purpose to manifest
  `capability_descriptions`, not by naming intent strings directly.
  Intent names must semantically reflect the nature of the operation being commissioned —
  the name is the primary signal the LLM uses to match a delegation need to the right specialist.

  **`query` field** — natural language commission text: self-contained, goal-oriented, describing
  what needs to be done and with what content. **Never put JSON or structured data in `query`** —
  that breaks the commissioning model. Structured inputs go in `context` (typed fields declared
  in `AgentDescriptor.context_schemas`). Plain content (report text, analysis) goes in `query`.

  **Formulating instructions for LLM** — all three forms are valid and understood by the LLM
  in the context of the tool declaration:
  - `"Use intent search_memory to retrieve facts about X"` — explicit intent name
  - `"Delegate to specialist with intent get_email_attachment"` — explicit intent name
  - `"Delegate to specialist for HTML page creation"` — purpose-based (LLM resolves intent)
  Use explicit intent names when precision matters (e.g. `search_memory` vs `search_web`).
  Use purpose-based phrasing when the specialist owns the decision (e.g. document creation).
- **DelegationEngine** (`src/infrastructure/delegation_engine.py`) — reusable multi-turn
  tool-calling loop. Owns: loop iteration, tool dispatch via AgentCoordinator, memory-first
  parallel execution (search_memory sequential, others via asyncio.gather), history management
  via `build_tool_turn()` (model message with raw_content, tool response parts with file_data).
  **Delegation datetime:** `AgentCoordinator.handle_delegation()` prepends `[Mon DD, HH:MM UTC]`
  timestamp to every delegation query so all specialists have temporal context.
  Does NOT own: LLM parameters (agent builds `LLMRequest`), response parsing (agent
  post-processes `DelegationResult`).
  **Context passthrough:** `execute()` accepts `message.context` dict directly (no intermediate
  DTO). Engine spreads `**context` into delegation_context, adding only `memory_context` and
  `params`. All context fields (`origin_channel_id`, `session_id`, etc.) propagate automatically
  to downstream tasks including async Cloud Task payloads. Agents pass `context=message.context`
  — zero knowledge of routing, channels, or session format.
  API: `engine.execute(call_llm, base_request, context, max_turns, terminal_tool?, intent_remap?,
  intent_fanout?)`.
  Smart: passes `terminal_tool="deliver_response"`, but this is **vestigial** — no adapter declares a
  `deliver_response` tool, so the engine's terminal-tool branch never fires. Smart's structured output
  arrives via `response_schema` instead: on Claude it is forwarded natively via `output_config.format`
  (schema-valid JSON as *text*; no tool — the synthesized `respond` tool was removed 2026-07-02, see
  [`src/adapters/CLAUDE.md`](../adapters/CLAUDE.md) → Agent Output Format Standards); Gemini/OpenAI return
  native JSON. The loop therefore always ends via the *no-tool-calls → return text* branch, and
  `_build_smart_response` parses `result.text` (the `terminal_tool_args` path is unreachable — see
  IMPLEMENTATION_ROADMAP.md TD-3).
  Quick: `intent_remap={}` (disabled), `intent_fanout` from descriptor.
  Bound agents: plain text response, no terminal tool, no remap, no fanout.
  `DelegationResult` carries: `text`, `terminal_tool_args`, `total_tokens`, `delivery_items`,
  `history_contexts`, `structured_data`, `messages`, `failed`.
  **Intent fan-out** (`intent_fanout`): declarative 1:N dispatch-time expansion. Configured via
  `FanoutSpec(intents, hint)` on `AgentDescriptor`. When LLM dispatches an intent that has a
  fan-out mapping (e.g. `search_web`), the engine runs the primary + all secondary intents
  (e.g. `maps_query`) in parallel via `asyncio.gather`. Results merged into a single
  `ToolResult` with labeled sections (`[Primary specialist: ...]`, `[Additional specialist: ...]`)
  and a reconciliation hint for the LLM. Secondary failures silently skipped — primary always
  returned. `FanoutSpec.hint` provides per-mapping conflict resolution instructions
  (e.g. "trust Maps for geodata, trust Web for reviews"). Both Quick and Smart pass
  `intent_fanout=dict(self._descriptor.intent_fanout)` to the engine.
- **BaseAgent lifecycle hooks** — `_on_agent_start(text)`, `_on_agent_success(char_count, token_count,
  output_text)`, `_on_agent_error(error, context)`, `_on_delegation(intent, query)`. All agents
  call these instead of direct `logger.*` calls. Changing infrastructure logging = edit BaseAgent
  only. `_on_agent_success(output_text=...)` auto-logs final text to debug bucket.
- **`build_tool_turn(response, tool_results)`** — domain-level function (`src/domain/llm.py`)
  for standard multi-turn tool history formatting. Builds model message (with `raw_content` for
  adapter-specific serialization) + tool response messages (with `tool_response` parts).
  `BaseAgent._build_tool_turn()` is a thin wrapper. Used by DelegationEngine and NotesAgent.
  `tool_results`: list of `(ToolCall, result_str)` or `(ToolCall, result_str, file_data)` tuples.
- **BaseAgent LLM-content capture** — `_call_llm(request, turn)` is the single LLM call site and the
  one capture point: it records the full request+response to the **BigQuery content store** via
  `PromptContentStore.record_turn()` (wired only when `DEBUG_PROMPTS=true` AND `BIGQUERY_PROMPT_DATASET`
  set — see root `CLAUDE.md` → Debugging Cloud Run for how to read it). Changing capture = edit this
  method only. **Escape hatch for raw SDK callers** — agents bypassing `LLMPort` (e.g.
  `ClaudeDeepResearchRunnerAgent` with native built-in tools) call `_debug_raw_turn(...)` (summary-only
  `logger.info`, no GCS). The legacy GCS prompt-dump (`PromptDebugLogger`, `DEBUG_PROMPTS_BUCKET`) was
  removed (TD-1, 2026-06-29) — BigQuery is the only content-capture path now.
- **CircuitBreaker** — in BaseAgent, protects against cascading failures.
- **Transcript integrity — one delegation transcript = one provider.** `_call_llm` cross-provider-
  fails-over only when `request.messages` is NOT provider-locked (no `tool_call`/`tool_response` part,
  no `raw_content`). Once locked (mid delegation-loop), a transient FAILOVER error retries the **same**
  provider (`_SAME_PROVIDER_RETRY_ATTEMPTS`) and otherwise raises terminal `TranscriptLockedError`
  — never a mixed transcript (would orphan `tool_use` ids / break thinking-replay / cache). See
  `decisions/transcript_integrity_one_provider.md`.
- **Cross-provider execution retry (provider rotation) — Smart.** `TranscriptLockedError` does NOT go
  straight to Quick. `SmartResponseAgent.execute()` catches it and **rotates**: rebuilds the full run
  on the next `allowed_provider` at the **same tier** (`AgentContextBuilder.resolve_next_provider` →
  walks `allowed_providers`, skips already-tried + breaker-open; `TaskExecutionResolver.next_provider_override`
  → `ExecutionOverride`) and re-runs from scratch (`event="smart_provider_rotation"`). Only exhausting
  the provider list falls through to the existing `AgentFallbackService` Quick rung. L2 retry, not a
  degradation — zero domain/handler/fallback change; `_run` re-raises the lock, `execute` owns the loop.
  See `decisions/cross_provider_execution_retry.md`. **Read before touching `smart_response_agent.execute`
  / `_run` / `resolve_next_provider` / `next_provider_override`.**

## Creating a New Agent

Follow [`docs/how_to/NEW_AGENT_PLAYBOOK.md`](../../docs/how_to/NEW_AGENT_PLAYBOOK.md) — mandatory protocol.
Short version:

1. Add `Intent` constant + `AgentDescriptor` to `infrastructure/agent_manifest.py`
   (include in `ALL_DESCRIPTORS`).
2. Inherit `BaseAgent`, implement `can_handle()` and `execute()`.
   Dependencies — via constructor (LLMPort, SessionStore, PromptBuilderPort).
3. Return `AgentResponse.success()` / `AgentResponse.failure()`.
4. Wire creation in `composition/user_agent_factory.py` (eager, or `eager=False`
   for on-demand creation via `AgentFactoryPort`).
5. Update `src/utils/capabilities.py` (user-facing `get_help` reference).

Structured-output agents must also follow the Agent Output Format Standards in
[`src/adapters/CLAUDE.md`](../adapters/CLAUDE.md) (OUTPUT_FORMAT token, `_parse_response`, retry).

## Important

- Agents do NOT access the database directly — only through services/ports.
- Prompts live in Firestore (token + blueprint via PromptBuilder). NO inline or
  fallback prompts in code — if `build_for_agent()` fails, return
  `AgentResponse.failure()`. Fail fast.
- CircuitBreaker is built into BaseAgent — do not duplicate.
- `AgentExecutionContext` contains model_name, tier, provider — the agent does not
  select the model itself (see `AgentProviderStrategy`).
- Use `_call_llm(request, turn)` from BaseAgent — it is the single billing + debug
  logging point. Never call the provider port directly.
- Multi-turn tool loops: use `infrastructure/delegation_engine.py`, do not hand-roll.
