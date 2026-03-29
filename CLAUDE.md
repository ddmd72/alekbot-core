# Alek-Core

Personal exocortex — a knowledge management system powered by LLMs.
Solo developer. Production on GCP (Cloud Run + Firestore).

## Commands

```bash
make check             # Quick check: unit tests + domain purity
make test              # All tests
make test-unit         # Unit tests
make test-integration  # Integration tests
make test-e2e-all      # E2E all agents
make dev               # Local run (Socket Mode)
make dev-emulator      # With Firestore emulator
make deploy-dev        # Deploy to dev
make deploy            # Deploy to prod
```

Linter and formatter not yet configured (Milestone 4).

## What and Why

Exocortex — AI extension of memory and thinking via Slack/Telegram.
Not a chatbot. A system that remembers, thinks in the background, and responds to the point.

**Cycle:** user speaks → bot responds using accumulated knowledge →
background process extracts new facts from the conversation → bot gets smarter.

## Key Mechanisms

**Multi-agent network** — not one LLM for everything, but specialists:
- Router (Gemini) — LLM triage on every request: classifies complexity (1–6 → Quick, 7–10 → Smart),
  extracts semantic lens and search intent, triggers memory/web enrichment before routing.
  Rule-based `_classify_request` is a fallback only (LLM unavailable or failed).
  Confidence safety net: low confidence always falls back to Smart.
  Vision (file attachments): forces complexity ≥ 7.
- Quick — functionally equivalent to Smart in tool access and intents.
  Two differences only: (1) no re-evaluation after tool results (Smart re-evaluates for follow-up
  delegation; Quick does not); (2) tool remapping: `search_web` → `search_web_light` via
  `intent_remap` at dispatch time. Handles complexity 1–6 (≈70% of requests), significantly cheaper.
- Smart — provider-agnostic, model resolved from execution context per user config.
  Called only for complexity 7–10 requests. After tool results, re-evaluates for follow-up delegation.
- WebSearchLight — single-pass provider-native search (`use_grounding=True`). Separate agent
  because Gemini cannot combine grounding + function calling in one request.
  Remapped from `search_web` by Quick. Internal (`internal=True`).
- WebSearch — provider-native web search with synthesis prompt. Called by Smart.
  `use_grounding=True` — each adapter injects its own tool: Gemini → Google Search,
  OpenAI → web_search, Claude → web_search_20250305 + web_fetch_20250910.
  Two intents: `search_web` (real-time web search) + `fetch_url` (fetch and extract content
  from a specific URL provided by the user).
- Memory (LLM key formulation + vector search) — MemorySearchAgent: ECO-tier LLM extracts
  search keys, then multi-vector RRF search. Shared between Quick and Smart paths.
  Two intents: `search_memory` (semantic search) + `save_to_memory` (explicit fact save).
  `save_to_memory`: zero LLM calls — orchestrator fills `context.text` via `context_schemas`
  with a self-contained fact passage; agent attaches it as `consolidation_text` on `MessagePart`
  (user message layer, never compressed) → consolidation picks it up in the normal batch cycle.
- EmailSearch — EmailSearchAgent: email archive specialist (BALANCED tier). Accessible to both
  Quick and Smart (registered in AgentDescriptor with `internal=False`). Three intents:
  `search_emails` (vector search in indexed archive), `get_email_details` (fetch full email body),
  `get_email_attachment` (parse attachment via markitdown).
- EmailClassification — EmailClassificationAgent: shared singleton in ServiceContainer.
  Called by EmailIndexingService (not by agents). Classifies raw emails via tool-calling
  mode; extracts fact sentences for Firestore storage. Exception to OUTPUT_FORMAT rule:
  uses markdown code block extraction in `_parse_response()` — see inline comment.
- DocPlanner (ASYNC, PERFORMANCE tier) — DOCX creation entry point (intent: `create_document`).
  Single LLM call → JSON layout spec → fire-and-forget delegate to DocGenerator via coordinator.
  Result: DeliveryItem("file_upload") delivered by AgentWorkerHandler → notify_file_bytes.
- DocGenerator (internal, PERFORMANCE tier) — Node.js DOCX code generation (intent:
  `generate_docx_code`, `internal=True`). LLM writes Node.js script → DocxRunnerPort executes
  subprocess → DOCX bytes returned. See docs/05_building_blocks/document_generation/README.md.
- PdfGenerator (ASYNC, PERFORMANCE tier) — Single-pass PDF creation (intent: `create_pdf`,
  `internal=False`, exposed directly to LLMs). One LLM call → raw HTML+CSS text response →
  NodePuppeteerRunner renders to PDF bytes → two DeliveryItem("document"): HTML (GCS link) +
  PDF (GCS link + Slack file upload). Filename extracted from `<title>` tag.
  System prompt loaded from PromptBuilder (required). Style auto-selected by LLM from 12-style catalogue.
  See docs/05_building_blocks/document_generation/README.md.
