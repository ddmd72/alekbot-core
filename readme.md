# Alek-Core — Personal Exocortex

A knowledge management system that extends memory and reasoning through Slack and Telegram.
Not a chatbot. A system that accumulates knowledge, thinks in the background, and always responds with context.

**Solo project. Production on GCP (Cloud Run + Firestore).**

---

## What it does

Every conversation produces a stream of facts — preferences, events, decisions, principles.
Alek-Core captures them automatically, deduplicates against existing knowledge, and uses them
to enrich every future response. The system gets smarter with each conversation.

**Core cycle:** user message → Router classifies complexity and enriches with memory/web →
the Smart agent responds with biographical context at a complexity-appropriate model tier →
the consolidation agent extracts new facts in the background → next conversation already knows them.

---

## Architecture

**Hexagonal Architecture (Ports & Adapters).** Domain and business logic have zero infrastructure
dependencies. All I/O goes through ~58 ABC interfaces (ports), with concrete implementations
injected at startup via `ServiceContainer`.

```
src/
  domain/         — Entities, enums, value objects. No external imports.
  ports/          — ~58 ABC interfaces. Only domain/ + stdlib.
  adapters/       — Firestore, Gemini, Claude, Grok, OpenAI, Slack, Telegram, Gmail, Microsoft To Do.
  services/       — Business logic (search enrichment, prompt assembly, fact writing, email indexing).
  agents/         — Multi-agent network. Receive all dependencies via constructor.
  handlers/       — ConversationHandler, ConsolidationHandler, WorkerHandler.
  infrastructure/ — AgentCoordinator, task queues, agent registry, agent manifest.
  composition/    — ServiceContainer: wires ports to adapters at startup.
  locales/        — Per-language UI string modules (uk, en, fr, es).
  web/            — Quart OAuth app + Cabinet UI + remote MCP server.
```

**Agents are provider-agnostic.** Each agent type has a default provider and a per-user
override mechanism. Providers (Gemini, Claude, OpenAI, Grok) are swappable without touching
agent code. Model tier (ECO/BALANCED/PERFORMANCE) is resolved from user config at runtime.

---

## Agent network

The Router runs LLM triage on **every** request — complexity score, tone, semantic lens, search
intent — and triggers memory/web enrichment before routing. The routing target is **always Smart**;
the complexity score drives Smart's per-request **model tier** (ECO → BALANCED → PERFORMANCE), not a
separate cheap-vs-expensive agent. Smart re-evaluates after tool results and can chain further
delegation. Specialists are commissioned through a single `delegate_to_specialist(intent, query)` tool.

| Agent | Default provider | Mode | Role |
|---|---|---|---|
| Router | Gemini | sync | LLM triage on every request: complexity, tone, semantic lens, search intent; triggers memory/web enrichment. Always routes to Smart |
| Smart | provider-agnostic (Gemini) | sync | Primary path for every request; multi-turn reasoning, re-evaluates after tool results; complexity → model tier |
| Quick | Gemini | sync | Not on the primary path. Emergency fallback when Smart fails/times out, and formatter for system notifications |
| Memory | Gemini (ECO) | sync | LLM formulates search keys → multi-vector RRF retrieval; also handles explicit `save_to_memory` |
| WebSearch | provider-native | sync | Provider-native grounded web search; called by Smart. Intents: `search_web`, `fetch_url` |
| EmailSearch | Gemini (BALANCED) | sync | Email archive specialist: semantic search, full-body fetch, attachment parsing |
| EmailClassification | — | sync | Classifies raw emails during indexing; extracts fact sentences — not user-facing |
| FileManagement | — (zero-LLM) | sync | `open_file` (GCS download + text/vision conversion) and `delete_file` — direct port operations |
| Tasks | Gemini | sync | Microsoft To Do CRUD (list/search/create/update/delete); search-before-mutate via short IDs |
| Notes | OpenAI (PERFORMANCE) | sync | Proactive self-reminders: deferred instructions that fire autonomously as new conversations |
| MapsSearch | Gemini (BALANCED) | sync, internal | Place search, route computation, weather via Google Maps AI Grounding (MCP); auto-triggered alongside web search |
| Compute | Gemini (ECO) | sync | Math, datetime, finance via Gemini code-execution sandbox |
| Help | Gemini | sync | User-facing capabilities guide (`get_help`) |
| DocPlanner | Claude | async | DOCX creation entry point: LLM → JSON layout spec → delegates to DocGenerator |
| DocGenerator | Claude | async | Writes Node.js script → subprocess → DOCX bytes; internal (not exposed to LLM) |
| PdfGenerator | Gemini | async | One LLM call → HTML+CSS → Puppeteer renders PDF; delivers GCS link + Slack upload |
| HtmlPageGenerator | Gemini | async | One LLM call → full HTML+CSS+JS page with Unsplash image integration; delivers GCS link |
| DeepResearch | Claude | async | Long-running research jobs; Claude Cloud Run Job (default) or OpenAI webhook |
| Consolidation | Claude (PERFORMANCE) | async | Background long-term memory formation ("Life Chronicler") via Cloud Tasks |

