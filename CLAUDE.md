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
- Router (Gemini Flash) — classifies requests, performs semantic memory search,
  passes enriched context downstream
- Quick (Flash) — simple answers, <2s, 70% of requests, 10x cheaper
- Smart (Claude Opus) — complex tasks, tool orchestration
- WebSearch (Flash + Grounding) — separate agent, because Gemini API cannot
  combine Google Search grounding and function calling in one request
- Memory (no LLM) — pure vector search, free
- Consolidation (Opus) — background "memory consolidation"

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
  ports/        — 28 ABC interfaces. Import only domain/ and stdlib.
  adapters/     — Port implementations (Firestore, Gemini, Claude, Grok, Slack, Telegram).
  services/     — Business logic. Receive ports via DI.
  agents/       — Multi-agent system. core/ — agents, infrastructure/ — billing/logging.
  handlers/     — Orchestrators (ConversationHandler, ConsolidationHandler).
  infrastructure/ — AgentCoordinator, queues.
  composition/  — ServiceContainer + SlackAdapterFactory: wires ports to adapters.
  config/       — EnvironmentConfig, Settings, AuthConfig.
  utils/        — Logger, telemetry.
  web/          — Quart web app (OAuth + Cabinet UI). Endpoints: /auth/login, /auth/callback,
                  /auth/link-oauth, /auth/me, /auth/refresh, /auth/logout.
                  Runs as a separate Quart app alongside the Slack/Telegram bot.
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

## Tests

- pytest + pytest-asyncio (asyncio_mode=auto).
- Fixtures in `tests/conftest.py`: `mock_env_config`, `mock_llm_service`, `mock_repository`.
- Mocks via `AsyncMock(spec=PortClass)`.
- Markers: `@pytest.mark.requirement("REQ-XXX")`, `@pytest.mark.performance`.
- Structure: `tests/unit/`, `tests/integration/`, `tests/performance/`.

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