- HtmlPageGenerator (ASYNC, PERFORMANCE tier) — Single-pass HTML page creation (intent:
  `create_html_page`, `internal=False`). One LLM call → complete HTML+CSS+JS document → one
  DeliveryItem("document"): HTML GCS public link (no Slack file upload, no Node.js subprocess).
  Filename from `<title>` tag. PromptBuilder mandatory (`agent_type="html_page"`); fail fast on
  prompt builder error. Design enforced by `COGNITIVE_PROCESS_HTML_PAGE` token: mobile-first,
  CSS custom properties, IntersectionObserver scroll animations.
  **Unsplash integration:** LLM writes `source.unsplash.com/WxH/?keywords` placeholder URLs
  natively. Post-processing (`_resolve_unsplash_placeholders`) replaces them with real Unsplash
  API photos (parallel fetch via `UnsplashAdapter`), honoring dimensions and injecting attribution.
  Requires `UNSPLASH_ACCESS_KEY` env var; graceful no-op when absent.
  Port: `ImageSearchPort` (`src/ports/image_search_port.py`).
  Adapter: `UnsplashAdapter` (`src/adapters/unsplash_adapter.py`).
  See docs/05_building_blocks/document_generation/README.md § 11.
- Proactive Self-Reminders (ECO tier) — `NotesAgent` backed by `FirestoreAgentNoteAdapter`.
  Intent `manage_self_reminders`. Paradigm: **deferred instructions for the orchestrator itself** —
  not a user-facing notepad, but a mechanism where the system sets reminders that fire autonomously
  and execute as new conversations (the orchestrator talks to itself on a schedule).
  Two-field model: `text` (≤15-word label) + `instruction` (full execution context, self-contained).
  `instruction` is the core of the reminder — but when firing, `_build_reminder_alert()` in
  `WorkerHandler` enriches it with: note_id, schedule type (one-time vs recurring with interval),
  self-authorship framing ("you wrote this to yourself"), proactive guidance (conversation history
  as primary signal, available intents to act on).
  3 tools: `create_self_reminder`, `update_self_reminder`, `delete_self_reminder`. Single LLM call.
  Firing: Cloud Scheduler every 15 min → `POST /worker {fire_due_reminders}` → `WorkerHandler` →
  `_build_reminder_alert(note)` → `UserNotificationService.notify(system_alert=..., agent_id_override=smart_response_agent_{user_id})` → SmartAgent executes as new conversation.
  One-time reminders: deleted after firing. Recurrent (`hourly/daily/weekly/monthly`): `reschedule()`
  computes `next_due` in user's local timezone (DST-safe), updates `due` + `last_fired`.
  Idempotency: `last_fired` guard (4-min window). Soft cap 20 active reminders.
  Transparency: every CRUD sends `notify_raw()` to user's channel immediately.
  Context: orchestrator sees `active_reminders {}` summary; NotesAgent sees full details + bio facts.
  Timezone: from `UserBotConfig.timezone` (IANA, set in Cabinet UI). Used for: prompt datetime,
  `due` UTC conversion, `next_due` recurrence, transparency notification formatting.
  See `docs/10_rfcs/PROACTIVE_SELF_REMINDERS_RFC.md`.
- Tasks — `TasksAgent` backed by `TasksProviderPort`. Intent `manage_user_tasks`.
  5 tools: `list_tasks`, `search_tasks`, `create_task`, `update_task`, `delete_task`. Max 6 turns.
  Search-before-mutate: LLM calls `search_tasks` first, gets `task_ref` (8-char short_id =
  `md5(task_id)[:8]`), then passes it to `update_task`/`delete_task`.
  Recurrence: 5 patterns (`daily`, `weekdays`, `weekly`, `absoluteMonthly`, `absoluteYearly`);
  smart defaults from `due_datetime`.
  Two provider adapters (injected per user via UserAgentFactory):
    `MicrosoftToDoAdapter` — Graph API CRUD + webhook subscriptions; implements
      TasksProviderPort + TaskLifecyclePort. OAuth: Azure consumers tenant, `Tasks.ReadWrite`.
      Webhook: `POST /webhook/microsoft-tasks/{user_id}` → self-healing index freshness.
      Worker tasks: `setup_microsoft_todo`, `reindex_task_list`, `renew_task_subscriptions`.
    `GoogleTasksAdapter` — Google Tasks REST API CRUD; implements TasksProviderPort.
      OAuth: `/auth/connect-google-tasks`.
  `TaskIndexingService` — embed→index pipeline + `resolve_short_id`.
  `TaskSearchIndex` — 2-vector RRF in Firestore (`task_search_index` collection).
  `TaskSetupService` — lifecycle: setup, ensure_subscriptions, disconnect.
  See `docs/05_building_blocks/tasks_integration/README.md`.
