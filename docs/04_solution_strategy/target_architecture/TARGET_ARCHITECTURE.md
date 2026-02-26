# Target Architecture: Alek-Core v6.0 (Staged)

## 📖 HowTo: Using This Document

### Purpose

Defines the target architecture (To-Be) and verified implementation status of Alek-Core.

### When to Read

- **For AI Agents:** Before proposing architecture changes or migrating components.
- **For Architects:** As the source of truth for system direction and milestones.

### When to Update

This document MUST be updated when:

- [ ] Milestone status changes.
- [ ] New architectural patterns are adopted.
- [ ] Core subsystems are added/removed.

### Cross-References

- **Building Blocks Index:** [../../05_building_blocks/README.md](../../05_building_blocks/README.md)
- **RFC Index:** [../../10_rfcs/README.md](../../10_rfcs/README.md)
- **Decisions Index:** [../../09_decisions/README.md](../../09_decisions/README.md)
- **Gap Tracker:** [../../\_project/migration/FEATURE_GAP_ANALYSIS.md](../../_project/migration/FEATURE_GAP_ANALYSIS.md)

---

## 1. Philosophy: Context Defines Truth

We move from flat search (Flat RAG) to **Lensed Perception**. The system adapts retrieval to user role and context, and the architecture is **invariant to infrastructure providers**.

## 2. Application Architecture (Modular & Cloud-Agnostic)

The system follows **Hexagonal Architecture (Ports & Adapters)** with explicit ports for external boundaries.

### 2.1 Layers

1. **Driving Adapters:** Slack, Telegram, Web (platform entrypoints).
2. **Application Layer:** Orchestrators (ConversationHandler, ConsolidationHandler).
3. **Domain Layer:** Multi-agent network (Router, Quick, Smart, Specialist agents).
4. **Driven Adapters:** Provider-specific implementations of ports.

**Port Examples (28 ports total):**

Core:
- `LLMService` → `GeminiAdapter`, `ClaudeAdapter`, `GrokAdapter`
- `FactRepository` → `FirestoreFactRepository`
- `SessionStore` → `FirestoreSessionStore`

Platform decoupling (added 2026-02-21):
- `ConversationHandlerPort` → `ConversationHandler`
- `PlatformAuthPort` → `IAMService`
- `PromptBuilderPort` → `PromptBuilder`

Agent/service decoupling (added 2026-02-21):
- `FactWritePort` → `FactWriteService`
- `SearchEnrichmentPort` → `SearchEnrichmentService`

Infrastructure:
- `TaskQueue` → `GcpTaskQueue`
- `LogSink` → `GcpLogSink`
- `ConsolidationQueue` → `FirestoreConsolidationQueue`

> **Note:** Firestore is treated as an adapter behind ports, not a core architectural dependency.
> See `STRUCTURE.md` for the full 28-port catalog.

## 3. Data Architecture (Memory Graph)

- **SCD Type 2** for fact versioning (`lineage_id`, `valid_from/to`, `is_current`).
- **Fact**, **Anti-Fact**, **Lens** as core domain entities.

## 4. Cognitive Processes

### 4.1 Dynamic LLM Switching

Runtime provider switching via `ProviderRegistry` and tier-based configuration.

### 4.2 Tool Encapsulation (Agent-Oriented)

Native functions are encapsulated by specialist agents (e.g., WebSearchAgent, MemorySearchAgent), orchestrated by SmartResponseAgent.

## 5. Default Infrastructure Stack

- **Compute:** Google Cloud Run (Docker)
- **Database:** Firestore (vector search enabled)
- **LLM Providers:** Gemini + Claude (via adapters)
- **IaC:** Terraform

## 6. Core Building Blocks

The system is composed of 11 core building blocks, each documented in detail:

### 6.1 Agent & Coordination

- **[Multi-Agent System](../../05_building_blocks/multi_agent_system/README.md)** — Actor Model, ACP, UserAgentFactory, AgentCoordinator
- **[Hybrid Router](../../05_building_blocks/hybrid_router/README.md)** — LLM triage + rule-based routing, tone awareness
- **[Quick Agent Delegation](../../05_building_blocks/quick_agent_delegation/README.md)** — QuickAgent delegation loop, WebSearchLightAgent, QUICK_INTENTS

### 6.2 Memory & Context

- **[Sliding Window Consolidation](../../05_building_blocks/sliding_window_consolidation/README.md)** — Hot/cold storage, batch processing
- **[Biographical Context Cache](../../05_building_blocks/biographical_context_cache/README.md)** — High-performance context retrieval
- **[Search Enrichment](../../05_building_blocks/search_enrichment/README.md)** — Triple search strategy, weighted merge

### 6.3 Prompt & Provider Management

- **[Prompt Design System v3](../../05_building_blocks/prompt_design_system_v3/README.md)** — Token-based assembly, 4-level resolution
- **[Provider Resolution](../../05_building_blocks/provider_resolution/README.md)** — Multi-provider tier-based selection

