# Changelog

All notable changes to the Alek-Core project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Added
- **Prompt Design System v3 - Phase 1+2+3+4 (Domain Models + Security + Repositories + Assembly + Integration)**: Implemented token-based prompt architecture with security by design (76 tests passing).
  - **Domain Models** (`src/domain/prompt_v3/`):
    - **Token**: Immutable prompt fragments with async factory method (Token.create()) and SecurityPort validation at creation time (7 tests).
    - **SlotSchema**: Token constraints with 4-level hierarchy (USER > ACCOUNT > AGENT > SYSTEM) and domain validation (9 tests).
    - **Blueprint**: Prompt templates with {{SLOT_NAME}} placeholders and slot validation (13 tests).
    - **SectionType**: Classification enum (TOKENIZED, STATIC, RUNTIME) with validation requirements (11 tests).
  - **Security Adapters** (`src/adapters/security/`):
    - **RegexSecurityAdapter**: Pattern-based validation with 13 patterns (CRITICAL: 4, HIGH: 5, MEDIUM: 4), flexible regex, trust zone support (12 tests, full MVP implementation).
    - **CompositeAdapter**: Aggregates multiple adapters with 3 strategies (worst_case, majority_vote, all_pass), pattern deduplication (10 tests, full MVP implementation).
    - **LLMSecurityAdapter**: Placeholder with regex fallback for future LLM-based semantic analysis (4 tests, Phase 6+ implementation).
    - **ExternalAPIAdapter**: Placeholder with regex fallback for external service validation (5 tests, Phase 6+ implementation).
  - **Security Features**: Defense in depth (5 validation layers: INPUT → TOKEN → RUNTIME → OUTPUT → RAG), trust zones, extensible adapter pattern.
  - **Port Interfaces** (`src/ports/prompt_v3/`):
    - **TokenRepository**: Token storage interface (get, save, list_by_category, exists, delete).
    - **BlueprintRepository**: Blueprint storage interface (get, save, list_all, exists, delete).
    - **AgentProfileRepository**: Profile storage with 4-level resolution (get_slot_assignments, save_slot_assignments, resolve_slot_assignments, get_excluded_slots, delete_profile).
  - **Firestore Adapters** (`src/adapters/prompt_v3/`):
    - **FirestoreTokenRepository**: Token storage in Firestore with SecurityPort validation at load time (8 tests).
    - **FirestoreBlueprintRepository**: Blueprint storage with SlotSchema serialization/deserialization.
    - **FirestoreAgentProfileRepository**: Profile storage with 4-level merge (USER > ACCOUNT > AGENT > SYSTEM) using composite keys.
  - **Assembly Service** (`src/services/prompt_v3/`):
    - **PromptAssemblyService**: Central service integrating all Phase 1+2 components with 3 section types (TOKENIZED, STATIC, RUNTIME), 4-level resolution, runtime validation via SecurityPort (5 tests, 283 lines).
    - **ContextFormatter**: Conversation history formatter with message/token limits (6 tests, 122 lines).
  - **Integration** (Phase 4):
    - **PromptBuilder**: Added optional `assembly_service_v3` parameter and `build_for_agent_v3()` method for v3 token-based assembly (backward-compatible).
    - **main.py**: Initialize v3 repositories (FirestoreTokenRepository, FirestoreBlueprintRepository, FirestoreAgentProfileRepository) and security layer (RegexSecurityAdapter + CompositeAdapter).
    - **ConversationHandler**: Added optional `security_port` and `validate_model_output()` method for OUTPUT validation (indirect injection prevention).
    - **UserAgentFactory + UserPromptBuilder**: Pass assembly_service_v3 through DI chain.
    - **E2E Tests** (`tests/integration/test_prompt_v3_e2e.py`): 5 tests covering full assembly flow, security validation, graceful degradation.
  - **Implementation Plan**: Detailed 12-16 day roadmap in `docs/10_rfcs/PROMPT_DESIGN_SYSTEM_IMPLEMENTATION_PLAN.md`.
  - **RFC v3**: Token-based design system in `docs/10_rfcs/PROMPT_DESIGN_SYSTEM_RFC.md`.