- Consolidation — background "memory consolidation" (PERFORMANCE tier, runs via Cloud Tasks)
- DeepResearch (async, provider-agnostic) — long-running research jobs. Agent calls
  `DeepResearchPort.create_interaction()` → returns ACK (job_id) immediately. Result delivered
  by adapter: polling every 120s (Gemini), webhook (OpenAI). `ClaudeDeepResearchRunnerAgent`
  wraps Claude's native extended thinking; runs as a **Cloud Run Job** (not Cloud Task) via
  `JobRunnerPort` + `CloudRunJobsAdapter`. Entrypoint: `job_main.py`. task-timeout=18000s (5h).
  Two-pass critic controlled by `DEEP_RESEARCH_SECOND_PASS` env var AND `_SECOND_PASS_ENABLED`
  class flag (currently `False` — second pass disabled in code). max_tokens=64K for thinking
  models (claude-sonnet-4-6/opus-4-6), 32K for others. Beta: `output-128k-2025-02-19`.
  Logs: `resource.type=cloud_run_job`,
  `make logs-research-job-dev-tail`. Debug prompts saved to GCS at `end_turn` and `max_tokens`.
- MapsSearch (SYNC, BALANCED tier) — `MapsSearchAgent` (`maps_search_agent.py`). Intent `maps_query`.
  Place search & discovery, route computation (distance/duration), current weather via Google Maps
  AI Grounding Lite (MCP). Multi-turn tool loop: LLM selects MCP tools, agent executes, LLM formats
  response with clickable Google Maps links. System prompt via PromptBuilder (`maps_search` profile).
  Backend injected via `MapsToolsPort`. See `docs/10_rfcs/MCP_INFRASTRUCTURE_RFC.md`.
- Compute (SYNC, ECO tier) — `ComputeAgent` (`compute_agent.py`). Four intents:
  `compute_math` (arithmetic, algebra, unit conversions), `compute_datetime` (date differences,
  day-of-week, age, timezone conversions, countdowns), `compute_finance` (loan/mortgage payments,
  compound interest, amortization), `compute` (general-purpose: statistics, BMI, scoring, any
  numeric analysis). All execute Python code via Gemini `code_execution` sandbox. Provider-agnostic:
  `LLMRequest.use_code_execution=True`. No external data access — compute-only.

**Gmail Email Indexing** — passive inbox-as-memory pipeline:
- User connects Gmail via OAuth (`/auth/connect-gmail`); credentials stored in `oauth_credentials`
- Indexing job triggered from Cabinet UI or Cloud Scheduler; runs as paginated Cloud Tasks
- `EmailIndexingService` → `GmailProviderAdapter` → `EmailClassificationAgent` (LLM triage)
- Valuable emails → `IndexedEmail` stored in `domain_email_facts_v1` (4-vector schema, mirrors FactEntity)
- `EmailEmbeddingRepairService` — async repair job for emails stored without vectors
- `UserNotificationService` — sends background notifications to user's last active channel.
  `notify()`: routes `system_alert` through QuickAgent → formatted delivery + session history save.
  `notify_raw()`: direct text delivery, no agent reformatting. Stores last active channel per user
  in `user_notification_state`. Callers: reminders worker, deep research, async docs, daily email review.
- `WorkerHandler` — dispatches `/worker` Cloud Tasks by `task_type`:
  `agent_execution`, `email_indexing`, `email_indexing_watchdog`, `start_email_indexing`,
  `consolidation`, `deep_research_polling`, `fire_due_reminders`, `setup_microsoft_todo`,
  `reindex_task_list`, `renew_task_subscriptions`, `renew_all_task_subscriptions`,
  `start_daily_email_review`, `daily_email_review`
  See `docs/07_deployment/SCHEDULERS.md` for full scheduler reference.
- Watchdog: Cloud Scheduler fires `email_indexing_watchdog` every 2h; marks stale `running`
  jobs as `failed`
- **Daily Email Review** — `gmail_daily_review` + `gmail_daily_review_hour` in `UserBotConfig`.
  Cloud Scheduler hourly → `start_daily_email_review` fan-out → `daily_email_review` per-user Cloud Task.
  Worker fetches last 24h emails via `GmailProviderAdapter` (`list_emails` + `batch_get_full_content(deep=False)`),
  caps at 200 emails, truncates body to 500 chars. Passes structured JSON array
  `[{email_id, from, subject, date, snippet, body, attachments}]` to SmartAgent via `notify()`.
  SmartAgent has full tool access: `get_email_details`, `get_email_attachment`, `search_web`.
  Expected output: HTML page via `create_html_page` delivered as GCS link to user's channel.

**Consolidation** — analogous to long-term memory formation:
- Sliding window fills → batch goes to queue. Thresholds (configurable per user):
  prod defaults: overflow_threshold=50 messages, batch_size=30; dev: threshold=70, batch_size=50
- Cloud Tasks runs ConsolidationAgent in the background (non-blocking for user)
- "Life Chronicler" extracts facts and principles from raw messages
- Deduplication (threshold 0.96, number-aware) — a duplicate is better than a loss
- 3 vectors per fact (text, tags, metadata) + SCD2 versioning
- Biographical cache updated → next conversation already knows the new facts

**Prompt Builder (Token System)** — not hardcoded prompts, but assembly:
- Tokens — verified fragments from a library (humor, style, voice, cognitive process, etc.)
- Blueprints — purely static templates with `{{CLASS_NAME}}` token slot placeholders; no runtime placeholders
- 4 priority levels: USER > ACCOUNT > AGENT > SYSTEM
- Static template cached in-memory (24h TTL, 5ms vs 110ms cold)
- Runtime context (biographical facts, conversation history) appended as `knowledge_base {}` block
- `PROMPT_CACHE_BOUNDARY` splits the final prompt: static prefix cached by Anthropic (5 min), dynamic suffix (datetime + Q-S context) sent fresh every request

