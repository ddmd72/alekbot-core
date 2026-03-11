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
- Router (Gemini Flash) — classifies requests (complexity 1-5 → Quick, 6-10 → Smart),
  builds enriched context (biographical facts, memory search results) and passes it downstream.
- Quick (Flash, BALANCED) — functionally equivalent to Smart in tool access and intents.
  Two differences only: (1) no re-evaluation after tool results (Smart re-evaluates for follow-up
  delegation; Quick does not); (2) tool remapping: `search_web` → `search_web_light` via
  `intent_remap` at dispatch time. Handles complexity 1–5 (≈70% of requests), significantly cheaper.
- Smart (PERFORMANCE tier — model resolved from execution context, provider-agnostic) — called
  only for complexity 6–10 requests. After tool results, re-evaluates for follow-up delegation.
- WebSearchLight (ECO) — single-pass Google grounding. Separate agent because Gemini cannot
  combine grounding + function calling in one request. Remapped from `search_web` by Quick.
- WebSearch (BALANCED) — single-pass Google grounding with synthesis prompt. Called by Smart.
- Memory (LLM key formulation + vector search) — MemorySearchAgent: ECO-tier LLM extracts
  search keys, then multi-vector RRF search. Shared between Quick and Smart paths.
- EmailSearch — EmailSearchAgent: email archive specialist (BALANCED tier). Accessible to both
  Quick and Smart (registered in AgentDescriptor with `internal=False`). Three intents:
  `search_emails` (vector search in indexed archive), `get_email_details` (fetch full email body),
  `get_email_attachment` (parse attachment via markitdown).
- EmailClassification — EmailClassificationAgent: shared singleton in ServiceContainer.
  Called by EmailIndexingService (not by agents). Classifies raw emails via tool-calling
  mode; extracts fact sentences for Firestore storage. Exception to OUTPUT_FORMAT rule:
  uses markdown code block extraction in `_parse_response()` — see inline comment.
- Consolidation — background "memory consolidation" (PERFORMANCE tier, runs via Cloud Tasks)
- DeepResearch (async, provider-agnostic) — long-running research jobs. Agent calls
  `DeepResearchPort.create_interaction()` → returns ACK (job_id) immediately. Result delivered
  by adapter: polling every 120s (Gemini), webhook (OpenAI). `ClaudeDeepResearchRunnerAgent`
  wraps Claude's native extended thinking as a synchronous variant (separate agent, same port).

**Gmail Email Indexing** — passive inbox-as-memory pipeline:
- User connects Gmail via OAuth (`/auth/connect-gmail`); credentials stored in `oauth_credentials`
- Indexing job triggered from Cabinet UI or Cloud Scheduler; runs as paginated Cloud Tasks
- `EmailIndexingService` → `GmailProviderAdapter` → `EmailClassificationAgent` (LLM triage)
- Valuable emails → `IndexedEmail` stored in `domain_email_facts_v1` (4-vector schema, mirrors FactEntity)
- `EmailEmbeddingRepairService` — async repair job for emails stored without vectors
- `UserNotificationService` — sends Slack/Telegram alert on job completion; stores last
  active channel per user in `user_notification_state`
- `WorkerHandler` — dispatches `/worker` Cloud Tasks by `task_type`:
  `email_indexing`, `email_indexing_watchdog`, `consolidation`, `agent_execution`
- Watchdog: Cloud Scheduler fires `email_indexing_watchdog` every 2h; marks stale `running`
  jobs as `failed`

**Consolidation** — analogous to long-term memory formation:
- Sliding window (100-200 messages) fills up → batch goes to queue
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

**Memory search** — 6 parallel queries across different vectors,
ranked via Reciprocal Rank Fusion (RRF). One search per request,
result reused by all agents.

## Economics