- **Prompt Design System v3 - Phase 5 (Migration Complete)**: Migration preparation with section classification, token library, blueprints, profiles, validation, and rollback plan.
  - **Phase 5.1: Section Classification**
    - `scripts/migration/tokenized_sections.yaml` (219 lines): 16 token candidates across 5 categories (HUMOR_ENGINE, ARCHETYPE, VOICE, RESPONSE_STYLE, VIBE).
    - `scripts/migration/static_sections.yaml` (237 lines): 6 static sections (~450 lines total - COGNITIVE_PROCESS, POLICIES, FEW_SHOT_EXAMPLES, PROTOCOLS, MOTTO, BEHAVIOR_GUIDE).
    - `scripts/migration/runtime_sections.yaml` (287 lines): 5 runtime injection points with validation matrix (BIOGRAPHICAL_CONTEXT, CONVERSATION_HISTORY, KNOWLEDGE_BASE_RAG, ROUTING_METADATA, SEMANTIC_CONTEXT).
  - **Phase 5.2: Token Library Creation**
    - `scripts/migration/create_token_library.py` (316 lines): Creates 18 tokens with NoOpSecurityPort validation.
    - Tokens: 4 HUMOR_ENGINE (RANEVSKAYA, OFF, FAMILY_FRIENDLY, LIGHT), 4 ARCHETYPE (INTELLECTUAL_SNIPER, MENTOR, ANALYST, CREATIVE), 4 VOICE (APHORISTIC, CONVERSATIONAL, FORMAL, TECHNICAL), 3 RESPONSE_STYLE (CONCISE, DETAILED, STRUCTURED), 3 VIBE (BATTLE_WEARY, OPTIMISTIC, NEUTRAL).
    - Supports --dry-run and --upload modes, target collection: `dev_prompt_tokens_v3`.
  - **Phase 5.3: Blueprint Creation**
    - `scripts/migration/create_blueprints.py` (316 lines): Creates 2 blueprints (smart_agent_v1: 5 slots, 3495 chars; quick_agent_v1: 3 slots, 574 chars).
    - RUNTIME placeholders use `[[...]]` format (vs TOKENIZED `{{...}}`).
    - Target collection: `dev_prompt_blueprints_v3`.
  - **Phase 5.4: Default Profiles Creation**
    - `scripts/migration/create_default_profiles.py` (316 lines): Creates 7 profiles demonstrating 4-level resolution (SYSTEM: 2, AGENT: 2, ACCOUNT: 1, USER: 2).
    - Examples: family_friendly_account (HUMOR_FAMILY_FRIENDLY), professional_user (HUMOR_OFF), detailed_learner (RESPONSE_DETAILED).
    - Target collection: `dev_agent_profiles_v3`.
  - **Phase 5.5: Dual-Run Validation**
    - `scripts/migration/dual_run_validation.py` (335 lines): 5 test cases comparing v2 vs v3 prompts.
    - Test cases: Smart Agent Default, User Override Humor, Account Family Friendly, Quick Agent, Runtime Validation.
    - Result: 5/5 test cases passed - semantic equivalence confirmed (no functionality regression).
  - **Phase 5.6: Rollback Plan**
    - `docs/10_rfcs/PROMPT_V3_ROLLBACK_PLAN.md` (300+ lines): Comprehensive rollback strategy.
    - Feature flag: ENABLE_PROMPT_V3 (added to `src/config/settings.py`, default: false).
    - Graceful fallback mechanism (v3 → v2 on errors).
    - Automatic rollback triggers (error rate > 5%, latency > 2x).
    - Gradual rollout plan (5% → 25% → 50% → 75% → 100%).
  - **Total Phase 5: 3 migration scripts (948 lines), 3 YAML analyses (743 lines), 1 validation script (335 lines), 1 rollback plan (300+ lines)**.
