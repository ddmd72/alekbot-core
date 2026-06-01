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

**Port Examples (~36 ports total):**

Core:
- `LLMPort` → `GeminiAdapter`, `ClaudeAdapter`, `GrokAdapter`, `OpenAIAdapter`
- `FactRepository` → `FirestoreFactRepository`
- `SessionStore` → `FirestoreSessionStore`
- `DeepResearchPort` → `ClaudeDeepResearchAdapter`, `OpenAIDeepResearchAdapter`

Email & Notification (added 2026-02-28):
- `EmailProviderPort` → `GmailProviderAdapter`
- `EmailClassifierPort` → `EmailClassificationAgent`
- `IndexedEmailRepository` → `FirestoreIndexedEmailRepository`
- `EmailIndexingJobRepository` → `FirestoreEmailJobRepository`
- `OAuthCredentialsPort` → `FirestoreOAuthCredentialsAdapter`
- `EmailExclusionsPort` → `FirestoreEmailExclusionsAdapter`
- `NotificationStatePort` → `FirestoreNotificationStateAdapter`
- `NotificationChannelFactoryPort` → `NotificationChannelFactory`

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
> See `STRUCTURE.md` for the full port catalog (~36 ABCs).

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
- **LLM Providers:** Gemini + Claude + Grok + OpenAI (via adapters)
- **IaC:** Terraform

## 6. Core Building Blocks

The system is composed of 11 core building blocks, each documented in detail:

### 6.1 Agent & Coordination

- **[Multi-Agent System](../../05_building_blocks/multi_agent_system/README.md)** — Actor Model, ACP, UserAgentFactory, AgentCoordinator
- **[Hybrid Router](../../05_building_blocks/hybrid_router/README.md)** — LLM triage + rule-based routing, tone awareness
- **[Quick Agent Delegation](../../05_building_blocks/quick_agent_delegation/README.md)** — QuickAgent delegation loop, WebSearchLightAgent, AgentDescriptor intent_remap (search_web → search_web_light)

### 6.2 Memory & Context

- **[Sliding Window Consolidation](../../05_building_blocks/sliding_window_consolidation/README.md)** — Hot/cold storage, batch processing
- **[Biographical Context Cache](../../05_building_blocks/biographical_context_cache/README.md)** — High-performance context retrieval
- **[Search Enrichment](../../05_building_blocks/search_enrichment/README.md)** — Triple search strategy, weighted merge

### 6.3 Prompt & Provider Management

- **[Prompt Design System v3](../../05_building_blocks/prompt_design_system_v3/README.md)** — Token-based assembly, 4-level resolution
- **[Provider Resolution](../../05_building_blocks/provider_resolution/README.md)** — Multi-provider tier-based selection

### 6.4 Platform Integration & External Services

- **[Gmail Email Indexing](../../05_building_blocks/gmail_email_indexing/README.md)** — Passive inbox-as-memory pipeline (OAuth, indexing, classification, search)
- **[Deep Research](../../10_rfcs/DEEP_RESEARCH_RFC.md)** — Provider-agnostic background research (Claude Cloud Run Job / OpenAI webhook)
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
- Core agents: Router, Quick (full tool access, no refinement loop), Smart (complexity 6+, with refinement loop)
- Specialist agents: MemorySearch, WebSearch, WebSearchLight, EmailSearch, MapsSearch, DeepResearch, Consolidation
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
- Telegram integration (webhook, MarkdownV2, rich content delivery)
- Rich Content Protocol — `widget`, `file` (md/html/txt/xlsx/docx) via `PlatformMediaPort`
- `PlaywrightHtmlRenderer` — headless Chromium, widget detection, element.screenshot
- `TelegramMediaAdapter` + `TelegramAdapterFactory` (composition root)
- Dev/Prod environment isolation
- Structured logging (LogSink)
- Async task queue (TaskQueue)
- OpenTelemetry + Cloud Trace
- Billing foundation
- Hybrid router
- Multi-provider resolution

### 7.2 ✅ Implemented (Milestones 2–4, completed)