**Multilingual Support** — two independent language axes:
- **Agent response language** — controlled via prompt tokens (`LANG_MIRROR`, `LANG_FIXED_UK`,
  `LANG_FIXED_EN`, `LANG_FIXED_FR`, `LANG_FIXED_ES`). Mirror mode (default): LLM responds in the
  user's input language. Fixed mode: always responds in the chosen language regardless of input.
  `LanguagePreferenceService` writes to UserProfile + swaps prompt token atomically. On change,
  injects system alert into active conversation so LLM picks up new policy immediately.
- **UI language** — status messages ("Thinking..."), file prompts, entertainment intros.
  `LocalizationPort` → `FileLocalizationAdapter` reads from `src/locales/{code}.py`.
  Resolution chain: USER preferred_language → ACCOUNT default_language → SYSTEM default.
- Domain: `LanguageCode` enum (uk, en, fr, es) in `src/domain/language.py`.
  Ports: `LanguageServicePort` (read-only resolution), `LocalizationPort` (UI strings).
  Service: `LanguagePreferenceService` (write path + notification).
  Adding a new language: (1) add `LanguageCode` entry, (2) create `src/locales/{code}.py`,
  (3) register in `FileLocalizationAdapter._REGISTRY`, (4) add `LANG_FIXED_{CODE}` token to Firestore.
  Cabinet UI: `/api/user/language` (GET/POST). RFC: `docs/10_rfcs/MULTILINGUAL_SUPPORT_RFC.md`.

**Memory search** — 6 parallel queries across different vectors,
ranked via Reciprocal Rank Fusion (RRF). One search per request,
result reused by all agents.

## Economics

- 70% of requests → Quick path (cheap ECO-tier model), 30% → Smart path (expensive) = -62% LLM costs
- Budget ~$100/month, 1 vCPU Cloud Run — async is mandatory
- Solo-dev — maintainability beats architectural elegance

## Architecture

Hexagonal Architecture (Ports & Adapters).

