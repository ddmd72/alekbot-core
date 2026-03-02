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
│   ├── firestore_email_exclusions_adapter.py # 🆕 Sender/domain/subject skip patterns
│   ├── firestore_email_job_repo.py # 🆕 Email indexing job journal (resume + status)
│   ├── firestore_iam_adapter.py # 🆕 Firestore IAM (role-based access control)
│   ├── firestore_indexed_email_repo.py # 🆕 IndexedEmail storage + 4-vector search
│   ├── firestore_invite_code_repo.py # Invite code storage
│   ├── firestore_notification_state_adapter.py # 🆕 Last active messaging channel per user
│   ├── firestore_oauth_credentials_adapter.py # 🆕 Gmail OAuth token storage
│   ├── firestore_prompt_repository.py # 🆕 Firestore PromptComponent storage
│   ├── firestore_quota_service.py # Firestore QuotaService implementation
│   ├── firestore_repo.py # Google Firestore Implementation (SCD Type 2)
│   ├── firestore_session_store.py # Session persistence for HTTP mode (90-day TTL)
│   ├── firestore_user_repo.py # Firestore UserRepository implementation
│   ├── firestore_whitelist_repo.py # Email/domain whitelist storage
│   ├── gemini_adapter.py # Google Gemini Implementation
│   ├── gemini_embedding_adapter.py # Text embedding via text-embedding-004
│   ├── gcs_media_adapter.py # 🆕 GCS bucket adapter for media URLs (map_image etc.)
│   ├── gmail_provider_adapter.py # 🆕 Gmail API — list_emails, batch_get_full_content, token refresh
│   ├── groovy_prompt_assembler.py # 🆕 Groovy DSL prompt assembler
│   ├── notification_channel_factory.py # 🆕 Wires Slack/Telegram adapters for UserNotificationService
│   ├── playwright_html_renderer.py # 🆕 HTML → PNG via headless Chromium (HtmlRendererPort)
│   ├── slack/          # Slack integration subsystem
│   │   ├── base.py
│   │   ├── http_adapter.py
│   │   ├── media_adapter.py # 🆕 SlackMediaAdapter — files_upload_v2
│   │   ├── socket_adapter.py
│   │   └── response_channel.py
│   └── telegram/       # 🆕 Telegram integration subsystem
│       ├── media_adapter.py  # TelegramMediaAdapter — send_photo / send_document
│       ├── response_channel.py # TelegramResponseChannel — MarkdownV2, fallback
│       └── webhook_adapter.py  # TelegramWebhookAdapter — HMAC, IAM, routing
├── composition/        # Composition Root (Dependency Injection)
│   ├── __init__.py
│   ├── service_container.py # ServiceContainer — owns all shared (singleton-per-worker) services
│   ├── user_agent_factory.py # 🆕 Per-user agent instance factory (moved from services/ to composition/)
│   ├── slack_adapter_factory.py # Factory for Slack adapter selection (composition root — legal to import all layers)
│   └── telegram_adapter_factory.py # 🆕 Factory for Telegram adapter (composition root — mirrors Slack)
├── agents/             # 🆕 Multi-Agent System (Specialized Task Handlers)
│   ├── __init__.py
│   ├── base_agent.py   # BaseAgent + CircuitBreaker
│   ├── memory_search_agent.py    # RAG specialist (vector search, shared Quick+Smart)
│   ├── web_search_agent.py       # Full web search specialist (Smart path, Gemini Grounding)
│   ├── web_search_light_agent.py # Lightweight grounding specialist (Quick path, ECO tier)
│   ├── email_search_agent.py     # 🆕 Email archive specialist (Smart path, 3 intents)
│   ├── email_classification_agent.py # 🆕 Shared singleton; classifies raw emails via tool-calling
│   ├── observation_agent.py      # ⚠️ LEGACY (replaced by session-based consolidation)
│   ├── consolidation_agent.py    # Knowledge synthesis specialist ("Life Chronicler")
│   ├── infrastructure/ # 🆕 Infrastructure Support Agents
│   │   ├── billing_agent.py # Usage reporting (Stub)
│   │   └── logger_agent.py  # Centralized logging (Stub)
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
│   ├── email.py        # 🆕 Email domain models: OAuthCredentials, EmailMetadata, EmailFullContent,
│   │                   #    EmailClassificationResult, IndexedEmail, IndexingState, IndexingJob, EmailExclusion
│   ├── entities.py     # FactEntity & FactType
│   ├── messaging.py    # Messaging DTOs & Protocols
│   ├── notification.py # 🆕 NotificationChannel (last active platform channel per user)
│   ├── prompt.py       # 🆕 Prompt Component models (OwnerType, ComponentScope)
│   ├── search.py       # 🆕 Enriched search context models (EnrichedFact, EnrichedContext)
│   ├── session.py      # 🆕 Session model with Sliding Window logic
│   ├── tone.py         # 🆕 UserTone enum & validation
│   ├── tool_result.py  # Standardized tool results
│   ├── ui_messages.py  # Semantic UI Status Types
│   └── user.py         # User domain entities + PerformanceTier
├── handlers/           # Application Layer (Orchestrators)
│   ├── agent_worker_handler.py  # 🆕 ASYNC agent execution from Cloud Tasks (ACP v2)
│   ├── consolidation_handler.py # 🆕 Batch processing orchestrator
│   ├── conversation_handler.py # Platform-agnostic main orchestrator
│   ├── worker_handler.py # 🆕 /worker dispatcher — routes Cloud Tasks by task_type:
│   │                     #    email_indexing, email_indexing_watchdog, consolidation, agent_execution
│   └── learning_loop.py # ⚠️ LEGACY
├── infrastructure/     # 🆕 System Infrastructure
│   ├── agent_coordinator.py # Central routing hub + handle_delegation() (ACP v2)
│   ├── agent_registry.py    # 🆕 AgentRegistry — dynamic intent → agent mapping (ACP v2)
│   └── message_queue.py     # Async communication hub
├── locales/            # 🆕 Localization System
│   ├── en.py           # English strings (Stub)
│   └── uk.py           # Ukrainian strings (Primary)
├── ports/              # Port Interfaces (~36 ABCs)
│   ├── llm_service.py  # LLM Provider Port (Gemini, Claude, Grok)
│   ├── repository.py   # FactRepository interface (SCD Type 2)
│   ├── session_store.py # Session persistence interface
│   ├── embedding_service.py # Text embedding port
│   ├── account_repository.py # Billing account port
│   ├── user_repository.py # User profile + platform identity port
│   ├── auth_port.py    # OAuth 2.0 / OIDC authentication port
│   ├── iam_port.py     # Role-based access control port
│   ├── invite_code_repository.py # Invite code management port
│   ├── whitelist_repository.py # Email/domain whitelist port
│   ├── quota_service.py # Usage tracking + quota port
│   ├── conversation_handler_port.py # ConversationHandlerPort (platform adapter decoupling)
│   ├── platform_auth_port.py # PlatformAuthPort + IAMDecision
│   ├── prompt_builder_port.py # PromptBuilderPort (all agents)
│   ├── fact_write_port.py # FactWritePort (agents + adapters)
│   ├── search_enrichment_port.py # SearchEnrichmentPort (router, memory, fact mgmt)
│   ├── fact_management_port.py # Deliberate fact management port
│   ├── consolidation_queue.py # Batch queue management port
│   ├── log_sink.py     # Structured logging sink port
│   ├── task_queue.py   # Background task queue port (+ enqueue_email_indexing_task)
│   ├── file_service.py # File upload port
│   ├── audio_transcription_port.py # Audio transcription port (inactive)
│   ├── html_renderer_port.py # 🆕 HtmlRendererPort ABC + HtmlRenderError
│   ├── platform_media_port.py # 🆕 PlatformMediaPort ABC (upload_image, upload_file)
│   ├── prompt_assembler.py # Prompt assembly port
│   ├── prompt_component_repository.py # Prompt component storage port
│   ├── email_provider_port.py # 🆕 EmailProviderPort — list_emails, batch_get_full_content, refresh_token
│   ├── email_classifier_port.py # 🆕 EmailClassifierPort — classify_batch(emails, credentials)
│   ├── email_exclusions_port.py # 🆕 EmailExclusionsPort — get_exclusions, add_exclusion
│   ├── indexed_email_repository.py # 🆕 IndexedEmailRepository — upsert, search, get_by_id
│   ├── email_indexing_job_repository.py # 🆕 EmailIndexingJobRepository — create, update, get, list stale
│   ├── oauth_credentials_port.py # 🆕 OAuthCredentialsPort — get/save/delete credentials
│   ├── notification_state_port.py # 🆕 NotificationStatePort — get/update last active channel
│   ├── notification_channel_factory_port.py # 🆕 NotificationChannelFactoryPort — get_channel(platform)
│   └── prompt_v3/      # Prompt Design System v3 ports
│       ├── token_repository.py
│       ├── blueprint_repository.py
│       └── agent_profile_repository.py
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
│   ├── email_embedding_repair_service.py # 🆕 Async repair job for emails stored without vectors
│   ├── email_indexing_service.py # 🆕 Paginated inbox-to-Firestore pipeline (fetch → classify → store)
│   ├── email_search_service.py # 🆕 Semantic search in indexed email archive
│   ├── gmail_oauth_service.py # 🆕 Gmail OAuth flow (authorization URL, token exchange)
│   ├── rich_content_service.py # 🆕 File conversion + html_card render + media dispatch
│   ├── search_enrichment_service.py # 🆕 Triple search & weighted merge
│   ├── user_notification_service.py # 🆕 Sends Slack/Telegram alerts; stores last active channel
│   └── user_prompt_builder.py # Per-user prompt customization
├── tools/                # ⚠️ LEGACY Agent Tools (wrapped by agents)
│   ├── base.py
│   ├── memory_search_tool.py
│   └── web_search_agent_tool.py
└── utils/
├── logger.py              # Centralized logging configuration
├── llm_response_parser.py # Unified parser for LLM response envelopes (full_response, response_summary, rich_content)
├── performance_logger.py  # Performance timing helpers
├── timer.py               # Timer utilities
├── weather_parser.py      # 🆕 Structured weather data extraction
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
    -   `base_adapter.py`: Abstract `PlatformAdapter`. Receives `ConversationHandlerPort` and `PlatformAuthPort` via constructor injection. Does not instantiate any concrete handlers.
