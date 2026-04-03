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
├── pdf_generator/      # Node.js project for PDF rendering via Puppeteer
│   ├── package.json    # puppeteer ^24.x (downloads bundled Chromium ~170 MB during npm install)
│   └── runner.js       # Puppeteer wrapper: reads HTML from stdin, writes PDF bytes to stdout
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
│   ├── gemini_deep_research_adapter.py # 🆕 GeminiDeepResearchAdapter — wraps Gemini Deep Research SDK (google-genai)
│   ├── gemini_embedding_adapter.py # Text embedding via text-embedding-004
│   ├── gcs_file_storage_adapter.py # 🆕 GcsFileStorageAdapter — FileStoragePort impl; user file attachments with Finder-style dedup
│   ├── gcs_media_adapter.py # 🆕 GCS bucket adapter for public media URLs (map images, Deep Research HTML reports)
│   ├── grok_adapter.py     # 🆕 Grok (xAI) LLMPort implementation
│   ├── gmail_provider_adapter.py # 🆕 Gmail API — list_emails, batch_get_full_content, token refresh
│   ├── groovy_prompt_assembler.py # 🆕 Groovy DSL prompt assembler
│   ├── node_puppeteer_runner.py # 🆕 NodePuppeteerRunner — PuppeteerRunnerPort impl; pipes HTML to
│   │                            #    pdf_generator/runner.js via stdin, captures PDF bytes from stdout
│   ├── notification_channel_factory.py # 🆕 Wires Slack/Telegram adapters for UserNotificationService
│   ├── openai_adapter.py  # 🆕 OpenAI Responses API LLMPort (gpt-5.4-nano/mini/full)
│   ├── openai_deep_research_adapter.py # 🆕 OpenAI Responses API DeepResearchPort (webhook delivery)
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
│   ├── base_agent.py   # BaseAgent + CircuitBreaker + lifecycle hooks (_on_agent_start,
│   │                   #   _on_agent_success, _on_agent_error, _on_delegation) + debug helpers
│   │                   #   (_debug_prompt, _debug_response, _format_history_for_debug)
│   ├── memory_search_agent.py    # FactsMemoryAgent — unified memory specialist (Quick+Smart).
│   │                             #   Two intents: search_memory (RAG/vector search via SearchEnrichmentService)
│   │                             #   + save_to_memory (explicit user save → attaches consolidation_text
│   │                             #   to user message via history_context → flows through normal pipeline).
│   ├── web_search_agent.py       # Full web search specialist (Smart path, Gemini Grounding)
│   ├── web_search_light_agent.py # Lightweight grounding specialist (Quick path, ECO tier)
│   ├── email_search_agent.py     # 🆕 Email archive specialist (Quick+Smart, 3 intents)
│   ├── email_classification_agent.py # 🆕 Shared singleton; classifies raw emails via tool-calling
│   ├── maps_search_agent.py      # 🆕 Google Maps specialist (location/place search, Gemini Maps grounding)
│   ├── deep_research_agent.py    # 🆕 Provider-agnostic Deep Research specialist. SYNC ACK —
│   │                             #    calls DeepResearchPort.create_interaction(query, user_id, account_id,
│   │                             #    original_query) and returns. Delivery mechanism (Cloud Task polling
│   │                             #    or webhook) is adapter-internal. No task_queue in agent.
│   ├── pdf_generator_agent.py    # 🆕 PDF creation (intent: create_pdf, ASYNC, BALANCED tier,
│   │                             #    internal=False). Single LLM call: natural language → complete
│   │                             #    HTML+CSS (auto-selects design language from style catalogue).
│   │                             #    NodePuppeteerRunner renders to PDF. Filename extracted from
│   │                             #    <title> tag. Returns two DeliveryItem("document"):
│   │                             #    HTML (GCS only) + PDF (GCS + Slack upload).
│   ├── html_page_generator_agent.py  # 🆕 HTML page creation (intent: create_html_page, ASYNC,
│   │                             #    PERFORMANCE tier, internal=False). Single LLM call: natural
│   │                             #    language → complete HTML+CSS+JS. No subprocess — HTML is
│   │                             #    final artifact. PromptBuilder mandatory (agent_type="html_page").
│   │                             #    Filename from <title> tag. Returns one DeliveryItem("document"):
│   │                             #    HTML (GCS public URL → Slack link, file_upload=False).
│   ├── file_management_agent.py  # 🆕 Zero-LLM file storage agent (intents: open_file,
│   │                             #    delete_file). Downloads from GCS + converts (text) or returns
│   │                             #    binary via file_data metadata (LLM vision). No LLM calls.
│   ├── observation_agent.py      # ⚠️ LEGACY (replaced by session-based consolidation)
│   ├── consolidation_agent.py    # Knowledge synthesis specialist ("Life Chronicler")
│   ├── infrastructure/ # 🆕 Infrastructure Support Agents
│   │   ├── billing_agent.py # Usage aggregation per account_id → QuotaService flush
│   │   └── logger_agent.py  # Centralized logging (Stub)
│   └── core/           # Core business agents (routing + response)
│       ├── __init__.py
│       ├── router_agent.py        # Rule-based classification & routing
│       ├── quick_response_agent.py # Fast LLM responses (BALANCED tier). Functionally equivalent
│       │                           #   to Smart — same intent set via AgentDescriptor.allowed_intents.
│       │                           #   Two differences: (1) single-pass (no refinement loop);
│       │                           #   (2) descriptor.intent_remap substitutes search_web→search_web_light.
│       │                           #   Class-level _descriptor = QUICK_RESPONSE (from agent_manifest.py).
│       │                           #   MAX_DELEGATION_TURNS=5. Memory-first parallel scheduling.
│       └── smart_response_agent.py # Complex reasoning + specialist delegation via ACP v2 registry.
│                                   #   Class-level _descriptor = SMART_RESPONSE (from agent_manifest.py).
├── config/
│   ├── environment.py  # Environment detection & configuration
│   └── settings.py     # Re-exports SearchConfig from domain/settings.py for backward compat
├── domain/             # Domain Entities (Business Logic)
│   ├── agent.py        # Agent Communication Protocol (ACP) + RoutingMetadata
│   ├── auth.py         # Auth domain models: TokenClaims, OAuthTokens, OAuthUserInfo, IAMDecision
│   ├── billing.py      # Billing account models
│   ├── consolidation.py # 🆕 ConsolidationBatch models
│   ├── email.py        # 🆕 Email domain models: OAuthCredentials, EmailMetadata, EmailFullContent,
│   │                   #    EmailClassificationResult, IndexedEmail, IndexingState, IndexingJob, EmailExclusion
│   ├── entities.py     # FactEntity & FactType + normalize_fact_taxonomy() helper
│   ├── llm.py          # Core LLM types: LLMRequest, LLMResponse, Message, MessagePart, ToolCall,
│   │                   #    ProviderCapabilities, UsageMetadata, PromptCacheConfig, CacheMetadata,
│   │                   #    AutomaticFunctionCallingConfig + PROMPT_CACHE_BOUNDARY constant.
│   │                   #    MessagePart.consolidation_text: invisible to LLM adapters; read only by
│   │                   #    consolidation serializer (overflow callback + $consolidate path).
│   ├── messaging.py    # Messaging DTOs & Protocols
│   ├── notification.py # 🆕 NotificationChannel (last active platform channel per user)
│   ├── prompt.py       # 🆕 Prompt Component models (OwnerType, ComponentScope)
│   ├── search.py       # 🆕 Enriched search context models (EnrichedFact, EnrichedContext)
│   ├── session.py      # 🆕 Session model with Sliding Window logic
│   ├── settings.py     # 🆕 SearchConfig dataclass (moved from config/ for hexagonal purity)
│   ├── tone.py         # 🆕 UserTone enum & validation
│   ├── tool_result.py  # Standardized tool results
│   ├── ui_messages.py  # Semantic UI Status Types
│   └── user.py         # User domain entities + PerformanceTier
├── handlers/           # Application Layer (Orchestrators)
│   ├── agent_worker_handler.py  # 🆕 ASYNC agent execution from Cloud Tasks (ACP v2)
│   ├── consolidation_handler.py # 🆕 Batch processing orchestrator
│   ├── conversation_handler.py # Platform-agnostic main orchestrator. Graceful degradation delegated to AgentFallbackService.
│   ├── deep_research_delivery.py # 🆕 Shared deep research delivery helpers:
│   │                     #    upload_html_report()    — wrap markdown in HTML, upload to GCS (debug only)
│   │                     #    _upload_round()         — upload raw markdown round text to GCS as .md file
│   │                     #    deliver_deep_research() — upload round files, send named links, enqueue HtmlPageGenerator task
│   │                     #    NotificationPort — structural Protocol for UserNotificationService
│   ├── worker_handler.py # 🆕 /worker dispatcher — routes Cloud Tasks by task_type:
│   │                     #    email_indexing, email_indexing_watchdog, consolidation, agent_execution,
│   │                     #    deep_research_polling (Gemini polling path: polls interaction → deliver_deep_research())
│   └── learning_loop.py # ⚠️ LEGACY
├── infrastructure/     # 🆕 System Infrastructure
│   ├── agent_config.py      # 🆕 Central registry of tunable behavior parameters for all agents.
│   │                        #    Also holds centralized feature flags: ENABLE_HISTORY_OPTIMIZATION,
│   │                        #    ENABLE_GROUNDING_ATTRIBUTION (read from env once at import time).
│   ├── agent_coordinator.py # Central routing hub + handle_delegation() (ACP v2)
│   ├── agent_manifest.py    # 🆕 Single source of truth for agent declarations.
│   │                        #    Intent — typed constants for all intent strings (no raw literals).
│   │                        #    AgentDescriptor instances for ALL agents (specialists + orchestrators):
│   │                        #      Specialists: MEMORY_SEARCH, WEB_SEARCH, WEB_SEARCH_LIGHT, EMAIL_SEARCH, MAPS_SEARCH, DEEP_RESEARCH_AGENT, PDF_PLANNER, PDF_GENERATOR
│   │                        #        → registered via ALL_DESCRIPTORS in main.py
│   │                        #      Orchestrators: QUICK_RESPONSE, SMART_RESPONSE
│   │                        #        → set as class-level _descriptor in agent classes
│   ├── agent_registry.py    # 🆕 AgentDescriptor dataclass + AgentRegistry mechanics (ACP v2).
│   │                        #    AgentDescriptor: identity + capabilities (intents, internal flag)
│   │                        #    + requirements (allowed_intents, intent_remap). AgentManifest = alias.
│   │                        #    get_available_intents() filters internal=True agents.
│   │                        #    get_available_intents_for(descriptor) filters by allowed_intents.
│   └── message_queue.py     # Async communication hub
├── locales/            # 🆕 Localization System
│   ├── en.py           # English strings (Stub)
│   └── uk.py           # Ukrainian strings (Primary)
├── ports/              # Port Interfaces (~41 ABCs)
│   ├── llm_port.py  # LLM Provider Port (Gemini, Claude, Grok)
│   ├── repository.py   # FactRepository interface (SCD Type 2)
│   ├── session_store.py # Session persistence interface
│   ├── dedup_store.py  # Event deduplication port (is_duplicate, try_mark_processed)
│   ├── embedding_service.py # Text embedding port
│   ├── account_repository.py # Billing account port
│   ├── user_repository.py # User profile + platform identity port
│   ├── auth_port.py    # OAuth 2.0 / OIDC authentication port (AuthPort ABC only; data models in domain/auth.py)
│   ├── security_port.py # 🆕 SecurityPort ABC — validate(message) → ValidationResult. Prompt injection + trust zone enforcement.
│   ├── platform_port.py # 🆕 PlatformPort ABC — start/stop/get_platform_name/_translate_platform_files. Base for Slack + Telegram adapters.
│   ├── iam_port.py     # Role-based access control port
│   ├── invite_code_repository.py # Invite code management port
│   ├── whitelist_repository.py # Email/domain whitelist port
│   ├── quota_service.py # Usage tracking + quota port
│   ├── conversation_handler_port.py # ConversationHandlerPort (platform adapter decoupling)
│   ├── platform_auth_port.py # PlatformAuthPort
│   ├── prompt_builder_port.py # PromptBuilderPort (all agents)
│   ├── fact_write_port.py # FactWritePort (agents + adapters)
│   ├── search_enrichment_port.py # SearchEnrichmentPort (router, memory, fact mgmt)
│   ├── fact_management_port.py # Deliberate fact management port
│   ├── consolidation_queue.py # Batch queue management port
│   ├── log_sink.py     # Structured logging sink port
│   ├── task_queue.py   # Background task queue port (enqueue_email_indexing_task, enqueue_deep_research_polling)
│   ├── deep_research_port.py # 🆕 DeepResearchPort — create_interaction(query, user_id, account_id,
│   │                         #    original_query) → interaction_id. Delivery is adapter-specific:
│   │                         #    Gemini adapter enqueues Cloud Task; OpenAI adapter uses webhook metadata.
│   ├── file_service.py # File upload port
│   ├── file_storage_port.py # 🆕 FileStoragePort ABC — user file attachments (upload/download/delete/exists/get_url)
│   ├── audio_transcription_port.py # Audio transcription port (inactive)
│   ├── html_renderer_port.py # 🆕 HtmlRendererPort ABC + HtmlRenderError
│   ├── platform_media_port.py # 🆕 PlatformMediaPort ABC (upload_image, upload_file)
│   ├── maps_tools_port.py # 🆕 MapsToolsPort ABC (search_maps_places)
│   ├── media_storage_port.py # 🆕 MediaStoragePort ABC (upload_bytes → public URL)
│   ├── puppeteer_runner_port.py # 🆕 PuppeteerRunnerPort ABC + PuppeteerRunnerError — system
│   │                            #    boundary for Node.js Puppeteer subprocess (HTML → PDF bytes)
│   ├── prompt_assembler.py # Prompt assembly port
│   ├── prompt_component_repository.py # Prompt component storage port
│   ├── prompt_cache_strategy_port.py # 🆕 PromptCacheStrategyPort — resolve cache config for agent type
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
│   ├── agent_fallback_service.py # 🆕 AgentFallbackService — 3-level fallback chain: primary failure → QuickAgent → synthetic apology
│   ├── history_summary_service.py # 🆕 LLM-based history compression (Gemini-locked, fail-fast)
│   ├── brain_service.py     # ⚠️ DEPRECATED (kept for reference)
│   ├── cloud_tasks_service.py # Background task scheduling (removed → gcp_task_queue)
│   ├── cost_calculator.py   # Token cost calculation utilities
│   ├── embedding_service.py # Google Text Embeddings
│   ├── file_upload_service.py # 🆕 File management implementation
│   ├── identity_resolver.py # Resolve platform IDs to user identities
│   ├── prompt_builder.py    # Compositional Prompt Builder + UserPromptBuilder (merged)
│   ├── prompt_component_service.py # 🆕 3-level component resolution service
│   ├── provider_registry.py # 🆕 LLM Provider Service Locator
│   ├── email_embedding_repair_service.py # 🆕 Async repair job for emails stored without vectors
│   ├── email_indexing_service.py # 🆕 Paginated inbox-to-Firestore pipeline (fetch → classify → store)
│   ├── email_search_service.py # 🆕 Semantic search in indexed email archive
│   ├── gmail_oauth_service.py # 🆕 Gmail OAuth flow (authorization URL, token exchange)
│   ├── document_delivery_service.py # 🆕 Stores document bytes (HTML, PDF) to GCS via MediaStoragePort.
│   │                                #    Key format: docs/{uuid4()}-{filename}. Used by PdfGeneratorAgent.
│   ├── file_conversion_service.py # 🆕 Centralized file upload to GCS + on-demand content resolution.
│   │                              #    process_attachment → reference-only MessagePart; resolve_content → text.
│   ├── rich_content_service.py # 🆕 File conversion + widget render + media dispatch
│   ├── search_enrichment_service.py # 🆕 Triple search & weighted merge
│   └── user_notification_service.py # 🆕 Sends Slack/Telegram alerts; stores last active channel
├── tools/                # ⚠️ LEGACY Agent Tools (wrapped by agents)
│   ├── base.py
│   ├── memory_search_tool.py
│   └── web_search_agent_tool.py
├── web/                  # 🆕 Quart web app (OAuth, Cabinet UI, webhooks)
│   ├── auth_blueprint.py      # OAuth 2.0 login/callback/refresh/logout
│   ├── cabinet_blueprint.py   # Cabinet UI (Gmail status, indexing control)
│   ├── deep_research_webhooks.py # 🆕 OpenAI Deep Research webhook receiver (Svix HMAC verification)
│   └── gmail_blueprint.py     # Gmail OAuth connect/disconnect + indexing API
└── utils/
├── file_conversion.py     # File-to-text conversion (convert_file_to_text, is_native_binary, make_history_stub)
├── logger.py              # Centralized logging configuration
├── debug_logger.py        # PromptDebugLogger — local/GCS prompt+response debug dumps (DEBUG_PROMPTS=true)
├── llm_response_parser.py # Unified parser for LLM response envelopes (full_response, response_summary, rich_content)
├── groovy_to_markdown_transformer.py # Groovy DSL → Markdown converter (USE_MARKDOWN_PROMPT feature, currently off)
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
-   **`firestore_quota_service.py`**: Firestore implementation of `QuotaService`. Takes `AccountRepository` via DI; calls `increment_account_usage(account_id, tokens, cost)` directly — no user→account lookup.
-   **`firestore_repo.py`**: Firestore implementation of `FactRepository`. Supports SCD Type 2 and native vector search. Receives `SmartDeduplicationService` via DI (no lazy imports).
-   **`firestore_session_store.py`**: Session persistence with **90-day TTL** and sliding window overflow logic.
-   **`platform/`**: Platform adapter factory:
    -   `factory.py`: `PlatformAdapterFactory` — registry of `PlatformPort` implementations; `create(platform, **kwargs)`. The `PlatformPort` ABC lives in `ports/platform_port.py`.
