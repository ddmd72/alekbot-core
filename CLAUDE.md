# Alek-Core

Personal exocortex — a knowledge management system powered by LLMs.
Solo developer. Production on GCP (Cloud Run + Firestore).

## What belongs in this file (read before adding anything)

This file is loaded into every session, so every line costs every session. Add a line only if an agent needs it
whatever the task is:
- **Here:** commands (what each does), rules, repo-wide traps (things that break silently), and the "Where things live" map.
- **Not here — a subsystem's description, status or history:** its owner's map (`docs/05_building_blocks/*/README.md`),
  its RFC (`docs/10_rfcs/`) or a decision record (`docs/04_solution_strategy/decisions/`). A new subsystem gets one row
  in the map, nothing more.
- **Not here — a trap of one area:** the `CLAUDE.md` of the narrowest folder holding that code (create it if missing;
  it holds only that folder's rules and traps).
- **Not here — when to run which tests:** that is the session procedure, kept outside the repo. Commands state only what they do.
- **Not here — dated history** ("fixed on…", "until X it was broken"): decision records hold it.

## What and Why

Exocortex — AI extension of memory and thinking via Slack/Telegram (and a voice companion).
Not a chatbot. A system that remembers, thinks in the background, and responds to the point.

**Cycle:** user speaks → bot responds using accumulated knowledge →
background process extracts new facts from the conversation → bot gets smarter.

## Dev User IDs

When you need user_id / account_id for manual triggers, gcloud commands, or scripts — read from memory (`project_infra.md`). Never ask the user for these values.

## Manual Triggers (dev)

`/worker` verifies a Google OIDC token (`docs/04_solution_strategy/decisions/worker_oidc_and_docx_sandbox.md`), so a plain
unauthenticated `curl` against the live service returns **401**. Project ID / user IDs / SA: from `.env` / memory
(`project_infra.md`), never hardcoded. Three ways, easiest first:

1. **Scheduled task types — run the Cloud Scheduler job** (it carries OIDC already):
   `gcloud scheduler jobs list|run <job> --location=us-central1 --project=<PROJECT_ID>` (e.g. `alek-bot-dev-fire-due-reminders`).
2. **Arbitrary payload against the live service — mint a token for the worker SA.** Needs a one-time
   `roles/iam.serviceAccountTokenCreator` grant on the SA to your user (the owner chooses to run it). `--include-email` is
   **mandatory**: the verifier checks the `email` claim (the audience is not pinned, but the flag is required):
   ```bash
   SA=$(gcloud secrets versions access latest --secret=SERVICE_ACCOUNT_EMAIL --project=<PROJECT_ID>)
   TOKEN=$(gcloud auth print-identity-token --impersonate-service-account="$SA" \
     --audiences="$SERVICE_URL_DEV/worker" --include-email)
   curl -s -X POST "$SERVICE_URL_DEV/worker" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"task_type": "daily_email_review", "user_id": "<DEV_USER_ID>", "account_id": "<DEV_ACCOUNT_ID>"}'
   ```
   `drive_sync` (body `{user_id, connection_id}`; a body without a connection is refused) runs on the indexer, not the main service: enqueue it on `drive-sync-dev` with
   `gcloud tasks create-http-task … --oidc-service-account-email=<SA>` (needs actAs on that identity;
   `docs/07_deployment/SCHEDULERS.md`).
3. **Local instance:** with no OIDC identity configured and `K_SERVICE` unset the gate is open, so a plain
   `curl http://localhost:8080/worker …` works. On Cloud Run an empty identity set refuses every call.

## Commands

What each command does:

```bash
make lint              # ruff check on src/ and the root entry points (pyflakes + pycodestyle errors; config ruff.toml)
make typecheck         # mypy --strict on src/domain + src/ports (mypy.ini)
make check-types       # mypy (lenient) on src/adapters — informational, prints an error count, never fails
make format            # ruff format — a convenience, not enforced; the codebase is not mass-reformatted
make test-affected     # pytest --testmon: the unit tests testmon selects by executed Python lines
make test-unit         # the whole unit/architecture suite, xdist, without `slow` and `timing` tests
make check             # lint + typecheck + the unit suite without `slow` tests + the offline G2 test
make ci                # make check + `slow` tests (RUN_SLOW_TESTS=1) + coverage gates per file and per layer; CI runs exactly this
make test-timing       # `timing` tests only (RUN_TIMING_TESTS=1); CI runs it after a merge to `main`, never on a PR
make test-integration  # integration tests (some call real services)
make test-e2e-all      # e2e tests of all agents (call real services)
make test-cloud        # Firestore/GCS parity scenarios on the test database/bucket (RUN_CLOUD_TESTS=1; env in tests/CLAUDE.md)
make deploy            # build + deploy to Cloud Run (single live environment)
make deploy-indexes    # deploy config/firestore.indexes.json
make logs / logs-tail  # recent / live Cloud Run logs
make fetch-logs [K=300] # last K entries to alek_debug.log (see Debugging)
make check-pricing     # audit src/domain/billing.py against two price catalogs
```

- `pytest <path>::<test>` runs one process (the `-n` flag lives in the Makefile, not `pytest.ini`).
- `make test-unit` / `make check` / `make test-affected` run under pytest-xdist (`-n auto`; cap with `PYTEST_WORKERS=6`).
  A test that fails only under xdist is order-dependent: a real test bug, fixed under the reviewer rule, never re-serialised.
  One full suite at a time per machine: a run with `-n` (or 2000+ tests) takes a file lock and waits
  (`PYTEST_LOCK_TIMEOUT_S`, `PYTEST_SUITE_LOCK=0` turns it off).
- `make test-affected` seeds `.testmondata` from the main checkout in a new worktree. It cannot see a test whose input is
  a data file or code run in a child process (e.g. `prompts_snapshot/` → `tests/unit/prompts`). Never combine `--testmon`
  with `--cov`.
- `@pytest.mark.slow` (over 0.5 s with coverage on) is skipped unless `RUN_SLOW_TESTS=1`, so a bare `pytest <file>` skips it
  silently. `@pytest.mark.timing` (any wall-clock/CPU threshold) is skipped unless `RUN_TIMING_TESTS=1`; a new time
  threshold gets the marker, and a suite is never re-run because a timing test failed.
- **CI** (`.github/workflows/ci.yml`) runs `make ci` on every push/PR and posts ✅/❌ to Slack. A docs-only change skips it
  but still runs the doc-reading tests; "docs" is defined in `scripts/ci/docs_only.py`. More: `docs/11_quality/README.md`.

## Code Navigation (CodeGraph)

A local code graph is served by the `codegraph` MCP server (index in `.codegraph/`, gitignored, machine-local; run
`codegraph init` once per clone; when its tools are absent, fall back to grep/Read). Use `codegraph_explore` first for
"how does X work / where is X". It stops at the doorstep of:
- **Runtime dispatch** — intent→agent (`agent_manifest.py`/`agent_registry.py`), provider/tier (`AgentProviderStrategy`),
  DI wiring in `composition/`: grep those files.
- **Class-level constants / config dicts** — `codegraph_search` returns name-substring test matches, not the `FOO = {...}`
  definition: grep.
- **Callers lists** are polluted with `scripts/` + `tests/` noise; `codegraph affected` (source→test) is unreliable.
After a big branch switch / rebase made while the server was down: `codegraph sync` (or `codegraph index`).

## Branching & Environment

- **Single live environment.** The `development_`-prefixed collections in the `us-production` database are the sole
  live data. The prefix and the prod-suffix path are kept by decision — collections are NOT renamed
  (`decisions/collection_prefix_retained.md`, `decisions/dead_prod_collections_deletion.md`).
- **Default branch is `main`**, one trunk. Every change reaches `main` through a pull request: a GitHub ruleset refuses
  direct pushes, force pushes and deletion for everyone, owner included, with no bypass (cloud sessions push with the
  owner's token). Small isolated changes get a small PR. Every change keeps the affected docs (this file, arc42 in
  `docs/`, decision records) in sync — documentation drift is what branch discipline exists to prevent.
- **Deploy-substitution trap:** `make deploy` does `include .env` and passes some keys as cloudbuild `--substitutions`,
  so any `.env` key reused that way bakes its **local** value into the deployed revision (this leaked `localhost` into
  `OAUTH_REDIRECT_URI`). When a value differs between local and deploy, give the deploy side its own key (e.g.
  `SERVICE_URL_DEV`, `*_DEV` keys in `docs/07_deployment/README.md`). Deploy reads the **working tree, not git** —
  uncommitted changes ship.
- **`config.get()` vs `os.getenv()` — an env var read the wrong way is silently dead.** `load_settings()` lists its keys
  by hand; `config.get("ANYTHING_NOT_LISTED")` returns `None` with no error. Rule: **secrets and required configuration go
  in `load_settings()`; optional knobs — model pins, rollback switches, kill switches — are read with `os.getenv()` at
  their call site.** Registering an optional knob is not free: every empty key in that dict is chased into Secret Manager
  on each boot. `KMS_CRYPTO_KEY` and `SECRET_DIGEST_KEY` are required: `build_application_graph` refuses to start
  without either, locally too. Per-service knob lists: `docs/07_deployment/README.md`.

## Where things live

| Subject | Owner's map / spec |
|---|---|
| Agent roster, per-agent traps, delegation patterns | `src/agents/CLAUDE.md` (§ Agent Roster, § Orchestration Patterns, § Drive — Logs); intents `src/infrastructure/agent_manifest.py`, default tiers `_DEFAULT_AGENT_TIERS` in `src/domain/user.py` |
| LLM adapters: tier→model, capability gates, output-format standard | `src/adapters/CLAUDE.md`, `docs/05_building_blocks/provider_resolution/README.md` |
| Tests: layout, fakes, guards, coverage gates, mutation | `tests/CLAUDE.md`, `docs/11_quality/README.md` |
| Runtime flows (chat turn, background work, voice) | `docs/06_runtime/README.md`, `docs/06_runtime/API_REFERENCE.md` |
| Memory: consolidation, facts, expiry and janitor, review, standing directives, `$memory save` | `docs/05_building_blocks/memory/README.md` |
| Memory search (RRF) | `docs/08_concepts/multi_vector_rrf_search.md` |
| User drive: storage, specialist, indexing, search, transcripts, notes | `docs/05_building_blocks/user_drive/README.md` |
| Gmail indexing, daily email review | `docs/05_building_blocks/gmail_email_indexing/README.md`, `docs/07_deployment/SCHEDULERS.md` |
| `/worker` task types, schedulers, queues, task dedup | `docs/07_deployment/SCHEDULERS.md`, `decisions/worker_task_dedup.md` |
| Deploy units, `.env` deploy keys, standing levers, sandbox deploy | `docs/07_deployment/README.md` |
| Long-running turns | `decisions/long_running_turns.md` |
| Voice companion (Lelik): phone, web transport, providers | `docs/10_rfcs/VOICE_COMPANION_RFC.md`, `VOICE_WEB_TRANSPORT_RFC.md`, `VOICE_MULTI_PROVIDER_RFC.md` |
| Remote MCP server | `docs/05_building_blocks/remote_mcp_server/README.md` |
| Untrusted-content sandbox | `decisions/sandbox_render_service.md`, `docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md` |
| Secrets at rest, credential store, reconnect notices | `decisions/secrets_at_rest_cipher_and_digest.md`, `decisions/credential_store_cutover.md`, `decisions/reconnect_notice_r1.md` |
| Access control (one whitelist, chat + Cabinet gates) | `decisions/cabinet_oauth_whitelist_gate.md` |
| Billing: ledger, per-model pricing, cache legs, budget alert | `decisions/billing_execution_scoped_ledger.md`, `billing_per_model_pricing.md`, `billing_cache_write_and_voice_seconds.md` |
| Prompt builder (tokens, blueprints, cache boundary, static blocks) | `docs/08_concepts/prompt_assembly_guide.md`, `docs/05_building_blocks/prompt_design_system_v3/README.md` |
| Agent skills / agent workspace | `docs/10_rfcs/AGENT_SKILLS_RFC.md` / `docs/10_rfcs/AGENT_WORKSPACE_RFC.md` (+ `decisions/agent_skills.md`, `agent_workspace_storage.md`) |
| File storage pipeline, voice messages | `docs/05_building_blocks/file_storage/README.md`, `decisions/voice_message_transcription.md` |
| Notifications and rich content | `decisions/rich_content_one_delivery_path.md`, `docs/05_building_blocks/rich_content_protocol/README.md` |
| Sessions (`session_id = f"{user_id}:{channel_id}"`, one history per channel) | `docs/08_concepts/DATABASE_SCHEMA.md` |
| Localization (response language vs UI language) | `docs/05_building_blocks/localization_system/README.md`, `docs/10_rfcs/MULTILINGUAL_SUPPORT_RFC.md` |
| Observability (logs, `prompt_content`, Logfire, memory telemetry) | `docs/05_building_blocks/observability_strategy/README.md`, `docs/07_deployment/LOGGING.md`, `docs/05_building_blocks/memory_telemetry/README.md` |

`decisions/` = `docs/04_solution_strategy/decisions/`. Area traps live in the `CLAUDE.md` of the narrowest folder holding
the code (`src/agents/`, `src/adapters/`, `src/handlers/`, `src/services/`, `src/composition/`, `src/web/`, `src/utils/`).

## Traps (repo-wide)

Things that break silently. The deploy-substitution and `config.get()` traps are under Branching & Environment.
- **Firestore composite indexes** (`config/firestore.indexes.json`, `make deploy-indexes`) must be `READY` before the code
  that queries them deploys (consolidation and companion queues take the oldest batch by `created_at`; the drive mirror's
  Δ217 indexes). A missing index fails at query time, not at deploy.
- **Firestore TTL:** never a TTL policy on the facts collection's `expires_at` (the janitor stales, never deletes; a test
  scans the tree). Records meant to vanish carry `purge_after`.