-   **`playwright_html_renderer.py`**: 🆕 `PlaywrightHtmlRenderer(HtmlRendererPort)` — headless Chromium singleton. Renders HTML to PNG via `element.screenshot(omit_background=True)`. Detects widget structure (bare fragment vs full-page) via `body.children.length`. Lazy init, auto-reconnect, `--no-sandbox` on Cloud Run.
-   **`gcs_media_adapter.py`**: 🆕 `GcsMediaAdapter` — uploads bytes to GCS and returns a public URL. Used for `weather_image` / `map_image` types. Slack only (no GCS on Telegram).
-   **`slack/`**: Slack integration subsystem with dual-mode support:
    -   `base.py`: Abstract `SlackAdapter` base class
    -   `http_adapter.py`: HTTP Events API adapter for Cloud Run production
    -   `socket_adapter.py`: Socket Mode adapter for local development
    -   `response_channel.py`: Implementation of `ResponseChannel` protocol for Slack
    -   `media_adapter.py`: 🆕 `SlackMediaAdapter(PlatformMediaPort)` — `files_upload_v2` for images and files
-   **`telegram/`**: 🆕 Telegram integration subsystem:
    -   `webhook_adapter.py`: `TelegramWebhookAdapter` — HMAC validation, IAM, deduplication, routing to `ConversationHandler`
    -   `response_channel.py`: `TelegramResponseChannel` — MarkdownV2 formatting, 3-layer fallback, chunking
    -   `media_adapter.py`: 🆕 `TelegramMediaAdapter(PlatformMediaPort)` — `bot.send_photo(BytesIO)` / `bot.send_document(BytesIO)`

