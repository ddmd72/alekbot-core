# Alek-Core

Personal exocortex — a knowledge management system powered by LLMs.
Solo developer. Production on GCP (Cloud Run + Firestore).

## Dev User IDs

When you need user_id / account_id for manual triggers, gcloud commands, or scripts — read from memory (`project_infra.md`). Never ask the user for these values.

## Manual Triggers (dev)

User IDs and the service URL for the commands below: read from `.env` / memory (`project_infra.md`).

```bash
# Trigger daily email review for dev user ($SERVICE_URL_DEV from .env)
curl -s -X POST "$SERVICE_URL_DEV/worker" \
  -H "Content-Type: application/json" \
  -d '{"task_type": "daily_email_review", "user_id": "<DEV_USER_ID>", "account_id": "<DEV_ACCOUNT_ID>"}'
```

## Commands

```bash
make check             # CI gate: ruff lint + unit/architecture tests
make test              # All tests
make test-unit         # Unit tests
make test-integration  # Integration tests
make test-e2e-all      # E2E all agents
make deploy            # Build + deploy to Cloud Run (single live environment)
make logs              # Recent Cloud Run logs
make logs-tail         # Live tail logs
```

Lint + format via `ruff` (`make lint` / `make format`, config in `ruff.toml`). `make check` runs
`ruff check src/` before the unit suite, and CI (`.github/workflows/ci.yml`) runs `make check` on
every push/PR. Lint scope is `src/` only (default high-signal ruleset: pyflakes + pycodestyle
errors). `ruff format` exists as a dev convenience but is not enforced in CI — the codebase is not
mass-reformatted.

## Branching & Environment

- **Single live environment.** The separate prod deployment was retired (2026-05-31); the
  `development_`-prefixed collections in the `us-production` database are the sole live data.
  The prod-suffix path and the env-prefix mechanism are kept by decision (a future real prod
  would reuse them) — collections are NOT renamed to drop the prefix (Firestore has no native
  rename; migration not worth it). See
  `docs/04_solution_strategy/decisions/collection_prefix_retained.md` and
  `docs/04_solution_strategy/decisions/dead_prod_collections_deletion.md`.
- **Default branch is `main`** (renamed from `develop` 2026-06-01; the old prod-tracking `main`
  was deleted). One trunk.
- **Branch discipline:** large or risky changes go on a feature branch off `main`, merged when
  green. Small isolated changes can land on `main` directly — but every change keeps the
  affected docs (this file, arc42 in `docs/`, decision records) in sync. Documentation drift is
  the thing branch discipline exists to prevent.

## What and Why

Exocortex — AI extension of memory and thinking via Slack/Telegram.
Not a chatbot. A system that remembers, thinks in the background, and responds to the point.

**Cycle:** user speaks → bot responds using accumulated knowledge →
background process extracts new facts from the conversation → bot gets smarter.

## Key Mechanisms

**Multi-agent network** — specialists, not one LLM for everything. Tiers: ECO/BALANCED/PERFORMANCE.
- Router (Gemini) — LLM triage on every request: complexity, tone, semantic lens, search intent;
  triggers memory/web enrichment. **Always routes to Smart** (`_apply_routing_rules`); complexity
  drives Smart's per-request tier, not a Quick-vs-Smart split. `_classify_request` = rule-based
  fallback only. Vision (attachments) forces complexity ≥ 7.
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
  called by Smart. QUICK/RESEARCH cognitive triage. JSON output (findings/source/url) enforced via
  `response_schema`. Intents: `search_web`, `fetch_url`.
- Memory — MemorySearchAgent (ECO): LLM extracts search keys → multi-vector RRF. Intents:
  `search_memory`; `save_to_memory` (zero LLM — orchestrator fills `context.text` via `context_schemas`;
  agent attaches `consolidation_text` on `MessagePart` → picked up in the normal consolidation batch).