-   **`openai_adapter.py`**: 🆕 `OpenAIAdapter(LLMPort)` — OpenAI Responses API implementation. Native web search with agentic reasoning, function calling, JSON mode, vision. Tier mapping: ECO→gpt-5.4-nano, BALANCED→gpt-5.4-mini, PERFORMANCE→gpt-5.4.
-   **`openai_deep_research_adapter.py`**: 🆕 `OpenAIDeepResearchAdapter(DeepResearchPort)` — Responses API with background mode. Webhook-based push delivery (no polling Cloud Tasks). Metadata (user_id, account_id, query) embedded at submit time and echoed back by OpenAI in the webhook payload. Tier mapping: ECO/BALANCED→o4-mini-deep-research, PERFORMANCE→o3-deep-research.
-   **`node_puppeteer_runner.py`**: 🆕 `NodePuppeteerRunner(PuppeteerRunnerPort)` — pipes HTML to `pdf_generator/runner.js` via stdin, captures raw PDF bytes from stdout. Error cases: non-zero exit code, timeout, or empty stdout → `PuppeteerRunnerError`. Temp file cleanup guaranteed in `finally` block.
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
-   **`settings.py`**: Application settings and constants. Re-exports `SearchConfig` from `domain/settings.py` for backward compatibility.

### `domain/` - Domain Layer (Business Logic)
-   **`billing.py`**: Billing account models (`BillingAccount`, `AccountUsageStats`).
-   **`deduplication_service.py`**: 🆕 `SmartDeduplicationService` — number-aware duplicate detection. Lives in `domain/` because it has zero external dependencies (only stdlib `re`). Used by `FirestoreFactRepository` and `SearchEnrichmentService` via DI.
-   **`entities.py`**: Core domain entities: `FactEntity` (with SCD Type 2 fields), `FactType` enum. Also contains `normalize_fact_taxonomy()` — centralizes `.lower()` normalization of 4D taxonomy fields (`domain`, `temporal_class`, `state`, `context_priority`) for enum compatibility. Used by `FactManagementAdapter` and `FirestoreFactManagementAdapter` to eliminate duplicated normalization code.
-   **`auth.py`**: Auth domain models moved from `ports/`: `TokenClaims`, `OAuthTokens`, `OAuthUserInfo`, `IAMDecision`. Pure data, zero external dependencies — belong in `domain/`, not `ports/`.
-   **`llm.py`**: Core LLM domain types — `Message`, `MessagePart`, `ToolCall`, `LLMRequest`, `LLMResponse`, `ProviderCapabilities`, `UsageMetadata`, `PromptCacheConfig`, `CacheMetadata`, `AutomaticFunctionCallingConfig` + `PROMPT_CACHE_BOUNDARY` constant. Moved from `ports/llm_port.py` (TD-V2 2026-03-08) so ports import domain, not each other.
-   **`messaging.py`**: Platform-agnostic messaging abstractions (DTOs & Protocols).
-   **`ui_messages.py`**: Centralized `StatusType` enum for semantic UI updates.
-   **`settings.py`**: 🆕 `SearchConfig` dataclass — tiered semantic search limits, biographical query patterns, keyword sets. Moved from `config/settings.py` to `domain/` for hexagonal purity: services import domain/, but must not import config/. `config/settings.py` re-exports for backward compatibility.
-   **`vector_math.py`**: Pure vector math utilities (`cosine_similarity`). Zero external deps.
-   **`tool_result.py`**: Standardized result object for tool executions (`ToolResult`).