### `composition/` - Composition Root (Dependency Injection)

-   **`service_container.py`**: `ServiceContainer` — created once per worker process. Owns all shared services: LLM adapters (Gemini, Claude, Grok), repositories (FirestoreFactRepository, FirestorePromptComponentRepository), services (ProviderRegistry, AgentContextBuilder, PromptComponentService, BiographicalContextService, ConfigurationService, **FactWriteService**) and `FirestoreSessionStore` with overflow-callback. Also owns all email infrastructure: `FirestoreIndexedEmailRepository`, `FirestoreOAuthCredentialsAdapter`, `GmailProviderAdapter`, `EmailSearchService`, `EmailIndexingService`, `EmailClassificationAgent` (shared singleton). Provides `agent_services()` dict for injection into `UserAgentFactory`. Also provides `create_fact_management_adapter(search_enrichment_service)` — factory method for per-user `FactManagementAdapter`.
-   **`user_agent_factory.py`**: 🆕 `UserAgentFactory` — per-user agent instance factory. Moved from `services/` to `composition/` (legal to import all layers). Caches agent sets per user (1h TTL). Resolves 3-level config (USER > ACCOUNT > SYSTEM) during instantiation.
-   **`slack_adapter_factory.py`**: `SlackAdapterFactory` — creates the appropriate Slack adapter (Socket or HTTP) based on `EnvironmentConfig`. Lives in `composition/` because it imports from `handlers/` (to create `ConversationHandler`) and `infrastructure/` — layers that `adapters/` cannot legally access. Creates `SlackMediaAdapter` → `RichContentService` → `ConversationHandler`, passes as `ConversationHandlerPort` to the platform adapter.
-   **`telegram_adapter_factory.py`**: 🆕 `TelegramAdapterFactory` — mirrors `SlackAdapterFactory`. Creates `TelegramMediaAdapter` → `RichContentService(html_renderer=html_renderer)` → `ConversationHandler` → `TelegramWebhookAdapter`. Receives shared `html_renderer` singleton from `main.py`.

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
-   **`agent_worker_handler.py`**: 🆕 Handles ASYNC agent execution payloads from Cloud Tasks (`task_type="agent_execution"`). Routes via `coordinator.route_message()`.
-   **`conversation_handler.py`**: **Primary platform-agnostic orchestrator**. Coordinates agent flow, session persistence, and UI updates. Implements graceful degradation: SmartAgent `TIMEOUT`/`FAILED` → QuickAgent direct fallback with injected `[System: ...]` context note. Raw error text is never exposed to the user.
-   **`consolidation_handler.py`**: 🆕 Orchestrates the sliding window batch processing (Cold Storage). Overflow trigger enqueues `task_type="consolidation"` via Cloud Tasks (own HTTP request = full CPU); manual `$consolidate` awaits directly in the worker request. Both patterns keep the HTTP request alive to prevent Cloud Run CPU throttling.
-   **`worker_handler.py`**: 🆕 `WorkerHandler` — central dispatcher for all `/worker` Cloud Tasks payloads. Dispatches by `task_type`: `agent_execution` → `AgentWorkerHandler`; `email_indexing` → paginated email indexing, re-enqueues on `next_page_token`, sends user notification on completion; `email_indexing_watchdog` → marks stale `running` jobs as `failed`; `consolidation` → one batch, re-enqueues if more remain. Falls back to `slack_adapter._handle_worker_task()` for unknown types.
-   **`learning_loop.py`**: ⚠️ LEGACY.

