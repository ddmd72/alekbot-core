# Project Structure

The project is organized into a `src` directory to maintain a clean root. All application logic resides within `src`.

/
├── .dockerignore
├── .gitignore
├── Dockerfile
├── main.py             # Main application entry point
├── Makefile
├── readme.md
├── requirements.txt
├── CHANGELOG.md       # Release history and notable changes
├── cloudbuild-dev.yaml
├── cloudbuild-prod.yaml
├── scripts/            # Utility scripts and maintenance tools
│   ├── README.md       # Scripts index and usage
│   ├── memory/         # Memory migration + operations
│   ├── prompt/         # Prompt debugging & comparison
│   ├── vectors/        # Vector/embedding diagnostics
│   ├── validation/     # Validation & test scripts
│   ├── debug_firestore_latency.py # 🆕 Firestore latency diagnostics
│   └── deprecated/     # Legacy scripts (do not use)
│
├── tests/              # Test suite
│   ├── unit/           # Unit tests
│   ├── integration/    # Integration tests
│   ├── performance/    # 🆕 Performance & latency benchmarks
│   └── ...
│
├── docs/               # All project documentation
│   ├── diagrams/       # Architecture diagrams (C4 + Mermaid)
│   ├── architecture/   # Architecture docs (blueprints, RFCs, reviews)
│   │   ├── implemented/ # ✅ Finalized architectural designs
│   │   ├── rfcs/       # 🧪 Active architectural proposals
│   │   └── deprecated/ # 🗑️ Legacy documentation
│   ├── management/     # Process, sprint, and roadmap docs
│   └── archive/        # 🗄️ Historical reference (RFCs, Plans, Sprints)
│
└── src/                # Core application source code
├── adapters/           # Infrastructure Adapters (Driven Adapters)
│   ├── claude_adapter.py # 🆕 Anthropic Claude Implementation
│   ├── gcp_log_sink.py # 🆕 GCP Cloud Logging adapter
│   ├── gcp_task_queue.py # 🆕 GCP Cloud Tasks adapter
│   ├── firestore_account_repo.py # Firestore AccountRepository implementation
│   ├── firestore_consolidation_queue.py # 🆕 Batch queue for consolidation
│   ├── firestore_dedup_store.py # 🆕 Firestore-backed event deduplication
│   ├── firestore_prompt_repository.py # 🆕 Firestore PromptComponent storage
│   ├── firestore_quota_service.py # Firestore QuotaService implementation
│   ├── firestore_repo.py # Google Firestore Implementation (SCD Type 2)
│   ├── firestore_session_store.py # Session persistence for HTTP mode (90-day TTL)
│   ├── firestore_user_repo.py # Firestore UserRepository implementation
│   ├── gemini_adapter.py # Google Gemini Implementation
│   ├── groovy_prompt_assembler.py # 🆕 Groovy DSL prompt assembler
│   └── slack/          # Slack integration subsystem
│       ├── base.py
│       ├── factory.py
│       ├── http_adapter.py
│       ├── socket_adapter.py
│       └── response_channel.py
├── composition/        # Composition Root (Dependency Injection)
│   ├── __init__.py
│   └── service_container.py # ServiceContainer — owns all shared (singleton-per-worker) services
├── agents/             # 🆕 Multi-Agent System (Specialized Task Handlers)
│   ├── __init__.py
│   ├── base_agent.py   # BaseAgent + CircuitBreaker
│   ├── memory_search_agent.py    # RAG specialist (vector search)
│   ├── web_search_agent.py       # Web search specialist (Gemini Grounding)
│   ├── observation_agent.py      # ⚠️ LEGACY (replaced by session-based consolidation)
│   ├── consolidation_agent.py    # Knowledge synthesis specialist ("Life Chronicler")
│   ├── infrastructure/ # 🆕 Infrastructure Support Agents
│   │   ├── billing_agent.py # Usage reporting (Stub)
│   │   └── logger_agent.py  # Centralized logging (Stub)
│   ├── prompts/        # Agent Prompts (Groovy-style templates)
│   │   ├── __init__.py
│   │   ├── triage_router_v1.groovy # LLM triage prompt for RouterAgent
│   │   ├── consolidation_v2.prompt
│   │   └── consolidation_legacy.prompt
│   └── core/           # Core business agents (routing + response)
│       ├── __init__.py
│       ├── router_agent.py        # Rule-based classification & routing
│       ├── quick_response_agent.py # Fast LLM responses (Gemini Flash)
│       └── smart_response_agent.py # Complex reasoning + agent delegation
├── config/
│   ├── environment.py  # Environment detection & configuration
│   └── settings.py
├── domain/             # Domain Entities (Business Logic)
│   ├── agent.py        # Agent Communication Protocol (ACP) + RoutingMetadata
│   ├── billing.py      # Billing account models
│   ├── consolidation.py # 🆕 ConsolidationBatch models
│   ├── entities.py     # FactEntity & FactType
│   ├── messaging.py    # Messaging DTOs & Protocols
│   ├── prompt.py       # 🆕 Prompt Component models (OwnerType, ComponentScope)
│   ├── search.py       # 🆕 Enriched search context models (EnrichedFact, EnrichedContext)
│   ├── session.py      # 🆕 Session model with Sliding Window logic
│   ├── tone.py         # 🆕 UserTone enum & validation
│   ├── tool_result.py  # Standardized tool results
│   ├── ui_messages.py  # Semantic UI Status Types
│   └── user.py         # User domain entities + PerformanceTier
├── handlers/           # Application Layer (Orchestrators)
│   ├── consolidation_handler.py # 🆕 Batch processing orchestrator
│   ├── conversation_handler.py # Platform-agnostic main orchestrator
│   └── learning_loop.py # ⚠️ LEGACY
├── infrastructure/     # 🆕 System Infrastructure
│   ├── agent_coordinator.py # Central routing hub
│   └── message_queue.py     # Async communication hub
├── locales/            # 🆕 Localization System
│   ├── en.py           # English strings (Stub)
│   └── uk.py           # Ukrainian strings (Primary)
├── ports/              # Port Interfaces (Abstractions)
│   ├── account_repository.py # AccountRepository port
│   ├── log_sink.py     # 🆕 Log sink port
│   ├── consolidation_queue.py # 🆕 Consolidation queue interface
│   ├── file_service.py # 🆕 File management interface
│   ├── quota_service.py # QuotaService port
│   ├── task_queue.py   # 🆕 Task queue port
│   ├── repository.py   # FactRepository interface
│   ├── llm_service.py  # LLM Provider Port
│   ├── session_store.py # 🆕 Session persistence interface
│   └── user_repository.py # UserRepository port
├── services/
│   ├── agent_context_builder.py # 🆕 Resolves Provider/Tier/Model for agents
│   ├── history_summary_service.py # 🆕 LLM-based history compression (Gemini-locked, fail-fast)
│   ├── brain_service.py     # ⚠️ DEPRECATED (kept for reference)
│   ├── cloud_tasks_service.py # Background task scheduling (removed → gcp_task_queue)
│   ├── cost_calculator.py   # Token cost calculation utilities
│   ├── embedding_service.py # Google Text Embeddings
│   ├── file_upload_service.py # 🆕 File management implementation
│   ├── identity_resolver.py # Resolve platform IDs to user identities
│   ├── prompt_builder.py    # Compositional Prompt Builder (Updated for Components)
│   ├── prompt_component_service.py # 🆕 3-level component resolution service
│   ├── provider_registry.py # 🆕 LLM Provider Service Locator
│   ├── search_enrichment_service.py # 🆕 Triple search & weighted merge
│   ├── user_agent_factory.py # Per-user agent instance factory
│   └── user_prompt_builder.py # Per-user prompt customization
├── tools/                # ⚠️ LEGACY Agent Tools (wrapped by agents)
│   ├── base.py
│   ├── memory_search_tool.py
│   └── web_search_agent_tool.py
└── utils/
├── logger.py         # Centralized logging configuration
├── performance_logger.py # Performance timing helpers
├── timer.py          # Timer utilities
├── weather_parser.py # 🆕 Structured weather data extraction
└── server.py