- 70% of requests → Flash (cheap), 30% → Opus (expensive) = -62% LLM costs
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
  ports/        — ~41 ABC interfaces. Import only domain/ and stdlib.
                  New (2026-03-08): SecurityPort (security_port.py), PlatformPort (platform_port.py),
                  DedupStore (dedup_store.py).
                  Email ports: EmailProviderPort, EmailClassifierPort, EmailExclusionsPort,
                  IndexedEmailRepository, EmailIndexingJobRepository, OAuthCredentialsPort,
                  NotificationStatePort, NotificationChannelFactoryPort.
  adapters/     — Port implementations (Firestore, Gemini, Claude, Grok, Slack, Telegram,
                  Gmail). Email adapters: GmailProviderAdapter, FirestoreIndexedEmailRepository,
                  FirestoreEmailJobRepository, FirestoreEmailExclusionsAdapter,
                  FirestoreOAuthCredentialsAdapter, FirestoreNotificationStateAdapter,
                  NotificationChannelFactory.
  services/     — Business logic. Receive ports via DI.
                  prompt_builder.py includes both PromptBuilder and UserPromptBuilder
                  (merged from former user_prompt_builder.py).
                  Email services: EmailIndexingService, EmailSearchService,
                  EmailEmbeddingRepairService, GmailOAuthService, UserNotificationService.
  agents/       — Multi-agent system. core/ — agents, infrastructure/ — billing/logging.
                  Email agents: EmailSearchAgent, EmailClassificationAgent.
  handlers/     — Orchestrators (ConversationHandler, ConsolidationHandler, WorkerHandler).
                  WorkerHandler dispatches /worker Cloud Tasks by task_type.
  infrastructure/ — AgentCoordinator, queues, agent_config.py (central behavior params),
                  agent_registry.py (AgentDescriptor dataclass + AgentRegistry mechanics),
                  agent_manifest.py (Intent constants + all agent declarations — single source of truth).
  composition/  — ServiceContainer + UserAgentFactory + SlackAdapterFactory + TelegramAdapterFactory.
                  UserAgentFactory lives in composition/ (NOT services/).
  config/       — EnvironmentConfig, Settings, AuthConfig.
  utils/        — Logger, telemetry, debug_logger (PromptDebugLogger), file_conversion
                  (convert_file_to_text, is_native_binary, make_history_stub),
                  groovy_to_markdown_transformer.
  web/          — Quart web app (OAuth + Cabinet UI). Endpoints:
                  Auth: /auth/login, /auth/callback, /auth/link-oauth, /auth/me,
                  /auth/refresh, /auth/logout, /auth/connect-gmail, /auth/connect-gmail/callback.
                  Gmail: /api/gmail/status, /api/gmail/index, /api/gmail/jobs/<id>,
                  /api/gmail/jobs/<id>/cancel, /api/gmail/disconnect, /api/gmail/data.
                  User: /api/user/link-platform, /api/user/platforms, /api/user/invite-codes,
                  /api/user/join-team, /api/user/facts, /api/user/facts/browse,
                  /api/user/facts/search, /api/user/facts/<id>/invalidate.
                  Cabinet UI: /cabinet, /cabinet/docs, /cabinet/docs/<path>.
                  Other: /health, deep_research_webhooks (OpenAI async results).
                  Runs as a shared Quart app (all blueprints on port 8080).
main.py         — Bootstrap: creates ServiceContainer + UserAgentFactory, graceful shutdown.
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
  remaps `search_web` → `search_web_light`). `AgentManifest` is a backward-compatible alias.
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

## Tests — CRITICAL RULE

**Never modify existing tests to make them pass.**

If a code change causes a test to fail:
1. Stop immediately.
2. Report which test failed and why.
3. Wait for explicit instruction from the user.

Allowed exceptions (only with explicit user instruction):
- The test had a pre-existing bug unrelated to the current change.
- The user is deliberately changing a requirement and asks to update the test.

This applies to: test files, conftest.py fixtures, shared test helpers.
Fixing an import path in a test after renaming a module is allowed — everything else requires explicit approval.

## What NOT to Do

- Do not add DI containers (dependency-injector etc.) — manual DI in main.py.
- Do not create ports for cleanliness — only when there's a real need.
- Do not commit .env, *-admin-key.json, service-account*.json.
- Do not touch `archive/` — this is deprecated legacy code.
- All PII or sensitive data exports (Firestore queries, user facts, analysis results) MUST be
  saved only to `scripts/memory/` (gitignored). Never save them to tracked directories.
- Both dev and prod Firestore use the `us-production` named database. The `(default)` database
  is not used. Always use `database="us-production"` (or rely on `FIRESTORE_DATABASE` env var).
