# Alek-Core — Personal Exocortex

A knowledge management system that extends memory and reasoning through Slack and Telegram.
Not a chatbot. A system that accumulates knowledge, thinks in the background, and always responds with context.

**Solo project. Production on GCP (Cloud Run + Firestore).**

---

## What it does

Every conversation produces a stream of facts — preferences, events, decisions, principles.
Alek-Core captures them automatically, deduplicates against existing knowledge, and uses them
to enrich every future response. The system gets smarter with each conversation.

**Core cycle:** user message → router searches memory + classifies intent → specialist agent
responds with biographical context → consolidation agent extracts new facts in the background →
next conversation already knows them.

---

## Architecture

**Hexagonal Architecture (Ports & Adapters).** Domain and business logic have zero infrastructure
dependencies. All I/O goes through ABC interfaces (ports), with concrete implementations injected
at startup via `ServiceContainer`.

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

---

## Agent network

| Agent | Model | Role |
|---|---|---|
| Router | Gemini Flash | Classifies intent, runs semantic memory search, enriches context |
| Quick | Gemini Flash | Simple answers, <2s, handles ~70% of requests |
| Smart | Claude Opus | Complex tasks, tool use, extended reasoning |
| WebSearch | Gemini Flash + Grounding | Real-time search (separate agent — Gemini cannot combine grounding and tool calls) |
| Memory | — | Pure vector search, no LLM, free |
| Consolidation | Claude Opus | Background long-term memory formation |

**Economics:** 70% Flash + 30% Opus = ~−62% LLM cost vs. routing everything to Opus.

---

## Key mechanisms

### Memory consolidation

Analogous to how the brain consolidates short-term into long-term memory:
- Sliding window (100–200 messages) fills → batch enqueued to Cloud Tasks
- `ConsolidationAgent` runs as a separate HTTP request (own CPU, no throttling)
- Extracts atomic facts and principles; deduplication threshold: 0.96
- 3 vectors per fact (text, tags, metadata) for multi-vector search
- SCD2 versioning — full history preserved, current state always queryable
- Biographical cache invalidated on write → next conversation sees new facts immediately

### Prompt Builder (v3 Token System)

Prompts are not hardcoded — they are assembled from verified fragments stored in Firestore:
- **Tokens** — reusable building blocks: voice, humor, cognitive process, output format
- **Blueprints** — static templates with `{{CLASS_NAME}}` slot placeholders
- **4 priority levels:** USER > ACCOUNT > AGENT > SYSTEM
- Static template cached in-memory (24h TTL, 5ms hit vs. 110ms cold)
- `PROMPT_CACHE_BOUNDARY` splits each final prompt: static prefix cached by Anthropic (5 min),
  dynamic suffix (datetime + query-specific context) sent fresh every request

### Memory search

6 parallel vector queries across text, tags, and metadata vectors.
Results ranked by Reciprocal Rank Fusion (RRF). One search per request, shared across all agents.

---

## Stack

- **Runtime:** Python 3.13, asyncio throughout (no sync I/O)
- **LLMs:** Google Gemini Flash / Pro, Anthropic Claude Opus / Sonnet
- **Infrastructure:** GCP Cloud Run, Firestore (named database `us-production`), Cloud Tasks
- **Interfaces:** Slack (Socket Mode), Telegram
- **Tests:** pytest + pytest-asyncio, 1184 unit tests

---

## Getting started

```bash
# Install dependencies
pip install -r requirements.txt

# Local run (Slack Socket Mode)
make dev

# Local run with Firestore emulator
make dev-emulator

# Run tests
make check          # unit tests + domain purity check
make test           # full suite
make test-unit      # unit only

# Deploy
make deploy-dev     # development environment
make deploy         # production
```

Requires `.env` with API keys and GCP credentials. See `docs/` for setup details.

---

## Documentation

Architecture and design docs follow the [arc42](https://arc42.org) template:

- **Architecture:** [`docs/04_solution_strategy/`](docs/04_solution_strategy/)
- **Concepts:** [`docs/08_concepts/`](docs/08_concepts/)
- **RFCs:** [`docs/10_rfcs/`](docs/10_rfcs/)
- **Roadmap:** [`docs/12_risks/IMPLEMENTATION_ROADMAP.md`](docs/12_risks/IMPLEMENTATION_ROADMAP.md)

---

## License

All rights reserved. This repository is shared for evaluation purposes only.
No copying, distribution, or reuse without explicit written permission from the author.