- **OAuth Multi-Tenant Domain Models (Session 1)**: Added OAuth integration fields to domain models for multi-tenant architecture.
  - **UserProfile**: Added `external_user_id` (OAuth identity: "firebase|abc123"), `auth_metadata` (provider-specific metadata).
  - **BillingAccount**: Added `iam_policy` (user_id → role mapping), `account_defaults` (shared config for 99% users - circular import resolved).
  - **FactEntity**: Added dual ownership model with `account_id` (billing owner) and `created_by_user_id` (creator attribution).
  - **FactVisibility**: New enum with `ACCOUNT_SHARED` and `USER_PRIVATE` visibility levels.
- **OAuth Multi-Tenant Ports (Session 2)**: Created provider-agnostic Port interfaces for OAuth and IAM.
  - **AuthPort** (`src/ports/auth_port.py`): OIDC-based OAuth interface (verify_token, exchange_code, get_user_info).
  - **IAMPort** (`src/ports/iam_port.py`): Role-based access control (can_access_resource, assign_role, revoke_access).
  - **UserRepository**: Added `get_user_by_external_id()`, `link_platform_identity()` methods.
  - **AccountRepository**: Added IAM operations comment (uses existing CRUD methods).
- **OAuth Multi-Tenant Firebase Adapter (Session 3)**: Implemented Firebase OAuth adapter and provider registry.
  - **FirebaseAuthAdapter** (`src/adapters/firebase_auth_adapter.py`): Firebase Authentication adapter implementing AuthPort with OAuth 2.0 / OIDC flows (298 lines).
  - **AuthConfig** (`src/config/auth.py`): Environment-based OAuth configuration class (108 lines).
  - **AuthProviderRegistry** (`src/services/auth_provider_registry.py`): Provider management service with lazy initialization (157 lines).
  - **config/auth.yaml**: OAuth configuration documentation and reference (73 lines).
  - **Firebase Admin SDK**: Added `firebase-admin>=6.0.0` dependency for token verification.
  - **Unit Tests**: 20 tests covering FirebaseAuthAdapter and AuthProviderRegistry (430 lines total).
- **OAuth Multi-Tenant Service & Web Endpoints (Session 4)**: Implemented OAuth authentication service and web API.
  - **AuthenticationService** (`src/services/authentication_service.py`): OAuth callback handler, user registration with Master Account First paradigm (296 lines).
  - **SessionService** (`src/services/session_service.py`): JWT-based session management with access and refresh tokens (258 lines).
  - **OAuth Web App** (`src/web/oauth_app.py`): Quart web application with 5 OAuth endpoints (360 lines):
    - `GET /auth/login` - OAuth provider redirect
    - `GET /auth/callback` - OAuth callback handler with CSRF protection
    - `POST /auth/refresh` - Refresh access token
    - `POST /auth/logout` - Logout (clear cookies)
    - `GET /auth/me` - Current user info
  - **PyJWT**: Added `PyJWT>=2.8.0` dependency for JWT token operations.
  - **Unit Tests**: 20 tests covering AuthenticationService and SessionService (330 lines total).
- **OAuth Multi-Tenant IAM Implementation (Session 5)**: Implemented role-based access control.
  - **FirestoreIAMAdapter** (`src/adapters/firestore_iam_adapter.py`): IAMPort implementation using BillingAccount.iam_policy (309 lines).
    - Permission checking via ROLE_PERMISSIONS matrix
    - Role hierarchy: OWNER > MEMBER > VIEWER
    - Resource types: ACCOUNT, USER, FACT, SESSION, CONFIG
    - Actions: READ, WRITE, DELETE, ADMIN
  - **Role Management**: OWNER-only role assignment and revocation with sole OWNER protection.
  - **Unit Tests**: 19 tests covering FirestoreIAMAdapter (273 lines total).
