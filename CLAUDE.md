# Alek-Core

Personal exocortex — a knowledge management system powered by LLMs.
Solo developer. Production on GCP (Cloud Run + Firestore).

## Dev User IDs

When you need user_id / account_id for manual triggers, gcloud commands, or scripts — read from memory (`project_infra.md`). Never ask the user for these values.

## Manual Triggers (dev)

`/worker` verifies a Google OIDC token (see
`docs/04_solution_strategy/decisions/worker_oidc_and_docx_sandbox.md`), so a plain unauthenticated
`curl` returns **401** against the live service. Project ID / user IDs / SA: read from `.env` /
memory (`project_infra.md`) — don't hardcode. Three ways to trigger, easiest first:

**1. Scheduled task types — run the Cloud Scheduler job** (it already carries OIDC; nothing to mint):
```bash
gcloud scheduler jobs list --location=us-central1 --project=<PROJECT_ID>   # see all jobs
gcloud scheduler jobs run alek-bot-dev-fire-due-reminders --location=us-central1 --project=<PROJECT_ID>
```

**2. Arbitrary per-user payload against the live service — mint an OIDC token for the worker SA.**
Needs a one-time grant of `roles/iam.serviceAccountTokenCreator` on the SA to your user.
`--include-email` is **mandatory** — the verifier checks the token's `email` claim (audience is not
pinned, so its value is irrelevant but the gcloud flag is required):
```bash
SA=$(gcloud secrets versions access latest --secret=SERVICE_ACCOUNT_EMAIL --project=<PROJECT_ID>)
# one-time grant (you choose to run this — it widens your access to the SA):
# gcloud iam service-accounts add-iam-policy-binding "$SA" \
#   --member="user:$(gcloud config get-value account)" \
#   --role=roles/iam.serviceAccountTokenCreator --project=<PROJECT_ID>
TOKEN=$(gcloud auth print-identity-token --impersonate-service-account="$SA" \
  --audiences="$SERVICE_URL_DEV/worker" --include-email)
curl -s -X POST "$SERVICE_URL_DEV/worker" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"task_type": "daily_email_review", "user_id": "<DEV_USER_ID>", "account_id": "<DEV_ACCOUNT_ID>"}'
```