```
src/
  domain/       — Models, enums, value objects. ZERO external dependencies.
                  Includes: auth.py (TokenClaims, OAuthTokens, OAuthUserInfo, IAMDecision),
                  llm.py (LLMRequest, LLMResponse, Message, MessagePart, ToolCall, ProviderCapabilities,
                  UsageMetadata, PromptCacheConfig, CacheMetadata, PROMPT_CACHE_BOUNDARY).
                  MessagePart includes `consolidation_text` field — visible only to consolidation
                  serializer; used by `save_to_memory` to attach facts to user messages.
  ports/        — ~51 ABC interfaces. Import only domain/ and stdlib.
                  New (2026-03-08): SecurityPort (security_port.py), PlatformPort (platform_port.py),
                  DedupStore (dedup_store.py).
                  New (2026-03-12): DocxRunnerPort (docx_runner_port.py) — system boundary for
                  Node.js subprocess execution; DocxRunnerError public exception.
                  Email ports: EmailProviderPort, EmailClassifierPort, EmailExclusionsPort,
                  IndexedEmailRepository, EmailIndexingJobRepository, OAuthCredentialsPort,
                  NotificationStatePort, NotificationChannelFactoryPort.
                  Tasks ports: TasksProviderPort (task_provider_port.py), TaskSearchIndex
                  (task_search_index.py), TaskConfigPort (task_config_port.py),
                  TaskLifecyclePort (task_lifecycle_port.py).
                  Maps ports: MapsToolsPort (maps_tools_port.py).
                  Language ports: LanguageServicePort (language_service_port.py),
                  LocalizationPort (localization_port.py).
  adapters/     — Port implementations (Firestore, Gemini, Claude, Grok, OpenAI, Slack, Telegram,
                  Gmail). Email adapters: GmailProviderAdapter, FirestoreIndexedEmailRepository,
                  FirestoreEmailJobRepository, FirestoreEmailExclusionsAdapter,
                  FirestoreOAuthCredentialsAdapter, FirestoreNotificationStateAdapter,
                  NotificationChannelFactory.
                  DOCX adapter: NodeDocxRunner (node_docx_runner.py) — DocxRunnerPort
                  implementation; writes temp script to docx_generator/ dir, executes via
                  subprocess, captures stdout as DOCX bytes.
                  PDF/HTML adapter: NodePuppeteerRunner (node_puppeteer_runner.py) — PuppeteerRunnerPort.
                  HtmlRendererPort: PlaywrightHtmlRenderer (playwright_html_renderer.py).
                  Tasks adapters: MicrosoftToDoAdapter (microsoft_todo_adapter.py) — Graph API
                  CRUD + subscription management; implements TasksProviderPort + TaskLifecyclePort.
                  GoogleTasksAdapter (google_tasks_adapter.py) — Google Tasks REST API CRUD;
                  implements TasksProviderPort. Provider injected per user by UserAgentFactory.
                  FirestoreTaskSearchIndex (firestore_task_search_index.py) — 2-vector RRF.
                  FirestoreTaskConfigRepository (firestore_task_config_repository.py).
                  Language adapters: FileLocalizationAdapter (file_localization_adapter.py).
  services/     — Business logic. Receive ports via DI.
                  prompt_builder.py includes both PromptBuilder and UserPromptBuilder
                  (merged from former user_prompt_builder.py).
                  Email services: EmailIndexingService, EmailSearchService,
                  EmailEmbeddingRepairService, GmailOAuthService, UserNotificationService.
                  Tasks services: TaskIndexingService (embed→index + resolve_short_id),
                  TaskSetupService (lifecycle: setup/disconnect/ensure_subscriptions).
                  Language services: LanguagePreferenceService (write path + prompt token swap).
  agents/       — Multi-agent system. core/ — agents, infrastructure/ — billing/logging.
                  Email agents: EmailSearchAgent, EmailClassificationAgent.
                  Document agents: DocPlannerAgent (doc_planner_agent.py, intent create_document,
                  ASYNC), DocGeneratorAgent (doc_generator_agent.py, intent generate_docx_code,
                  internal=True, called only by DocPlannerAgent via coordinator).
                  PDF agents: PdfGeneratorAgent (pdf_generator_agent.py, intent create_pdf,
                  ASYNC, internal=False). PuppeteerRunnerPort implemented by NodePuppeteerRunner
                  (node_puppeteer_runner.py). Node.js runner in pdf_generator/runner.js.
                  HTML page agents: HtmlPageGeneratorAgent (html_page_generator_agent.py, intent
                  create_html_page, ASYNC, internal=False). HTML is final artifact; optional
                  ImageSearchPort (UnsplashAdapter) for post-generation image resolution.
                  Maps agents: MapsSearchAgent (maps_search_agent.py, intent maps_query, SYNC).
                  Compute agents: ComputeAgent (compute_agent.py, intents compute_math,
                  compute_datetime, compute_finance, compute; SYNC, code_execution sandbox).
  handlers/     — Orchestrators (ConversationHandler, ConsolidationHandler, WorkerHandler).
                  WorkerHandler dispatches /worker Cloud Tasks by task_type.
  infrastructure/ — AgentCoordinator, queues, agent_config.py (central behavior params),
                  agent_registry.py (AgentDescriptor dataclass + AgentRegistry mechanics),
                  agent_manifest.py (Intent constants + all agent declarations — single source of truth).
  composition/  — ServiceContainer + UserAgentFactory + SlackAdapterFactory + TelegramAdapterFactory.
                  UserAgentFactory lives in composition/ (NOT services/).
  locales/      — Per-language UI string modules (uk.py, en.py, fr.py, es.py).
                  Loaded by FileLocalizationAdapter. Add new file per new language.
  config/       — EnvironmentConfig, Settings, AuthConfig.
  utils/        — Logger, telemetry, debug_logger (PromptDebugLogger), file_conversion
                  (convert_file_to_text, is_native_binary, make_history_stub),
                  groovy_to_markdown_transformer.
  web/          — Quart web app (OAuth + Cabinet UI). Endpoints:
                  Auth: /auth/login, /auth/callback, /auth/link-oauth, /auth/me,
                  /auth/refresh, /auth/logout, /auth/connect-gmail, /auth/connect-gmail/callback,
                  /auth/connect-google-tasks, /auth/connect-google-tasks/callback,
                  /auth/connect-microsoft-todo, /auth/connect-microsoft-todo/callback.
                  Gmail: /api/gmail/status, /api/gmail/index, /api/gmail/jobs/<id>,
                  /api/gmail/jobs/<id>/cancel, /api/gmail/disconnect, /api/gmail/data (GET/DELETE),
                  /api/gmail/auto-index (GET/PUT — daily auto-index schedule).
                  Tasks: /api/tasks/status (GET), /api/tasks/disconnect (DELETE),
                  /api/tasks/microsoft/status, /api/tasks/microsoft/reindex,
                  /api/tasks/microsoft/lists, /api/tasks/microsoft/disconnect.
                  Webhook: /webhook/microsoft-tasks/<user_id>.
                  User: /api/user/link-platform (POST/DELETE), /api/user/link-telegram (POST),
                  /api/user/platforms, /api/user/invite-codes (GET/POST),
                  /api/user/join-team, /api/user/facts, /api/user/facts/browse,
                  /api/user/facts/search, /api/user/facts/<id>/invalidate,
                  /api/user/timezone (GET/PUT — IANA timezone setting),
                  /api/user/language (GET/POST — UI + bot response language).
                  Cabinet UI: /cabinet, /cabinet/docs, /cabinet/docs/<path>.
                  Other: /health, deep_research_webhooks (OpenAI async results).
                  Runs as a shared Quart app (all blueprints on port 8080).
main.py         — Bootstrap: creates ServiceContainer + UserAgentFactory, graceful shutdown.
docx_generator/ — Node.js project with docx npm library. NodeDocxRunner writes temp scripts
                  here so node_modules/docx resolves at execution time. Not a Python package.
```

## Import Rules (CRITICAL)