- **OAuth Multi-Tenant Configuration Inheritance (Session 6)**: Implemented configuration inheritance service.
  - **ConfigurationService** (`src/services/configuration_service.py`): Configuration inheritance service for multi-tenant architecture (259 lines).
    - Account defaults + User overrides = Effective configuration (99/1 pattern)
    - Field-by-field merge: scalar override, dict deep merge
    - Helper methods: `has_user_overrides()`, `get_override_summary()`, `reset_user_config()`, `apply_account_defaults()`
    - Use cases: Family accounts (parent sets defaults), team accounts (admin sets defaults)
  - **Unit Tests**: 30 tests covering ConfigurationService (437 lines total).
- **OAuth Multi-Tenant Repository OAuth Methods (Session 7)**: Added OAuth methods to FirestoreUserRepository.
  - **FirestoreUserRepository** (`src/adapters/firestore_user_repo.py`): Added OAuth identity lookup and platform linking (+93 lines).
    - `get_user_by_external_id()` - Find user by OAuth external identity ("firebase|abc123")
    - `link_platform_identity()` - Link platform identity (Slack, Telegram) with conflict detection
    - Collection-agnostic: Works with both `{prefix}users` and `{prefix}users_oauth`
    - Idempotent operations: Relinking same platform to same user succeeds
  - **Unit Tests**: 13 tests covering OAuth repository methods (328 lines total).
- **OAuth Multi-Tenant Data Migration Script (Session 8)**: Created safe data migration script for OAuth schema.
  - **Migration Script** (`scripts/migrate_to_oauth.py`): Safe data migration with dry-run mode (471 lines).
    - Data transformations: Users (add OAuth fields), Accounts (create defaults), Facts (new ownership model)
    - Safety features: Dry-run mode (default), backup verification, progress tracking, error handling
    - Migration strategy: New collections with `_oauth` suffix (old collections preserved)
    - User migration: Creates default BillingAccount for each user with OWNER role
    - Fact migration: Transforms `owner_id` → `created_by_user_id`, adds `account_id` and `visibility`
  - **Migration Guide** (`docs/05_building_blocks/oauth_multi_tenant/MIGRATION_GUIDE.md`): Comprehensive migration guide (385 lines).
    - Prerequisites: Backup instructions, verification steps
    - Step-by-step guide: Dry-run → Live migration → Verification
    - Rollback procedures: Delete collections or restore from backup
    - Troubleshooting: Common errors, performance optimization
    - Post-migration checklist and FAQ
- **OAuth Multi-Tenant Integration Tests (Session 9)**: Comprehensive integration test suite for OAuth system.
  - **Integration Tests** (`tests/integration/test_oauth_integration.py`): Integration test suite (586 lines, 15 tests).
    - OAuth registration flow (Master Account First): Creates account, user, sets OWNER role
    - OAuth login flow: Existing user authentication, account loading
    - JWT session management: Access and refresh token creation and verification
    - IAM permission enforcement: OWNER (full access), MEMBER (limited), VIEWER (read-only), role assignment
    - Configuration inheritance: Account defaults, user overrides, dict deep merge
    - Platform linking: OAuth user connects Slack/Telegram
    - Complete end-to-end flow: Family account scenario (parent + child)
- **OAuth Building Block Documentation** (`docs/05_building_blocks/oauth_multi_tenant/README.md`): OAuth multi-tenant architecture documentation (updated through Session 9, 90% complete).
- **Search Enrichment Domain Models**: Added `EnrichedFact` and `EnrichedContext` in `src/domain/search.py` for router-level context.
- **SearchEnrichmentService**: New application service for triple search, weighted merge, and biographical dedup (`src/services/search_enrichment_service.py`).
- **Search Enrichment Tests**: Added unit tests for merge/dedup logic (`tests/unit/services/test_search_enrichment_service.py`).
- **TaskQueue Port**: Added `src/ports/task_queue.py` with GCP adapter `src/adapters/gcp_task_queue.py`.
- **LogSink Port**: Added `src/ports/log_sink.py` with GCP adapter `src/adapters/gcp_log_sink.py`.