### `locales/` - Localization Layer
-   **`uk.py` / `en.py`**: Centralized UI strings and phrases for different languages.

### `ports/` - Port Interfaces (Abstractions)

~36 port interfaces organized by domain concern:

**Core Ports:**
-   **`llm_service.py`**: `LLMService` ABC. Interface for LLM provider operations (generate, stream, upload files). Re-exports `Message`, `MessagePart`, `ToolCall` from `domain/llm.py` for backward compatibility. Adapters: `GeminiAdapter`, `ClaudeAdapter`, `GrokAdapter`.
-   **`repository.py`**: `FactRepository` ABC. Interface for memory storage operations (SCD Type 2, vector search). Adapter: `FirestoreFactRepository`.
-   **`session_store.py`**: `SessionStore` ABC. Interface for session persistence (load, save, append, batch append). Adapter: `FirestoreSessionStore`.
-   **`embedding_service.py`**: `EmbeddingService` ABC. Text embedding generation. Adapter: `GeminiEmbeddingAdapter`.

**User & Account Ports:**
-   **`account_repository.py`**: Port for billing account operations + quota management. Adapter: `FirestoreAccountRepository`.
-   **`user_repository.py`**: Port for user profile CRUD + platform identity linking. Adapter: `FirestoreUserRepository`.
-   **`auth_port.py`**: `AuthPort` ABC. OAuth 2.0 / OIDC authentication (authorization URL, token exchange, verification). Adapter: `FirebaseAuthAdapter`.
-   **`iam_port.py`**: `IAMPort` ABC. Role-based access control (OWNER/MEMBER/VIEWER, resource-level permissions). Adapter: `FirestoreIAMAdapter`.
-   **`invite_code_repository.py`**: Port for invite code management (create, consume, list). Adapter: `FirestoreInviteCodeRepository`.
-   **`whitelist_repository.py`**: Port for email/domain whitelist management. Adapter: `FirestoreWhitelistRepository`.
-   **`quota_service.py`**: Port for non-blocking usage tracking and quota management. Adapter: `FirestoreQuotaService`.