- **Every Firestore client passes `database=`** (`us-production`; scripts via `scripts/_firestore.py`); an AST test enforces it.
- **Cloud Tasks names dedupe only while a task is queued or running and for about an hour after** — a name is not a
  "once per day" guard; rhythm comes from stored state (`decisions/worker_task_dedup.md`).
- **Cloud Run throttles CPU once a response is sent:** background work started in a route dies or stalls. Flush pending
  writes (e.g. `PromptContentStore`) before responding, or enqueue a Cloud Task.
- **The bot never writes to memory itself.** The consolidator reads every user-role message as the owner's own words, so
  bot-written text in a user message becomes facts and directives; only an owner-confirmed `$memory save` path writes
  (`decisions/memory_facts_owner_confirmed.md`).
- **Two delivery paths for prompt text:** tokens (`PROTOCOL_*`, `OUTPUT_FORMAT_*`, …) live in Firestore and ship by token
  upload, not by deploy; texts in `src/domain/*_texts.py` and `src/locales/` ship by code deploy.
- **Relay `--max-instances=1` is a pairing constraint** (both SFU legs must land on one process): scale calls with relay
  shards, never by raising it (`decisions/voice_web_transport_cloudflare_sfu.md`).

## Economics

- 1 vCPU Cloud Run — async is mandatory.
- No binding budget cap (owner decision 2026-07-26): report cost when asked; the `daily_cost_limit` alert is advisory and
  never gates a request. Cost optimisation is complexity-driven tier selection within Smart.