### Changed
- **BREAKING (OAuth Multi-Tenant Session 1)**: Domain models updated for multi-tenant architecture (separate branch: `feature/oauth-multi-tenant`).
  - **UserProfile**: Removed `tier` field (now from BillingAccount), removed `usage` field (MVP: account-level only).
  - **BillingAccount**: Removed `owner_user_id` (use iam_policy), removed `member_user_ids` (query via UserProfile.account_id).
  - **FactEntity**: Renamed `owner_id` → split into `account_id` + `created_by_user_id`, changed `visibility` from str to enum.
- **RouterAgent Enrichment Pipeline**: RouterAgent now invokes SearchEnrichmentService and passes `enriched_context` downstream.
- **Triage Prompt Schema**: Added `search_phrase_1` and `search_phrase_2` fields to the triage prompt output.
- **Quick/Smart Agents**: Prefer router-provided enriched context before running local semantic lens search.
- **UserAgentFactory DI**: Injects SearchEnrichmentService into RouterAgent.
- **RouterAgent LLM Tests**: Updated mocked triage payloads to include new schema fields.
- **Slack HTTP Adapter**: Switched to TaskQueue port for background task enqueueing.
- **LoggerAgent**: Now uses injected LogSink; GCP client is created in adapter layer.

## [2.0.0] - 2026-01-27
### Added
- **Alek 2.0 Production Release**: Deployed v6.0 to Cloud Run, including kernel/kernel_light sync and biographical cache warmup.
- **Production Migration Archive**: Archived migration plan in `docs/archive/plans/` for historical reference.
- **Semantic Lens (Proactive RAG)**: Implemented intent-based proactive memory search in `QuickResponseAgent` and `SmartResponseAgent` using extracted keywords.
- **Context-Aware Triage**: `RouterAgent` now passes the last 5 messages of conversation history to the Triage LLM for nuanced keyword extraction.
- **Robust Triage Parsing**: Added regex-based JSON extraction in `RouterAgent` to handle conversational noise in LLM responses.
- **Fallback Keyword Extraction**: Implemented rule-based keyword extraction in `RouterAgent` as a safety net when LLM triage fails.
- **Firestore Performance Suite**: Added `tests/performance/test_firestore_latency.py` and `scripts/debug_firestore_latency.py` for monitoring and debugging database latency.
- **Firestore Connection Warmup**: Implemented proactive connection initialization and embedding pre-computation in `main.py` to eliminate cold-start delays for the first user.
- **Hybrid Router Phase 1 (LLM Triage + Tone)**: RouterAgent now uses LLM classification with rule-based fallback.
- **RoutingMetadata (ACP extension)**: Added typed metadata for tone, complexity, confidence, and tool needs.
- **UserTone domain model**: Canonical tone enum with humor allowance rules.
- **Tone awareness in kernels**: Added Tone_Awareness policy to kernel.groovy and kernel_light.groovy.
- **Hybrid Router RFC**: Added comprehensive 3-phase plan in `docs/architecture/rfcs/HYBRID_ROUTER_RFC.md`.
- **Sliding Window Consolidation (v6.0)**: Event-driven memory processing with 90-day TTL and batch processing.
- **Biographical Context Cache**: Pre-computed embeddings for instant context loading (~10ms).
- **ConsolidationBatch Queue**: Reliable batch processing with retry logic.
- **Rich Content Protocol**: Support for structured data in agent responses.
- **Weather Parser**: Utility for extracting structured weather data from search.
- **Event Deduplication**: Firestore-backed store for Slack event idempotency.
- **Biographical Context Caching System**: Implemented high-performance cache for consolidation context using new `user_context` collection.
- **Dependency Injection for EmbeddingService**: Clean architecture pattern for repository initialization.
- **Per-User RouterAgent Wiring**: UserAgentFactory now creates per-user RouterAgent instances with user-scoped routing IDs.
- **SmartResponseAgent**: Implemented complex response agent with memory-first delegation and parallel agent execution.
- **SmartResponseAgent Tests**: Added unit tests covering delegation flow, parallel execution, and history sanitization.
- **Core Agent Exports**: Exposed SmartResponseAgent via `src/agents/core/__init__.py`.
- **Billing & Usage Foundation**: Implemented `BillingAccount`, `AccountRepository`, and usage tracking logic (Milestone 1).
- **Cost Calculation**: Added `cost_calculator.py` for estimating token costs (Gemini pricing).
- **Quota Enforcement**: Added account-level quota checks before LLM calls.
- **Production Cloud Build pipeline**: Added `cloudbuild-prod.yaml` for build+deploy parity with dev.
- OpenTelemetry tracing with human-readable logs and trace context propagation.
- Firestore-backed Slack event deduplication for HTTP mode.
- Observability documentation and detailed logging instructions.
- Firestore SessionStore now skips raw LLM `raw_content` to prevent serialization errors.
- Web search agent now enforces explicit temperature formatting for weather responses.
- Documentation archive index at `docs/archive/README.md`.
- Consolidated current sprint overview in `docs/management/CURRENT_SPRINT.md`.
- Strategic architecture review document with MVP/Enterprise milestones and future vision (`docs/architecture/STRATEGIC_ARCHITECTURE_REVIEW_2026_01.md`).
- Multi-agent system blueprint and changelog (`docs/architecture/MULTI_AGENT_SYSTEM_BLUEPRINT.md`, `docs/architecture/MULTI_AGENT_SYSTEM_BLUEPRINT_CHANGELOG.md`).