### `handlers/` - Application Layer (Orchestrators)
-   **`agent_worker_handler.py`**: 🆕 Handles ASYNC agent execution payloads from Cloud Tasks (`task_type="agent_execution"`). Routes via `coordinator.route_message()`.
-   **`conversation_handler.py`**: **Primary platform-agnostic orchestrator**. Coordinates agent flow, session persistence, and UI updates. Receives `overflow_callback` via constructor injection (not direct import of `consolidation_handler`) to avoid horizontal coupling between handlers. Delegates graceful degradation to `AgentFallbackService` — any `TIMEOUT`/`FAILED` response triggers the three-level fallback chain (QuickAgent → synthetic apology). Raw error text is never exposed to the user.
-   **`consolidation_handler.py`**: 🆕 Orchestrates the sliding window batch processing (Cold Storage). Overflow trigger enqueues `task_type="consolidation"` via Cloud Tasks (own HTTP request = full CPU); manual `$consolidate` awaits directly in the worker request. Both patterns keep the HTTP request alive to prevent Cloud Run CPU throttling.
-   **`worker_handler.py`**: 🆕 `WorkerHandler` — central dispatcher for all `/worker` Cloud Tasks payloads. Dispatches by `task_type`: `agent_execution` → `AgentWorkerHandler`; `email_indexing` → paginated email indexing, re-enqueues on `next_page_token`, sends user notification on completion; `email_indexing_watchdog` → marks stale `running` jobs as `failed`; `consolidation` → one batch, re-enqueues if more remain. Falls back to `slack_adapter._handle_worker_task()` for unknown types.
-   **`learning_loop.py`**: ⚠️ LEGACY.