- **Prices change under you.** A provider can cut prices weeks after GA. `make check-pricing` audits `billing.py` against
  two catalogs that lag a fresh announcement by days — a dated `PRICE_SCHEDULE` entry outranks them. Act on
  `consensus_differs` / `schedule_drift`; verify everything else at the provider (`decisions/openai_gpt56_price_cut.md`).
- Smart's cost sweet spot is a small model with reasoning effort `medium` (`decisions/model_refresh_2026_10.md`).
- Solo-dev — maintainability beats architectural elegance.

## Architecture

Hexagonal Architecture (Ports & Adapters).

```
src/
  domain/       — Models, enums, value objects, pure algorithms. ZERO external deps (stdlib + pydantic only).
                  Key: llm.py (LLMRequest/LLMResponse/Message/MessagePart/ToolCall, PROMPT_CACHE_BOUNDARY, build_tool_turn),
                  entities.py (FactEntity, SCD2-versioned; FactType STATE/EVENT/PRINCIPLE/SYSTEM/ALERT), agent.py
                  (AgentMessage/AgentResponse; AgentConfig: id, type, model, timeout, circuit breaker), messaging.py
                  (MessageContext), user.py (UserBotConfig: tier, provider, per-agent overrides; PerformanceTier
                  ECO/BALANCED/PERFORMANCE), auth.py, retry_policy.py, exceptions.py.
  ports/        — ABC interfaces (domain/ + stdlib only). One port per system boundary (src/ports/CLAUDE.md).
  adapters/     — Port implementations: LLM (Gemini/Claude/Grok/OpenAI), Firestore repos, Slack/Telegram, Gmail,
                  Microsoft Graph (To Do, OneDrive), Node runners (DOCX/Puppeteer), sandbox engine/client, …
                  No cross-subpackage adapter imports (REQ-ARCH-23).
  services/     — Business logic; ports via DI. No concrete-adapter / cross-service imports (REQ-ARCH-22).
  agents/       — Multi-agent system (inherit BaseAgent). core/ — orchestrators; rest — specialists.
  handlers/     — Entry points: ConversationHandler, WorkerHandler (/worker by task_type), AgentWorkerHandler.
  infrastructure/ — AgentCoordinator, queues, agent_config.py, agent_registry.py, agent_manifest.py (Intent constants +
                  all agent declarations — single source of truth), delegation_engine.py, local tool builders.
  composition/  — ServiceContainer, UserAgentFactory (AgentFactoryPort), application_graph.py (`build_application_graph`:
                  every non-web object the main service runs, shared by `main()` and the tests), adapter factories,
                  MCP and sandbox wiring. The only layer allowed to cross all boundaries.
  locales/      — Per-language UI strings (uk/en/fr/es.py).
  config/       — EnvironmentConfig, Settings, AuthConfig.   utils/ — logger, telemetry, file_conversion.
  web/          — Quart apps: OAuth, Cabinet, MCP consent, voice webhooks, landing, sandbox server routes.
  skills/       — System skills (SKILL.md + files), loaded at startup; the repo is public.
main.py         — Main service bootstrap: settings, Firestore, `build_application_graph`, web layer, MCP ASGI dispatcher, /worker.
indexer_main.py — Drive indexer service (`drive_sync`-only /worker).   relay_main.py — voice relay (plain websockets).
job_main.py     — Cloud Run Job (deep research).   sandbox_main.py — untrusted-content sandbox (imports no app layers).
docx_generator/ — Node.js project for DOCX; not a Python package.
```