- EmailSearch — EmailSearchAgent (BALANCED, `internal=False`). Intents: `search_emails`,
  `get_email_details`, `get_email_attachment` (markitdown).
- EmailClassification — shared ServiceContainer singleton, called by EmailIndexingService (not agents);
  tool-calling triage, extracts fact sentences. OUTPUT_FORMAT exception: markdown-block extraction in
  `_parse_response()` (see inline comment).
- DocPlanner (ASYNC, PERFORMANCE, intent `create_document`) — LLM → JSON layout spec →
  fire-and-forget delegate to DocGenerator → DeliveryItem("file_upload").
- DocGenerator (internal, PERFORMANCE, intent `generate_docx_code`) — LLM writes Node.js script →
  DocxRunnerPort subprocess → DOCX bytes.
- PdfGenerator (ASYNC, PERFORMANCE, intent `create_pdf`, `internal=False`) — one LLM call → HTML+CSS →
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
- Consolidation (PERFORMANCE, Claude; Cloud Tasks) — background long-term memory formation (see below).
- DeepResearch (async, provider-agnostic, intent `deep_research`) — `create_interaction()` returns
  ACK (job_id); result delivered by adapter. **Default Claude** (`ClaudeDeepResearchRunnerAgent`,
  `NO_RETRY`) runs as a **Cloud Run Job** (`job_main.py`, task-timeout 18000s) via `JobRunnerPort`+
  `CloudRunJobsAdapter`; OpenAI backend = webhook. Gemini backend removed 2026-05-29. Two-pass critic via
  `UserBotConfig.deep_research_second_pass`. Logs: `make logs-research-job-dev-tail`.
- MapsSearch (SYNC, BALANCED, intent `maps_query`, **`internal=True`**) — place search, routes, weather
  via Google Maps AI Grounding (MCP, `MapsToolsPort`). Not shown to LLMs; auto-triggered via
  `intent_fanout` when the orchestrator dispatches `search_web`, results merged under labeled sections.
- Compute (SYNC, ECO) — intents `compute_math`/`compute_datetime`/`compute_finance`/`compute`; runs
  Python in Gemini `code_execution` sandbox (`use_code_execution=True`). No external data — compute-only.

**Remote MCP Server** — exposes memory search to claude.ai Custom Connectors (alekbot as MCP *server*,
the inverse of its Maps MCP *client*). Built on the `mcp` Python SDK (`FastMCP`). One tool,
`get_user_context(query, alternate_phrasing?, keywords?)`, calls `SearchEnrichmentService.enrich_context`
directly (bypasses the agent stack; ~1–1.4s). Full in-process OAuth 2.1 AS (DCR, PKCE S256, RFC 8707
resource indicator, refresh-token rotation) via the SDK's `OAuthAuthorizationServerProvider`. Endpoints at
server root (`/mcp`, `/authorize`, `/token`, `/register`, `/.well-known/oauth-*`, `/mcp/consent`); `main.py`
routes them via a plain ASGI dispatcher (NOT Starlette `Mount`, which breaks exact `POST /mcp` + the RFC 9728
PRM path). Consent binds identity from the **Cabinet JWT cookie**, not the OAuth flow. Storage: three
env-prefixed Firestore collections (`mcp_oauth_clients`/`mcp_auth_codes`/`mcp_refresh_tokens`, doc-id lookups
only); access tokens = stateless HS256 JWTs carrying `user_id`+`account_id` (`AlekAccessToken`). The SDK shim
`composition/mcp_sdk_oauth_provider.py` lives in `composition/` (not `adapters/`) — REQ-ARCH-01 forbids
adapters→services; it delegates to `MCPAuthorizationService`. **MVP, dev-only, experimental.** Code under
`src/{domain/mcp.py,ports/mcp_client_repository.py,adapters/firestore_mcp_client_repository.py,services/mcp_authorization_service.py,composition/mcp_*.py,web/mcp_consent_app.py}`.
See `docs/05_building_blocks/remote_mcp_server/` + `docs/10_rfcs/REMOTE_MCP_SERVER_RFC.md`.