**Cost control is complexity-driven tier selection within Smart:** the Router's complexity score
resolves a cheaper model tier (ECO/BALANCED) for simple requests and reserves top-tier models for
complex ones — instead of routing everything to a single expensive model. (Earlier this was a
Quick-vs-Smart path split; the primary path is now Smart-only.)

Providers are user-configurable per agent. Defaults listed above reflect the production baseline.

Adding a new specialist requires a registry entry in `agent_manifest.py` — no changes to orchestrators.
Also update [`src/utils/capabilities.py`](src/utils/capabilities.py) — the user-facing capabilities
reference returned by `get_help`.

---

## Key mechanisms

### Memory consolidation

Analogous to how the brain consolidates short-term into long-term memory:

- Sliding window fills → oldest batch sent to Cloud Tasks (prod: overflow at 50 messages, batch=30; dev: 70/50)
- `ConsolidationAgent` runs as a separate HTTP request on Cloud Run (full CPU guaranteed)
- Extracts atomic facts and principles; semantic deduplication threshold: 0.96
- 3 vectors per fact (text, tags, metadata) for multi-vector search
- SCD2 versioning — full history preserved, current state always queryable
- `Size_Triggers_Review` rule: facts > 40 words trigger decomposition deliberation before UPDATE —
  compound facts split into atomic parts if independently queryable
- Biographical cache invalidated on write → next conversation sees new facts immediately
- Stalled batches (e.g. a provider outage) are swept hourly by Cloud Scheduler and retried —
  no data lost between session history and memory

### Multi-vector search with RRF

Memory search runs 6 parallel queries across text, tag, and metadata vectors.
Results ranked by **Reciprocal Rank Fusion (RRF)** — facts appearing across multiple query
channels rank higher. Deduplication is number-aware (75 kg ≠ 84 kg).

### Prompt Builder (Token System)

Prompts assembled from verified fragments stored in Firestore, not written inline:

- **Tokens** — reusable building blocks: voice, humor, cognitive process, output format
- **Blueprints** — static templates with `{{CLASS_NAME}}` slot placeholders
- **4 priority levels:** USER > ACCOUNT > AGENT > SYSTEM
- Static template cached in-memory (24h TTL) — 5ms hit vs. 110ms cold assembly
- `PROMPT_CACHE_BOUNDARY` splits each final prompt: static prefix cached by the LLM provider
  (5-min TTL), dynamic suffix (datetime + query-specific context) sent fresh every request

### Layered transient-failure resilience

Three retry layers at different granularities, single-sourced and non-overlapping:

- **In-process** — a shared executor retries transient provider errors (429/503) with exponential
  backoff + jitter; one `RetryPolicy` and one error taxonomy used by both the LLM and embedding paths
- **Cloud Tasks** — the queue re-runs a whole worker task on 5xx (minutes-scale)
- **Application** — re-enqueue / batch-attempt counters / the consolidation sweep (work-item progress)

Layers that nest are prevented from multiplying: Cloud-Task-backed deliveries suppress in-process
retry so the outer queue retry is the single retry.

### Gmail email indexing

Passive inbox-as-memory pipeline:

- User connects Gmail via OAuth; incremental indexing runs on schedule via Cloud Scheduler
- `EmailClassificationAgent` triages each email; valuable ones stored as `IndexedEmail` in Firestore
- 4-vector schema (mirrors the fact schema) — emails are searchable the same way as memory facts
- `EmailSearchAgent` surfaces relevant emails in conversation context

### Daily email review

An opt-in daily briefing assembled by the Smart agent:

- Cloud Scheduler (hourly fan-out) enqueues a per-user job at the user's chosen local hour
- Last 24h of email is triaged ([ACTION]/[FYI]/[DIGEST]/[NOISE]), action items deep-read, context researched
- Output: an HTML report (GCS link, subjects as clickable Gmail links) + a short chat summary, in the user's language

### Proactive self-reminders

The orchestrator sets reminders that fire autonomously and execute as new conversations:

- Tools: `create_self_reminder`, `update_self_reminder`, `delete_self_reminder`
- Cloud Scheduler fires every 15 min → each due reminder executes as a new **Smart-agent** conversation
- One-time: deleted after firing. Recurrent (hourly/daily/weekly/monthly): rescheduled with DST-safe UTC conversion
- Every CRUD immediately notifies the user's channel for transparency

### Remote MCP server

alekbot exposes its memory to claude.ai Custom Connectors as an MCP **server** (the inverse of its
own Maps MCP *client* usage):

- One tool, `get_user_context`, calls the search-enrichment service directly for ~1s retrieval
- Full in-process OAuth 2.1 authorization server (DCR, PKCE, refresh-token rotation); consent bound
  to the Cabinet login. Experimental, dev-only.

### Multilingual support

Two independent language axes:

- **Response language** — controlled via prompt tokens (`LANG_MIRROR` default: respond in user's input language; or fixed to uk/en/fr/es)
- **UI language** — status messages, file prompts, notifications; resolved from user → account → system default

---

## Hexagonal architecture as a guardrail for AI pair-programming

Built solo with heavy AI pairing. The agent failure mode isn't bad code — it's
drift: cross-layer imports, weakened tests, hallucinated intent strings, swallowed
exceptions. The boundaries make drift fail loudly.

- **Layering enforced by an AST test, not discipline.** 36 rule checks
  (`tests/unit/test_req_arch_01_hexagonal_isolation.py`, 1262 lines) parse every
  file's AST and fail on forbidden imports. Resolves relative imports (no dodging),
  excludes `TYPE_CHECKING`. Covers layer isolation, no cross-service/sibling-agent
  imports, no `print`/`getLogger`/env access/provider branching in core, no silent
  `except: pass`, no `assert` in `src/`.
- **Known violations are a tracked whitelist, not silent.** `tests/unit/arch_tech_debt.py` —
  each exception has a root cause + fix plan; removing the entry proves the fix.
- **Tests carry a banner aimed at the AI.** "Failing test = violation in `src/`;
  fix the code, don't weaken the test; owner sign-off required." Stops the agent
  from "fixing" red tests by editing them.
  `tests/unit/test_req_arch_01_hexagonal_isolation.py:18`, `tests/contracts/adapter_contracts.py:4`.
- **`extra="forbid"` on `LLMRequest`.** A `max_tokens`→`max_output_tokens` rename
  was silently dropped; a generator ran on 4–8× smaller budgets for ~46 days.
  Unknown kwargs now raise at construction. `src/domain/llm.py:96`.
- **One source of truth for intents.** `class Intent` + `AgentDescriptor`
  (`src/infrastructure/agent_manifest.py`). Unregistered intent → registry returns
  `None`; can't be invented ad hoc.
- **Adapter behavior pinned by contracts.** `tests/contracts/adapter_contracts.py` —
  named per-provider `ContractRule`s assert on captured SDK calls (e.g. every
  Firestore email query must filter `user_id`); same rules in unit + integration.
- **Four-gate decision protocol in `CLAUDE.md`.** RFC/POC is ground truth; diff
  intent vs. build; stop on any divergence — "found a simpler approach" flagged as
  a red flag, not a win.

---

## Memory subsystem notes

Long-term memory is a fact store with multi-vector retrieval and SCD2 history,
not a flat RAG index. Decisions that aren't obvious:

- **3 vectors per fact, not 1.** Text, tags, and metadata each get a separate
  768-dim embedding. Keyword queries hit `tags_vector`, phrases hit `vector` —
  keeping a keyword out of the same space as prose avoids contaminating retrieval.
  `src/domain/entities.py:125`, `src/services/fact_write_service.py:220`.
- **RRF (k=60) to fuse 6 parallel queries.** `score = Σ 1/(k + rank)` across
  queries; a fact ranked #1 in tags but #50 in text still surfaces. Concatenating
  or averaging scores loses that. `src/services/search_enrichment_service.py:40,357`.
- **Number-aware dedup.** Vector similarity alone deletes contradictory facts: two
  weights sit at ~0.97 similarity. Numbers are extracted and compared first — if
  they differ, not a duplicate, regardless of similarity. Thresholds 0.96/0.98.
  `src/domain/deduplication_service.py:13,98`.
- **SCD2 versioning.** Facts carry `lineage_id`/`valid_from`/`valid_to`/
  `is_current`. Updates don't overwrite — the old version is closed, the new one
  shares the lineage. Full history queryable; can tell *when* a fact changed.
  `src/domain/entities.py:161`.
- **40-word atomic limit.** Consolidation must split facts >40 words so each is
  independently retrievable (one fact per attribute, not a compound sentence
  bundling several). `src/agents/consolidation_agent.py:913`.
- **Consolidation skips semantic dedup.** Dedup at search time would hide merge
  candidates from the consolidation LLM. It runs with `skip_semantic_dedup=True`
  (ID-dedup only) so the agent sees near-duplicates and decides merge/discard
  itself. `src/services/search_enrichment_service.py:196`.
- **Consolidation reads a different serialization than the user.** Model parts use
  `p.text` (summary), not `p.full_text` (verbose + web context); user parts use
  `consolidation_text or text`. Keeps thinking traces and full files out of memory
  formation. `src/agents/base_agent.py:402`.
- **Biographical cache double-invalidation on write.** After consolidation, both
  the repo cache and the PromptBuilder cache are refreshed — miss either and the
  next session's prompt is built on stale facts. `src/agents/consolidation_agent.py:321`.

---

## Stack

- **Runtime:** Python 3.13, asyncio throughout — no synchronous I/O
- **LLM providers:** Google Gemini, Anthropic Claude, OpenAI, Grok (provider-agnostic; per-agent default + per-user override)
- **Infrastructure:** GCP Cloud Run, Firestore (named database `us-production`), Cloud Tasks, Cloud Scheduler
- **Interfaces:** Slack (Socket Mode dev / HTTP Events API prod), Telegram
- **Integrations:** Gmail (OAuth + indexing), Microsoft To Do (Graph API + webhooks), Unsplash, Google Maps (MCP)
- **Observability:** Logfire tracing + a queryable BigQuery LLM content store (prompts/responses, 30-day TTL), joined by trace ID
- **Tests:** pytest + pytest-asyncio

---

## Getting started

```bash
# Local run (Slack Socket Mode)
make dev

# Local run with Firestore emulator
make dev-emulator

# Tests
make check          # unit tests + domain purity check
make test           # full suite
make test-unit      # unit only
make test-e2e-all   # E2E all agents

# Deploy
make deploy-dev     # development environment
make deploy         # production
```

Requires `.env` with API keys and GCP credentials.

---

## Documentation

Architecture and design docs follow the [arc42](https://arc42.org) template:

- **Architecture:** [`docs/04_solution_strategy/`](docs/04_solution_strategy/)
- **Building blocks:** [`docs/05_building_blocks/`](docs/05_building_blocks/)
- **Concepts:** [`docs/08_concepts/`](docs/08_concepts/)
- **RFCs:** [`docs/10_rfcs/`](docs/10_rfcs/)
- **Roadmap:** [`docs/12_risks/IMPLEMENTATION_ROADMAP.md`](docs/12_risks/IMPLEMENTATION_ROADMAP.md)

---

## License

Copyright © 2026 Dmytro Deleur. All rights reserved.

This repository is shared for evaluation purposes only. No copying, distribution,
modification, or reuse without explicit written permission from the copyright holder.