## File Size Convention

One public class per file. If a file contains more than one public class — split it (a module may waive this on
purpose, saying so in its docstring). File length is not a constraint; mixed responsibilities are.
For files over ~600 lines: put the public interface (class + method signatures with docstrings) at the top, implementation below.

## Layer Semantics (when in doubt, use this to decide where a new class goes)

- **`domain/`** — pure data and algorithms. No I/O, no side effects, no logging. If a class only needs stdlib + pydantic,
  it belongs here regardless of how complex the logic is.
- **`ports/`** — contracts (ABC). One port per system boundary. Do not create a port for a single internal service with
  no substitution need.
- **`services/`** — orchestrate I/O through ports. Do NOT participate in agent routing. Do NOT inherit BaseAgent.
- **`agents/`** — inherit BaseAgent, receive AgentMessage, return AgentResponse, participate in routing via
  AgentCoordinator. Agents never access the database directly — only through services/ports.
- **`handlers/`** — entry points for external events (webhooks, Cloud Task dispatch, Scheduler triggers). A new handler
  only for a new external event source — not because a class is large.
- **`composition/`** — wiring. If constructing an object requires importing from 2+ concrete layers, it belongs here.

## Import Rules (CRITICAL)

```
domain/   → ONLY stdlib, pydantic (dataclasses, enum, typing). Never adapters/, services/, config/, utils/, ports/.
ports/    → domain/ + stdlib + ABC.
adapters/ → domain/, ports/, config/. No cross-subpackage adapter imports (REQ-ARCH-23).
services/ → domain/, ports/. Do NOT import concrete adapters or other services (REQ-ARCH-22).
            Cross-service deps use TYPE_CHECKING guards or constructor injection.
agents/   → Inherit BaseAgent. Receive dependencies via constructor.
```