### 6.4 Platform Integration

- **[Slack Dual Mode](../../05_building_blocks/slack_dual_mode/README.md)** — Socket Mode (dev) + HTTP Events API (prod)
- **[Telegram Integration](../../05_building_blocks/telegram_integration/README.md)** — Webhook adapter
- **[Rich Content Protocol](../../05_building_blocks/rich_content_protocol/README.md)** — Structured response rendering

### 6.5 Cross-Cutting Concerns

- **[Observability Strategy](../../05_building_blocks/observability_strategy/README.md)** — Logging, tracing, metrics
- **[Localization System](../../05_building_blocks/localization_system/README.md)** — Multi-language UI support

## 7. Implementation Status (Verified)

### 7.1 ✅ Implemented (Production Ready)

**Hexagonal Core & Agents**

- AgentCoordinator (routing hub)
- UserAgentFactory (per-user agents)
- Core agents: Router, Quick (with delegation loop), Smart
- Specialist agents: MemorySearch, WebSearch, WebSearchLight, Consolidation
- Async message queue (infrastructure layer)

**Session Lifecycle**

- Sliding window (hot storage)
- 90-day TTL
- Consolidation queue
- Biographical context cache
- Event deduplication

**Memory & RAG**

- SCD Type 2 fact storage
- Firestore vector search
- Semantic deduplication
- Prompt Design System v3 (token-based)
- Search enrichment (triple search)

**Infrastructure & Ops**

- Slack dual-mode
- Telegram integration
- Dev/Prod environment isolation
- Structured logging (LogSink)
- Async task queue (TaskQueue)
- OpenTelemetry + Cloud Trace
- Billing foundation
- Hybrid router
- Multi-provider resolution

### 7.2 ✅ Implemented (Milestones 2–4, completed)

- **BillingAgent** — usage aggregation per-user, asyncio.Lock, threshold-based flush to QuotaService, `start()`/`shutdown()` lifecycle
- **LoggerAgent** — centralized log buffer with asyncio.Lock, periodic flush to GcpLogSink (prod) / stdout (dev), `start()`/`shutdown()` lifecycle
- **Graceful shutdown** (SIGTERM/SIGINT) — task draining, agent shutdown sequence ~~GAP-005~~ ✅
- **Composition Root** — `ServiceContainer` extracts shared service creation from `UserAgentFactory`; factory now owns only per-user agent lifecycle
- **Per-user asyncio.Lock** in `UserAgentFactory` — prevents duplicate agent creation under concurrent requests
- **TTL sweep** in `UserAgentFactory` — background eviction of expired user-agent sets (5min interval)
- **Overflow safety** — `FirestoreSessionStore` overflow tracked via `_pending_tasks`, `while` loop handles multi-batch overflow
- **Hexagonal Architecture Cleanup** (2026-02-21) — 5 new ports created (`ConversationHandlerPort`, `PlatformAuthPort`, `PromptBuilderPort`, `FactWritePort`, `SearchEnrichmentPort`), `SlackAdapterFactory` moved to `composition/`, import violations reduced from 29 to 3, all port contracts verified complete (28 ports, 34 contract tests)

### 7.3 📋 Planned (Milestones 5-6)

- **Health checks** (liveness/readiness probes)
- Lens Engine: dynamic weight tuning (Phase 2)
- User onboarding & OAuth
- Admin dashboard

### 7.4 🗑 Deprecated / Legacy

- BrainService (legacy in `src/legacy`)
- ObservationAgent (replaced by consolidation)
- tools/ (replaced by specialist agents)
- YAML memory (replaced by Firestore adapter)

## 8. Architecture Patterns in Action

### 8.1 Dependency Injection Flow

```
main.py
  ├─> EnvironmentConfig
  ├─> ServiceContainer (shared singletons: LLM adapters, repos, services)
  ├─> ProviderRegistry (Gemini, Claude, Grok)
  ├─> UserAgentFactory (per-user agents, receives ports via DI)
  ├─> AgentCoordinator
  ├─> ConversationHandler (implements ConversationHandlerPort)
  ├─> SlackAdapterFactory (composition/ — creates adapter with ports injected)
  └─> adapter.start()
```

### 8.2 Message Flow (Platform-Agnostic)

```
Slack Event
  └─> SlackAdapter
       └─> ConversationHandler
            └─> AgentCoordinator
                 └─> RouterAgent
                      └─> Quick/Smart
                           └─> LLMService
```

### 8.3 Environment Detection

```
APP_ENV=development + SLACK_MODE=http → Cloud Run Dev
APP_ENV=production + SLACK_MODE=http → Cloud Run Prod
Collections: development_domain_facts_v2 / domain_facts_v2
```

## 9. Related Documents

- Building blocks (implemented specs): [../../05_building_blocks/README.md](../../05_building_blocks/README.md)
- Decisions: [../../09_decisions/README.md](../../09_decisions/README.md)
- RFCs: [../../10_rfcs/README.md](../../10_rfcs/README.md)
