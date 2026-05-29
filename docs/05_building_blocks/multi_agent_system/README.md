# Multi-Agent System (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the Actor Model-based multi-agent architecture, agent coordination patterns, and resilience mechanisms.

### When to Read

- **For AI Agents:** Before changing agent coordination, routing logic, or ACP semantics.
- **For Developers:** When adding new agents, modifying message flow, or tuning circuit breakers/retries.

### When to Update

This document MUST be updated when:

- [ ] `AgentCoordinator` routing logic changes.
- [ ] `AgentMessage` or `AgentResponse` schemas are modified.
- [ ] New core or infrastructure agents are introduced.
- [ ] Resilience rules (circuit breaker, retry) are adjusted.
- [ ] Agent lifecycle management in `UserAgentFactory` changes.
- [ ] `AgentResponse.history_context` or session history persistence logic changes in `ConversationHandler`.
- [ ] A new `DeliveryItem` type is added or an existing data contract changes.

### Cross-References

- **Target Architecture:** [../../04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md](../../04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md)
- **Hybrid Router:** [../hybrid_router/README.md](../hybrid_router/README.md)
- **Agent Registry (ACP v2):** [../agent_registry/README.md](../agent_registry/README.md)
- **Prompt Design System v3:** [../prompt_design_system_v3/README.md](../prompt_design_system_v3/README.md)
- **Sliding Window Consolidation:** [../sliding_window_consolidation/README.md](../sliding_window_consolidation/README.md)

---

## 1. Overview

Alek-Core uses a **Multi-Agent System (MAS)** based on the **Actor Model** to handle complex reasoning and specialized tasks. Instead of a monolithic orchestrator, the system consists of independent, specialized agents that communicate via a unified protocol.

**Core Principle:** Every agent is a specialist. Coordination is handled by a central hub, and instances are isolated per user.

### 1.1 Key Benefits

- **Specialization:** Each agent (Memory, Web, Consolidation) focuses on one domain.
- **Resilience:** Failures in one agent (e.g., Web Search timeout) don't crash the entire system.
- **Scalability:** Agents can execute in parallel via `asyncio.gather`.
- **Extensibility:** New capabilities can be added by registering new agents without modifying core logic.

---

## 2. Agent Communication Protocol (ACP)

The ACP standardizes all inter-agent traffic, ensuring platform-agnostic communication.

### 2.1 AgentMessage (Request)

- `task_id`: Unique identifier for tracking.
- `sender` / `recipient`: Routing addresses.
- `intent`: `QUERY`, `DELEGATE`, `INFORM`, `REQUEST_FEEDBACK`.
- `payload`: Task-specific data (e.g., search query).
- `context`: Metadata (user_id, session_id, account_id).
- `priority`: 0-10 scale for scheduling.

### 2.2 AgentResponse (Result)

- `status`: `SUCCESS`, `PARTIAL`, `FAILED`, `TIMEOUT`, `CANNOT_HANDLE`.
- `result`: The actual data or `SmartResponse`.
- `confidence`: 0.0-1.0 score.
- `metadata`: Usage stats (tokens), latency, reasoning traces. SmartAgent additionally carries:
  - `response_summary` — pre-computed compact history entry (if available synchronously).
  - `response_summary_task` — `asyncio.Task` for deferred summary generation (fire-and-forget postprocessing).
