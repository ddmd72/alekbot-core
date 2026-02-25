# Alek-Core — Personal Exocortex

A knowledge management system that extends memory and reasoning through Slack and Telegram.
Not a chatbot. A system that accumulates knowledge, thinks in the background, and always responds with context.

**Solo project. Production on GCP (Cloud Run + Firestore).**

---

## What it does

Every conversation produces a stream of facts — preferences, events, decisions, principles.
Alek-Core captures them automatically, deduplicates against existing knowledge, and uses them
to enrich every future response. The system gets smarter with each conversation.

**Core cycle:** user message → router searches memory and classifies intent → specialist agent
responds with biographical context → consolidation agent extracts new facts in the background →
next conversation already knows them.

---

## Architecture

**Hexagonal Architecture (Ports & Adapters).** Domain and business logic have zero infrastructure
dependencies. All I/O goes through 28 ABC interfaces (ports), with concrete implementations
injected at startup via `ServiceContainer`.

```
src/
  domain/         — Entities, enums, value objects. No external imports.
  ports/          — 28 ABC interfaces. Only domain/ + stdlib.
  adapters/       — Firestore, Gemini, Claude, Grok, Slack, Telegram.
  services/       — Business logic (search enrichment, prompt assembly, fact writing).
  agents/         — Multi-agent network. Receive all dependencies via constructor.
  handlers/       — ConversationHandler, ConsolidationHandler.
  infrastructure/ — AgentCoordinator, task queues.
  composition/    — ServiceContainer: wires ports to adapters at startup.
  web/            — Quart OAuth app + Cabinet UI.
```

**Agents are provider-agnostic.** Each agent declares a performance tier (ECO / BALANCED /
PERFORMANCE); a strategy resolves the concrete model at runtime. Providers (Gemini, Claude,
Grok) are swappable without touching agent code.

---

## Agent network

| Agent | Tier | Role |
|---|---|---|
| Router | ECO | Classifies intent, extracts semantic lens, triggers memory search |
| Quick | ECO | Simple answers, <2s, handles ~70% of requests |
| Smart | PERFORMANCE | Complex tasks, tool orchestration, multi-turn reasoning |
| Memory | ECO | Two-phase: LLM formulates search keys → multi-vector RRF retrieval |
| WebSearch | ECO + Grounding | Real-time search (separate agent — cannot combine grounding and function calls) |
| Consolidation | PERFORMANCE | Background long-term memory formation ("Life Chronicler") |

**70% ECO + 30% PERFORMANCE = ~−62% LLM cost** vs. routing everything to a top-tier model.

Smart delegates to specialists via a single generic tool: `delegate_to_specialist(intent, query, context)`.
Adding a new specialist requires only a registry entry — no changes to SmartAgent.

---

## Key mechanisms

### Memory consolidation

Analogous to how the brain consolidates short-term into long-term memory:

- Sliding window (100 messages) fills → oldest 50 batched to Cloud Tasks
- `ConsolidationAgent` runs as a separate HTTP request on Cloud Run (full CPU guaranteed)
- Extracts atomic facts and principles; semantic deduplication threshold: 0.96
- 3 vectors per fact (text, tags, metadata) for multi-vector search
- SCD2 versioning — full history preserved, current state always queryable
- `Size_Triggers_Review` rule: existing facts > 40 words trigger decomposition deliberation before
  any UPDATE — compound facts are split into atomic parts if independently queryable
- Biographical cache invalidated on write → next conversation sees new facts immediately

### Multi-vector search with RRF

Memory search runs up to 7 parallel queries across text, tag, and metadata vectors.
Results are ranked by **Reciprocal Rank Fusion (RRF)** — facts that appear across multiple
query channels are ranked higher. Smart deduplication is number-aware (83 kg ≠ 84 kg).

### Prompt Builder (v3 Token System)

Prompts are assembled from verified fragments stored in Firestore, not written inline:

- **Tokens** — reusable building blocks: voice, humor, cognitive process, output format
- **Blueprints** — static templates with `{{CLASS_NAME}}` slot placeholders (no runtime placeholders)
- **4 priority levels:** USER > ACCOUNT > AGENT > SYSTEM
- Static template cached in-memory (24h TTL) — 5ms hit vs. 110ms cold assembly
- `PROMPT_CACHE_BOUNDARY` marker splits each final prompt: static prefix cached by the LLM
  provider (5-min TTL), dynamic suffix (datetime + query-specific context) sent fresh every request

---

## Stack

- **Runtime:** Python 3.13, asyncio throughout — no synchronous I/O
- **LLM providers:** Google Gemini, Anthropic Claude, Grok (provider-agnostic tier abstraction)
- **Infrastructure:** GCP Cloud Run, Firestore (named database `us-production`), Cloud Tasks
- **Interfaces:** Slack (Socket Mode dev / HTTP Events API prod), Telegram
- **Tests:** pytest + pytest-asyncio, 1184 unit tests

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