## `src/`

The core application follows **Hexagonal Architecture (Ports & Adapters)** with clear separation of concerns.

### `adapters/` - Infrastructure Adapters (Driven Adapters)
-   **`gcp_log_sink.py`**: GCP Cloud Logging adapter implementing `LogSink`.
-   **`gcp_task_queue.py`**: GCP Cloud Tasks adapter implementing `TaskQueue`.
-   **`firestore_account_repo.py`**: Firestore implementation of `AccountRepository`.
-   **`firestore_consolidation_queue.py`**: 🆕 Manages batches of messages for cold-storage processing.
-   **`fact_management_adapter.py`**: 🆕 `FactManagementAdapter` — implements `FactManagementPort`. Orchestrates `FactRepository`, `EmbeddingService`, `FactWriteService`, `SearchEnrichmentService` for deliberate fact management (search/create/update/merge/discard). Per-user — created via `ServiceContainer.create_fact_management_adapter()`.
-   **`firestore_dedup_store.py`**: 🆕 Deduplication store for external events (e.g., Slack retry attempts).
-   **`firestore_quota_service.py`**: Firestore implementation of `QuotaService` for non-blocking usage tracking.
-   **`firestore_repo.py`**: Firestore implementation of `FactRepository`. Supports SCD Type 2 and native vector search. Receives `SmartDeduplicationService` via DI (no lazy imports).
-   **`firestore_session_store.py`**: Session persistence with **90-day TTL** and sliding window overflow logic.
-   **`platform/`**: Base adapter layer for all messaging platforms:
    -   `base_adapter.py`: Abstract `PlatformAdapter`. Creates shared `ConversationHandler` once in `__init__` (stateless, reused across requests). Subclasses access it via `self.conversation_handler`.