```
domain/   → ONLY stdlib, pydantic. Never adapters/, services/, config/.
ports/    → domain/ + stdlib + ABC.
adapters/ → domain/, ports/, config/. No cross-subpackage adapter imports (REQ-ARCH-23).
services/ → domain/, ports/. Do NOT import concrete adapters or other services (REQ-ARCH-22).
            Cross-service deps use TYPE_CHECKING guards or constructor injection.
agents/   → Inherit BaseAgent. Receive dependencies via constructor.
```

## Code Conventions

- **All I/O — async/await.** No synchronous calls to DB or LLM.
- **Pydantic BaseModel** for domain entities. **@dataclass** for value objects (MessageContext, RoutingMetadata).
- **File naming:** `{entity}_service.py`, `{provider}_adapter.py`, `firestore_{entity}_repo.py`, `{purpose}_agent.py`.
- **Class naming:** `GeminiAdapter(LLMPort)`, `FirestoreFactRepository(FactRepository)`, `QuickResponseAgent(BaseAgent)`.
- **Shared state** protect with `asyncio.Lock`. No exceptions.
- **Errors** log before re-raise. Do not silently swallow exceptions.
- **Do not use print()** — only `from src.utils.logger import logger`.

## Patterns

- **Port is justified** when: 2+ implementations, testable substitution, system boundary.
- **Port is not needed** for internal services with a single implementation.
- **PerformanceTier** (ECO/BALANCED/PERFORMANCE) — abstraction between agents and concrete models.
- **ProviderRegistry** — runtime LLM provider selection (gemini/claude/grok).
- **PromptCacheStrategy** — transparent prompt caching via proxy pattern. Agents declare their
  type; strategy resolves cache config; `CachingLLMProxy` wraps the provider. Agents never
  import or reference `PromptCacheConfig`. See `docs/10_rfcs/HEXAGONAL_PROMPT_CACHING_RFC.md`.
- **AgentConfig** — central registry of tunable behavior parameters in `src/infrastructure/agent_config.py`.
  Agents read typed `@dataclass` values as class-level constants at definition time
  (`CONTEXT_WINDOW = QUICK.context_window`). Structured for Level 2 upgrade: replace class-level
  assignments with constructor-injected `self._cfg = get_agent_config(type, user_id)` backed by
  an `AgentConfigPort` + Firestore adapter — agents don't change. Provider selection is a separate
  concern — see `AgentProviderStrategy` in `src/services/agent_context_builder.py`.
- **AgentDescriptor** — unified per-agent declaration. Dataclass defined in
  `src/infrastructure/agent_registry.py`; all instances live in `src/infrastructure/agent_manifest.py`.
  Every agent in the system — specialist and orchestrator — has one descriptor there.
  Two halves: (A) `capabilities` — what this agent offers (intents it exposes, with `internal=True`
  hiding an agent from LLM tool descriptions); (B) `requirements` — `allowed_intents` (which intents
  it may call; `None` = all non-internal) + `intent_remap` (dispatch-time substitution, e.g. Quick
  remaps `search_web` → `search_web_light`); (C) `context_schemas` — per-intent typed parameter
  contracts (dict of field name → description). When present, orchestrator fills structured
  `context` params at delegation time instead of passing a bare `query` string. Used by
  `save_to_memory` (text), `get_email_details` (email_id), `get_email_attachment` (email_id,
  filename). `AgentManifest` is a backward-compatible alias.
  Specialists: declared in `agent_manifest.py` → registered via `ALL_DESCRIPTORS` in `main.py`.
  Orchestrators (Quick, Smart): declared in `agent_manifest.py`, set as class-level `_descriptor`
  in the agent class — coordinator never routes TO them via registry.
  See **Adding a New Specialist Agent** below for the complete checklist.
- **Intent** — typed string constants for all agent intent names. Defined in `agent_manifest.py`
  as `class Intent`. Import `Intent.SEARCH_MEMORY` etc. instead of raw string literals everywhere.
- **BaseAgent lifecycle hooks** — `_on_agent_start(text)`, `_on_agent_success(char_count, token_count,
  output_text)`, `_on_agent_error(error, context)`, `_on_delegation(intent, query)`. All agents
  call these instead of direct `logger.*` calls. Changing infrastructure logging = edit BaseAgent
  only. `_on_agent_success(output_text=...)` auto-logs final text to debug bucket.
- **BaseAgent debug logging** — `_call_llm(request, turn)` is the single logging point for agents
  that use `LLMPort`. Before calling the provider it logs the full `LLMRequest` via
  `PromptDebugLogger.log_llm_request()` (model, temperature, use_grounding, messages with real
  newlines). After the call it logs the `LLMResponse` via `_debug_llm_response()` (text +
  tool_calls + tokens as JSON). All no-ops when `DEBUG_PROMPTS=false`. Agents must never call
  `_debug_prompt()` or `_debug_response()` directly — those methods exist only for backward compat
  and are unused. `_on_agent_success(output_text=...)` separately logs the final user-facing text
  to the debug bucket.
  **Escape hatch for raw SDK callers** — agents that must bypass `LLMPort` (e.g.
  `ClaudeDeepResearchRunnerAgent` with native built-in tools) call `_debug_raw_turn(system_blocks,
  user_content, response_texts, tokens, turn, model)` instead. Do NOT use `_debug_prompt/response`
  from such agents.