**3. Local instance — the gate bypasses** when `SERVICE_ACCOUNT_EMAIL` is unset (local dev), so a
plain unauthenticated `curl http://localhost:8080/worker ...` works with no token.

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
make fetch-logs [K=300] # Pull last K logs to alek_debug.log (grep locally — see Debugging below)
```

Lint + format via `ruff` (`make lint` / `make format`, config in `ruff.toml`). `make check` runs
`ruff check src/` before the unit suite, and CI (`.github/workflows/ci.yml`) runs `make check` on
every push/PR (posting ✅/❌ to Slack). The full unit/architecture suite is green and runs ~1:14
single-process — **safe to run in full** (any historical "don't run the full suite" warning was a
since-fixed chunker infinite loop, not a standing hazard). Lint scope is `src/` only (default high-signal ruleset: pyflakes + pycodestyle
errors). `ruff format` exists as a dev convenience but is not enforced in CI — the codebase is not
mass-reformatted.

## Code Navigation (CodeGraph)

A local code knowledge graph is available via the `codegraph` MCP server (index in `.codegraph/`,
gitignored, ~28MB, machine-local — run `codegraph init` once per clone; the server may be absent in
some environments — fall back to grep/Read when its tools aren't listed).

**Default to `codegraph_explore` FIRST** for "how does X work / where is X / trace this flow /
survey this area" — it returns the verbatim source of the relevant symbols in one call
(Read-equivalent). Use grep/Read only to confirm a detail it didn't cover. This cuts the usual
grep+Read fan-out to 1–2 calls.

**Catches well (trust it):**
- Whole-area / flow source in one shot (e.g. the DelegationEngine dispatch chain, an adapter's
  request build) — verbatim, well-targeted.
- Static structure: `extends` (port→adapter, agents→BaseAgent), `codegraph_callers`/`callees`,
  `codegraph_impact` (symbol + its tests).

**Dive deeper yourself (it stops at the doorstep):**
- **Runtime dispatch** — intent→agent via `agent_manifest.py`/`agent_registry.py`, provider/tier
  via `AgentProviderStrategy`, DI wiring in `composition/`. Explore points you to
  `handle_delegation` etc. but does NOT resolve the registry/config mapping — grep those files.
- **Class-level constants / config dicts** — `codegraph_search` returns name-substring test
  matches, not the `FOO = {...}` definition. Grep instead.
- The relationships "callers" list is polluted with `scripts/`+`tests/` noise — trust the Source
  section, filter the rest. `codegraph affected` (source→test mapping) is unreliable — use
  `make test-unit` / grep to find affected tests.

**Keeping it fresh:** a native file watcher re-indexes ~1–2s after edits (debounced), tool
responses warn when referencing pending files, and the MCP server reconciles against the working
tree on reconnect — so normal editing stays fresh automatically. After a big branch switch / rebase
/ bulk change made while the server was down, run `codegraph sync` (incremental) or `codegraph
index` (full); `codegraph status` shows index health.

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
- **Deploy-substitution trap:** `make deploy` does `include .env` and passes some keys as cloudbuild
  `--substitutions`, so any `.env` key reused that way bakes its **local** value into the deployed
  revision (this leaked `localhost` into prod `OAUTH_REDIRECT_URI`). When a value differs between
  local and deploy, give the deploy side its own key (e.g. `SERVICE_URL_DEV`) — never reuse the
  dual-purpose local key. Deploy reads the **working tree, not git** — uncommitted changes still ship.

## What and Why

Exocortex — AI extension of memory and thinking via Slack/Telegram.
Not a chatbot. A system that remembers, thinks in the background, and responds to the point.

**Cycle:** user speaks → bot responds using accumulated knowledge →
background process extracts new facts from the conversation → bot gets smarter.

## Key Mechanisms

**Multi-agent network** — specialists, not one LLM for everything. Tiers: ECO/BALANCED/PERFORMANCE.
Full per-agent detail (mechanics, intents, tiers, gotchas) lives in
[`src/agents/CLAUDE.md`](src/agents/CLAUDE.md) → Agent Roster. Quick index:

| Agent | Tier | Key intents | One-liner |
|-------|------|-------------|-----------|
| Router (Gemini) | ECO | — | LLM triage every request; **always routes to Smart** |
| Smart | per-config | `delegate_to_specialist` | primary path; re-evaluates after tool results |
| Quick | ECO | — | emergency fallback + `notify` formatter (never primary) |
| WebSearch | BALANCED | `search_web`, `fetch_url` | provider-native grounded search |
| Memory | ECO | `search_memory`, `save_to_memory` | multi-vector RRF; save = zero-LLM |
| EmailSearch | ECO | `search_emails`, `get_email_details`, `get_email_attachment` | |
| EmailClassification | BALANCED | — | called by EmailIndexingService, not by agents |
| DocPlanner | PERFORMANCE | `create_document` | JSON layout spec → DocGenerator |
| DocGenerator | BALANCED | `generate_docx_code` (internal) | Node.js script → DOCX |
| PdfGenerator | BALANCED | `create_pdf` | HTML+CSS → Puppeteer → PDF |
| HtmlPageGenerator | PERFORMANCE | `create_html_page` | full HTML+CSS+JS; Unsplash placeholders |
| FileManagement | zero-LLM | `open_file`, `delete_file` | |
| Notes / Self-Reminders | PERFORMANCE (OpenAI) | `manage_self_reminders` | autonomous deferred firing |
| Tasks | — | `manage_user_tasks` | MicrosoftToDo (GoogleTasks frozen) |
| Consolidation | PERFORMANCE (Claude) | — | background memory formation (see below) |
| DeepResearch | — | `deep_research` | async; default Claude, Cloud Run Job |
| MapsSearch | BALANCED (OpenAI) | `maps_query` (internal) | auto fan-out from `search_web` |
| Compute | ECO | `compute_*` | Gemini `code_execution` sandbox, compute-only |

**Remote MCP Server** — alekbot as MCP *server* exposing memory search to claude.ai Custom Connectors
(inverse of its Maps MCP *client*). One tool `get_user_context(query, …)` → `SearchEnrichmentService.enrich_context`
directly (bypasses the agent stack). Full in-process OAuth 2.1 AS (DCR, PKCE S256, RFC 8707, refresh
rotation) via the `mcp` SDK; endpoints at server root routed by a plain ASGI dispatcher in `main.py`
(NOT Starlette `Mount`). SDK shim `composition/mcp_sdk_oauth_provider.py` in `composition/` (REQ-ARCH-01).
**MVP, dev-only, experimental.** Code under `src/{domain/mcp.py,ports/mcp_client_repository.py,adapters/firestore_mcp_client_repository.py,services/mcp_authorization_service.py,composition/mcp_*.py,web/mcp_consent_app.py}`.
See `docs/05_building_blocks/remote_mcp_server/` + `docs/10_rfcs/REMOTE_MCP_SERVER_RFC.md`.

**Gmail Email Indexing** — passive inbox-as-memory: OAuth connect (`/auth/connect-gmail`) → paginated
Cloud Tasks → `EmailIndexingService` → `GmailProviderAdapter` → `EmailClassificationAgent` triage →
valuable emails as `IndexedEmail` in `domain_email_facts_v1` (4-vector, mirrors FactEntity).
`EmailEmbeddingRepairService` backfills missing vectors; `email_indexing_watchdog` (every 2h) fails
stale `running` jobs.
- **`WorkerHandler`** dispatches `/worker` Cloud Tasks by `task_type`:
  `agent_execution`, `email_indexing`, `email_indexing_watchdog`, `start_email_indexing`,
  `consolidation`, `sweep_consolidation`, `deep_research_polling`, `fire_due_reminders`,
  `execute_reminder`, `setup_microsoft_todo`, `reindex_task_list`, `renew_task_subscriptions`,
  `renew_all_task_subscriptions`, `start_daily_email_review`, `daily_email_review`,
  `billing_daily_summary`, `repair_email_embeddings`. Full reference: `docs/07_deployment/SCHEDULERS.md`.
- **`UserNotificationService`** — background notifications to the user's last active channel
  (`user_notification_state`). `notify()` routes `system_alert` through a formatter agent (Quick by
  default; `agent_id_override` → Smart for reminders/daily-review) → formatted delivery + session history.
  `notify_raw()`: direct text, no reformatting. `prompt_tokens` in `UsageMetadata` always = uncached input
  (OpenAI/Gemini subtract cached from total).
- **Billing daily summary** — Scheduler 09:00 Europe/Madrid → `billing_daily_summary`: posts yesterday's
  `prev_daily_tokens/cost` snapshot to Slack. Per-provider cache pricing Claude 0.1×/OpenAI 0.1×/Gemini 0.25×.
- **Token accounting is per-execution, NOT per-instance.** `TokenLedger` (`domain/billing.py`) lives in a
  `ContextVar` opened by `BaseAgent._execution_billing_scope()`; `_call_llm` accumulates into it,
  `_flush_billing` reads it. Agent instances are per-user singletons and `DelegationEngine` fans a tool
  batch out via `asyncio.gather`, so instance-level accumulators billed each concurrent execution the
  running total (3.6× inflation, fixed 2026-07-28). The scope's `reset(token)` is load-bearing — an
  inline-awaited specialist would otherwise capture its caller's ledger. **Account counters written before
  2026-07-28 are inflated; for historical cost use BigQuery `prompt_content`, not `usage.*`.**
- **Daily budget alert (advisory, never a gate).** `BillingAccount.daily_cost_limit` (default $5) →
  `increment_account_usage` returns `UsageIncrement`; `FirestoreQuotaService` posts to the ops sink
  (`AlertSinkPort`, `BILLING_SLACK_WEBHOOK_URL`) when the day *crosses* the limit — once per day, not
  while-over. `check_quota` is dead **by decision** (alert-only; owner dropped the hard cap 2026-07-26).
  See `decisions/billing_execution_scoped_ledger.md`.
- **Daily Email Review** (`gmail_daily_review*` in `UserBotConfig`; hourly `start_daily_email_review`
  fan-out → per-user `daily_email_review`) — fetches last-24h emails (cap 200, full body, cleaned by
  adapter: BS4 + `html.unescape` + invisible-Unicode strip), passes a structured JSON array to Smart via
  `notify(save_history=False)`. Smart protocol: triage ([ACTION]/[FYI]/[DIGEST]/[NOISE]) → deep-reads
  (`get_email_details`/`get_email_attachment`) → `search_web` → `create_html_page` report (Gmail-linked
  subjects) + short chat message. `notify_document_link` saves report URL + `fetch_url` hint to history.
  Cabinet: `/api/gmail/daily-review`.

**Consolidation** — long-term memory formation: sliding window fills → batch to Cloud Tasks queue →
ConsolidationAgent ("Life Chronicler") extracts facts/principles from raw messages (non-blocking).
Thresholds (per-user): prod threshold=50/batch=30, dev threshold=70/batch=50. Dedup 0.96 (number-aware —
a duplicate beats a loss). 3 vectors/fact + SCD2; biographical cache updated for next conversation.
**Serialization:** model parts use `p.text` (summary), NOT `p.full_text` (verbose + web_search_context);
user parts `p.consolidation_text or p.text` (prefixed `\n\n`).

**Standing Directives** — user behavioral rules ("never give partial answers", "trace conditional
logic before judging") are a first-class fact domain `FactDomain.AGENT_DIRECTIVE`, rendered as a
binding `standing_directives {}` block in the **orchestrator's** system prompt every request (NOT
biographical `preference`, NOT firing self-reminders). Rendering = a channel split at
`PromptBuilder.build_for_agent` (mirrors `query_specific_context`): partitions the flat cache list
into `static_bio` / `directives`, each its own `assemble()` param; gated by `include_directives`
(False for the consolidator — it curates them as records, never obeys). Curation = **unconditional
Stage 2b** (`ConsolidationAgent._review_directives`, runs every consolidation, fed the FULL rulebook)
that optimises them as a system-instruction section: imperative English (except quoted literals the
agent must output/match), terse, one rule each, no overlap — *convergence not churn* (already-optimal
→ NO-OP). **Hard cap 15, two layers:** prompt (merge adjacent, else invalidate least essential — no
"umbrella" merges of unrelated behaviors) + deterministic code backstop `_enforce_directive_cap`
(invalidates lowest-priority tail if LLM left >15); injection independently bounded by
`DEFAULT_DIRECTIVES_CACHE_LIMIT=15`. See `decisions/standing_directives.md` +
`docs/10_rfcs/STANDING_DIRECTIVES_RFC.md`.

**Prompt Builder (Token System)** — assembly, not hardcoded prompts: verified Tokens (humor, voice,
cognitive process…) + static Blueprints with `{{CLASS_NAME}}` slots; 4 priority levels
USER > ACCOUNT > AGENT > SYSTEM; static template cached in-memory (24h TTL, 5ms vs 110ms cold). Runtime
context appended as `knowledge_base {}`. `PROMPT_CACHE_BOUNDARY` splits the final prompt: static prefix
cached by Anthropic (5 min), dynamic suffix (agent_notes + Q-S context) fresh every request.
- **Datetime injection disabled by default** (`include_datetime=False`) — time injected via
  `_inject_timestamps()` in user messages (user tz from `UserBotConfig.timezone`) + UTC prefix on
  delegation queries. Opt in with `include_datetime=True`.
- **User location** → `knowledge_base { user_location }` when set (`UserBotConfig.location`, Cabinet
  `/api/user/location`).
- **Injecting large static content** — background tasks pass a large static dataset (email triage
  payload, corpus) via `extra_static_blocks: List[str]` on `build_for_agent()`; placed in the **static
  section** (before `PROMPT_CACHE_BOUNDARY`, after `knowledge_base {}`) as a named Groovy block
  `name {\n…\n}`, referenced by name in the user instruction. Never embed large payloads in the user
  message (pollutes history + bypasses caching).

**File Storage Pipeline** — upload: ConversationHandler → `FileConversionService.process_attachment()` →
GCS → reference-only MessagePart (no content in history). Fetch: specialist delegation with `file_ref` →
`AgentCoordinator._resolve_file_refs()` downloads + converts + injects `file_content`; FileManagementAgent
handles direct `open_file`/`delete_file`. `GcsFileStorageAdapter`: Finder-style dedup + sanitization;
conditional on `GCS_MEDIA_BUCKET`. **Bound channels:** ConversationHandler strips `path` from `file_data`
(`mode.is_bound`) — agent sees `[File: name (size)]` label, accesses via `open_file`. See
`docs/05_building_blocks/file_storage/README.md`.

**Per-channel sessions** — `session_id = f"{user_id}:{channel_id}"`, deterministic; each channel (Slack
C.../D..., Telegram chat_id) has its own session/history/consolidation stream (a DM is just channel D...).
Adapters resolve session_id synchronously. Async delivery uses `origin_channel_id` from message context
(propagated via DelegationEngine passthrough); system notifications go to primary channel. `GcpTaskQueue`
uses `_DomainEncoder` for transparent Pydantic serialization in Cloud Task payloads.

**Multilingual Support** — two independent axes: (1) **response language** via prompt tokens
(`LANG_MIRROR` default = mirror input; `LANG_FIXED_{UK,EN,FR,ES}` = fixed); `LanguagePreferenceService`
swaps the token atomically + injects a system alert so the LLM adopts it immediately. (2) **UI language**
(status messages, prompts) via `LocalizationPort` → `FileLocalizationAdapter` (`src/locales/{code}.py`),
resolution USER → ACCOUNT → SYSTEM. Domain: `LanguageCode` enum (uk/en/fr/es). Adding a language +
Cabinet `/api/user/language`: see `docs/10_rfcs/MULTILINGUAL_SUPPORT_RFC.md`.

**Memory search** — 6 parallel queries across different vectors, ranked via Reciprocal Rank Fusion (RRF).
One search per request, result reused by all agents.

## Economics

- Cost optimization is complexity-driven tier selection within Smart: simple requests resolve to a
  cheaper tier model (ECO/BALANCED), expensive models reserved for complex requests. (Earlier this
  was a Quick-vs-Smart path split; primary routing is now Smart-only — see Multi-agent network above.)
- Budget ~$100/month, 1 vCPU Cloud Run — async is mandatory
- Solo-dev — maintainability beats architectural elegance
- **Smart cost sweet spot (eval 2026-04-12):** `gpt-5.4-mini` + `reasoning_effort: medium` matched
  flagship (sonnet-4-6 / gpt-5.4) quality on Smart's multi-step delegation, beat haiku/flash, ~3–5×
  cheaper. Configure via `UserBotConfig.agent_thinking={"smart":"medium"}` + provider
  `openai`/`gpt-5.4-mini`. Reasoning compensates for smaller model size on multi-step tasks.

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
  agents/       — Multi-agent system (inherit BaseAgent). core/ — orchestrators; rest — specialists (roster + patterns in src/agents/CLAUDE.md).
  handlers/     — Entry points: ConversationHandler, WorkerHandler (dispatches /worker Cloud Tasks
                  by task_type), AgentWorkerHandler (task_type="agent_execution" — async agent runs
                  + deep-research delivery). consolidation_handler.py is a shim over
                  ConsolidationService (kept for composition-layer compatibility).
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
  Cloud Task dispatch, Cloud Scheduler trigger. Three handler classes exist (Conversation,
  Worker, AgentWorker). A new one only if a new external event source is added — not because
  a class is large.
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
- **Adapter / provider patterns** — PerformanceTier tier→model traps, ProviderRegistry, adapter
  capability gates, GeminiEmbeddingAdapter, PromptCacheStrategy → moved to
  [`src/adapters/CLAUDE.md`](src/adapters/CLAUDE.md) (loads when working in `adapters/`).
- **Agent-orchestration patterns** — AgentDescriptor, Intent, specialist delegation (commissioning
  model, `query` vs `context`), DelegationEngine (multi-turn loop, context passthrough, `deliver_response`
  vestigial note, intent fan-out), BaseAgent lifecycle hooks, `build_tool_turn`, LLM-content capture,
  CircuitBreaker, transcript integrity, AgentConfig → moved to
  [`src/agents/CLAUDE.md`](src/agents/CLAUDE.md) → Orchestration Patterns (loads when working in `agents/`).
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

**Every agent with structured LLM output MUST follow the output-format standard.** The full standard
lives in [`src/adapters/CLAUDE.md`](src/adapters/CLAUDE.md) → Agent Output Format Standards — it sits
next to the adapter code that enforces it and loads when working in `adapters/`. It covers: mandatory
`OUTPUT_FORMAT_{AGENT}` token; `_parse_response()` → `json.loads` with **no regex fallback**;
retry-on-invalid (not silent degradation); the three provider-specific JSON-enforcement mechanisms
(`response_mime_type`, `response_schema`, OUTPUT_FORMAT token) incl. the Claude synthesized **`respond`
tool**; `_RESPONSE_SCHEMA` on Quick/Smart; and the `rich_content.data.rows` `[{cells:[…]}]` shape.

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

**Where to write records:** multi-step refactors with a migration plan → `docs/10_rfcs/`;
single backward-looking decision records (50–150 lines: decision + alternatives + why) →
`docs/04_solution_strategy/decisions/`. Do NOT write to `docs/09_decisions/` (arc42 §9 canonical
home) until its planned cleanup — only ADR-001 lives there now.

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
A failing test is signal — not an obstacle to remove. The ~3600+ tests across 233 files (for ~150
source files) are **load-bearing, not over-engineering**: this codebase is AI-pair-programmed, and
tests are the only enforcement that runs faster than generated code. Critical bugs map 1:1 to files
lacking parallel tests. Never propose reducing coverage as "cleanup" or "simplification".

## ⛔⛔⛔ Debugging Cloud Run — MANDATORY PROTOCOL ⛔⛔⛔

**When debugging any issue that manifests in Cloud Run, the FIRST action is ALWAYS to read the
actual data. No theories, no speculation from code alone, no proposed fixes before seeing the
actual error.**

### Step 1 — Pull logs LOCALLY, then grep locally (do NOT loop `gcloud logging read`)

`make fetch-logs [K=300]` writes the last K entries to `alek_debug.log`. **One fetch, then grep
the local file** — far faster and cheaper than repeated remote `gcloud logging read` calls.
- `make logs [K=300]` — quick view of last K entries (no file)
- `make logs-tail` — live tail (`make logs-perf` = perf lines only)
- Cloud Run Jobs (e.g. deep research): `make fetch-logs-job [K]` → `alek_debug_job.log`;
  `make logs-job`; `make logs-execution EXECUTION=<name>`

### Step 2 — Two data sources. Pick by what you need; do NOT conflate them.

| Need | Source | How |
|------|--------|-----|
| Operational events, errors, tracebacks, control flow | **Cloud Logging** | `make fetch-logs` (above) |
| Actual LLM prompt/response **content** + tokens | **BigQuery** `alek_observability_dev.prompt_content` | `bq query` (below) |

**`prompt_content`** — one row per LLM call, 30-day TTL. Columns: `trace_id, span_id, timestamp,
user_id, account_id, agent_id, agent_type, model, provider, turn, request_text, response_text,
tool_calls, prompt_tokens, completion_tokens, total_tokens`. **`request_text` is populated even
when the call fails** — a 400'd request still has its row (with empty `response_text`), so failed
LLM calls ARE inspectable here. Query locally; for the large multi-line `request_text` use
`--format=json` and parse (CSV breaks on embedded newlines):
```
bq query --project_id=<PROJECT_ID> --use_legacy_sql=false --format=json \
  'SELECT request_text, response_text, tool_calls FROM
   `<PROJECT_ID>.alek_observability_dev.prompt_content`
   WHERE timestamp BETWEEN "<from>" AND "<to>" AND agent_type LIKE "%smart%" ORDER BY turn'
```

### Scope discipline
Investigate the **reported incident's time window**. Do NOT trawl unrelated prior-day errors. If
checking for recurrence, search only the **same error signature** — and if you surface another
error class, either analyze it fully or leave it; no half-analysis.

### "No `gcloud`" ≠ "no access" (remote/web session)
The same data is reachable from Python via `google.cloud.logging` / `google.cloud.bigquery` using
the environment's service-account credentials. A missing CLI does NOT mean the data is unavailable.

**Reading the right source costs 1–2 calls. Wrong theories cost 10+ turns and user patience.**

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