**Platform & Handler Ports:**
-   **`conversation_handler_port.py`**: `ConversationHandlerPort` ABC — injected into all platform adapters (Slack, Telegram). Decouples adapters from the concrete `ConversationHandler` in `handlers/`.
-   **`platform_auth_port.py`**: `PlatformAuthPort` ABC + `IAMDecision` dataclass — injected into all platform adapters for centralized authorization. Implemented by `IAMService`.

**Agent & Service Ports:**
-   **`prompt_builder_port.py`**: `PromptBuilderPort` ABC — injected into all 6 agents (RouterAgent, QuickResponseAgent, SmartResponseAgent, WebSearchAgent, WebSearchLightAgent, ConsolidationAgent). Implemented by `PromptBuilder`.
-   **`fact_write_port.py`**: `FactWritePort` ABC — injected into `ConsolidationAgent`, `FactManagementAdapter`, and `UserAgentFactory`. Implemented by `FactWriteService`.
-   **`search_enrichment_port.py`**: `SearchEnrichmentPort` ABC — injected into `RouterAgent`, `MemorySearchAgent`, and `FactManagementAdapter`. Implemented by `SearchEnrichmentService`.
-   **`fact_management_port.py`**: `FactManagementPort` ABC. Deliberate fact management (search, create, merge, discard). Adapter: `FactManagementAdapter`.
-   **`consolidation_queue.py`**: `ConsolidationQueue` ABC. Batch queue management (enqueue, get pending, update status, cleanup). Adapter: `FirestoreConsolidationQueue`.

**Infrastructure Ports:**
-   **`log_sink.py`**: Port for structured logging sinks. Adapter: `GcpLogSink`.
-   **`task_queue.py`**: Port for background task queues (also defines `enqueue_email_indexing_task`). Adapter: `GcpTaskQueue`.
-   **`file_service.py`**: Port for file upload to LLM providers.
-   **`audio_transcription_port.py`**: `AudioTranscriptionPort` ABC. Audio-to-text transcription (not yet active).
-   **`platform_media_port.py`**: 🆕 `PlatformMediaPort` ABC — `upload_image(bytes, alt_text, channel_id)` and `upload_file(bytes, filename, title, channel_id)`. Implemented by `SlackMediaAdapter` and `TelegramMediaAdapter`.
-   **`html_renderer_port.py`**: 🆕 `HtmlRendererPort` ABC + `HtmlRenderError`. `render(html, width=480) → bytes`. `HtmlRenderError` lives in ports/ so services can catch it without violating import rules. Implemented by `PlaywrightHtmlRenderer`.