## Code Conventions

- **All I/O — async/await.** No synchronous calls to DB or LLM.
- **Pydantic BaseModel** (with validation) for domain entities; **@dataclass** for value objects (immutable, no id: MessageContext,
  RoutingMetadata); enums are `(str, Enum)`; factory methods (`AgentResponse.success()`, `AgentMessage.create()`).
- **File naming:** `{entity}_service.py`, `{provider}_adapter.py`, `firestore_{entity}_repo.py`, `{purpose}_agent.py`.
- **Class naming:** `GeminiAdapter(LLMPort)`, `FirestoreFactRepository(FactRepository)`, `QuickResponseAgent(BaseAgent)`.
- **Shared state** protect with `asyncio.Lock`. No exceptions.
- **Errors** log before re-raise. Do not silently swallow exceptions.
- **Do not use print()** — only `from src.utils.logger import logger`.
- **No fallback prompts.** Agents must not contain inline/hardcoded fallback prompts. If `PromptBuilder.build_for_agent()`
  fails — return `AgentResponse.failure()`, do not degrade to an empty or inline prompt. The Firestore prompt (token +
  blueprint + profile) is the single source of truth. Fail fast on missing prompts.

## Patterns

- **Port is justified** when: 2+ implementations, testable substitution, system boundary. **Not needed** for internal
  services with a single implementation.