**Gmail Email Indexing** — passive inbox-as-memory pipeline:
- User connects Gmail via OAuth (`/auth/connect-gmail`); credentials stored in `oauth_credentials`
- Indexing job triggered from Cabinet UI or Cloud Scheduler; runs as paginated Cloud Tasks
- `EmailIndexingService` → `GmailProviderAdapter` → `EmailClassificationAgent` (LLM triage)
- Valuable emails → `IndexedEmail` stored in `domain_email_facts_v1` (4-vector schema, mirrors FactEntity)
- `EmailEmbeddingRepairService` — async repair job for emails stored without vectors
- `UserNotificationService` — background notifications to the user's last active channel.
  `notify()`: routes `system_alert` through a formatter agent (Quick by default; `agent_id_override`
  lets callers pick Smart — reminders and daily-review do) → formatted delivery + session history
  (`text`=`response_summary`, `full_text`=full response). `notify_raw()`: direct text, no reformatting.
  Last active channel in `user_notification_state`. Callers: reminders, deep research, async docs, daily review.
- `WorkerHandler` — dispatches `/worker` Cloud Tasks by `task_type`:
  `agent_execution`, `email_indexing`, `email_indexing_watchdog`, `start_email_indexing`,
  `consolidation`, `deep_research_polling`, `fire_due_reminders`, `setup_microsoft_todo`,
  `reindex_task_list`, `renew_task_subscriptions`, `renew_all_task_subscriptions`,
  `start_daily_email_review`, `daily_email_review`, `billing_daily_summary`,
  `repair_email_embeddings`
  See `docs/07_deployment/SCHEDULERS.md` for full scheduler reference.
