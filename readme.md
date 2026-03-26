# Alek-Core — Personal Exocortex

A knowledge management system that extends memory and reasoning through Slack and Telegram.
Not a chatbot. A system that accumulates knowledge, thinks in the background, and always responds with context.

**Solo project. Production on GCP (Cloud Run + Firestore).**

---

## What it does

Every conversation produces a stream of facts — preferences, events, decisions, principles.
Alek-Core captures them automatically, deduplicates against existing knowledge, and uses them
to enrich every future response. The system gets smarter with each conversation.

**Core cycle:** user message → router classifies complexity and searches memory → specialist agent
responds with biographical context → consolidation agent extracts new facts in the background →
next conversation already knows them.

---

## Architecture

**Hexagonal Architecture (Ports & Adapters).** Domain and business logic have zero infrastructure
dependencies. All I/O goes through ~56 ABC interfaces (ports), with concrete implementations
injected at startup via `ServiceContainer`.

```
src/
  domain/         — Entities, enums, value objects. No external imports.
  ports/          — ~56 ABC interfaces. Only domain/ + stdlib.
  adapters/       — Firestore, Gemini, Claude, Grok, Slack, Telegram, Gmail, Microsoft To Do.
  services/       — Business logic (search enrichment, prompt assembly, fact writing, email indexing).
  agents/         — Multi-agent network. Receive all dependencies via constructor.
  handlers/       — ConversationHandler, ConsolidationHandler, WorkerHandler.
  infrastructure/ — AgentCoordinator, task queues, agent registry, agent manifest.
  composition/    — ServiceContainer: wires ports to adapters at startup.
  locales/        — Per-language UI string modules (uk, en, fr, es).
  web/            — Quart OAuth app + Cabinet UI.
```

**Agents are provider-agnostic.** Each agent declares a performance tier (ECO / BALANCED /
PERFORMANCE); a strategy resolves the concrete model at runtime. Providers (Gemini, Claude,
Grok) are swappable without touching agent code.

---

## Agent network

| Agent | Tier | Mode | Role |
|---|---|---|---|
| Router | ECO | sync | Classifies complexity (1–10), triggers memory search, builds enriched context |
| Quick | ECO | sync | Handles complexity 1–5 (~70% of requests); full tool access, single-pass |
| Smart | PERFORMANCE | sync | Handles complexity 6–10; multi-turn reasoning with re-evaluation after tool results |
| Memory | ECO | sync | LLM formulates search keys → multi-vector RRF retrieval; also handles explicit `save_to_memory` |
| WebSearch | BALANCED | sync | Single-pass Google Grounding with synthesis; called by Smart |
| WebSearchLight | ECO | sync | Single-pass Google Grounding; called by Quick (tool remap at dispatch time) |
| EmailSearch | BALANCED | sync | Email archive specialist: semantic search, full body fetch, attachment parsing |
| EmailClassification | BALANCED | sync | Classifies raw emails; extracts fact sentences for indexing — not user-facing |
| DocPlanner | PERFORMANCE | async | DOCX creation entry point: LLM → JSON layout spec → delegates to DocGenerator |
| DocGenerator | PERFORMANCE | async | Writes Node.js script → subprocess → DOCX bytes; internal (not exposed to LLM) |
| PdfGenerator | PERFORMANCE | async | One LLM call → HTML+CSS → Puppeteer renders PDF; delivers GCS link + Slack upload |
| HtmlPageGenerator | PERFORMANCE | async | One LLM call → full HTML+CSS+JS page with Unsplash image integration; delivers GCS link |
| Tasks | BALANCED | sync | Microsoft To Do: list, search, create, update, delete tasks with recurrence support |
| Notes | ECO | sync | Proactive self-reminders: deferred instructions that fire autonomously on a schedule |
| MapsSearch | BALANCED | sync | Place search, route computation, weather via Google Maps AI Grounding (MCP) |
| Compute | ECO | sync | Math, datetime, finance calculations via Gemini code execution sandbox |
| DeepResearch | PERFORMANCE | async | Long-running research jobs; provider-agnostic (Gemini polling / OpenAI webhook) |
| Consolidation | PERFORMANCE | async | Background long-term memory formation ("Life Chronicler") via Cloud Tasks |

**70% ECO + 30% PERFORMANCE = ~−62% LLM cost** vs. routing everything to a top-tier model.

Quick and Smart are functionally identical in tool access. Two differences only: Quick skips
re-evaluation after tool results; Quick remaps `search_web` → `search_web_light` at dispatch time.

Adding a new specialist requires a registry entry in `agent_manifest.py` — no changes to orchestrators.

---

## Key mechanisms

### Memory consolidation

Analogous to how the brain consolidates short-term into long-term memory:

- Sliding window (100–200 messages) fills → oldest batch sent to Cloud Tasks
- `ConsolidationAgent` runs as a separate HTTP request on Cloud Run (full CPU guaranteed)
- Extracts atomic facts and principles; semantic deduplication threshold: 0.96
- 3 vectors per fact (text, tags, metadata) for multi-vector search
- SCD2 versioning — full history preserved, current state always queryable
- `Size_Triggers_Review` rule: facts > 40 words trigger decomposition deliberation before UPDATE —
  compound facts split into atomic parts if independently queryable
- Biographical cache invalidated on write → next conversation sees new facts immediately

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

### Gmail email indexing

Passive inbox-as-memory pipeline:

- User connects Gmail via OAuth; incremental indexing runs on schedule via Cloud Scheduler
- `EmailClassificationAgent` triages each email; valuable ones stored as `IndexedEmail` in Firestore
- 4-vector schema (mirrors fact schema) — emails are searchable the same way as memory facts
- `EmailSearchAgent` surfaces relevant emails in conversation context

### Proactive self-reminders

The orchestrator sets reminders that fire autonomously and execute as new conversations:

- 3 tools: `create_self_reminder`, `update_self_reminder`, `delete_self_reminder`
- Cloud Scheduler fires every 15 min → due reminders trigger `QuickAgent` as a new conversation
- One-time: deleted after firing. Recurrent (hourly/daily/weekly/monthly): rescheduled with DST-safe UTC conversion
- Every CRUD immediately notifies the user's channel for transparency

### Multilingual support

Two independent language axes:

- **Response language** — controlled via prompt tokens (`LANG_MIRROR` default: respond in user's input language; or fixed to uk/en/fr/es)
- **UI language** — status messages, file prompts, notifications; resolved from user → account → system default

---

## Stack

- **Runtime:** Python 3.13, asyncio throughout — no synchronous I/O
- **LLM providers:** Google Gemini, Anthropic Claude, Grok (provider-agnostic tier abstraction)
- **Infrastructure:** GCP Cloud Run, Firestore (named database `us-production`), Cloud Tasks, Cloud Scheduler
- **Interfaces:** Slack (Socket Mode dev / HTTP Events API prod), Telegram
- **Integrations:** Gmail (OAuth + indexing), Microsoft To Do (Graph API + webhooks), Unsplash, Google Maps (MCP)
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

All rights reserved. This repository is shared for evaluation purposes only.
No copying, distribution, or reuse without explicit written permission from the author.
