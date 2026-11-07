<p align="center">
  <img src="src/web/static/alek.jpg" width="220" alt="Alek, a robot with a visible brain; its visor reads: I remember it so you don't have to.">
</p>

# Alek-Core — Personal Exocortex

An AI extension of memory and thinking that lives in Slack and Telegram, with a voice companion on the phone and in the
browser. Not a chatbot: a system that remembers, works in the background, and answers with context.

**Solo project, in daily use. One live environment on GCP (Cloud Run + Firestore).**
It is also a reference for how I build: multi-agent orchestration, hexagonal architecture, and an engineering system
designed for AI-assisted development.

---

## What it does

- **Long-term memory.** A background agent turns conversations into atomic, versioned facts; every answer is enriched
  with them. Nothing is written to memory from a document without the owner's confirmation.
- **One orchestrator, many specialists.** A router triages every message (complexity, tone, what to look up); the Smart
  agent answers at a model tier matched to the difficulty and commissions specialists for web, maps, email, drive,
  tasks, documents, research, images and video.
- **Voice companion.** A phone call or a browser call with a companion that shares the assistant's character and memory;
  realtime voice runs on more than one provider.
- **Your drive, searchable.** Google Drive and OneDrive connections are indexed once (documents, scans, photos, audio and
  video transcripts), so search and answers from file content need no model call per query. Owner notes on files are
  merged into search.
- **Email as memory.** Gmail is indexed passively; an opt-in daily review triages the inbox into an HTML report.
- **Connectors for Claude.** Memory and a read-only view of the drive are exposed as remote MCP servers, usable as custom
  connectors in claude.ai (OAuth 2.1 with dynamic client registration and PKCE).
- **Skills and a workspace.** Named procedures the assistant loads on demand, drafted from chat and inactive until
  confirmed; a versioned workspace of its own working files.
- **Proactive reminders.** Deferred instructions that fire later as new conversations, one-time or recurring.
- **Documents and media.** DOCX, PDF and HTML pages, deep research reports, images and video.

---

## How it is built

### Hexagonal architecture, enforced by tests

Domain and business logic have no infrastructure dependencies. All I/O goes through ABC ports; adapters are wired by
hand in `src/composition/`. The layer rules are AST-based tests
([`test_req_arch_01_hexagonal_isolation.py`](tests/unit/test_req_arch_01_hexagonal_isolation.py)), not documentation.
The file opens with a policy addressed to AI assistants: a failing rule means fix `src/`, never the test. The few
tolerated exceptions live in a registry ([`arch_tech_debt.py`](tests/unit/arch_tech_debt.py)) that needs the owner's
sign-off; the cross-port and cross-adapter lists are empty.

```
src/
  domain/         — Entities, value objects, pure algorithms. stdlib + pydantic only.
  ports/          — ABC interfaces, one per system boundary.
  adapters/       — LLM providers, Firestore, GCS, KMS, Slack, Telegram, Gmail, Google Drive, Microsoft Graph, voice, sandbox client.
  services/       — Business logic through ports.
  agents/         — The multi-agent network.
  handlers/       — Entry points: chat events, /worker tasks, async agent tasks.
  infrastructure/ — Agent coordinator, delegation engine, agent manifest (single source of truth for intents).
  composition/    — Manual dependency injection; the only layer that crosses all boundaries.
  web/            — OAuth, Cabinet, MCP servers, voice webhooks, landing page.
```

Separate deploy units: the main service, a drive indexer, a voice relay, a Cloud Run Job for deep research, and an
untrusted-content sandbox.

### Provider-agnostic agents

Claude, Gemini, OpenAI and Grok are swappable per agent and per user without touching agent code. A model tier
(ECO / BALANCED / PERFORMANCE) resolves to a concrete model at runtime; the router's complexity score picks the tier for
each request, so simple requests run on small models and hard ones on strong models. Failover and circuit breakers are
per agent. The live roster and default tiers: [`src/agents/CLAUDE.md`](src/agents/CLAUDE.md).

Agents emit structured tool calls instead of doing I/O; the delegation engine executes them (in parallel where
independent) and feeds a tool failure back to the model as an observation, so it corrects itself on the next turn.

### Tests that check the right thing

- **Contract tests at the SDK boundary.** Adapter tests validate the payload sent to each provider SDK, not the text a
  model returns ([`ADAPTER_WIRE_TESTING.md`](docs/how_to/ADAPTER_WIRE_TESTING.md),
  [`adapter_contracts.py`](tests/contracts/adapter_contracts.py)). A weekly job compares the providers' published API
  specs with the recorded contracts.