- `delivery_items`: `List[DeliveryItem]` — typed artifacts to deliver after the main text response.
  Populated by specialist agents; aggregated transparently by Quick/Smart delegation loops into
  the final response. `ConversationHandler` dispatches each item after sending the main message.
  See [§ 10. DeliveryItem](#10-deliveryitem-side-channel-artifact-delivery) for full details.

---

## 3. Coordination & Routing

### 3.1 AgentCoordinator

The central hub for all agent interactions.

- **Registry:** Maintains a map of active agent instances.
- **Explicit Routing:** Routes messages to a specific `agent_id`.
- **Broadcast Routing:** Finds capable agents based on intent and capabilities.
- **Parallel Execution:** Executes multiple tasks simultaneously and aggregates results.

### 3.2 Execution Flow

1. **Ingress:** `ConversationHandler` creates an `AgentMessage` and routes it to the `RouterAgent`.
2. **Triage:** `RouterAgent` classifies the intent and delegates to `Quick` or `Smart` agents.
3. **Specialization (Smart path):** `SmartResponseAgent` builds an `LLMRequest` with tools and passes it to `DelegationEngine.execute()`. The engine runs the multi-turn loop: LLM call → tool dispatch via `AgentCoordinator` → history update → repeat. Smart uses `terminal_tool="deliver_response"` to extract structured output. Post-processing converts `DelegationResult` into `SmartResponse`.
4. **Specialization (Quick path):** Same `DelegationEngine.execute()` — builds `LLMRequest` with `intent_remap={"search_web": "search_web_light"}`. Engine handles loop, dispatch, parallel execution. Quick post-processes `DelegationResult.text` via `parse_llm_response`. See [Quick Agent Delegation](../quick_agent_delegation/README.md).
5. **Specialization (Bound channel path):** `ConversationHandler` calls `coordinator.handle_delegation()` directly with the binding's intent. Bound agents (e.g. `DomainResearcherAgent`) use `DelegationEngine` for tool calling when `allowed_intents` is configured on their descriptor.
6. **Aggregation:** Results are synthesized and returned to the user.

### 3.3 ACP v2: Agent Registry Pattern

ACP v1 had SmartAgent hardcoding tool schemas per specialist (tight coupling). ACP v2 replaces this with a dynamic registry:

- **AgentDescriptor** (alias: `AgentManifest`) — unified per-agent declaration with two halves:
  - A (capabilities): `capabilities: Dict[str, ExecutionMode]`, `internal: bool` (hides from LLM)
  - B (requirements): `allowed_intents: Optional[frozenset]`, `intent_remap: Dict[str, str]`
- **AgentRegistry** maps intents → descriptors. `get_available_intents()` returns non-internal intents. `get_available_intents_for(descriptor)` filters by descriptor's `allowed_intents`.
- **SmartAgent** has 1 fixed tool: `delegate_to_specialist(intent, query, context)`. Never grows.
- **QuickAgent** uses the same tool declaration, filtered via `get_available_intents_for`. Applies `intent_remap` at dispatch time.
- **Bound agents** (e.g. `DomainResearcherAgent`) can declare `allowed_intents` on their descriptor to get a filtered tool set. Use `DelegationEngine` for multi-turn tool calling.
- **DelegationEngine** (`src/infrastructure/delegation_engine.py`) — reusable multi-turn tool-calling loop. Shared by Smart, Quick, and bound agents. Owns: loop iteration, memory-first parallel dispatch via `AgentCoordinator`, tool response history management. Does NOT own: LLM parameters (agent's `LLMRequest`), response parsing (agent post-processes `DelegationResult`).
- **AgentCoordinator** adds `handle_delegation()` — translates the generic tool call into a concrete AgentMessage routed to the right specialist.
- **ExecutionMode:** SYNC (search queries, inline result) or ASYNC (long tasks, Cloud Tasks + callback).

Adding a new agent = register `AgentDescriptor` in `main.py` + add entry to `PROTOCOL_SMART_AGENT_SELECTION` Firestore token. Zero agent code changes.

See: [Agent Registry Building Block](../agent_registry/README.md) for full details.

---

## 4. Agent Lifecycle (UserAgentFactory)

Agents are instantiated and managed per user to ensure strict data isolation and personalized configuration.

### 4.1 Per-User Isolation

- **Factory:** `UserAgentFactory` builds agents for each user using a two-tier strategy:
  - **Eager agents** — created on first request: Router, Quick, Smart, Memory, WebSearch, WebSearchLight, EmailSearch, Maps, Compute, Consolidation, Help, and conditionally Notes/Tasks.
  - **Lazy agents** — created on first delegation via `AgentFactoryPort`: DocGenerator, DocPlanner, PdfGenerator, HtmlPageGenerator, DeepResearch, ClaudeDeepResearchRunner, FileManagement. These are rarely used specialists that save ~40% of per-user initialization time.
- **Caching:** Agent instances are cached for **1 hour (TTL)** to optimize "warm starts".
- **Configuration:** 3-level inheritance (USER > ACCOUNT > SYSTEM) is resolved during instantiation. Resolved values include `semantic_search_limit`, `biographical_cache_limit`, `principles_cache_limit`, and `history_recent_full_turns` (how many recent model turns receive full context vs. compressed summary; system default: 2, applied to both Quick and Smart).

### 4.2 Lazy Agent Instantiation

Agents marked with `eager=False` in their `AgentDescriptor` are not created during `ensure_agents_for_user()`. Instead:

1. `AgentDescriptor` is registered in `AgentRegistry` at startup → intents appear in LLM tool lists.
2. On first delegation (`handle_delegation`) or ASYNC callback (`route_message`), `AgentCoordinator` detects the agent is missing and calls `AgentFactoryPort.create_agent_on_demand(agent_type, user_id)`.
3. `UserAgentFactory` (which implements `AgentFactoryPort`) creates the agent using cached `_UserContext` (user profile + prompt builder), registers it with the coordinator, and tracks its `agent_id` for eviction.
4. Subsequent delegations find the agent already registered — no overhead.

**Concurrency safety:** per-user `asyncio.Lock` in the factory prevents duplicate creation when concurrent delegations target the same lazy agent. The coordinator checks `get_agent(expected_id)` before calling the factory.

**Eviction:** lazy agents are tracked in `_lazy_agent_ids` list per user cache entry. The background TTL sweep unregisters them alongside eager agents.

### 4.3 Prompt Integration

- Agents use `UserPromptBuilder` to assemble their system instructions.
- **v3 Integration:** Supports token-based prompt assembly with security validation.
- **Preloading:** Prompts are pre-assembled and cached during agent initialization.

---

## 5. Agent Categories

### 5.1 Core Agents (Reasoning)

- **Router Agent:** Intent classification and triage.
- **Quick Agent:** Fast responses (BALANCED tier = Gemini Flash). Functionally equivalent to Smart in tool access — same non-internal intents from AgentDescriptor, with `intent_remap` substituting `search_web` → `search_web_light` at dispatch time. `MAX_DELEGATION_TURNS=5`. Prompt token `PROTOCOL_QUICK_AGENT_SELECTION` defines when and how to delegate. Outputs JSON (`full_response`, `response_summary`, `rich_content`) parsed by `parse_llm_response`. `HistorySummaryService` used only as fallback for the plain-text path. `_clean_history_for_quick` strips tool interactions from history before each LLM call.
- **Smart Agent:** Deep reasoning and specialist delegation (PERFORMANCE tier = Gemini Pro / Claude). Uses 1 generic `delegate_to_specialist(intent, query)` tool — intents resolved via AgentRegistry. After generating a response, fires async `response_summary_task` (via `HistorySummaryService`) so history compression never blocks Slack delivery. Timeout: `300s`, `max_retries=0`.

### 5.2 Specialist Agents (Tools)

- **FactsMemoryAgent** (`memory_search_agent.py`): Unified memory specialist — two intents:
  - `search_memory`: Two-phase: (1) LLM key formulation via `COGNITIVE_PROCESS_MEMORY_SEARCH` Firestore token — Gemini Flash extracts `keywords`, `primary_query`, `alternative_query`, `domains` from the delegation query; (2) multi-vector RRF search via `SearchEnrichmentService`. Schema-enforced: 3–5 keywords, 2 domains max, 50-char query limit. Reachable from both Quick and Smart.
  - `save_to_memory`: Explicit-save path. Called only on direct user request ("remember this", "save this", etc.). The LLM writes a rich, detailed, third-person passage and delegates it via `delegate_to_specialist(intent="save_to_memory", text="<passage>")`. `FactsMemoryAgent._handle_save()` returns `AgentResponse(history_context={"consolidation_text": text})` — no direct Firestore write. `ConversationHandler` reads this from `response.metadata["consolidation_text"]` and appends `MessagePart(consolidation_text=combined)` to the user message before persisting. The consolidation serializer reads `p.full_text or p.consolidation_text or p.text`, so the passage flows naturally into the next `ConsolidationBatch` → SEARCH+DECIDE pipeline. The `consolidation_text` field is invisible to all LLM adapters, which only read `text`/`full_text`/`file_data`.
- **Web Search Light Agent:** Lightweight single-pass grounding agent called exclusively by `QuickResponseAgent` via the `search_web_light` intent. ECO tier (Gemini Flash Lite), single LLM call with Google Search grounding tool, returns plain Slack mrkdwn. No multi-turn refinement. Prompt via PromptBuilder v3 (`agent_type="websearch_light"`) with inline Groovy fallback.
- **Web Search Agent:** Full-depth real-time information retrieval via Google Search grounding. Called exclusively by `SmartResponseAgent` via `search_web` intent. BALANCED tier.
- **Email Search Agent:** Email archive specialist (BALANCED tier). Called by both Quick and Smart via 3 intents registered in `AgentRegistry` (registered with `internal=False`, so both orchestrators discover it via `get_available_intents_for`):
  - `search_emails` — semantic 4-vector RRF search in `domain_email_facts_v1`. Pass `query` = user's question as-is. Result includes email ids and attachment filenames.
  - `get_email_details` — fetch full email body from Gmail API. Pass `context={"email_id": "<id from search_emails>"}`. Requires a prior `search_emails` call.
  - `get_email_attachment` — parse attachment as text via markitdown. Pass `context={"email_id": "<id>", "filename": "<filename from search_emails>"}`. Requires a prior `search_emails` call.
  Routing: `execute()` dispatches by `payload["intent"]` (not by payload key presence). Missing `email_id` returns `AgentResponse.failure()` with actionable message telling the LLM exactly what to pass.
  `context_schemas` declared in `AgentDescriptor` (agent_manifest.py) — `email_id` and `filename` are exposed as typed `context.properties` in the tool schema so Gemini populates them correctly.
  Delegates to `EmailSearchService` (indexed search) or `GmailProviderAdapter` (live fetch).
- **Maps Search Agent:** Location services specialist backed by Google Maps AI Grounding Lite via MCP protocol. Called by Quick and Smart via `maps_query` intent. Provider-agnostic (any LLM with `native_tools` capability; Gemini default). Runs a multi-turn LLM ↔ MCP tool loop (max 4 turns): LLM selects which tool(s) to call → agent executes via `MapsToolsPort` → LLM formats final response. Three tools available: `places_search` (name, address, rating, hours, Maps URL), `route_computation` (distance + duration; no turn-by-turn), `weather_lookup` (current conditions + forecast). Tool calls within one turn are executed in sequence. System prompt is a hardcoded `_SYSTEM_INSTRUCTION` constant (tech debt — no Firestore profile yet). Returns plain text response. No `html_gcs_link` (widget token delivery was non-functional, removed). See [MCP_INFRASTRUCTURE_RFC.md](../../10_rfcs/MCP_INFRASTRUCTURE_RFC.md).
- **Compute Agent:** Precise calculation specialist. Called by Quick and Smart via four typed intents (`compute_math`, `compute_datetime`, `compute_finance`, `compute`). BALANCED tier (Gemini Flash). Single `LLMRequest(use_code_execution=True, ...)` — Gemini writes and executes Python code in a sandbox; `GeminiAdapter` injects `types.Tool(code_execution=...)` internally (agent stays provider-agnostic). Standard library only (math, datetime, statistics, decimal, fractions) — no network, no pip packages. Returns plain text result. Honest failure protocol: if the task requires live data (exchange rates, stock prices), the agent explicitly reports what is missing and defers to `web_search_agent`. See [COMPUTE_AGENT_RFC.md](../../10_rfcs/COMPUTE_AGENT_RFC.md).
- **Deep Research Agent:** Provider-agnostic Deep Research specialist. Called by Smart only via `deep_research` intent. Execution mode: SYNC ACK — calls `DeepResearchPort.create_interaction(query, user_id, account_id, original_query, tier, system_prompt)` and returns immediately. No `task_queue` or model name in the agent — delivery and model selection are adapter-internal. Constructor: `(config, job_port, tier, prompt_builder, user_id)` — standard specialist pattern. Two backends: `ClaudeDeepResearchAdapter` (default — enqueues `agent_execution` Cloud Task → `ClaudeDeepResearchRunnerAgent` runs multi-turn loop with native tools in a Cloud Run Job) and `OpenAIDeepResearchAdapter` (webhook-based push delivery via `/webhooks/openai/deep-research`, no polling; PERFORMANCE tier → o3, others → o4-mini). Adapters accept optional `model_override` constructor param (env var) to pin a specific model. `system_prompt` assembled from PromptBuilder profile `deep_research`. On completion: both paths deliver via shared `deliver_deep_research()` from `src/services/deep_research_delivery.py` — (1) upload raw `.md` round files to GCS (`deep_research/{user_id}/{timestamp}-{suffix}.md`) and send a named `notify_document_link()` per round ("Round 1 — raw research" / "Round 2 — verified report" in two-pass mode, or "Research report (raw)" in single-pass); (2) enqueue `create_html_page` Cloud Task → `HtmlPageGeneratorAgent` → styled single-file HTML page → GCS public link → user channel. Requires `GCS_MEDIA_BUCKET`. Preparation is prompt-driven via `PROTOCOL_DEEP_RESEARCH_PREP` Firestore token in Smart's profile. The earlier `GeminiDeepResearchAdapter` (polling Cloud Tasks) was removed 2026-05-29 due to zero production usage and Gemini's Interactions API v2 breaking-change cycle — see [decisions/gemini_deep_research_adapter_removal.md](../../04_solution_strategy/decisions/gemini_deep_research_adapter_removal.md). See [DEEP_RESEARCH_RFC.md](../../10_rfcs/DEEP_RESEARCH_RFC.md).
- **Tasks Agent:** Personal task management specialist backed by Microsoft To Do (Graph API). Called by both Quick and Smart via the single intent `manage_user_tasks`. Architecture:
  - **Single intent design:** The orchestrator delegates a natural-language instruction (e.g. "Find tasks about milk", "Add task: buy flowers, due tomorrow") — the agent autonomously selects the right CRUD operation. No intent per operation — one smart endpoint.
  - **Tool-calling loop (max 4 turns):** The LLM chooses from 5 tools (`list_tasks`, `search_tasks`, `create_task`, `update_task`, `delete_task`). Tool calls are executed against `TasksProviderPort` (MS Graph API). Loop terminates when LLM produces a final text response.
  - **Search-before-mutate:** For `update_task` and `delete_task`, the LLM first calls `search_tasks` to find the `task_ref` (short_id), then proceeds. Handles 0 / 1 / many results gracefully.
  - **task_ref (short_id):** LLM receives an 8-char short_id (`md5(task_id)[:8]`) as `task_ref` in search results — never the full 180-char MS Graph task ID. `update_task` and `delete_task` accept `task_ref`; agent resolves to `(list_id, task_id)` via `TaskIndexingService.resolve_short_id()`.
  - **Semantic search:** `search_tasks` embeds the query and searches `task_search_index` in Firestore (RRF across `content_vector` + `context_vector`), then `batch_get_tasks` fetches full Task objects from Graph API.
  - **Recurrence support:** `create_task` accepts a `recurrence` object with 5 patterns: `daily`, `weekdays` (Mon–Fri alias), `weekly`, `absoluteMonthly`, `absoluteYearly`. Smart defaults derived from `due_datetime` when optional fields omitted.
  - **Biographical context:** `include_biographical=True` — personal references ("buy something for the flat move") resolved against user facts.
  - **Auto-tagging:** LLM instructed to derive MS To Do categories from task context (e.g. "remind me about Prague hotel" → tags: ["prague", "trip"]).
  - **Language:** Delegation queries and responses use the orchestrator's active language.
  - **Dedicated tasklist:** `MicrosoftToDoAdapter.ensure_primary_list()` creates "Alek Bot Tasks" on first use; `list_id` cached per user in `task_config` Firestore collection.
  - **Auth:** OAuth2 via `OAuthCredentialsPort`, provider `"microsoft_todo"` (Azure consumers tenant, `Tasks.ReadWrite offline_access`). Cabinet UI: `/auth/connect-microsoft-todo`.
  - **Webhook sync:** Graph subscriptions keep `task_search_index` fresh when user edits tasks directly in the MS To Do app. Worker task `reindex_task_list` reindexes after subscription setup.
  - **Parallel delegation:** Multiple task-related requests in one message → multiple parallel `delegate_to_specialist` calls, each with its own tool loop.
  - Registered as `internal=False`. BALANCED tier (Gemini Flash default). See [Tasks Integration Building Block](../tasks_integration/README.md).
- **Notes Agent (Proactive Self-Reminders):** Specialist for deferred self-instructions that fire proactively via Cloud Scheduler. Intent: `manage_self_reminders`. Uses one LLM call (ECO tier, Gemini default) to parse natural language → CRUD via `AgentNotePort`. See [PROACTIVE_SELF_REMINDERS_RFC.md](../../10_rfcs/PROACTIVE_SELF_REMINDERS_RFC.md) for full design.
  - **Two-field model:** `text` (≤15-word display label) + `instruction` (complete execution context, no limit). Only `instruction` is used when the reminder fires — it must be fully self-contained.
  - **Firing mechanism:** Cloud Scheduler calls `POST /worker {task_type: fire_due_reminders}` every 15 min → `WorkerHandler._handle_fire_due_reminders()` → `AgentNotePort.list_due_reminders(as_of=now)` → `_build_reminder_alert(note)` enriches the instruction with note_id, schedule type, self-authorship framing, and proactive guidance (conversation history as primary signal + available intents) → `UserNotificationService.notify(system_alert=..., agent_id_override=smart_response_agent_{user_id})` → SmartAgent formats and delivers to user's channel. Fired conversations are saved to session history (both turns).
  - **Recurrence:** `ReminderRecurrence(type, interval)` — `hourly/daily/weekly/monthly`. After firing: `AgentNotePort.reschedule(note_id, next_due, last_fired)`. One-time reminders are deleted after firing.
  - **Idempotency:** `last_fired` guard — skip if fired within the last 14 min (cron overlap protection).
  - **Context in prompt:** `active_reminders {}` block injected after `PROMPT_CACHE_BOUNDARY` by `PromptAssemblyService`. Orchestrator sees `text + fires: <datetime> [id]` summary. NotesAgent sees full `text + instruction + due + recurrence` block loaded directly in `_run()`.
  - **Biographical context:** NotesAgent calls `build_for_agent(include_biographical=True)` — user facts included in prompt.
  - **Timezone:** stored in `UserBotConfig.timezone` (IANA string, set via Cabinet UI). Used for: datetime in all prompts (user-local), `due` UTC conversion in NotesAgent, `next_due` recurrence computation, transparency notification formatting.
  - **Transparency:** every create/update/delete sends `notify_raw()` to user's channel immediately. Soft cap: 20 active (alert in result). Hard cap: 30 (adapter-level exception).
  - **Persistence:** `{env_prefix}orchestrator_notes` Firestore collection. Document ID = epoch milliseconds. Firestore index on `due ASC` for efficient `list_due_reminders` query. Port: `AgentNotePort` (6 methods: `create_note`, `delete_note`, `update_note`, `list_active_notes`, `list_due_reminders`, `reschedule`). Adapter: `FirestoreAgentNoteAdapter`.
- **Document Planner Agent:** Two-phase DOCX creation entry point. Called by both Quick and Smart via `create_document` intent (`ExecutionMode.ASYNC` → Cloud Task dispatch). PERFORMANCE tier (Claude default). Phase 1: LLM generates a structured JSON layout spec (`{status, task_summary, doc_spec}`), enforced via `_RESPONSE_SCHEMA` (Gemini) or `OUTPUT_FORMAT` token (Claude). Phase 2: delegates to `DocGeneratorAgent` via `coordinator.handle_delegation(Intent.GENERATE_DOCX_CODE, ...)`. Retry loop (max `MAX_RETRIES=3`): `JSONDecodeError` → LLM self-corrects JSON; generator failure → LLM patches the spec; `status != "ready"` → immediate failure (unrecoverable planner refusal). On success: forwards `DocGeneratorAgent`'s `DeliveryItem("file_upload", {...})` up to `AgentWorkerHandler` which calls `notify_file_bytes()`. Registered `internal=False`. System prompt: PromptBuilder profile `doc_planner`. Accepts both `QUERY` (sync / test path) and `DELEGATE` (normal async Cloud Task path) intents in `can_handle()`.
- **Document Generator Agent:** Internal DOCX code generation specialist (`internal=True` — never shown to LLMs). Called exclusively by `DocPlannerAgent` via `generate_docx_code` intent. PERFORMANCE tier (Claude default). Receives a JSON layout spec in `payload["query"]`; LLM writes a Node.js script using the `docx` npm library and calls the `generate_docx` tool. Script is executed via `DocxRunnerPort` (system boundary — subprocess isolation). Retry loop (max `MAX_TURNS=5`): on `DocxRunnerError` → `stderr` returned as tool response, LLM retries with a corrected script; no tool call → immediate failure. On success: returns `DeliveryItem("file_upload", {"file_bytes_b64": ..., "filename": ..., "title": ...})`. `DocxRunnerPort` has one implementation: `NodeDocxRunner` (writes temp script to `docx_generator/` so `node_modules/docx` resolves, reads DOCX bytes from stdout). Future implementations (Cloud Function, remote runner) require no agent changes. System prompt: PromptBuilder profile `doc_generator`. See [Document Generation Building Block](../document_generation/README.md).
- **PDF Generator Agent:** PDF creation specialist. Called by both Quick and Smart via `create_pdf` intent (`ExecutionMode.ASYNC` → Cloud Task dispatch). BALANCED tier (Gemini, `agent_type="pdf_generator"`, `internal=False`). Single LLM call: natural language → complete HTML+CSS document. System prompt from PromptBuilder (profile `pdf_generator`); style catalogue (12 design systems) instructs LLM to auto-select appropriate style. HTML rendered to PDF via `PuppeteerRunnerPort` (Node.js subprocess, `pdf_generator/runner.js`). Filename extracted from `<title>` tag. Returns two `DeliveryItem("document", ...)` items — HTML (`file_upload=False`, GCS only) and PDF (`file_upload=True`, GCS + Slack upload). See [Document Generation Building Block](../document_generation/README.md).
- **HTML Page Generator Agent:** Single-page web layout specialist. Called by both Quick and Smart via `create_html_page` intent (`ExecutionMode.ASYNC` → Cloud Task dispatch). PERFORMANCE tier (Gemini, `agent_type="html_page"`, `internal=False`). Single LLM call: natural language → complete HTML+CSS+JS document. No Node.js subprocess — HTML is the final artifact. System prompt from PromptBuilder mandatory (profile `html_page`); enforces mobile-first design, fluid type scale, CSS custom properties, and `IntersectionObserver` scroll animations. Filename from `<title>` tag. Returns one `DeliveryItem("document", ...)` — HTML (`file_upload=False`, GCS public URL → Slack link). See [Document Generation Building Block](../document_generation/README.md) §11.
- **File Management Agent:** Zero-LLM file storage archivist. Two intents: `open_file` (SYNC — download from GCS + convert to text, or return binary via `file_data` metadata for LLM vision) and `delete_file` (SYNC — remove from GCS). No LLM calls — direct `FileConversionService` and `FileStoragePort` operations. Context schema: `file_ref` (filename from `[File: name (size)]` label). See [File Storage Building Block](../file_storage/README.md).
- **Consolidation Agent:** Background synthesis of conversation history into facts.

### 5.3 Infrastructure Agents

- **Billing Agent:** Quota enforcement and usage tracking.
- **Logger Agent:** Centralized structured logging and trace correlation.

---

## 6. Resilience Mechanisms

### 6.1 Circuit Breaker

Every agent is protected by a `CircuitBreaker` to prevent cascading failures.

- **Threshold:** 3 consecutive failures.
- **State:** Opens for **5 minutes** (recovery timeout).
- **Action:** Requests to an "Open" agent are immediately rejected with `FAILED` status.

### 6.2 Retry Logic

- **Strategy:** Exponential backoff (1s, 2s, 4s...).
- **Limit:** 2 retries by default (3 total attempts). **Exception: SmartAgent uses `max_retries=0`** — retrying a thinking model that timed out doubles wall-time to 480s+ and provides no UX benefit.
- **Timeout:** Message-level timeout overrides agent-level configuration. SmartAgent: `timeout_ms=300000` (5 min) to cover worst-case 3-turn reasoning cycles on large sessions and slow PDF attachment parsing. Previous value (150s) was too close to the boundary: Claude API required 149.7s under load. All timeout values centralized in `src/infrastructure/agent_config.py`.

---

## 7. SmartAgent: Fire-and-Forget Postprocessing

Thinking models (gemini-pro-preview) are expensive to call twice. History compression runs as an async background task so the user receives a response immediately:

```
SmartAgent.execute()
  │
  ├─ await LLM multi-turn reasoning  (~13–90s depending on context size)
  │
  ├─ asyncio.create_task(_generate_history_summary(response_text))
  │      └─ delegates to HistorySummaryService.summarize_model_response()
  │         → Gemini Flash (BALANCED tier), response_schema JSON, ≤300 chars
  │         runs CONCURRENTLY with Slack delivery below
  │
  └─ return AgentResponse(metadata={"response_summary_task": task})

ConversationHandler
  │
  ├─ await response_channel.send(text)   ← user sees this immediately
  │
  └─ summary = await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
       └─ history_text = summary if ENABLE_HISTORY_OPTIMIZATION else full_text
```

**Key invariants:**
- `asyncio.shield()` prevents cancellation if ConversationHandler times out before the task finishes.
- Fallback: if the summary task fails or exceeds 10s, full response text is saved to session history (no data loss). Failure is logged as `WARNING`.
- `HistorySummaryService` is always Gemini — provider locked at composition time, immune to `provider_preference: "claude"/"grok"`. This is required because `response_schema` (structured JSON output) is a Gemini-only feature; other adapters silently ignore it, causing JSON parse failures.
- Fail-fast: one attempt, no retry. Under `asyncio.shield` + 10s window, a retry only wastes time with no UX benefit.
- The `response_summary` key is the single standard across both agents: SmartAgent postprocessing output, QuickAgent JSON field, and `parse_llm_response` parser key are all named `response_summary`.
- `HistorySummaryService` is injected into `SmartResponseAgent` via constructor. Designed to be reused by other agents (e.g., `QuickResponseAgent`) without code duplication.

**Environment flag:** `ENABLE_HISTORY_OPTIMIZATION=true` (default: `false`). When disabled, full response text is always stored — safe fallback for debugging. Read once at import time via `agent_config.ENABLE_HISTORY_OPTIMIZATION` — agents never call `os.getenv()` directly.

---

## 8. Graceful Degradation: AgentFallbackService

When any agent returns `TIMEOUT` or `FAILED`, the system never exposes a raw error to the user.
`ConversationHandler` delegates to `AgentFallbackService`, which guarantees a displayable response
via a three-level chain:

```
ConversationHandler.handle_message()
  │
  ├─ await coordinator.route_message(message → router_agent)
  │      └─ Primary agent: TIMEOUT or FAILED
  │
  └─ await fallback_service.try_quick_fallback(response, context, message_parts)
         │
         ├─ Level 1: route to quick_response_agent_{user_id} directly (bypasses Router)
         │      current_message_parts += MessagePart(text="[System: ...apologize, no technical details...]")
         │      └─ QuickAgent: SUCCESS → returned as the final response
         │
         ├─ Level 2: QuickAgent also fails / raises Exception
         │      └─ AgentFallbackService catches — no re-raise
         │
         └─ Level 3: synthetic AgentResponse.SUCCESS(result=apology_text)
                └─ ConversationHandler sees SUCCESS, sends apology as normal message
                   User NEVER sees ERROR status or a stack trace
```

**Key design decisions:**
- `AgentFallbackService` lives in `src/services/agent_fallback_service.py`. Uses `MessageRouter`
  Protocol (not concrete `AgentCoordinator`) to avoid services/ → infrastructure/ import violation.
  `ConversationHandler` instantiates it from the `coordinator` in `__init__` — no external wiring needed.
- All three levels guarantee `AgentStatus.SUCCESS` to the caller — the `send_status(ERROR)` path
  in `ConversationHandler` is only reachable if `AgentFallbackService` itself has a bug.
- `[System: ...]` note instructs the LLM to acknowledge the delay gracefully in the bot's own voice
  and to never mention technical details or error codes.
- Synthetic apology text (`_APOLOGY_TEXT`) is a class-level constant on `AgentFallbackService` —
  single place to change the last-resort message.

**Tests:** `tests/unit/handlers/test_conversation_handler_fallback.py` (7 tests).

---

## 9. Session History Persistence: Specialist Results and Rich Content

### 9.1 Principle

Specialists decide what gets persisted — not orchestrators. `AgentResponse.history_context`
is a domain field set by specialist agents when they want their result available to the LLM in
subsequent turns. Orchestrators accumulate these generically; `ConversationHandler` saves them
generically. No coupling to specific intent names at any layer.

### 9.2 Mechanism

**Specialist agents** set `history_context` on their `AgentResponse.success(...)`:

```python
return AgentResponse.success(
    ...,
    history_context={"web_search_context": {"query": query, "result": result_text}},
)
```

**Orchestrators** (Quick/Smart) accumulate generically during the delegation loop:

```python
accumulated_history: Dict[str, List[Any]] = {}
if response.history_context:
    for key, value in response.history_context.items():
        accumulated_history.setdefault(key, []).append(value)
# → metadata["web_search_context"] = [...]
# → metadata["email_search_context"] = [...]
```

**ConversationHandler** appends all `*_context` keys from metadata plus `rich_content`:

```python
# Tool results — any key ending in "_context"
for ctx_key, ctx_value in (response.metadata or {}).items():
    if ctx_key.endswith("_context") and ctx_value:
        response_text += "\n\n" + json.dumps({ctx_key: ctx_value}, ...)

# Rich content — structured data delivered to the user
if structured_data:
    response_text += "\n\n" + json.dumps(
        {"rich_content": {"type": structured_data.content_type, "data": structured_data.data}}, ...
    )
```

All blocks are appended to `full_text` only (not `history_text`/summary). History tiering
(`BaseAgent._apply_history_tier()`, system default `history_recent_full_turns=2` → 3 full turns in practice) controls visibility: recent turns
send the full blocks; older turns use the compressed `text` field.

### 9.3 Active Context Types

| Key | Set by | Purpose | LLM-visible? |
|---|---|---|---|
| `web_search_context` | `WebSearchAgent`, `WebSearchLightAgent` | LLM sees search results in follow-up turns — avoids re-searching | Yes (`full_text`) |
| `email_search_context` | `EmailSearchAgent` | LLM references email IDs for `get_email_details`/`get_email_attachment` without re-searching | Yes (`full_text`) |
| `rich_content` | `ConversationHandler` (from `structured_data`) | LLM sees the table/card delivered to the user — enables follow-up questions about the data | Yes (`full_text`) |
| `consolidation_text` | `FactsMemoryAgent` (`save_to_memory` intent) | Rich passage attaches to user message for consolidation serializer only — **never LLM-visible** | No (adapters never read `MessagePart.consolidation_text`) |

**`consolidation_text` is a special case.** Unlike `*_context` keys (which are serialized as JSON into the model message's `full_text`), `consolidation_text` is written into `MessagePart.consolidation_text` on the *user* message and read only by the consolidation serializer (`p.full_text or p.consolidation_text or p.text`). It does not appear in any LLM call. See [§ 9.8](#98-consolidation_text-special-handling) for details.

### 9.4 Email Search Context Format

```json
{"email_search_context":[{"you_searched":"invoices from Google","you_received":[{"id":"19caa50e57fca7dc","from":"Google Payments <noreply@google.com>","date":"2026-03-01","summary":"Invoice #123 for $29.99"}]}]}
```

### 9.5 Adding a New Context Type

Set `history_context` in the specialist agent's `AgentResponse.success()` — no other changes
needed. The key must end in `_context` to be picked up by ConversationHandler automatically.

**Exception:** `consolidation_text` does NOT end in `_context` and has dedicated handling in
`ConversationHandler`. It is not serialized into LLM history — it is attached as
`MessagePart(consolidation_text=combined)` to the user message for the consolidation pipeline only.
See [§ 9.8](#98-consolidation_text-special-handling).

### 9.6 Prompt Side

`PROTOCOL_SMART_AGENT_SELECTION` and `PROTOCOL_QUICK_AGENT_SELECTION` tokens must explain
each context type so the LLM knows how to use prior results:

```
When you see email_search_context in history — use email_id directly for get_email_details.
When you see web_search_context in history — reference the cached result before searching again.
When you see rich_content in history — you know what structured data was shown to the user.
```

### 9.7 Implementation

- `src/domain/agent.py` — `AgentResponse.history_context: Optional[Dict[str, Any]]`
- `src/agents/web_search_agent.py`, `web_search_light_agent.py` — set `web_search_context`
- `src/agents/email_search_agent.py` — set `email_search_context` via `_build_email_history_context()`
- `src/agents/memory_search_agent.py` — `FactsMemoryAgent._handle_save()` sets `consolidation_text`
- `src/agents/core/quick_response_agent.py` — generic `accumulated_history` in `_execute_quick_delegation_loop`
- `src/agents/core/smart_response_agent.py` — same pattern in `_execute_smart_delegation_loop`
- `src/handlers/conversation_handler.py` — generic `*_context` loop + `rich_content` append + dedicated `consolidation_text` block

### 9.8 `consolidation_text` Special Handling

**Problem context:** When the user sends a file (image/PDF), the LLM processes it natively. After
the turn, the binary `file_data` part is dropped from history — the user message has no `text`
parts. The consolidation serializer iterates `p.full_text or p.text`, producing an empty user turn.
`ConsolidationAgent`'s `Domain_Scope` policy ("NEVER process ASSISTANT statements as facts unless
USER confirms with NEW information") then discards the model's description of the file. Facts
extracted from files are lost from long-term memory unless the user re-states them in text.

**`save_to_memory` intent solves this** — and the general case of any fact the user wants
explicitly preserved regardless of whether it came from a file, a live search result, or any
source that wouldn't normally survive consolidation.

**Flow:**

```
User: "remember that my weight is 80 kg"
  ↓
Quick/Smart LLM: delegate_to_specialist(
  intent="save_to_memory",
  query="Save user weight fact",
  context={"text": "User mentioned their current weight is 80 kg. Came up in diet
    discussion. January 2026 tracking start date."})
  ↓
AgentCoordinator._execute_sync():
  → context_schemas enforces typed context object for save_to_memory
  → extra_payload = context["params"]  →  AgentMessage.payload["text"] = "<passage>"
  ↓
FactsMemoryAgent._handle_save()
  → text = payload.get("text") or payload.get("query")
  → AgentResponse.success(history_context={"consolidation_text": "<passage>"})
  ↓
Quick._execute_quick_delegation_loop() / Smart._execute_smart_delegation_loop()
  → accumulated_history["consolidation_text"] = ["<passage>"]
  → response.metadata["consolidation_text"] = ["<passage>"]
  ↓
ConversationHandler (before _save_history_with_retry):
  → consolidation_texts = response.metadata.get("consolidation_text", [])
  → clean_message_parts.append(MessagePart(consolidation_text=combined))
  ↓
FirestoreSessionStore._serialize_part():
  → {"consolidation_text": combined}   ← persisted to Firestore session history
  ↓
Consolidation serializer (overflow_callback / $consolidate):
  → "parts": [{"text": p.full_text or p.consolidation_text or p.text} ...]
  → passage lands in ConsolidationBatch as user-side content
  ↓
ConsolidationAgent SEARCH+DECIDE:
  → Domain_Scope satisfied (it IS a user message) → fact extracted → Firestore
```

**Key invariant:** `MessagePart.consolidation_text` is never read by any LLM adapter
(`GeminiAdapter`, `ClaudeAdapter`, `GrokAdapter`) — they only access `text`, `full_text`,
and `file_data`. The field is 100% consolidation-pipeline-only.

---

## 10. DeliveryItem: Side-Channel Artifact Delivery

### 10.1 Purpose

`DeliveryItem` is a side-channel for structured artifacts that a specialist agent wants delivered
to the user in addition to — or instead of — a plain text response. It decouples content production
(specialist) from content delivery (ConversationHandler) without burdening the orchestrators with
any knowledge of content structure.

**Core principle (hexagonal):** The sender (specialist agent) constructs the complete payload and
decides what the item means. Orchestrators (Quick/Smart, ConversationHandler) dispatch by type rule
only — they never inspect or transform `data`.

### 10.2 Data Structure

```python
@dataclass
class DeliveryItem:
    type: str               # routing key — determines dispatch path
    data: Dict[str, Any]    # opaque payload; structure owned entirely by the sender
```

`DeliveryItem` is intentionally generic. The `type` field is the only thing receivers act on.

### 10.3 Known Types and Data Contracts

| type | Sender | `data` contract | Dispatch action |
|------|--------|-----------------|-----------------|
| `"message"` | Any specialist | `{"text": str}` — Slack mrkdwn, may contain `<url\|label>` links | `response_channel.send_message(data["text"], thread_id)` |
| `"rich_content"` | Any specialist | `{"content_type": str, "data": dict, "fallback": str}` — same schema as `RichContent` domain object | Constructs `RichContent` → `_deliver_rich_content(...)` |
| `"html_gcs_link"` | DeepResearchAgent | `{"html": str, "filename": str, "link_text": str}` | Uploads HTML to GCS → sends public URL as Slack link |
| `"file_upload"` | DocGeneratorAgent | `{"file_bytes_b64": str, "filename": str, "title": str}` — base64-encoded binary, name with extension (e.g. `report-2026-03-12.docx`), human-readable title | **ASYNC path** (Cloud Task): `AgentWorkerHandler._deliver_docx_result()` decodes bytes → `notify_file_bytes()` → resolves channel ID → `PlatformMediaPort.upload_file()`. **SYNC path** (ConversationHandler `_deliver_item()`): decodes bytes → `RichContentService.upload_file_bytes()` via current `response_channel.channel_id`. |
| `"document"` | PdfGeneratorAgent, HtmlPageGeneratorAgent | `{"content_b64": str, "filename": str, "content_type": str, "label": str, "file_upload": bool}` — base64-encoded content (HTML or PDF), MIME type, human-readable label, and a flag controlling whether to also upload binary to Slack (`True` for PDF, `False` for HTML and HTML page). | `AgentWorkerHandler` decodes bytes → `DocumentDeliveryService.store(bytes, filename)` → GCS upload (key: `docs/{uuid}-{filename}`). If `file_upload=True`: additionally calls `notify_file_bytes()` to deliver the binary to the user's Slack channel. |

### 10.4 Aggregation Path

Specialist agents return `delivery_items` on their `AgentResponse`. Quick and Smart accumulate
them transparently during their delegation loops — no per-type logic, no content inspection:

```python
# QuickResponseAgent / SmartResponseAgent — delegation loop
all_delivery_items: list[DeliveryItem] = []
...
tool_response = await coordinator.handle_delegation(...)
all_delivery_items.extend(tool_response.delivery_items)
...
return AgentResponse.success(..., delivery_items=all_delivery_items)
```

### 10.5 Dispatch in ConversationHandler

`ConversationHandler._deliver_item()` is the single dispatch point. It reads `item.type` and
applies the matching rule. New types are added here only — all other layers are unchanged:

```python
async def _deliver_item(self, item: DeliveryItem, response_channel, thread_id):
    if item.type == "html_gcs_link":
        ...
    elif item.type == "rich_content":
        content = RichContent(
            content_type=item.data["content_type"],
            data=item.data["data"],
            fallback_text=item.data.get("fallback", ""),
        )
        await self._deliver_rich_content(content, response_channel, thread_id)
    elif item.type == "message":
        await response_channel.send_message(item.data["text"], thread_id)
    elif item.type == "file_upload":
        # Sync delivery path (non-ASYNC agents). ASYNC agents (DocPlannerAgent) are
        # delivered by AgentWorkerHandler._deliver_docx_result() → notify_file_bytes() instead.
        file_bytes = base64.b64decode(item.data["file_bytes_b64"])
        await self._rich_content_service.upload_file_bytes(
            file_bytes=file_bytes,
            filename=item.data["filename"],
            title=item.data["title"],
            channel_id=response_channel.channel_id,
        )
```

Multiple `DeliveryItem`s are dispatched in order after the main text response.

### 10.6 Current Usage

`DeliveryItem` is used by three specialist agents:

- **`DeepResearchAgent`** — `html_gcs_link` type. Delivers HTML research report URLs uploaded to GCS. Dispatched synchronously by `ConversationHandler._deliver_item()` (the agent is SYNC ACK — it acknowledges immediately; actual delivery happens via webhook/polling adapters, not through `delivery_items`).

- **`DocGeneratorAgent`** — `file_upload` type. Carries base64-encoded DOCX bytes. Delivery path: `AgentWorkerHandler._deliver_docx_result()` (ASYNC Cloud Task) → decodes bytes → `UserNotificationService.notify_file_bytes()` → `response_channel.send_message("📎")` (resolves Slack user ID → DM channel ID) → `PlatformMediaPort.upload_file()`. `ConversationHandler._deliver_item()` also handles `file_upload` via `RichContentService.upload_file_bytes()` for any future sync usage.

- **`PdfGeneratorAgent`** — `document` type. Returns two items per successful generation: HTML source (`file_upload=False`) and PDF binary (`file_upload=True`). Both are stored to GCS via `DocumentDeliveryService` (key: `docs/{uuid4()}-{filename}`). The PDF item additionally triggers `notify_file_bytes()` to deliver the binary file to the user's Slack channel.

- **`HtmlPageGeneratorAgent`** — `document` type. Returns one item: HTML source (`file_upload=False`, `content_type="text/html; charset=utf-8"`). Stored to GCS via `DocumentDeliveryService`. Only `notify_document_link()` is called — no binary upload to Slack. `notify_document_link()` additionally saves a user/model history pair (`[System: async document ready — {label}]` + URL note with `fetch_url` hint) so the agent can re-fetch the report if the user asks about it later. Delivery intent: `CREATE_HTML_PAGE` in `AgentWorkerHandler._deliver_document_result()`.

`MapsSearchAgent` does not use `DeliveryItem` — place links are embedded directly in the LLM
response text as Slack mrkdwn `<placeUrl|Name>` links. The `_SYSTEM_INSTRUCTION` in the agent
instructs the LLM to use `googleMapsLinks` fields from the `search_places` tool result for this
formatting. `places[i]` in the tool result corresponds to the i-th place cited in `summary`.

### 10.7 Adding a New DeliveryItem Type

1. Decide the `type` string (lowercase snake_case).
2. Define the `data` contract in the specialist agent (document it in the agent file and here).
3. Add one `elif item.type == "..."` branch in `ConversationHandler._deliver_item()`.
4. No changes to Quick, Smart, AgentCoordinator, or AgentResponse — they are type-agnostic.

---

## 11. Code References

- `src/domain/agent.py`: ACP definitions (Message, Response, Config).
- `src/infrastructure/agent_config.py`: Central registry of tunable behavior parameters (context windows, delegation turns, temperatures, timeouts). All agent class constants are sourced from here.
- `src/infrastructure/agent_coordinator.py`: Routing, parallel execution, and `handle_delegation()`, `get_available_intents_for()` (ACP v2).
- `src/infrastructure/agent_registry.py`: AgentDescriptor (alias: AgentManifest), AgentRegistry, ExecutionMode (ACP v2).
- `src/agents/base_agent.py`: Base class with resilience primitives + lifecycle hooks (`_on_agent_start`, `_on_agent_success`, `_on_agent_error`, `_on_delegation`) + `_call_llm()` (single debug logging entry point — logs request + response automatically).
- `src/agents/core/quick_response_agent.py`: Delegation loop, `MAX_DELEGATION_TURNS=5`, `_INTENT_REMAP`, memory-first parallel scheduling, JSON output.
- `src/agents/core/smart_response_agent.py`: `delegate_to_specialist` tool + memory-first parallel scheduling.
- `src/agents/web_search_light_agent.py`: Lightweight grounding specialist (`internal=True`, Quick path via remap).
- `src/agents/memory_search_agent.py`: LLM key formulation + `MEMORY_SEARCH_RESPONSE_SCHEMA`.
- `src/handlers/agent_worker_handler.py`: ASYNC task execution from Cloud Tasks. `_deliver_docx_result()` dispatches `file_upload` delivery items from DocPlannerAgent.
- `src/agents/doc_planner_agent.py`: Two-phase DOCX creation specialist (intent: `create_document`, ASYNC).
- `src/agents/doc_generator_agent.py`: Internal Node.js DOCX code generation specialist (intent: `generate_docx_code`, ASYNC, `internal=True`).
- `src/ports/docx_runner_port.py`: `DocxRunnerPort` ABC + `DocxRunnerError` — system boundary for subprocess execution.
- `src/adapters/node_docx_runner.py`: `NodeDocxRunner` — runs Node.js scripts in `docx_generator/`, captures stdout as DOCX bytes.
- `src/agents/pdf_generator_agent.py`: PDF creation specialist (intent: `create_pdf`, ASYNC, BALANCED tier, `internal=False`). Single LLM call → HTML+CSS → Puppeteer rendering.
- `src/agents/html_page_generator_agent.py`: HTML page creation specialist (intent: `create_html_page`, ASYNC, PERFORMANCE tier, `internal=False`). Single LLM call → HTML+CSS+JS → GCS public URL. No subprocess, no Slack file upload.
- `src/ports/puppeteer_runner_port.py`: `PuppeteerRunnerPort` ABC — system boundary for Node.js Puppeteer subprocess.
- `src/adapters/node_puppeteer_runner.py`: `NodePuppeteerRunner` — pipes HTML to `pdf_generator/runner.js` via stdin, captures PDF bytes from stdout.
- `src/services/document_delivery_service.py`: `DocumentDeliveryService` — stores document bytes to GCS via `MediaStoragePort` (key: `docs/{uuid4()}-{filename}`). Used by both PdfGeneratorAgent and HtmlPageGeneratorAgent.
- `src/composition/user_agent_factory.py`: Lifecycle and DI management.
- `src/services/history_summary_service.py`: LLM-based response compression (Gemini-locked, fail-fast).
- `src/utils/llm_response_parser.py`: Unified JSON parser for `full_response` + `response_summary` + `rich_content`. Guards against mistaking embedded JSON examples for response envelopes.
- `src/utils/debug_logger.py`: `PromptDebugLogger` — centralized debug prompt/response/output logging (GCS or local). Used exclusively via `BaseAgent._debug_prompt` / `_debug_response`.
- `src/handlers/conversation_handler.py`: Fire-and-forget summary task + delegates graceful degradation to `AgentFallbackService`.
- `src/services/agent_fallback_service.py`: `AgentFallbackService` — three-level fallback chain (QuickAgent → synthetic apology). Guarantees `AgentStatus.SUCCESS` to caller.
- Building Block: [Quick Agent Delegation](../quick_agent_delegation/README.md)

---

## 12. Status

**Status:** ✅ Production Ready
**Last Updated:** 2026-03-29

---