- **SCD2 versioning** — a content change is a new `FactEntity` document linked by `lineage_id`, `version`,
  `replaces_fact_id` and `superseded_by` (`domain/fact_lifecycle.py`); `is_current` = latest of its lineage, `state` is the
  only visibility switch.
- **Multi-tenant** — always pass account_id. Collections with env prefix.
- Adapter / provider patterns: `src/adapters/CLAUDE.md`. Agent-orchestration patterns: `src/agents/CLAUDE.md` →
  Orchestration Patterns.

## Adding a New Specialist Agent

Follow [`docs/how_to/NEW_AGENT_PLAYBOOK.md`](docs/how_to/NEW_AGENT_PLAYBOOK.md) — **mandatory protocol**; read Phase 0
before writing code, follow the steps in order. Also update [`src/utils/capabilities.py`](src/utils/capabilities.py), the
user-facing capabilities reference returned by the `get_help` intent.

## Adding or Modifying an LLM Adapter

Mandatory protocol: [`docs/how_to/ADAPTER_WIRE_TESTING.md`](docs/how_to/ADAPTER_WIRE_TESTING.md) — wire tests at the SDK
boundary (never the port) + contract validators in `tests/contracts/adapter_contracts.py`. Detail: `src/adapters/CLAUDE.md`.

## Agent Output Format Standards

Every agent with structured LLM output follows the standard in `src/adapters/CLAUDE.md` → Agent Output Format Standards
(`OUTPUT_FORMAT_{AGENT}` token, `_parse_response()` with `json.loads` and no regex fallback, retry on invalid JSON).

## Tests

pytest + pytest-asyncio (`asyncio_mode=auto`). Layout, fixtures, markers, fakes over call assertions, the offline unit
suite and coverage gates: [`tests/CLAUDE.md`](tests/CLAUDE.md). Test rules for editing: the REVIEWER RULE below.

## Decision-Making Protocol (CRITICAL — apply before every non-trivial task)

Skip only when the task is unambiguous, isolated and trivial (typo / rename / single line).

1. **Orient — find the authoritative source.** A relevant RFC in `docs/10_rfcs/`? Read it fully before writing code. If
   the RFC references a POC script in `scripts/`, read the POC fully: it is the implementation spec (only POCs referenced
   from an RFC qualify). No RFC → the existing production code is the spec; read it first.
2. **Gap analysis.** State what the RFC/POC prescribes, what you are about to implement, and any difference (missing
   filter, different algorithm or data structure, altered flow). Any difference → step 4 before step 3.
3. **Uncertainty check.** Stop and ask when the reading is ambiguous, when you found a "simpler" approach than the POC
   (a red flag, not a win), when you are assuming a parameter, filter, threshold or flow, or when the change touches a
   subsystem whose code you have not read.