### Changed
- **Cloud Run Deployment**: Public invoker binding applied for production service access.
- **Web Search & Smart Agent Timeouts**: Increased web_search agent timeout to 60s and SmartResponseAgent timeout to 90s to reduce failures on complex queries.
- **RoutingMetadata**: Added `semantic_lens` field to ACP for inter-agent keyword propagation.
- **UserTone Validation**: Refactored to ensure string return values for better serialization and logging.
- **Firestore Repository Optimization**: Switched from `stream()` to `get()` for small result sets in `src/adapters/firestore_repo.py`, reducing gRPC overhead.
- **Service Instance Reuse**: Refactored `UserAgentFactory` to reuse `GeminiAdapter`, `EmbeddingService`, and `FirestoreFactRepository` instances across all users.
- **Kernel System Unification**:
  - kernel_light now includes Tool_Usage_Protocol with Ukrainian tool guidance.
  - QuickResponseAgent now uses full kernel (AlekWithTools) aligned with SmartResponseAgent.
- **RouterAgent**: LLM triage preferred with rule-based fallback, complexity threshold = 5.
- **Quick/SmartResponseAgent**: `can_handle()` now checks routing metadata from LLM triage.
- **PromptBuilder usage**: Avoid passing `user_id=None` to preserve test behavior.
- **UserAgentFactory**: RouterAgent now receives LLM service and model for triage.
- **Test Suite Stabilization**: Audited and fixed the entire test suite, achieving a 100% pass rate (61/61 tests). Addressed critical failures in Sliding Window consolidation and dependency injection tests from the BrainService → Multi-Agent migration.
- **Documentation Structure**: Radical cleanup and reorganization. Moved 15+ outdated RFCs/Plans to `docs/archive/`. Established `docs/architecture/implemented/` as the single source of truth for finalized designs.
- **Implementation Roadmap**: Lightweight v3.0 focus, moved historical contexts to `docs/archive/roadmap_history/`.
- **Navigation**: Updated `README.md` and `ESSENTIAL_READING.md` to reflect new 3-tier structure.
- **Startup Prompt**: Optimized the AI initialization prompt in `docs/guides/OPERATIONS.md` for Living Architecture v6.0 compatibility.
- **AI Development Culture**: Updated `docs/management/AI_DEVELOPMENT_CULTURE.md` with new Living Architecture rules, archive protection, and synchronized initialization protocols.
- **Documentation Alignment**: Comprehensive audit and update of Tier 1 documents (STRUCTURE, TARGET_ARCHITECTURE, ROADMAP) to reflect v6.0 state.
- **ConsolidationAgent**: Rebranded to "Life Chronicler" with improved prompt architecture.
- **ConsolidationAgent Timeout**: Increased from 60s to 180s (3 minutes) for large batch processing.
- **Session TTL**: Extended from 24 hours to 90 days of inactivity for better UX and persistent context.
- **Repository Initialization**: Added async `initialize()` method for embedding pre-computation.
- **RouterAgent Timeout**: Increased to 60s to allow SmartResponseAgent + web search completion.
- **Routing Entry Point**: ConversationHandler now targets `router_agent_{user_id}` and main DI no longer registers a global router.
- **QuickResponseAgent Model**: Default model updated to `gemini-2.0-flash-exp` for fast responses.
- **Deploy workflow**: `make deploy` now runs build+deploy via Cloud Build (same flow as dev).
- Slack weather table rendering now uses Block Kit table schema with `raw_text` cells.
- Weather parser now handles single-temperature lines without duplicating values.
- HTTP adapter wiring now uses `brain_factory`, `identity_resolver`, and `dedup_store` dependencies.
- Root `readme.md` now includes AI entry-point instructions and links.
- Updated documentation navigation and Tier 1 initialization counts to reflect current entry path.
- Moved completed plans, sprints, and session history into structured archives.
- AI development culture updated with Multi-Language First principle.
- Documentation structure index updated with new architecture docs.