**Email & Notification Ports:**
-   **`email_provider_port.py`**: 🆕 `EmailProviderPort` ABC — `list_emails`, `batch_get_full_content`, `refresh_token`. Adapter: `GmailProviderAdapter`.
-   **`email_classifier_port.py`**: 🆕 `EmailClassifierPort` ABC — `classify_batch(emails, credentials)`. Adapter: `EmailClassificationAgent`.
-   **`email_exclusions_port.py`**: 🆕 `EmailExclusionsPort` ABC — `get_exclusions`, `add_exclusion`. Adapter: `FirestoreEmailExclusionsAdapter`.
-   **`indexed_email_repository.py`**: 🆕 `IndexedEmailRepository` ABC — upsert, semantic search (4-vector RRF), `get_by_id`, `get_pending_embeddings`. Adapter: `FirestoreIndexedEmailRepository`.
-   **`email_indexing_job_repository.py`**: 🆕 `EmailIndexingJobRepository` ABC — create, update, get, list stale running jobs. Adapter: `FirestoreEmailJobRepository`.
-   **`oauth_credentials_port.py`**: 🆕 `OAuthCredentialsPort` ABC — `get_credentials`, `save_credentials`, `delete_credentials`. Adapter: `FirestoreOAuthCredentialsAdapter`.
-   **`notification_state_port.py`**: 🆕 `NotificationStatePort` ABC — `get_channel`, `update_channel`. Adapter: `FirestoreNotificationStateAdapter`.
-   **`notification_channel_factory_port.py`**: 🆕 `NotificationChannelFactoryPort` ABC — `get_channel(platform)` → platform response adapter. Implemented by `NotificationChannelFactory`.

**Prompt v3 Ports:**
-   **`prompt_assembler.py`**: `PromptAssembler` ABC. Format-agnostic prompt assembly. Adapter: `GroovyPromptAssembler`.
-   **`prompt_component_repository.py`**: Port for prompt component storage (defaults, user overrides, agent-specific). Adapter: `FirestorePromptComponentRepository`.
-   **`prompt_v3/token_repository.py`**: Port for token (prompt fragment) storage with categorization. Adapter: `FirestoreTokenRepository`.
-   **`prompt_v3/blueprint_repository.py`**: Port for blueprint (prompt template) storage. Adapter: `FirestoreBlueprintRepository`.
-   **`prompt_v3/agent_profile_repository.py`**: Port for agent profile storage with 4-level priority resolution. Adapter: `FirestoreAgentProfileRepository`.

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
-   **`email_embedding_repair_service.py`**: 🆕 Async repair job that fetches emails with `embedding_pending=True` and computes their vectors.
-   **`email_indexing_service.py`**: 🆕 Paginated inbox-to-Firestore pipeline. One call = one page of emails (fetch → classify → store). Writes `IndexedEmail` to Firestore; sets `embedding_pending=True` for async vector computation. Returns updated `IndexingJob` with `next_page_token` for resume.
-   **`email_search_service.py`**: 🆕 Semantic search in indexed email archive (4-vector RRF). Also delegates to `GmailProviderAdapter` for live email body / attachment fetch when `Mode B` (deep search) is needed.
-   **`gmail_oauth_service.py`**: 🆕 Web-only service for Gmail OAuth flow (authorization URL generation, authorization code exchange). Not part of the indexing pipeline.
-   **`rich_content_service.py`**: 🆕 `RichContentService` — converts agent `RichContent` DTOs into platform-specific media. Routes `file` types through format converters (openpyxl for xlsx, python-docx for docx). Routes `html_card` through `HtmlRendererPort` → PNG bytes → `PlatformMediaPort.upload_image`. Routes GCS-based types through `GcsMediaAdapter`. Wired at composition root — never imported by agents or handlers.
-   **`user_notification_service.py`**: 🆕 `UserNotificationService` — sends platform alerts (Slack/Telegram) for background events (e.g., email indexing complete). Stores/reads last active channel via `NotificationStatePort`; delegates send to coordinator → QuickAgent.
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
-   **`quick_response_agent.py`**: Fast responses (BALANCED tier). Has a delegation loop (`MAX_DELEGATION_TURNS=2`) for `QUICK_INTENTS = {"search_memory", "search_web_light"}`. Memory-first parallel scheduling. `_clean_history_for_quick` strips tool turns from history. Outputs JSON (`full_response`, `response_summary`, `rich_content`) via `parse_llm_response`.
-   **`smart_response_agent.py`**: Complex reasoning + specialist delegation via `delegate_to_specialist(intent, query)` — generic ACP v2 tool. Available intents injected dynamically from `AgentRegistry`. Memory-first parallel scheduling for `search_memory` intent.