4. **Explicit delta declaration.** Before writing code, state each difference from the RFC/POC and why, and wait for the
   user's confirmation. Never implement first and explain later; never silently simplify.

## Project Documentation

Detailed docs in `docs/` (arc42): architecture `docs/04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md`,
structure `docs/04_solution_strategy/current_implementation/STRUCTURE.md`, RFCs `docs/10_rfcs/`, risks and technical
debt `docs/12_risks/IMPLEMENTATION_ROADMAP.md`.

**Where to write records:** multi-step refactors with a migration plan → `docs/10_rfcs/`; single backward-looking
decision records (50–150 lines: decision + alternatives + why) → `docs/04_solution_strategy/decisions/`. Do NOT add
records to `docs/09_decisions/` (it keeps only the older ADR-004–ADR-008).

## Language

- Respond to the user in whatever language they write in.
- All changes to documents (docs/, CLAUDE.md, code comments, docstrings, log messages) are written in English.

## ⛔ SECRETS RULE — READ BEFORE TOUCHING ANY FILE

**NEVER write secrets, credentials, infrastructure details, or PII into any git-tracked file.** This includes API keys,
tokens, passwords, signing and webhook secrets; Cloud Run service URLs, project IDs, service account emails; internal
hostnames, IP addresses, resource names; user IDs, account IDs, email addresses; OAuth client IDs/secrets, Firebase
config values.

**The only place for this data is `.env` (gitignored) or GCP Secret Manager.** A Makefile target, script or config that
needs a URL or ID defines it as a variable loaded from `.env`. When in doubt: if it identifies or grants access to
infrastructure, it goes in `.env`.

## ⛔ PERSONAL DATA RULE

**The repo is public. Every example in docs, tests, fixtures, prompts, comments and commit messages is invented**, taken
from the owner's invented universe (rule in [`tests/CLAUDE.md`](tests/CLAUDE.md) § Invented cast). Never take an example
from the owner's memory, `get_user_context`, journals or chat history: no real names, relatives, health values, home,
legal status, documents or contact data. Personal skills are never system skills (`src/skills/` ships in the public repo).
A gate checks every commit and PR (`scripts/pii_gate/`, `.githooks/`, `.github/workflows/pii_gate.yml`); its private
denylist and profile never enter this repo. CI runs the denylist only and prints counts; the model check is local-only.
**Before every push** (any branch: a pushed branch is public) run the local semantic personal-data review described in
the session setup; its findings stay local, and nothing is pushed until it is clean.

## ⛔⛔⛔ Tests — REVIEWER RULE — READ BEFORE TOUCHING ANY TEST FILE ⛔⛔⛔

**The one who writes the code never edits an existing test to make that code pass. A reviewer decides each edit.**
Owner's standing rule since 2026-09-30.

If a code change makes an existing test fail:
1. STOP. The implementer does not touch the test (except the mechanical commit below).
2. Record EXACTLY which test failed and WHY (assertion, actual vs expected).
3. Hand it to a **reviewer**: the task reviewer in subagent-driven work, otherwise a dedicated reviewer subagent. Give
   it the failing test, the intended change and its spec (RFC, plan).
4. The reviewer decides per test:
   - **The test encodes behaviour the spec intentionally changes.** The reviewer edits it and justifies that one test in
     its report.
   - **Otherwise the code is wrong.** The implementer fixes the code.
5. The owner is not asked. A test is never weakened to make code pass: no deleted assertions, no loosened checks, no skips.

New behaviour gets NEW tests. Self-authorized without review: fixing a broken import path caused by a module rename you
just performed. Every other mechanical adaptation (a rename, a signature or constructor-argument change) goes in the
implementer's own commit titled `test(mechanical): ...`; the reviewer reads that commit as a whole and checks one thing:
no assertion changed (a different value, condition or count asserted; renaming an identifier inside an assert after a
rename is mechanical). **Tests of deleted code are deleted in the same PR** by the implementer and listed in the PR body;
the reviewer checks that each deleted test's subject is gone (not a weakening).

This applies to test files (`tests/`), conftest.py, shared test helpers and fixtures. Tests are the specification and
load-bearing: this codebase is AI-pair-programmed, and tests are the only enforcement that runs faster than generated
code. A failing test is signal, not an obstacle. Never propose reducing coverage as "cleanup" or "simplification".