### `locales/` - Localization Layer
-   **`uk.py` / `en.py`**: Centralized UI strings and phrases for different languages.

### `ports/` - Port Interfaces (Abstractions)

~41 port interfaces organized by domain concern:

**Core Ports:**
-   **`llm_port.py`**: `LLMPort` ABC. Interface for LLM provider operations (generate, stream, upload files). Re-exports from `domain/llm.py` for backward compatibility. Adapters: `GeminiAdapter`, `ClaudeAdapter`, `GrokAdapter`, `OpenAIAdapter`.
-   **`repository.py`**: `FactRepository` ABC. Interface for memory storage operations (SCD Type 2, vector search). Adapter: `FirestoreFactRepository`.
-   **`session_store.py`**: `SessionStore` ABC. Interface for session persistence (load, save, append, batch append). Adapter: `FirestoreSessionStore`.
-   **`dedup_store.py`**: `DedupStore` ABC. Event deduplication — `is_duplicate(event_id)` + `try_mark_processed(event_id)` (atomic check-and-set). Adapter: `FirestoreDedupStore`.
-   **`security_port.py`**: `SecurityPort` ABC. `validate(message) → ValidationResult`. Prompt injection detection + trust zone enforcement. Implementations: `PromptInjectionDetector`, etc.
-   **`platform_port.py`**: `PlatformPort` ABC. Base contract for all platform adapters — `start()`, `stop()`, `get_platform_name()`, `_translate_platform_files()`. Implemented by `SlackAdapter` and `TelegramWebhookAdapter`.
-   **`embedding_service.py`**: `EmbeddingService` ABC. Text embedding generation. Adapter: `GeminiEmbeddingAdapter`.

