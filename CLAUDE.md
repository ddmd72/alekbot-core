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
  performs semantic memory search, passes enriched context downstream
- Quick (Flash, BALANCED) — functionally equivalent to Smart in tool access and intents.
  Two differences only: (1) no refinement loop in cognitive process — single pass: INTENT → delegate → FORMAT;
  (2) tool remapping: `search_web` → `search_web_light` (ECO-tier web search instead of full).
  Handles complexity 1–5 (≈70% of requests), significantly cheaper than Smart.
- Smart (Gemini Pro / Claude Opus, PERFORMANCE) — called only for complexity 6–10 requests.
  Adds a multi-turn refinement loop: after tool results, re-evaluates for follow-up delegation.
- WebSearchLight (Flash Lite, ECO) — single-pass Google grounding, called by Quick only.
  Separate agent because Gemini cannot combine grounding + function calling in one request.
- WebSearch (Flash, BALANCED) — full-depth search with synthesis, called by Smart only.
- Memory (LLM key formulation + vector search) — MemorySearchAgent: ECO-tier LLM extracts
  search keys, then multi-vector RRF search. Shared between Quick and Smart paths.
- EmailSearch — EmailSearchAgent: email archive specialist (BALANCED tier). Accessible to both
  Quick and Smart via `DEFAULT_INTENTS`. Three intents: `search_emails` (vector search in indexed
  archive), `get_email_details` (fetch full email body), `get_email_attachment` (parse attachment
  via markitdown).
- EmailClassification — EmailClassificationAgent: shared singleton in ServiceContainer.
  Called by EmailIndexingService (not by agents). Classifies raw emails via tool-calling
  mode; extracts fact sentences for Firestore storage. Exception to OUTPUT_FORMAT rule:
  uses markdown code block extraction in `_parse_response()` — see inline comment.
- Consolidation (Opus/Thinking) — background "memory consolidation"

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
  ports/        — ~36 ABC interfaces. Import only domain/ and stdlib.
                  Email ports: EmailProviderPort, EmailClassifierPort, EmailExclusionsPort,
                  IndexedEmailRepository, EmailIndexingJobRepository, OAuthCredentialsPort,
                  NotificationStatePort, NotificationChannelFactoryPort.
  adapters/     — Port implementations (Firestore, Gemini, Claude, Grok, Slack, Telegram,
                  Gmail). Email adapters: GmailProviderAdapter, FirestoreIndexedEmailRepository,
                  FirestoreEmailJobRepository, FirestoreEmailExclusionsAdapter,
                  FirestoreOAuthCredentialsAdapter, FirestoreNotificationStateAdapter,
                  NotificationChannelFactory.
  services/     — Business logic. Receive ports via DI.
                  Email services: EmailIndexingService, EmailSearchService,
                  EmailEmbeddingRepairService, GmailOAuthService, UserNotificationService.
  agents/       — Multi-agent system. core/ — agents, infrastructure/ — billing/logging.
                  Email agents: EmailSearchAgent, EmailClassificationAgent.
  handlers/     — Orchestrators (ConversationHandler, ConsolidationHandler, WorkerHandler).
                  WorkerHandler dispatches /worker Cloud Tasks by task_type.
  infrastructure/ — AgentCoordinator, queues.
  composition/  — ServiceContainer + UserAgentFactory + SlackAdapterFactory + TelegramAdapterFactory.
                  UserAgentFactory lives in composition/ (NOT services/).
  config/       — EnvironmentConfig, Settings, AuthConfig.
  utils/        — Logger, telemetry.
  web/          — Quart web app (OAuth + Cabinet UI). Endpoints: /auth/login, /auth/callback,
                  /auth/link-oauth, /auth/me, /auth/refresh, /auth/logout,
                  /auth/connect-gmail, /auth/connect-gmail/callback,
                  /api/gmail/status, /api/gmail/index, /api/gmail/jobs/<id>,
                  /api/gmail/jobs/<id>/cancel, /api/gmail/disconnect, /api/gmail/data.
                  Runs as a shared Quart app (all blueprints on port 8080).
main.py         — Bootstrap: creates ServiceContainer + UserAgentFactory, graceful shutdown.
```

## Import Rules (CRITICAL)

```
domain/   → ONLY stdlib, pydantic. Never adapters/, services/, config/.
ports/    → domain/ + stdlib + ABC.
adapters/ → domain/, ports/, config/.
services/ → domain/, ports/. Do NOT import concrete adapters.
agents/   → Inherit BaseAgent. Receive dependencies via constructor.
```

## Code Conventions

- **All I/O — async/await.** No synchronous calls to DB or LLM.
- **Pydantic BaseModel** for domain entities. **@dataclass** for value objects (MessageContext, RoutingMetadata).
- **File naming:** `{entity}_service.py`, `{provider}_adapter.py`, `firestore_{entity}_repo.py`, `{purpose}_agent.py`.
- **Class naming:** `GeminiAdapter(LLMService)`, `FirestoreFactRepository(FactRepository)`, `QuickResponseAgent(BaseAgent)`.
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
- **CircuitBreaker** — in BaseAgent, protects against cascading failures.
- **SCD2 versioning** — FactEntity uses valid_from/valid_to/is_current.
- **Multi-tenant** — always pass account_id. Collections with env prefix.

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

## Tests

- pytest + pytest-asyncio (asyncio_mode=auto).
- Fixtures in `tests/conftest.py`: `mock_env_config`, `mock_llm_service`, `mock_repository`.
- Mocks via `AsyncMock(spec=PortClass)`.
- Markers: `@pytest.mark.requirement("REQ-XXX")`, `@pytest.mark.performance`.
- Structure: `tests/unit/`, `tests/integration/`, `tests/performance/`.

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

## What NOT to Do

- Do not add DI containers (dependency-injector etc.) — manual DI in main.py.
- Do not create ports for cleanliness — only when there's a real need.
- Do not commit .env, *-admin-key.json, service-account*.json.
- Do not touch `archive/` — this is deprecated legacy code.
- All PII or sensitive data exports (Firestore queries, user facts, analysis results) MUST be
  saved only to `scripts/memory/` (gitignored). Never save them to tracked directories.
- Both dev and prod Firestore use the `us-production` named database. The `(default)` database
  is not used. Always use `database="us-production"` (or rely on `FIRESTORE_DATABASE` env var).