## ⛔⛔⛔ Debugging Cloud Run — MANDATORY PROTOCOL ⛔⛔⛔

**When debugging any issue that manifests in Cloud Run, the FIRST action is ALWAYS to read the actual data. No theories,
no speculation from code alone, no proposed fixes before seeing the actual error.**

### Step 1 — Pull logs LOCALLY, then grep locally (do NOT loop `gcloud logging read`)

`make fetch-logs [K=300]` writes the last K entries to `alek_debug.log`; one fetch, then grep the local file.
- `make logs [K=300]` — quick view; `make logs-tail` — live tail (`make logs-perf` = perf lines only)
- Cloud Run Jobs (deep research): `make fetch-logs-job [K]` → `alek_debug_job.log`; `make logs-job`;
  `make logs-execution EXECUTION=<name>`
- Drive indexer (`[DriveIndex]` lines): `make fetch-logs-indexer [K]` → `alek_debug_indexer.log`; `make logs-indexer`
- Voice relay: `make fetch-logs-relay`; drive specialist lines: `src/agents/CLAUDE.md` → Drive — Logs

### Step 2 — Pick the source by what you need; do NOT conflate them

| Need | Source |
|------|--------|
| Operational events, errors, tracebacks, control flow | **Cloud Logging** (`make fetch-logs`) |
| Actual LLM prompt/response **content** + tokens, bulk/historical SQL over turns | **BigQuery** `alek_observability_dev.prompt_content` (`bq query`) |
| The **cascade** — who delegated to whom, what each agent got and returned, failed calls | **Logfire** (UI, or SQL via the Logfire MCP server) |
| **Memory** over time per process / work unit | **BigQuery** `alek_memory_dev` (queries in `docs/05_building_blocks/memory_telemetry/README.md`) |

- `prompt_content`: one row per LLM call, 30-day TTL. Columns: `trace_id, span_id, timestamp, user_id, account_id,
  agent_id, agent_type, model, provider, turn, request_text, response_text, tool_calls, prompt_tokens,
  completion_tokens, total_tokens`. **A failed LLM call has NO row** (and no span): recording sits on the success path
  of `_call_llm`. Failed calls are visible in **Logfire only**.
- Logfire holds prompt/response content too (`LOGFIRE_CAPTURE_CONTENT=true`); prefer it for relationships between
  calls — BigQuery has no `parent_span_id`, and its `span_id` is the enclosing span.
- `trace_id` in BigQuery carries a `tr_` prefix: strip it (`SUBSTR`) before joining to Logfire.
- Query the multi-line `request_text` with `--format=json` and parse (CSV breaks on embedded newlines):
  ```
  bq query --project_id=<PROJECT_ID> --use_legacy_sql=false --format=json \
    'SELECT request_text, response_text, tool_calls FROM `<PROJECT_ID>.alek_observability_dev.prompt_content`
     WHERE timestamp BETWEEN "<from>" AND "<to>" AND agent_type LIKE "%smart%" ORDER BY turn'
  ```

### Scope discipline

Investigate the **reported incident's time window**. Do NOT trawl unrelated prior-day errors. If checking for
recurrence, search only the **same error signature** — another error class is either analyzed fully or left alone.

### "No `gcloud`" ≠ "no access" (remote/web session)

The same data is reachable from Python via `google.cloud.logging` / `google.cloud.bigquery` with the environment's
service-account credentials.

**Reading the right source costs 1–2 calls. Wrong theories cost 10+ turns and user patience.**

## What NOT to Do

- Do not add DI containers (dependency-injector etc.) — manual DI in `src/composition/` (`application_graph.py`).
- Do not create ports for cleanliness — only when there's a real need.
- Do not commit .env, *-admin-key.json, service-account*.json.
- Do not touch `archive/` — deprecated legacy code.
- All PII or sensitive data exports (Firestore queries, user facts, analysis results) are saved only to `scripts/memory/`
  (gitignored), never to tracked directories.
- Firestore uses the `us-production` named database (the `(default)` database is not used); live data is in the
  `development_`-prefixed collections.