### Deprecated
- `BrainService`: Replaced by the multi-agent system.
- `ObservationAgent`: Replaced by session-based batch consolidation.
- Legacy Tool wrappers (wrapped by agents).
- YAML-based memory files in `memory/`.

### Fixed
- **Provider Resolution Logic**: Fixed critical bug where user preference overrode strict agent capabilities, forcing Router/Quick agents to use incompatible providers (e.g., Claude).
- **QuickResponseAgent Timeout**: Increased timeout to 60s to accommodate Automatic Function Calling (AFC) latency.
- **Weather Formatting**: Restored rich weather tables and emoji by integrating `parse_weather` utility directly into `SlackResponseChannel`.
- **Semantic Lens Data Path**: Fixed bug in `build_routing_metadata` where `semantic_lens` was incorrectly looked up in the `metadata` sub-object.
- **Dev Startup**: Fixed crash when `google.cloud.logging` is unavailable by lazy-loading GCP LogSink.
- **Hexagonal Leaks**: Removed infrastructure SDK imports from WebSearchAgent and LoggerAgent.
- **LLM routed messages**: QuickResponseAgent now accepts LLM-triaged requests (no more CANNOT_HANDLE).
- **SmartResponseAgent prompt assembly**: Fixed bug where `user_id` was not stored or passed to PromptBuilder, causing empty biographical context.
- **Fact Inspection Crash**: Fixed `len(None)` error in `inspect_consolidation.py` when encountering system documents without vectors.
- **JSON Serialization Error**: Fixed `DatetimeWithNanoseconds` serialization in biographical context cache (read & write sanitization).
- **Global Worker Removal**: Eliminated redundant background worker, now using pure event-driven overflow trigger in `overflow_callback`.
- **Consolidation Flow**: Cleaned up duplicate batch processing logic in `ConversationHandler`.
- **Gemini Tool Declarations**: Converted dict tool specs to SDK `types.Tool`/`FunctionDeclaration` to prevent tool validation errors.
- **Weather Bot Tool Call**: Fixed `RuntimeWarning` in `FirestoreUserRepository` where async `update` was not awaited, causing tool execution failures.
- Slack weather table no longer fails with `invalid_blocks` errors.
- Weather forecast output avoids implausible duplicated min/max temperatures.
- Session persistence RFC and related docs updated for Phase 1 implementation status.
- Tool execution flow now emits trace spans for LLM/tool operations.
- Log format now supports suffix-only trace context, with clean/full modes via LOG_TRACE_CONTEXT and Makefile helpers.
- Personal requests now skip dual-response flow to avoid mixed ordering; only smart response with memory is returned.
- Tool loop history now persists in session state so Pro responses retain context across turns.
- Dual-response flow reinstated for non-simple requests: Fast placeholder + Pro tool-loop response.
- Removed keyword-based external routing; Pro now decides tool usage via tool loop.
- Identified HTTP-mode session loss; follow-ups fail without persistent SessionStore (see SESSION_PERSISTENCE_RFC).