**User & Account Ports:**
-   **`account_repository.py`**: Port for billing account operations + quota management. Adapter: `FirestoreAccountRepository`.
-   **`user_repository.py`**: Port for user profile CRUD + platform identity linking. Adapter: `FirestoreUserRepository`.
-   **`auth_port.py`**: `AuthPort` ABC. OAuth 2.0 / OIDC authentication (authorization URL, token exchange, verification). Adapter: `FirebaseAuthAdapter`.
-   **`iam_port.py`**: `IAMPort` ABC. Role-based access control (OWNER/MEMBER/VIEWER, resource-level permissions). Adapter: `FirestoreIAMAdapter`.
-   **`invite_code_repository.py`**: Port for invite code management (create, consume, list). Adapter: `FirestoreInviteCodeRepository`.
-   **`whitelist_repository.py`**: Port for email/domain whitelist management. Adapter: `FirestoreWhitelistRepository`.
-   **`quota_service.py`**: Port for non-blocking usage tracking and quota management. Key method: `record_usage(account_id, model, tokens, cost)`. Adapter: `FirestoreQuotaService`.

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
-   **`puppeteer_runner_port.py`**: 🆕 `PuppeteerRunnerPort` ABC + `PuppeteerRunnerError`. `run(html, timeout) → bytes`. System boundary for the Node.js Puppeteer subprocess. Implemented by `NodePuppeteerRunner`.

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
-   **`agent_fallback_service.py`**: `AgentFallbackService` — three-level graceful degradation chain used by `ConversationHandler`. Level 1: route failed response to `quick_response_agent_{user_id}` with a `[System: ...]` apology note. Level 2: catches any Quick failure or exception. Level 3: returns a synthetic `AgentResponse.SUCCESS(result=apology_text)` — the caller always receives a displayable response and never triggers the `send_status(ERROR)` path. Uses `MessageRouter` Protocol (not concrete `AgentCoordinator`) to avoid services/ → infrastructure/ import violation.
-   **`history_summary_service.py`**: 🆕 Compresses model responses into ≤300-char session memory entries via a fast LLM call. Always uses Gemini structured output (`response_schema`). Provider locked to Gemini at composition time — immune to user `provider_preference`. Fail-fast: one attempt, `WARNING` on failure, returns `None` (caller stores full text). Injected into `SmartResponseAgent`; designed to be reused by other agents.
-   **`brain_service.py`**: **The orchestrator (provider-agnostic)**. Manages "Fast Path/Slow Path" logic, conversational sessions, manual tool execution loop, prompt building. Depends on `LLMPort` port, not specific implementation. (Legacy - replaced by AgentCoordinator flow)
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
-   **`rich_content_service.py`**: 🆕 `RichContentService` — converts agent `RichContent` DTOs into platform-specific media. Routes `file` types through format converters (openpyxl for xlsx, python-docx for docx). Routes `widget` through `HtmlRendererPort` → PNG bytes → `PlatformMediaPort.upload_image`. Routes GCS-based types through `GcsMediaAdapter`. Wired at composition root — never imported by agents or handlers.
-   **`document_delivery_service.py`**: 🆕 `DocumentDeliveryService` — stores document bytes (HTML, PDF) to GCS via `MediaStoragePort`. Key format: `docs/{uuid4()}-{filename}`. Used by `PdfGeneratorAgent` to persist both HTML and PDF before delivery notifications are sent. Separate from `RichContentService` — handles document storage only, no rendering or platform upload logic.
-   **`user_notification_service.py`**: 🆕 `UserNotificationService` — sends platform alerts (Slack/Telegram) for background events (e.g., email indexing complete, deep research finished). Stores/reads last active channel via `NotificationStatePort`; delegates send to coordinator via `MessageRouter` Protocol (not concrete `AgentCoordinator`).
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
-   **`base_agent.py`**: Abstract `BaseAgent` class with built-in resilience patterns (Circuit
    Breaker, Retries, Timeouts). Owns all cross-cutting agent behavior:
    - Lifecycle hooks: `_on_agent_start`, `_on_agent_success(output_text)`, `_on_agent_error`, `_on_delegation` — agents call these; infrastructure (logging, metrics) lives here only.
    - Debug helpers: `_debug_prompt`, `_debug_response`, `_format_history_for_debug` — centralizes all `DEBUG_PROMPTS` logging. Agents never import `get_debug_logger()` directly.
    - **Universal billing hook**: `_call_llm()` accumulates `prompt_tokens`, `completion_tokens`, `cache_read_tokens`, `cache_creation_tokens` from every LLM response. `process()` resets accumulators at request start and calls `_flush_billing()` after execute (success or exhausted retries). `_flush_billing()` fires a fire-and-forget `AgentMessage(INFORM)` to `billing_agent` via `coordinator`. No-op when `coordinator` or `account_id` not set. All agents get billing automatically — no per-agent `_track_usage` needed.