- **Billing daily summary** — Cloud Scheduler 09:00 Europe/Madrid → `billing_daily_summary`.
  Reads `prev_daily_tokens/prev_daily_cost` (yesterday's snapshot, saved at daily counter reset)
  → posts to Slack webhook. Per-provider cache pricing: Claude 0.1×, OpenAI 0.1×, Gemini 0.25×.
  All adapters populate `cache_read_tokens` in `UsageMetadata`.
  `prompt_tokens` in `UsageMetadata` always means uncached input tokens — OpenAI and Gemini
  adapters subtract cached from total (providers include cached in their prompt_token_count).
- Watchdog: Cloud Scheduler fires `email_indexing_watchdog` every 2h; marks stale `running`
  jobs as `failed`
- **Daily Email Review** — `gmail_daily_review` + `gmail_daily_review_hour` in `UserBotConfig`.
  Cloud Scheduler hourly → `start_daily_email_review` fan-out → `daily_email_review` per-user Cloud Task.
  Worker fetches last 24h emails via `GmailProviderAdapter` (`list_emails(date_to=None)` + `batch_get_full_content(deep=False)`),
  caps at 200 emails, truncates body to 500 chars. Body text cleaned by adapter: BS4 HTML parsing +
  `html.unescape()` + invisible Unicode stripping (zero-width joiners, non-breaking spaces etc.).
  Passes structured JSON array `[{email_id, from, subject, date, snippet, body, attachments}]` to SmartAgent
  via `notify(save_history=False)` — not saved to session history to avoid context pollution.
  SmartAgent protocol: Phase 0 triage (disposition tags per email: [ACTION]/[FYI]/[DIGEST]/[NOISE]),
  Phase 1 deep reads (`get_email_details` for all [ACTION], `get_email_attachment` for relevant attachments),
  Phase 2 research (`search_web` for context). Every email must appear in the HTML report.
  Subject lines rendered as clickable Gmail links (`https://mail.google.com/mail/u/0/#all/{email_id}`).
  Output: HTML page via `create_html_page` (GCS link) + short chat message. Language: user's language.
  After HTML delivery: `notify_document_link` saves user/model pair to session history with URL +
  `fetch_url` hint — agent can re-fetch the report if user asks about it later.
  Cabinet UI: `/api/gmail/daily-review` (GET/PUT). Scheduler: `alek-bot-{env}-start-daily-email-review`.

**Consolidation** — analogous to long-term memory formation:
- Sliding window fills → batch goes to queue. Thresholds (configurable per user):
  prod defaults: overflow_threshold=50 messages, batch_size=30; dev: threshold=70, batch_size=50
- Cloud Tasks runs ConsolidationAgent in the background (non-blocking for user)
- "Life Chronicler" extracts facts and principles from raw messages
- Deduplication (threshold 0.96, number-aware) — a duplicate is better than a loss
- 3 vectors per fact (text, tags, metadata) + SCD2 versioning
- Biographical cache updated → next conversation already knows the new facts
- **Serialization for consolidation:** model parts use `p.text` (summary), NOT `p.full_text`
  (verbose response + web_search_context). User parts: `p.consolidation_text or p.text`.
  `consolidation_text` prefixed with `\n\n` separator.

**Prompt Builder (Token System)** — not hardcoded prompts, but assembly:
- Tokens — verified fragments from a library (humor, style, voice, cognitive process, etc.)
- Blueprints — purely static templates with `{{CLASS_NAME}}` token slot placeholders; no runtime placeholders
- 4 priority levels: USER > ACCOUNT > AGENT > SYSTEM
- Static template cached in-memory (24h TTL, 5ms vs 110ms cold)
- Runtime context (biographical facts, conversation history) appended as `knowledge_base {}` block
- `PROMPT_CACHE_BOUNDARY` splits the final prompt: static prefix cached by Anthropic (5 min), dynamic suffix (agent_notes + Q-S context) sent fresh every request
- **Datetime injection disabled by default** (`include_datetime=False`). Instead, current time
  is injected via `_inject_timestamps()` in user messages (user's local timezone from
  `UserBotConfig.timezone`) and via UTC timestamp prefix on delegation queries
  (`AgentCoordinator.handle_delegation`). Agents can opt in with `include_datetime=True`.
- **User location** injected into `knowledge_base { user_location: '...' }` when set.
  Free text field in `UserBotConfig.location`, set via Cabinet UI `/api/user/location`.

**Injecting large static content into a system prompt** — when a background task needs to pass
a large static dataset (e.g. email triage payload, document corpus) into the agent's context:
- Place it in the **static section** (before `PROMPT_CACHE_BOUNDARY`), right after `knowledge_base {}`.
  This ensures the LLM sees it before instructions, and it gets cached by the provider.
- Use `extra_static_blocks: List[str]` parameter on `build_for_agent()` → `assemble()` →
  `_inject_runtime_context()`. The block is injected between `knowledge_base {}` and the blueprint.
- Format as a named Groovy-style block: `block_name {\n<content>\n}`. Reference it by name
  in the user-message instruction (e.g. `"Full data is in email_for_triage {} in your system context"`).
- Never embed large payloads in the user message — it pollutes conversation history and
  bypasses prompt caching.

**File Storage Pipeline** — upload path: ConversationHandler → FileConversionService.process_attachment()
→ GCS upload → reference-only MessagePart (no content in history). Fetch path: specialist delegation
with file_ref → AgentCoordinator._resolve_file_refs() intercepts, downloads + converts, injects
file_content into params. FileManagementAgent handles direct open_file and delete_file intents.
GcsFileStorageAdapter: Finder-style dedup (report.docx → report (1).docx), filename sanitization.
Conditional on `GCS_MEDIA_BUCKET` env var. See docs/05_building_blocks/file_storage/README.md.
**Bound channel file handling:** ConversationHandler strips `path` from `file_data` for bound channels
(`mode.is_bound`) — adapters won't inline binary content. Agent sees `[File: name (size)]` label
in platform history and accesses content via `open_file` delegation.

**Per-channel sessions** — `session_id = f"{user_id}:{channel_id}"`, deterministic.
Each channel (Slack C.../D..., Telegram chat_id) has its own session, history, and
consolidation stream. No special cases for DMs — a DM is just another channel (D...).
Adapters resolve session_id synchronously (no Firestore query). `get_latest_session_id()`
deprecated. Async task delivery uses `origin_channel_id` from message context (propagated
automatically via DelegationEngine context passthrough). System notifications (reminders,
daily email) go to primary channel. `NotificationService` derives session_id from delivery
channel when not explicitly provided. `GcpTaskQueue` uses `_DomainEncoder` for transparent
Pydantic model serialization in Cloud Task payloads.

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

- Cost optimization is complexity-driven tier selection within Smart: simple requests resolve to a
  cheaper tier model (ECO/BALANCED), expensive models reserved for complex requests. (Earlier this
  was a Quick-vs-Smart path split; primary routing is now Smart-only — see Multi-agent network above.)
- Budget ~$100/month, 1 vCPU Cloud Run — async is mandatory
- Solo-dev — maintainability beats architectural elegance

## Architecture

Hexagonal Architecture (Ports & Adapters).

```
src/
  domain/       — Models, enums, value objects. ZERO external deps (stdlib + pydantic only).
                  Key: llm.py (LLMRequest/LLMResponse/Message/MessagePart/ToolCall, PROMPT_CACHE_BOUNDARY,
                  build_tool_turn), auth.py, retry_policy.py, exceptions.py. MessagePart.consolidation_text
                  is visible only to the consolidation serializer (used by save_to_memory).
  ports/        — ~58 ABC interfaces (domain/ + stdlib only). One port per system boundary.
  adapters/     — Port implementations: LLM (Gemini/Claude/Grok/OpenAI), Firestore repos, Slack/Telegram,
                  Gmail, MicrosoftToDo (GoogleTasks frozen), Node runners (DOCX/Puppeteer), Unsplash, MCP repo.
                  No cross-subpackage adapter imports (REQ-ARCH-23).
  services/     — Business logic; ports via DI. No concrete-adapter / cross-service imports (REQ-ARCH-22).
                  Incl. prompt_builder.py (PromptBuilder + UserPromptBuilder), search enrichment, email
                  (indexing/search/repair/notification), tasks (indexing/setup), MCPAuthorizationService.
  agents/       — Multi-agent system (inherit BaseAgent). core/ — orchestrators; rest — specialists (see Key Mechanisms).
  handlers/     — Exactly 3 entry points: ConversationHandler, ConsolidationHandler, WorkerHandler
                  (dispatches /worker Cloud Tasks by task_type).
  infrastructure/ — AgentCoordinator, queues, agent_config.py, agent_registry.py (AgentDescriptor + registry),
                  agent_manifest.py (Intent constants + all agent declarations — single source of truth),
                  delegation_engine.py (reusable multi-turn tool loop).
  composition/  — ServiceContainer + UserAgentFactory (AgentFactoryPort; lives HERE, not services/) +
                  adapter factories + MCP wiring (mcp_setup.py, mcp_sdk_oauth_provider.py). The only layer
                  allowed to cross all boundaries.
  locales/      — Per-language UI strings (uk/en/fr/es.py), loaded by FileLocalizationAdapter.
  config/       — EnvironmentConfig, Settings, AuthConfig.   utils/ — logger, telemetry, debug_logger, file_conversion.
  web/          — Quart app (OAuth + Cabinet UI + MCP consent). Endpoint families: /auth/* (login + connect
                  gmail/google-tasks/microsoft-todo), /api/gmail/*, /api/tasks/*, /api/user/* (facts, timezone,
                  location, language, reminders, deep-research, platforms/invites), /webhook/microsoft-tasks/<id>,
                  /cabinet*, /mcp/consent, deep_research_webhooks, /health.
main.py         — Bootstrap: ServiceContainer + UserAgentFactory + remote MCP server (dedicated
                  SearchEnrichmentService, build_mcp_components, ASGI dispatcher via hypercorn) + graceful shutdown.
docx_generator/ — Node.js project (docx npm lib); NodeDocxRunner writes temp scripts here. Not a Python package.
```

## File Size Convention

One public class per file. If a file contains more than one public class — split it.
File length is not a constraint; mixed responsibilities are.

For files over ~600 lines: put the public interface (class + method signatures with docstrings)
at the top of the file, implementation below. This allows reading the contract in the first
50 lines without scrolling through the implementation.

## Layer Semantics (when in doubt, use this to decide where a new class goes)

- **`domain/`** — pure data and algorithms. No I/O, no side effects, no logging. If a class only
  needs stdlib + pydantic, it belongs here regardless of how complex the logic is.
- **`ports/`** — contracts (ABC). One port per system boundary. Do not create a port for a
  single internal service with no substitution need.
- **`services/`** — orchestrate I/O through ports. Do NOT participate in agent routing.
  Do NOT inherit BaseAgent. If it takes ports via constructor and coordinates work — it's a service.
- **`agents/`** — inherit BaseAgent, receive AgentMessage, return AgentResponse. Participate in
  multi-agent routing via AgentCoordinator. If it doesn't extend BaseAgent — it's not an agent.
- **`handlers/`** — entry points for external events: HTTP request (Slack/Telegram webhook),
  Cloud Task dispatch, Cloud Scheduler trigger. Exactly three exist. A new one only if a new
  external event source is added — not because a class is large.
- **`composition/`** — wiring layer. The only layer allowed to know about all other layers
  simultaneously. ServiceContainer + factories live here. If constructing an object requires
  importing from 2+ concrete layers — it belongs in composition/.

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
- **No fallback prompts.** Agents must not contain inline/hardcoded fallback prompts.
  If `PromptBuilder.build_for_agent()` fails — return `AgentResponse.failure()`, do not
  degrade to an empty or inline prompt. The Firestore prompt (token + blueprint + profile)
  is the single source of truth. Fail fast on missing prompts.

## Patterns

- **Port is justified** when: 2+ implementations, testable substitution, system boundary.
- **Port is not needed** for internal services with a single implementation.
- **PerformanceTier** (ECO/BALANCED/PERFORMANCE) — abstraction between agents and concrete models.
  When picking a default tier for a new agent, verify the resolved model accepts every
  parameter the agent sends. Concrete trap: `BALANCED` on Claude → `claude-haiku-4-5-20251001`,
  which rejects `output_config.effort` (HTTP 400). ConsolidationAgent default is therefore
  `PERFORMANCE` (`claude-sonnet-4-6`) in `_DEFAULT_AGENT_TIERS`. See
  `docs/05_building_blocks/provider_resolution/README.md` §2.1, §2.4.
- **ProviderRegistry** — runtime LLM provider selection (gemini/claude/grok).
- **Adapter capability gates** — each adapter silently drops parameters the resolved model
  doesn't accept instead of forwarding and crashing on 400. ClaudeAdapter gates `thinking`,
  `output_config.effort`, and `web_search_20260209` on `_THINKING_MODELS` / `_DYNAMIC_SEARCH_MODELS`
  substring checks; verify against `client.models.retrieve(<model>).capabilities` when adding
  a new gate. SDK pin: `anthropic >= 0.97.0`.
- **GeminiEmbeddingAdapter** — `gemini-embedding-2`, dim 768 (Matryoshka from native 3072; migrated
  from `-001` 2026-05-29). Legacy `task_type` → inline instruction prefix inside the adapter
  (`RETRIEVAL_DOCUMENT`→`"title: | text: …"`, `RETRIEVAL_QUERY`→`"task: search result | query: …"`,
  `SEMANTIC_SIMILARITY`→passthrough; unknown→`ValueError`). No true batch — `get_embeddings_batch`
  fans out N parallel single-content calls via `asyncio.gather`. Throttle: process-local
  `asyncio.Semaphore` (`GEMINI_EMBED_CONCURRENCY=20`). Transient 429/503 mapped to typed `LLMError`
  and retried via the shared `retry_async` executor (see `decisions/typed_retry_policy.md`).
  See `docs/05_building_blocks/embedding_system/README.md`.
- **PromptCacheStrategy** — transparent prompt caching via proxy pattern. Agents declare their
  type; strategy resolves cache config; `CachingLLMProxy` wraps the provider. Agents never
  import or reference `PromptCacheConfig`. See `docs/10_rfcs/HEXAGONAL_PROMPT_CACHING_RFC.md`.
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
  See **Adding a New Specialist Agent** below for the complete checklist.
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
  Smart: `terminal_tool="deliver_response"` (structured JSON output via tool).
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

When adding a new agent or capability, also update [`src/utils/capabilities.py`](src/utils/capabilities.py) —
this file is the user-facing capabilities reference returned by the `get_help` intent.
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

- **JSON output enforcement — three mechanisms, provider-specific:**

  **`response_mime_type="application/json"`** — forces model to return raw JSON (no markdown).
  Gemini: natively supported, but **cannot combine with function calling** (API error).
  OpenAI/Grok: mapped to `response_format: {"type": "json_object"}`.
  Claude: **no equivalent in API — silently ignored**. Claude has no native json_object mode.

  **`response_schema`** — flat envelope describing top-level JSON structure.
  Gemini: natively enforced. **Known issue:** schema + Groovy DSL prompt → Flash Lite returns
  empty responses (session 7, confirmed by 22+ tests). This is why MemorySearch uses
  `response_mime_type` without `response_schema`.
  OpenAI/Grok: triggers `json_object` mode (schema itself is not forwarded to API; inner
  structure enforced by OUTPUT_FORMAT prompt token).
  Claude: translated to `output_config={"format":{"type":"json_schema","schema":...}}` (GA
  structured outputs API, no beta header). Works with tools and with thinking in the same
  request. Model returns JSON directly in a text block — no tool interception needed.

  **OUTPUT_FORMAT token** — prompt-level instruction in Firestore blueprint. The authoritative
  source of truth for output structure. All JSON agents must have one. `response_schema` and
  `response_mime_type` are provider hints to enforce the format at API level; the token
  defines the actual schema the LLM follows.

  **What agents should pass:**
  - JSON agents WITHOUT tools: `response_mime_type` + `response_schema` (both).
    Gemini uses both natively. OpenAI/Grok react via json_object. Claude: `response_mime_type`
    is silently ignored; `response_schema` triggers `output_config.format`.
  - JSON agents WITH tools: `response_schema` only (no `response_mime_type`).
    Gemini cannot combine mime_type + tools. Schema works with tools on all providers.
  - Non-JSON agents: neither. OUTPUT_FORMAT token handles everything.

  **Guard:** agents requiring JSON output must be locked to providers that support it
  in `AgentProviderStrategy.STRATEGIES` (`allowed_providers`). If an agent uses
  `response_mime_type` without `response_schema`, it **must not** run on Claude
  (`response_mime_type` is still silently ignored by Claude — only `response_schema` is
  honoured via `output_config.format`).

- **`_RESPONSE_SCHEMA` on Quick/Smart.** Both orchestrators pass
  `response_schema=_RESPONSE_SCHEMA` to `LLMRequest` even when tools are active.
  Schema enforces only the top-level envelope (`full_response`, `response_summary`,
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
- Firestore uses the `us-production` named database (the `(default)` database is not used).
  Always use `database="us-production"` (or rely on `FIRESTORE_DATABASE` env var). Live data is
  in the `development_`-prefixed collections; the unprefixed prod collections were deleted
  2026-05-31 (see Branching & Environment above).