- **BillingAgent** — usage aggregation per `account_id`, asyncio.Lock, threshold-based flush to `QuotaService`, `start()`/`shutdown()` lifecycle. `BaseAgent` accumulates tokens in `_call_llm()` and flushes via `coordinator` after every `process()` — universal billing for all agents with zero per-agent code.
- **LoggerAgent** — centralized log buffer with asyncio.Lock, periodic flush to GcpLogSink (prod) / stdout (dev), `start()`/`shutdown()` lifecycle
- **Graceful shutdown** (SIGTERM/SIGINT) — task draining, agent shutdown sequence ~~GAP-005~~ ✅
- **Composition Root** — `ServiceContainer` extracts shared service creation from `UserAgentFactory`; factory now owns only per-user agent lifecycle
- **Per-user asyncio.Lock** in `UserAgentFactory` — prevents duplicate agent creation under concurrent requests
- **TTL sweep** in `UserAgentFactory` — background eviction of expired user-agent sets (5min interval)
- **Lazy agent loading** — `AgentFactoryPort` (`src/ports/agent_factory_port.py`) enables on-demand creation of rarely-used agents (doc generation, deep research, file management). `AgentDescriptor.eager=False` marks lazy agents; `AgentCoordinator` triggers creation on first delegation. Saves ~40% per-user initialization cost while keeping intents visible in LLM tool lists from startup
- **Overflow safety** — `FirestoreSessionStore` overflow tracked via `_pending_tasks`, `while` loop handles multi-batch overflow
- **Hexagonal Architecture Cleanup** (2026-02-21) — 5 new ports created (`ConversationHandlerPort`, `PlatformAuthPort`, `PromptBuilderPort`, `FactWritePort`, `SearchEnrichmentPort`), `SlackAdapterFactory` moved to `composition/`, import violations reduced from 29 to 3, all port contracts verified complete (28 ports, 34 contract tests)
- **Gmail Email Indexing** (2026-02-28) — 5 email ABCs, 4 Firestore/Gmail adapters, `EmailIndexingService`, `EmailSearchService`, `EmailClassificationAgent`, `UserNotificationService`, web blueprints (Cabinet UI + Gmail OAuth)
- **Deep Research** (2026-03-01; Gemini backend removed 2026-05-29) — `DeepResearchPort` with two backends: `ClaudeDeepResearchAdapter` (agent_execution Cloud Task → `ClaudeDeepResearchRunnerAgent`, default), `OpenAIDeepResearchAdapter` (webhook delivery). `DeepResearchAgent` (SYNC ACK), `WorkerHandler` (central `/worker` dispatcher), `deep_research_webhooks.py` (OpenAI Svix-format webhook receiver). All paths deliver via `UserNotificationService` — parallel SmartAgent summary + direct report link. Removal of Gemini backend: see [decisions/gemini_deep_research_adapter_removal.md](../decisions/gemini_deep_research_adapter_removal.md)
- **Maps Search** (2026-03-02) — `MapsSearchAgent` with Gemini Maps grounding, `<gmp-place-contextual>` widget HTML delivery via `DeliveryItem(type="html_gcs_link")`
- **OpenAI Provider** (2026-03-03) — `OpenAIAdapter(LLMPort)` for GPT-5 family (nano/mini/full), function calling, JSON mode, streaming, vision
- **Hexagonal Architecture Cleanup v2** (2026-03-05) — `SearchConfig` moved to `domain/settings.py`; `normalize_fact_taxonomy()` domain helper; `MessageRouter` Protocol decouples services/ from infrastructure/; feature flags centralized in `agent_config.py`; `overflow_callback` injection in `ConversationHandler`; agents no longer call `os.getenv()` directly

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
  ├─> ServiceContainer (shared singletons: LLM adapters, repos, email pipeline, services)
  ├─> ProviderRegistry (Gemini, Claude, Grok, OpenAI)
  ├─> UserAgentFactory (per-user agents, receives ports via DI)
  ├─> AgentCoordinator
  ├─> WorkerHandler (central /worker Cloud Tasks dispatcher)
  ├─> PlaywrightHtmlRenderer (lazy init; shared singleton for all platforms)
  ├─> SlackAdapterFactory (composition/ — creates SlackMediaAdapter + RichContentService + ConversationHandler)
  ├─> TelegramAdapterFactory (composition/ — creates TelegramMediaAdapter + RichContentService + ConversationHandler)
  ├─> deep_research_webhooks Blueprint (OpenAI webhook receiver)
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
                           └─> LLMPort
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