-   **`infrastructure/billing_agent.py`**: Aggregates usage per `account_id`, flushes to `QuotaService` when threshold reached or periodic interval fires. `asyncio.Lock` protects the buffer. `start()` launches periodic flush task. Required payload fields: `account_id`, `tokens`, `cost`, `model`.
-   **`infrastructure/logger_agent.py`**: Centralized log buffer with asyncio.Lock. `start()` launches periodic flush to GcpLogSink (prod) or stdout (dev).

**Core Agents (`agents/core/`):**
-   **`router_agent.py`**: LLM triage + rule-based fallback routing (complexity threshold=5).
-   **`quick_response_agent.py`**: Fast responses (BALANCED tier). Functionally equivalent to Smart
    in tool access — intents determined by `AgentDescriptor.allowed_intents` (all non-internal, same
    as Smart). Two differences: (1) no refinement loop in cognitive process (single INTENT → delegate
    → FORMAT pass); (2) `_INTENT_REMAP` substitutes `search_web` → `search_web_light` at dispatch
    time via `AgentDescriptor.intent_remap`. `MAX_DELEGATION_TURNS=5`. Memory-first parallel
    scheduling. `_clean_history_for_quick` — currently a no-op (ConversationHandler never writes
    tool turns to session); reserved for future Brainstorm Mode. Outputs JSON (`full_response`,
    `response_summary`, `rich_content`) via `parse_llm_response`.