- **CircuitBreaker** — in BaseAgent, protects against cascading failures.
- **SCD2 versioning** — FactEntity uses valid_from/valid_to/is_current.
- **Multi-tenant** — always pass account_id. Collections with env prefix.

## Adding a New Specialist Agent

See [`docs/how_to/NEW_AGENT_PLAYBOOK.md`](docs/how_to/NEW_AGENT_PLAYBOOK.md) — **mandatory protocol**.
Read Phase 0 before writing any code. Follow steps in order. Do not skip.

## Adding or Modifying an LLM Adapter

See [`docs/how_to/ADAPTER_WIRE_TESTING.md`](docs/how_to/ADAPTER_WIRE_TESTING.md) — **mandatory protocol**.
Every new or modified adapter must have wire tests (mock at SDK boundary, not port) and
contract validators in `tests/contracts/adapter_contracts.py`. Never mock at the port level
in adapter tests — that pattern cannot detect translation regressions.

## Agent Output Format Standards

Every agent that produces structured LLM output MUST follow these rules — no exceptions:

- **OUTPUT_FORMAT token is mandatory.** Every agent with structured output must have a dedicated
  `OUTPUT_FORMAT_{AGENT}` token in its blueprint. Never embed format instructions inside
  `cognitive_process` or any other token.

- **No regex fallbacks.** `_parse_response()` calls `json.loads()` directly on the raw LLM output.
  On `JSONDecodeError` → raise `ValueError`. Never extract partial output via `re.search`.
  Exception: `EmailClassificationAgent._parse_response()` — markdown code block extraction
  allowed due to cost/latency trade-off in tool-calling mode. See inline comment for rationale.

- **Retry on invalid output, not silent degradation.** When `_parse_response()` raises `ValueError`:
  append the bad model response + a user correction message to history, then continue the loop.
  After `MAX_PARSE_RETRIES` exhausted → `_all_failed(..., "parse_error")` + log error.
  Never post-process malformed output in Python.

- **`response_mime_type="application/json"` for single-pass only.** Gemini cannot combine JSON mode
  with function calling. When tools are active, rely on the OUTPUT_FORMAT token + retry logic.

- **`_RESPONSE_SCHEMA` on Quick/Smart (Gemini experiment).** Both orchestrators pass
  `response_schema=_RESPONSE_SCHEMA` to `LLMRequest` even when tools are active. GeminiAdapter
  applies it; ClaudeAdapter/GrokAdapter/OpenAIAdapter silently ignore it (or map to `json_object`
  mode). Schema enforces only the top-level envelope (`full_response`, `response_summary`,
  `rich_content.type` enum, `rich_content.fallback`). **`data` is declared as flat `{"type":
  "object"}`** — Gemini has a hard nesting depth limit; going deeper causes `400 INVALID_ARGUMENT`.
  Inner `data` structure is enforced by the OUTPUT_FORMAT token in the prompt.

- **`rich_content.data.rows` format: `[{"cells": [...]}, ...]`.** Each table row is an object with
  a `cells` key (array of strings). Never use `[[...], [...]]` (Gemini hangs on
  `array<array<string>>` in `response_schema`) or duplicate `rows` keys (JSON parse drops all but
  last). The Slack adapter normalizes all row variants: `{cells}` objects, plain arrays, flat lists.

## Tests

- pytest + pytest-asyncio (asyncio_mode=auto).
- Fixtures in `tests/conftest.py`: `mock_env_config`, `mock_llm_port`, `mock_repository`.
- Mocks via `AsyncMock(spec=PortClass)`.
- Markers: `@pytest.mark.requirement("REQ-XXX")`, `@pytest.mark.performance`.
- Structure: `tests/unit/`, `tests/integration/`, `tests/performance/`.
- **Adapter wire tests:** `tests/unit/adapters/` — mock at SDK boundary (not port). See `ADAPTER_WIRE_TESTING.md`.
- **Contract repository:** `tests/contracts/adapter_contracts.py` — named `ContractRule` objects with per-provider validators. Reused by both unit and integration tests.
- **Integration layer:** `tests/integration/adapters/` — `CapturingStub` + contract validation. Run with `pytest tests/integration/adapters/ -v`.

## Decision-Making Protocol (CRITICAL — apply before every non-trivial task)

Every implementation decision must pass through four sequential gates.
Skip any gate only when the task is unambiguous, isolated, and trivial (typo / rename / single line).

### Gate 1 — Orient: find the authoritative source

```
Is there a relevant RFC in docs/10_rfcs/?
  YES → Read it fully before writing a single line of code.
        Does the RFC reference a POC script in scripts/?
          YES → Read the POC fully. The POC is the authoritative implementation spec.
                POC = ground truth. It encodes validated, debugged logic.
                Only POCs explicitly referenced from an RFC qualify as authoritative.
          NO  → RFC alone is the spec.
  NO  → Existing production code is the spec. Read it before proposing changes.
```