- **Mutation testing.** A package is done when every semantic mutant is killed or ruled equivalent.
- **Coverage floors per file and per layer** that only rise.
- **Fakes over call assertions;** the unit suite runs offline and in parallel.
- **The reviewer rule.** Whoever writes the code never edits an existing test to make it pass. A failing test goes to a
  reviewer, who decides whether the spec changed or the code is wrong.

### Engineered for AI-assisted development

The codebase is built in daily AI pair-programming, and the engineering system assumes it:

- **Layered context.** A hierarchy of `CLAUDE.md` files (root and per layer) gives every session the local rules and
  traps instead of leaving the model to infer them from code.
- **Decisions are recorded so they are not re-litigated.** RFCs ([`docs/10_rfcs/`](docs/10_rfcs/)) and short decision
  records with rejected alternatives ([`docs/04_solution_strategy/decisions/`](docs/04_solution_strategy/decisions/)).
- **Prompts are data.** Agent prompts are assembled at runtime from versioned tokens; there are no inline fallback
  prompts, and a missing prompt fails fast.
- **The repository is public, so every example is invented.** Docs, tests and fixtures draw on one invented cast; a gate
  checks every commit and pull request for personal data.

### Security

- **Untrusted content runs elsewhere.** Model-written document scripts render in a separate service with no IAM role and
  no secret ([decision](docs/04_solution_strategy/decisions/sandbox_render_service.md)).
- **Secrets at rest** are encrypted with Cloud KMS, bound to their record; verify-only values are stored as keyed digests
  ([decision](docs/04_solution_strategy/decisions/secrets_at_rest_cipher_and_digest.md)).
- **One whitelist** gates chat and Cabinet sign-in; the `/worker` endpoint verifies a Google OIDC token.
- **Text from files and email is marked untrusted** and never treated as an instruction.

### Operations

- **Background work survives Cloud Run.** Consolidation, indexing and reminders run as Cloud Tasks and Cloud Scheduler
  jobs, with stored state for rhythm and a sweep for stalled batches.
- **Long turns.** A chat turn has one absolute deadline instead of a short hard timeout; a late answer is delivered when
  it is ready ([decision](docs/04_solution_strategy/decisions/long_running_turns.md)).
- **Observability.** Logfire traces the delegation cascade; a BigQuery store keeps prompts and responses, joined by trace
  ID; every model call is billed to an execution-scoped ledger.
- **Deployment is manual by choice:** one live environment and model behaviour that tests cannot fully verify make a
  deliberate `make deploy` saner than auto-deploy
  ([decision](docs/04_solution_strategy/decisions/ci_present_cd_deliberately_absent.md)).

---

## Stack

- **Runtime:** Python 3.11, asyncio throughout (no synchronous I/O), Quart
- **Models:** Anthropic Claude, Google Gemini, OpenAI, xAI Grok; realtime voice on OpenAI and xAI
- **GCP:** Cloud Run (services and jobs), Firestore, Cloud Storage, Cloud Tasks, Cloud Scheduler, Cloud KMS, BigQuery
- **Interfaces:** Slack, Telegram, phone (Twilio), browser calls (Cloudflare SFU), MCP
- **Integrations:** Gmail, Google Drive, Microsoft OneDrive and To Do, Google Maps (MCP)
- **Quality:** pytest, ruff, mypy `--strict` on the domain and ports, GitHub Actions

---

## Getting started

```bash
make install-dev    # dependencies + test/lint tools
make check          # lint + typecheck + unit/architecture suite
make ci             # what CI runs: make check + slow tests + coverage gates
make deploy         # build + deploy to Cloud Run
```

The system needs GCP: Cloud Tasks, schedulers, GCS and KMS have no local stand-ins. Setup from scratch:
[`BOOTSTRAP.md`](BOOTSTRAP.md).

---

## Documentation

Architecture docs follow the [arc42](https://arc42.org) template:

- **Architecture and decisions:** [`docs/04_solution_strategy/`](docs/04_solution_strategy/)
- **Building blocks:** [`docs/05_building_blocks/`](docs/05_building_blocks/)
- **Runtime flows:** [`docs/06_runtime/`](docs/06_runtime/)
- **Concepts:** [`docs/08_concepts/`](docs/08_concepts/)
- **RFCs:** [`docs/10_rfcs/`](docs/10_rfcs/)
- **Quality gates:** [`docs/11_quality/`](docs/11_quality/)
- **Risks and technical debt:** [`docs/12_risks/IMPLEMENTATION_ROADMAP.md`](docs/12_risks/IMPLEMENTATION_ROADMAP.md)

---

## License

Copyright © 2026 Dmytro Deleur. All rights reserved.

This repository is source-available for evaluation only. No copying, distribution, modification or reuse without
explicit written permission from the copyright holder.