-   **`smart_response_agent.py`**: Complex reasoning + specialist delegation via
    `delegate_to_specialist(intent, query)` — generic ACP v2 tool. Available intents injected
    dynamically from `AgentRegistry.get_available_intents_for(descriptor)`. Memory-first parallel
    scheduling for `search_memory` intent.

**Specialized Agents:**
-   **`memory_search_agent.py`**: Two-phase memory retrieval: (1) LLM key formulation — Gemini Flash converts the delegation query into 3 optimized search keys (keywords, primary_query, alternative_query) + optional domains using `COGNITIVE_PROCESS_MEMORY_SEARCH` Firestore token; (2) multi-vector RRF search via `SearchEnrichmentService`. Schema enforced at API level: 3–5 keywords, 2 domains max (enum), 50-char query limit. Shared specialist — called from both Quick (`search_memory`) and Smart (`search_memory`).
-   **`web_search_light_agent.py`**: Lightweight single-pass grounding specialist (ECO tier). Called exclusively by QuickResponseAgent via `search_web_light` intent. Single Gemini + Google Search grounding call. Returns plain Slack mrkdwn. Prompt via PromptBuilder v3 (`agent_type="websearch_light"`).
-   **`web_search_agent.py`**: Full-depth web search specialist using Gemini Grounding (BALANCED tier). Called exclusively by SmartResponseAgent via `search_web` intent.
-   **`email_search_agent.py`**: 🆕 Email archive specialist (BALANCED tier). Accessible to both Quick and Smart (`internal=False` in AgentDescriptor). Three intents: `search_emails` (semantic search in `domain_email_facts_v1`), `get_email_details` (fetch full body from Gmail), `get_email_attachment` (parse attachment via markitdown). Registered via `AgentDescriptor` in `main.py` at startup.
-   **`email_classification_agent.py`**: 🆕 Shared singleton agent (created in `ServiceContainer`, not per-user). Classifies raw `EmailMetadata` + snippets via tool-calling mode. Outputs `EmailClassificationResult` per email. Called by `EmailIndexingService` (not by the agent delegation chain). Exception to the OUTPUT_FORMAT rule: uses markdown code block extraction in `_parse_response()` due to tool-calling + JSON mode incompatibility — see inline comment.
-   **`pdf_generator_agent.py`**: 🆕 PDF creation specialist. ASYNC, BALANCED tier (Gemini, `agent_type="pdf_generator"`, `internal=False`). Single LLM call: natural language → complete HTML+CSS document. System prompt embeds a style catalogue (12 design systems); LLM auto-selects the most appropriate style. HTML is rendered to PDF by `NodePuppeteerRunner`. Filename extracted from `<title>` tag (`_extract_filename_from_html`). Returns two `DeliveryItem("document", ...)` items — HTML (`file_upload=False`, GCS only) and PDF (`file_upload=True`, GCS + Slack upload). Stored via `DocumentDeliveryService`.
-   **`consolidation_agent.py`**: Knowledge synthesis specialist ("Life Chronicler"). Uses biographical context caching and vector-based deduplication.
-   **`observation_agent.py`**: ⚠️ LEGACY (kept for reference).

**Agent Infrastructure:**
-   **`infrastructure/agent_config.py`**: 🆕 Central registry of tunable agent behavior parameters. One typed `@dataclass` per agent (`QuickAgentConfig`, `SmartAgentConfig`, etc.) with module-level instances (`QUICK`, `SMART`, …). Agents assign class-level constants at definition time (`CONTEXT_WINDOW = QUICK.context_window`). Also centralizes feature flags read from environment: `ENABLE_HISTORY_OPTIMIZATION` and `ENABLE_GROUNDING_ATTRIBUTION` — agents import these constants instead of calling `os.getenv()` directly (agents must not read env vars). Provider selection is a separate concern — see `AgentProviderStrategy` in `services/agent_context_builder.py`. Extension path to Level 2 (per-user Firestore overrides) documented in the file header.
-   **`infrastructure/agent_manifest.py`**: 🆕 Single source of truth for all agent declarations.
    -   `Intent` — typed string constants for all intent names. Import instead of raw literals.
    -   `AgentDescriptor` instances for every agent in the system:
        -   Specialists (`MEMORY_SEARCH`, `WEB_SEARCH`, `WEB_SEARCH_LIGHT`, `EMAIL_SEARCH`) — Part A only (capabilities, descriptions, internal flag). Collected in `ALL_DESCRIPTORS`, registered by `main.py`.
        -   Orchestrators (`QUICK_RESPONSE`, `SMART_RESPONSE`) — Part B only (allowed_intents, intent_remap). Set as class-level `_descriptor` in agent classes. Not in `ALL_DESCRIPTORS`.
    -   Adding a new specialist: add `Intent` constant + descriptor + include in `ALL_DESCRIPTORS` + wire in `user_agent_factory.py`.