### Gate 2 — Gap analysis: compare intent vs. reality

Before writing code, explicitly answer:
1. What exactly does the RFC/POC prescribe for this step?
2. What am I about to implement?
3. Is there any difference? (missing filter, different algorithm, different data structure, altered flow)

If there is ANY difference → do not proceed to Gate 3. Go to Gate 4 first.

### Gate 3 — Uncertainty check: stop or go?

Ask yourself: "Am I fully certain about every detail of this implementation?"

Signals that mean STOP and ask:
- The RFC/POC covers this case but my reading is ambiguous
- I found a "simpler" approach than what the POC uses — this is a red flag, not a win
- I am about to make an assumption about a parameter, a filter, a threshold, or a flow
- The implementation touches more than one subsystem and I haven't read all relevant code
- Something feels "obvious" but I haven't verified it against the source

Asking questions is efficient. One clarifying question costs 30 seconds.
A wrong autonomous assumption costs hours of debugging and rework.

### Gate 4 — Explicit delta declaration

If your implementation differs from the RFC/POC in any way:
- State the difference explicitly before writing any code
- Explain the reason
- Wait for user confirmation

Do NOT implement first and explain later. Do NOT silently simplify.
Autonomous decisions that diverge from the spec without notification are bugs in the process,
regardless of whether the code itself works.

---

## Project Documentation

Detailed docs in `docs/` (arc42). Read as needed:
- Architecture: `docs/04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md`
- Structure: `docs/04_solution_strategy/current_implementation/STRUCTURE.md`
- RFCs: `docs/10_rfcs/`
- Roadmap: `docs/12_risks/IMPLEMENTATION_ROADMAP.md`

## Language

- Respond to the user in whatever language they write in.
- All changes to documents (docs/, CLAUDE.md, code comments, docstrings, log messages) must be written in English.

## ⛔ SECRETS RULE — READ BEFORE TOUCHING ANY FILE

**NEVER write secrets, credentials, infrastructure details, or PII into any git-tracked file.**

This includes — but is not limited to:
- API keys, tokens, passwords, signing secrets, webhook secrets
- Cloud Run service URLs, project IDs, service account emails
- Internal hostnames, IP addresses, resource names
- User IDs, account IDs, email addresses
- OAuth client IDs/secrets, Firebase config values

**The only place for this data is `.env` (gitignored) or GCP Secret Manager.**

If a Makefile target, script, or config needs a URL or ID — define it as a variable
loaded from `.env`, never hardcoded in the tracked file itself.

When in doubt: if it identifies or grants access to infrastructure, it goes in `.env`.

---

## ⛔⛔⛔ Tests — ABSOLUTE RULE — READ BEFORE TOUCHING ANY TEST FILE ⛔⛔⛔

**NEVER modify, delete, or rewrite any existing test without EXPLICIT per-test permission from the user.**

This means: one test = one explicit approval. Blanket approval ("fix the tests") does NOT exist.
You MUST name the specific test and wait for a "yes, fix that one" before touching it.

If a code change causes a test to fail:
1. STOP. Do not touch the test.
2. Report EXACTLY which test failed and WHY (what assertion, what actual vs expected).
3. Wait for explicit per-test instruction from the user.

The ONLY self-authorized exceptions — no approval needed:
- Fixing a broken import path caused by a module rename you just performed.
- Nothing else.

This applies to: test files (`tests/`), conftest.py, shared test helpers, fixtures.

Rationale: tests are the specification. Modifying them to make code pass destroys the specification.
A failing test is signal — not an obstacle to remove.

## ⛔⛔⛔ Debugging Cloud Run — MANDATORY PROTOCOL ⛔⛔⛔

**When debugging any issue that manifests in Cloud Run, the FIRST action is ALWAYS to read the actual logs.**

Do NOT:
- Build theories about what might be failing
- Speculate based on code reading alone
- Propose fixes before seeing the actual error

DO — in this exact order:
1. **Read the logs first.** Use `gcloud logging read` or `gcloud beta logging tail`:
   ```
   gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=SERVICE_NAME" \
     --limit=50 --format="value(textPayload)" --project=PROJECT_ID
   ```
2. Find the actual error message, traceback, or unexpected output in the logs.
3. Only then diagnose and propose a fix.

If the service writes debug files to GCS (e.g. `gs://...-debug-prompts/`), read the relevant
request/response files with `gsutil cat` before theorizing about LLM behavior.

**Reading logs costs 1 tool call. Building wrong theories costs 10+ turns and user patience.**

---

## What NOT to Do

- Do not add DI containers (dependency-injector etc.) — manual DI in main.py.
- Do not create ports for cleanliness — only when there's a real need.
- Do not commit .env, *-admin-key.json, service-account*.json.
- Do not touch `archive/` — this is deprecated legacy code.
- All PII or sensitive data exports (Firestore queries, user facts, analysis results) MUST be
  saved only to `scripts/memory/` (gitignored). Never save them to tracked directories.
- Both dev and prod Firestore use the `us-production` named database. The `(default)` database
  is not used. Always use `database="us-production"` (or rely on `FIRESTORE_DATABASE` env var).