**Specialized Agents:**
-   **`memory_search_agent.py`**: Two-phase memory retrieval: (1) LLM key formulation — Gemini Flash converts the delegation query into 3 optimized search keys (keywords, primary_query, alternative_query) + optional domains using `COGNITIVE_PROCESS_MEMORY_SEARCH` Firestore token; (2) multi-vector RRF search via `SearchEnrichmentService`. Schema enforced at API level: 3–5 keywords, 2 domains max (enum), 50-char query limit. Shared specialist — called from both Quick (`search_memory`) and Smart (`search_memory`).
-   **`web_search_light_agent.py`**: Lightweight single-pass grounding specialist (ECO tier). Called exclusively by QuickResponseAgent via `search_web_light` intent. Single Gemini + Google Search grounding call. Returns plain Slack mrkdwn. Prompt via PromptBuilder v3 (`agent_type="websearch_light"`).
-   **`web_search_agent.py`**: Full-depth web search specialist using Gemini Grounding (BALANCED tier). Called exclusively by SmartResponseAgent via `search_web` intent.
-   **`email_search_agent.py`**: 🆕 Email archive specialist (BALANCED tier). Called by SmartResponseAgent via 3 intents: `search_emails` (semantic search in `domain_email_facts_v1`), `get_email_details` (fetch full body from Gmail), `get_email_attachment` (parse attachment via markitdown). Registered in `AgentRegistry` at startup.
-   **`email_classification_agent.py`**: 🆕 Shared singleton agent (created in `ServiceContainer`, not per-user). Classifies raw `EmailMetadata` + snippets via tool-calling mode. Outputs `EmailClassificationResult` per email. Called by `EmailIndexingService` (not by the agent delegation chain). Exception to the OUTPUT_FORMAT rule: uses markdown code block extraction in `_parse_response()` due to tool-calling + JSON mode incompatibility — see inline comment.
-   **`consolidation_agent.py`**: Knowledge synthesis specialist ("Life Chronicler"). Uses biographical context caching and vector-based deduplication.
-   **`observation_agent.py`**: ⚠️ LEGACY (kept for reference).

**Agent Infrastructure:**
-   **`infrastructure/agent_coordinator.py`**: Central routing hub with explicit and broadcast routing.
    -   Explicit routing (by agent_id), broadcast routing, parallel execution
    -   `handle_delegation(intent, query, context)` — ACP v2 entry point; resolves via AgentRegistry, routes SYNC or ASYNC
    -   `get_available_intents()` — proxies to registry for SmartAgent tool description injection
-   **`infrastructure/agent_registry.py`**: 🆕 ACP v2 registry.
    -   `AgentRegistry.register(AgentManifest)` — maps intents → manifests
    -   `AgentManifest(agent_id, intents: Dict[str, ExecutionMode], description, requires_auth)`
    -   `ExecutionMode`: SYNC (inline) or ASYNC (Cloud Tasks + callback)

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