-   **`slack/`**: Slack integration subsystem with dual-mode support:
    -   `base.py`: Abstract `SlackAdapter` base class
    -   `factory.py`: Factory pattern for creating HTTP or Socket adapters
    -   `http_adapter.py`: HTTP Events API adapter for Cloud Run production
    -   `socket_adapter.py`: Socket Mode adapter for local development
    -   `response_channel.py`: Implementation of `ResponseChannel` protocol for Slack

### `composition/` - Composition Root (Dependency Injection)

-   **`service_container.py`**: `ServiceContainer` — created once per worker process. Owns all shared services: LLM adapters (Gemini, Claude, Grok), repositories (FirestoreFactRepository, FirestorePromptComponentRepository), services (ProviderRegistry, AgentContextBuilder, PromptComponentService, BiographicalContextService, ConfigurationService, **FactWriteService**) and `FirestoreSessionStore` with overflow-callback. Provides `agent_services()` dict for injection into `UserAgentFactory`. Also provides `create_fact_management_adapter(search_enrichment_service)` — factory method for per-user `FactManagementAdapter`.

### `config/` - Configuration Layer
-   **`environment.py`**: Centralized environment detection and configuration. Manages `APP_ENV` (development/production/test) and `SLACK_MODE` (http/socket). Provides Firestore collection prefixes for environment isolation.
-   **`settings.py`**: Application settings and constants.

### `domain/` - Domain Layer (Business Logic)
-   **`billing.py`**: Billing account models (`BillingAccount`, `AccountUsageStats`).
-   **`deduplication_service.py`**: 🆕 `SmartDeduplicationService` — number-aware duplicate detection. Lives in `domain/` because it has zero external dependencies (only stdlib `re`). Used by `FirestoreFactRepository` and `SearchEnrichmentService` via DI.
-   **`entities.py`**: Core domain entities: `FactEntity` (with SCD Type 2 fields), `FactType` enum.
-   **`llm.py`**: 🆕 Canonical conversation types: `Message`, `MessagePart`, `ToolCall`. Shared across all ports and services — lives in `domain/` to avoid port→port imports.
-   **`messaging.py`**: Platform-agnostic messaging abstractions (DTOs & Protocols).
-   **`ui_messages.py`**: Centralized `StatusType` enum for semantic UI updates.
-   **`vector_math.py`**: Pure vector math utilities (`cosine_similarity`). Zero external deps.
-   **`tool_result.py`**: Standardized result object for tool executions (`ToolResult`).

### `handlers/` - Application Layer (Orchestrators)
-   **`conversation_handler.py`**: **Primary platform-agnostic orchestrator**. Coordinates agent flow, session persistence, and UI updates. Implements graceful degradation: SmartAgent `TIMEOUT`/`FAILED` → QuickAgent direct fallback with injected `[System: ...]` context note. Raw error text is never exposed to the user.
-   **`consolidation_handler.py`**: 🆕 Orchestrates the sliding window batch processing (Cold Storage).
-   **`learning_loop.py`**: ⚠️ LEGACY.

### `locales/` - Localization Layer
-   **`uk.py` / `en.py`**: Centralized UI strings and phrases for different languages.

### `ports/` - Port Interfaces (Abstractions)
-   **`account_repository.py`**: Port for account-level usage + quota.
-   **`log_sink.py`**: Port for structured logging sinks.
-   **`quota_service.py`**: Port for non-blocking usage tracking and quota management.
-   **`task_queue.py`**: Port for background task queues.
-   **`llm_service.py`**: `LLMService` ABC. Interface for LLM provider operations (generate, stream, upload files). Re-exports `Message`, `MessagePart`, `ToolCall` from `domain/llm.py` for backward compatibility.
-   **`repository.py`**: `FactRepository` ABC (Abstract Base Class). Interface for memory storage operations.
-   **`session_store.py`**: `SessionStore` ABC. Interface for session persistence. Imports `Message` from `domain/llm.py` (not from `ports/llm_service.py`).
-   **`user_repository.py`**: Port for user profile retrieval/storage.