-   **`infrastructure/agent_coordinator.py`**: Central routing hub with explicit and broadcast routing.
    -   Explicit routing (by agent_id), broadcast routing, parallel execution
    -   `handle_delegation(intent, query, context)` — ACP v2 entry point; resolves via AgentRegistry, routes SYNC or ASYNC
    -   `get_available_intents()` and `get_available_intents_for(descriptor)` — proxy to registry
-   **`infrastructure/agent_registry.py`**: 🆕 ACP v2 registry mechanics + `AgentDescriptor` dataclass.
    -   `AgentDescriptor` (alias: `AgentManifest`) — unified A/B declaration per agent:
        -   A (capabilities): `capabilities: Dict[str, ExecutionMode]`, `internal: bool` (hides agent from LLM tool list), `capability_descriptions`
        -   B (requirements): `allowed_intents: Optional[frozenset]` (None = all non-internal), `intent_remap: Dict[str, str]` (dispatch-time substitution)
    -   All descriptor instances live in `agent_manifest.py` — `agent_registry.py` holds only the dataclass definition and registry mechanics.
    -   `AgentRegistry.register(AgentDescriptor)` — maps intents → descriptors
    -   `get_available_intents()` — all non-internal intents (for SmartAgent prompt injection)
    -   `get_available_intents_for(descriptor)` — filtered by descriptor's `allowed_intents`
    -   `ExecutionMode`: SYNC (inline) or ASYNC (Cloud Tasks + callback)

**Agent Communication Protocol (ACP):**
-   **`domain/agent.py`**: Defines unified communication protocol:
    -   `AgentMessage`: Task requests with intent, payload, context
    -   `AgentResponse`: Structured responses with status, confidence, metadata
    -   `AgentIntent`: QUERY, DELEGATE, INFORM, REQUEST_FEEDBACK
    -   `AgentStatus`: SUCCESS, PARTIAL, FAILED, TIMEOUT, CANNOT_HANDLE
    -   `AgentConfig`: Per-agent configuration (model, timeout, retries, capabilities)
    -   `RoutingMetadata`: Typed routing metadata (tone, complexity, confidence, tools, semantic_lens)

### `web/` - Web Application (Quart Blueprints)
-   **`auth_blueprint.py`**: OAuth 2.0 authentication flow (login, callback, token refresh, logout, link-oauth).
-   **`cabinet_blueprint.py`**: Cabinet UI — web dashboard for Gmail status, indexing control, user settings.
-   **`deep_research_webhooks.py`**: 🆕 OpenAI Deep Research webhook receiver. POST `/webhooks/openai/deep-research` — receives completion/failure/cancellation events. Svix-format HMAC-SHA256 signature verification (`webhook-id`, `webhook-timestamp`, `webhook-signature` headers). Metadata (user_id, account_id, query, session_id) echoed by OpenAI. On completion: uploads HTML report to GCS via `MediaStoragePort`, delivers two parallel notifications via `UserNotificationService` — SmartAgent-formatted summary (`notify()` with `agent_id_override`) + direct report link (`notify_raw()`).
-   **`gmail_blueprint.py`**: Gmail OAuth connect/disconnect + email indexing API endpoints (`/api/gmail/status`, `/api/gmail/index`, `/api/gmail/jobs/<id>`, `/api/gmail/disconnect`, `/api/gmail/data`).

### `utils/` - Utilities
-   **`logger.py`**: Centralized logging configuration (human-readable + trace IDs).
-   **`debug_logger.py`**: `PromptDebugLogger` — saves LLM prompts, responses, and final agent
    output to GCS (when `DEBUG_PROMPTS_BUCKET` set) or local filesystem (local dev). Controlled by
    `DEBUG_PROMPTS` env var. All agents use it via `BaseAgent._debug_prompt` / `_debug_response`
    — never import `get_debug_logger()` directly from individual agent files.
-   **`logging_context.py`**: Context propagation for trace/session/user IDs.
-   **`telemetry.py`**: OpenTelemetry setup and trace helpers.
-   **`performance_logger.py`**: Timing helper for perf logging.
-   **`timer.py`**: Lightweight timer utility.
-   **`server.py`**: HTTP server utilities for Cloud Run (health checks, event endpoints).