### Added (2026-01-21 - Multi-Agent Logic Doc)
- **Multi-Agent Logic**: Added `docs/management/MULTI_AGENT_LOGIC.md` with detailed storage, routing, and mindmap chains.

### Changed (2026-01-21 - Dual Response Optimization)
- **Routing & UX**: Refined request routing (simple vs external vs personal) and restored 🧠 prefix for Pro responses.
- **Slack Formatting**: Normalized Pro output to Slack mrkdwn (single `*bold*`, bullets).
- **Performance**: Flash/Pro temperature set to 0.7, history limited to last 20 messages, memory context skipped for external search.
- **Parallelism**: Flash + Pro and memory lookup now run concurrently in dual-response flow.
- **Web Search Model**: WebSearchAgent switched to Flash with Pro fallback.

### Added (2026-01-21 - Multi-Agent Test Coverage)
- **Agent Unit Tests**: Added unit tests for Agent Protocol, BaseAgent/CircuitBreaker, AgentCoordinator, and all 4 agents (Memory, Web, Observation, Consolidation).
- **Test Fix**: Updated MemorySearchAgent test to align with current FactEntity schema.
- **Verification**: 59 agent unit tests passing; manual validation completed on local socket dev build.

### Changed (2026-01-21 - Bot-as-a-Service Defaults)
- **Default Model Template**: Changed default `full_model` for new users from `models/gemini-3-pro-preview` to `gemini-2.0-flash-exp` for cost efficiency.
- **User Config Migration**: Updated existing DEV users to use the new default model configuration.
- **Dev Tooling**: Added `scripts/validation/check_dev_users.py` for inspecting user configurations across environments.

### Changed (2026-01-23 - Core Agent Refactoring & Specialist Agent Fix)
- **QuickResponseAgent**: Refactored to use `BaseAgent._load_conversation_context()` for consistent history composition (batch write optimization preserved).
- **SmartResponseAgent**: Refactored to use `BaseAgent._load_conversation_context()` for consistent history composition.
- **RouterAgent**: Refactored to use simplified history loading (current message only, no context window needed).
- **WebSearchAgent.can_handle()**: Removed over-engineered keyword validation. Agent now acts as executor, trusting SmartResponseAgent's LLM delegation decision.
- **MemorySearchAgent.can_handle()**: Removed over-engineered keyword validation. Simplified to basic intent+payload validation.
- **AgentCoordinator.route_message()**: Added INFO-level logging with exception handling for better observability.
- **BaseAgent.process()**: Added comprehensive debug logging for circuit breaker, can_handle(), and execution flow.

### Fixed (2026-01-23 - Specialist Agent Delegation)
- **WebSearchAgent failing on valid queries**: Fixed `can_handle()` returning False for queries without specific keywords (e.g., "Культурная программа на завтра").
- **MemorySearchAgent failing on valid queries**: Fixed `can_handle()` returning False for queries without specific keywords.
- **Test Import**: Fixed `test_agent_coordinator.py` import path (`src.services` → `src.infrastructure`).
- **Architecture Decision**: Specialist agents (web_search, memory_search) are now pure executors - delegation decision is made by SmartResponseAgent's LLM only.