### `services/` - Application Services
-   **`agent_context_builder.py`**: Resolves Provider/Tier/Model for agents. Defines `AgentProviderStrategy` (allowed providers per agent type — including `"postprocessing"` locked to Gemini) and `AgentContextBuilder` (builds `AgentExecutionContext` from user config + strategy).
-   **`history_summary_service.py`**: 🆕 Compresses model responses into ≤300-char session memory entries via a fast LLM call. Always uses Gemini structured output (`response_schema`). Provider locked to Gemini at composition time — immune to user `provider_preference`. Fail-fast: one attempt, `WARNING` on failure, returns `None` (caller stores full text). Injected into `SmartResponseAgent`; designed to be reused by other agents.
-   **`brain_service.py`**: **The orchestrator (provider-agnostic)**. Manages "Fast Path/Slow Path" logic, conversational sessions, manual tool execution loop, prompt building. Depends on `LLMService` port, not specific implementation. (Legacy - replaced by AgentCoordinator flow)
-   **`cost_calculator.py`**: Token cost calculation utilities.
-   **`cloud_tasks_service.py`**: Cloud Tasks integration (legacy, moved to `adapters/gcp_task_queue.py`).
-   **`embedding_service.py`**: Generates embeddings using `text-embedding-004` (Latency <0.5s, multilingual support).
-   **`gcs_service.py`**: (Legacy) Google Cloud Storage interactions for YAML-based memory (deprecated in favor of Firestore).
-   **`identity_resolver.py`**: Resolves platform identities (e.g., Slack ID → user UUID).
-   **`prompt_builder.py`**: Compositional prompt builder with component-level caching. Now supports unified dynamic `biographical_context` component with explicit on-demand invalidation.
-   **`user_brain_factory.py`**: Creates per-user `BrainService` instances for multi-tenant isolation. (Legacy - replaced by UserAgentFactory)
-   **`user_prompt_builder.py`**: Builds per-user system prompts with custom kernel/examples.
-   **`task_queue/`**: Task queue infrastructure for asynchronous processing.

### `tools/` - Encapsulated Agent Tools
-   **`base.py`**: Abstract `BaseTool` class with Circuit Breaker and Retry logic.
-   **`memory_search_tool.py`**: RAG tool for semantic search in user's memory (Firestore vector search).
-   **`web_search_agent_tool.py`**: Google Custom Search integration with weather-specific formatting constraints.

### `agents/` - Multi-Agent System
The multi-agent system enables specialized task handling with different LLM models per agent for cost optimization and performance.

**Infrastructure Support:**
-   **`base_agent.py`**: Abstract `BaseAgent` class with built-in resilience patterns (Circuit Breaker, Retries, Timeouts).
-   **`infrastructure/billing_agent.py`**: Aggregates usage per-user, flushes to QuotaService when threshold reached. asyncio.Lock protects the buffer, `start()` launches periodic flush.
-   **`infrastructure/logger_agent.py`**: Centralized log buffer with asyncio.Lock. `start()` launches periodic flush to GcpLogSink (prod) or stdout (dev).

**Core Agents (`agents/core/`):**
-   **`router_agent.py`**: LLM triage + rule-based fallback routing (complexity threshold=5).
-   **`quick_response_agent.py`**: Fast responses using `gemini-3-flash-preview`.
-   **`smart_response_agent.py`**: Complex reasoning + parallel agent delegation.

**Specialized Agents:**
-   **`memory_search_agent.py`**: RAG specialist (Pure vector search).
-   **`web_search_agent.py`**: Web search specialist using Gemini Grounding.
-   **`consolidation_agent.py`**: Knowledge synthesis specialist ("Life Chronicler"). Uses biographical context caching and vector-based deduplication.
-   **`observation_agent.py`**: ⚠️ LEGACY (kept for reference).

**Agent Infrastructure:**
-   **`infrastructure/agent_coordinator.py`**: Central routing hub with explicit and broadcast routing.
    -   Explicit routing (by agent_id)
    -   Broadcast routing (intent-based, capability-based)
    -   Parallel execution support
    -   Health monitoring and circuit breaker coordination

**Agent Communication Protocol (ACP):**
-   **`domain/agent.py`**: Defines unified communication protocol:
    -   `AgentMessage`: Task requests with intent, payload, context
    -   `AgentResponse`: Structured responses with status, confidence, metadata
    -   `AgentIntent`: QUERY, DELEGATE, INFORM, REQUEST_FEEDBACK
    -   `AgentStatus`: SUCCESS, PARTIAL, FAILED, TIMEOUT, CANNOT_HANDLE
    -   `AgentConfig`: Per-agent configuration (model, timeout, retries, capabilities)
    -   `RoutingMetadata`: Typed routing metadata (tone, complexity, confidence, tools, semantic_lens)

### `utils/` - Utilities
-   **`logger.py`**: Centralized logging configuration (human-readable + trace IDs).
-   **`logging_context.py`**: Context propagation for trace/session/user IDs.
-   **`telemetry.py`**: OpenTelemetry setup and trace helpers.
-   **`performance_logger.py`**: Timing helper for perf logging.
-   **`timer.py`**: Lightweight timer utility.
-   **`server.py`**: HTTP server utilities for Cloud Run (health checks, event endpoints).
