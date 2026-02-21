# Hexagonal Architecture Review

**Date:** 2026-02-21
**Scope:** Full codebase audit of `/src/` against Hexagonal Architecture (Ports & Adapters) principles
**Verdict:** 7.5 / 10 — solid foundation, several boundary violations remain

---

## Executive Summary

The codebase demonstrates a **well-structured hexagonal architecture** with clean separation at the core layers (domain, ports). The domain and ports layers are pristine — zero violations. However, the outer layers (adapters, services, agents) have accumulated **import boundary violations** that undermine the architecture's testability and replaceability guarantees.

**Key numbers:**
- **176 Python files** across 20 directories
- **0 violations** in domain/ (26 files) and ports/ (23 files)
- **18 violations** in adapters/ (6 files out of 47)
- **2 violations** in services/ (2 files out of 26)
- **9 violations** in agents/ (7 files out of 16) — services imports, mitigated by DI

---

## Layer-by-Layer Assessment

### 1. Domain Layer — EXCELLENT (10/10)

**Files:** 26 (including `prompt_v3/` subpackage)
**Violations:** 0

The domain layer is impeccably clean:
- Imports **only** from stdlib, pydantic, and numpy (for `vector_math.py`)
- Zero imports from adapters, services, config, infrastructure, or utils
- Proper use of Pydantic `BaseModel` for entities, `@dataclass` for value objects, `(str, Enum)` for enums
- Circular dependency between `billing.py` ↔ `user.py` handled correctly via `TYPE_CHECKING` + `model_rebuild()`
- `ResponseChannel` defined as `Protocol` — proper structural typing
- `SecurityPort` defined as `ABC` — classic OOP abstraction

**Notable patterns:**
- `RequestContext` uses `contextvars` for implicit threading of user/account context
- `SmartDeduplicationService` is pure domain logic (no I/O), correctly placed in domain/
- SCD2 versioning (`valid_from`/`valid_to`/`is_current`) baked into `FactEntity`

### 2. Ports Layer — EXCELLENT (10/10)

**Files:** 23 (including `prompt_v3/` subpackage)
**Violations:** 0

All port interfaces are properly abstract:
- 20 use `ABC`, 2 use `Protocol` (`LogSink`, `TaskQueue`)
- 98 `@abstractmethod` declarations across 21 files
- Import only from `domain/` + stdlib
- Comprehensive type hints with docstrings

**Port coverage** for all system boundaries:
- LLM services, embedding, audio transcription
- Firestore repositories (facts, users, accounts, sessions, invites, whitelist)
- Authentication, IAM, quotas
- Task queues, consolidation queues, log sinks
- Prompt assembly (v1 and v3)

### 3. Adapters Layer — NEEDS WORK (5/10)

**Files:** 47
**Violations:** 18 import statements across 6 files

This is the weakest layer architecturally. While the majority of adapters (41/47) are clean, the **platform adapters** (Slack, Telegram base) have significant boundary violations.

#### Violations found:

| File | Forbidden Import | Layer Violated |
|------|-----------------|----------------|
| `platform/base_adapter.py:8` | `handlers.conversation_handler.ConversationHandler` | handlers |
| `platform/base_adapter.py:9` | `infrastructure.agent_coordinator.AgentCoordinator` | infrastructure |
| `platform/base_adapter.py:10` | `services.user_agent_factory.UserAgentFactory` | services |
| `platform/base_adapter.py:11` | `services.iam_service.IAMService` | services |
| `slack/factory.py:11-13` | `adapters.gcp_task_queue`, `firestore_session_store`, `firestore_dedup_store` | adapters (circular) |
| `slack/factory.py:16` | `infrastructure.agent_coordinator.AgentCoordinator` | infrastructure |
| `slack/factory.py:17-18` | `services.user_agent_factory`, `services.iam_service` | services |
| `slack/socket_adapter.py:13-15` | `infrastructure.agent_coordinator`, `services.user_agent_factory`, `services.iam_service` | infrastructure, services |
| `slack/http_adapter.py:19-23` | `adapters.firestore_*`, `infrastructure.agent_coordinator`, `services.*` | adapters, infrastructure, services |
| `fact_management_adapter.py:17-18` | `services.fact_write_service`, `services.search_enrichment_service` | services |
| `firestore_fact_management_adapter.py:17-18` | `services.fact_write_service`, `services.search_enrichment_service` | services |
| `firestore_repo.py:15` | `adapters.gemini_embedding_adapter.GeminiEmbeddingAdapter` | adapters (self-reference) |

#### Root cause analysis:

1. **Platform adapters act as mini-composition roots.** `PlatformAdapter` (base class) pulls in `ConversationHandler`, `AgentCoordinator`, `UserAgentFactory`, and `IAMService`. These should be injected as ports, not imported directly. The `SlackAdapterFactory` creates concrete adapters internally, duplicating the composition root's responsibility.

2. **Fact management adapters depend on services.** `FactManagementAdapter` and `FirestoreFactManagementAdapter` import `FactWriteService` and `SearchEnrichmentService` from `services/`. An adapter should not depend on a service — the direction should be reversed, or these should be injected.

3. **Firestore repo imports a concrete adapter.** `firestore_repo.py` imports `GeminiEmbeddingAdapter` directly instead of using the `EmbeddingService` port it already has access to.

### 4. Services Layer — GOOD (8/10)

**Files:** 26
**Violations:** 2 (localized)

The vast majority of services (24/26) follow hexagonal rules perfectly — they import only from `domain/` and `ports/`, receive all dependencies via constructor injection, and never touch adapters.

#### Violations found:

| File | Forbidden Import | Issue |
|------|-----------------|-------|
| `auth_provider_registry.py:12` | `adapters.firebase_auth_adapter.FirebaseAuthAdapter` | Creates adapter instance directly (line 59) |
| `user_agent_factory.py:41` | `infrastructure.agent_coordinator.AgentCoordinator` | Imports infrastructure type (injected via DI, but type import violates boundary) |

#### Strengths:
- 100% constructor injection — no service locators, no singletons
- Config imports used only for reading values, not creating services
- Clean `ProviderRegistry` pattern for LLM provider selection
- `PromptBuilder` and v3 `PromptAssemblyService` properly compose from ports

### 5. Agents Layer — GOOD (7/10)

**Files:** 16
**Violations:** 9 service imports across 7 files (all mitigated by DI)

All agents inherit from `BaseAgent` and receive dependencies via constructor. `BaseAgent` itself is clean (imports only `domain/` and `ports/`). The violations are imports of concrete service classes used as type hints for constructor parameters.

#### Violations found:

| File | Import |
|------|--------|
| `consolidation_agent.py:31,35` | `services.prompt_builder`, `services.fact_write_service` |
| `memory_search_agent.py:20` | `services.search_enrichment_service` |
| `web_search_agent.py:16` | `services.prompt_builder` |
| `core/router_agent.py:31-32` | `services.search_enrichment_service`, `services.prompt_builder` |
| `core/quick_response_agent.py:35` | `services.prompt_builder` |
| `core/smart_response_agent.py:42,44` | `services.prompt_builder`, `services.history_summary_service` |

**Mitigating factor:** All these services are injected via constructor — agents never create them directly. The import is purely for type annotation. This is a gray area — the CLAUDE.md says agents should "receive dependencies via constructor" (which they do) but doesn't explicitly forbid service type imports.

**Recommendation:** If these services will never have multiple implementations, the current pattern is pragmatic. If portability matters, promote `PromptBuilder`, `SearchEnrichmentService`, `HistorySummaryService`, and `FactWriteService` to ports.

#### Strengths:
- `CircuitBreaker` pattern in `BaseAgent` — protects against cascading failures
- `BillingAgent` and `LoggerAgent` use `asyncio.Lock` for shared state
- Error logging before re-raise consistently applied
- `AgentExecutionContext` abstracts away concrete LLM providers

### 6. Handlers Layer — GOOD (8/10)

**Files:** 2
**No direct adapter imports.**

Handlers (`ConversationHandler`, `ConsolidationHandler`) properly orchestrate through `AgentCoordinator` and ports. They import from `infrastructure/` and `services/`, which is acceptable for their role as orchestrators.

**Minor issue:** `ConversationHandler` uses `Optional[Any]` for `consolidation_queue` and `security_port` instead of proper port type hints.

### 7. Infrastructure Layer — EXCELLENT (9/10)

**Files:** 3

- `AgentCoordinator` — depends only on `domain/` and `agents/base_agent` (ABC). Clean registry pattern.
- `MessageQueue` — textbook ABC + `InMemoryQueue` implementation. Zero external deps.

### 8. Composition Layer — CORRECT (9/10)

**Files:** 2

`ServiceContainer` is the proper composition root:
- Creates all adapters and injects into services
- Returns everything as typed ports via `agent_services()`
- Lazy-loads optional components (Grok, Prompt v3) with fallback
- Handles unavoidable circular deps (`BiographicalContextService` ↔ `FirestoreFactRepository`) via `set_repository()`

**Minor issue:** `main.py` creates a second `FirestoreFactRepository` instance for the user cabinet (line 288) instead of reusing `container.repository`.

### 9. Web Layer — GOOD (8/10)

**Files:** 2

Both `oauth_app.py` and `user_cabinet_app.py` receive all dependencies via factory function DI. No adapters imported. Proper use of ports (`UserRepository`, `FactRepository`, `EmbeddingService`).

---

## Violation Map (Visual)

```
                    ALLOWED IMPORTS
                    ─────────────────
    domain/ ──→ stdlib, pydantic                        ✅ CLEAN
    ports/  ──→ domain/, stdlib, ABC                    ✅ CLEAN
    adapters/ ──→ domain/, ports/, config/              ⚠️ 6 files violate
    services/ ──→ domain/, ports/                       ⚠️ 2 files violate
    agents/ ──→ BaseAgent, domain/, ports/              ⚠️ 7 files (gray area)
    handlers/ ──→ services/, infrastructure/, ports/    ✅ CLEAN
    infrastructure/ ──→ domain/, agents/                ✅ CLEAN
    composition/ ──→ everything (composition root)      ✅ CORRECT
    main.py ──→ everything (bootstrap)                  ✅ CORRECT

    ACTUAL FORBIDDEN CROSS-CUTS FOUND:
    ┌─────────────────────┐
    │ adapters → services │  4 files (fact_management_*, platform/base)
    │ adapters → handlers │  1 file  (platform/base)
    │ adapters → infra    │  4 files (platform/*, slack/*)
    │ adapters → adapters │  3 files (slack/factory, http, firestore_repo)
    │ services → adapters │  1 file  (auth_provider_registry)
    │ services → infra    │  1 file  (user_agent_factory)
    │ agents → services   │  7 files (type imports, DI-mitigated)
    └─────────────────────┘
```

---

## What's Good

1. **Domain purity is perfect.** Zero leakage. Proper use of Pydantic, dataclasses, enums, protocols. This is the hardest part to get right and it's done well.

2. **Ports are comprehensive.** Every system boundary has a port. 22 abstract interfaces covering LLM, storage, auth, IAM, queues, embedding, quotas, prompts, files, audio.

3. **Constructor injection everywhere.** No service locators, no DI containers, no magic. All wiring visible in `main.py` and `ServiceContainer`. Easy to trace, easy to test.

4. **Single composition root.** `ServiceContainer` creates all adapters and returns ports. `main.py` orchestrates startup/shutdown. Clean separation.

5. **Agent system is well-abstracted.** `BaseAgent` depends only on domain/ports. `AgentExecutionContext` hides concrete LLM providers. `AgentCoordinator` is a clean registry.

6. **Async discipline.** All I/O is async/await. Shared state protected with `asyncio.Lock`. Graceful shutdown with task draining.

7. **Multi-tenant design.** `RequestContext`, `account_id` threading, environment-prefixed collections — baked in from the start.

---

## What Needs Improvement

### Critical (Architectural Boundary Violations)

1. **Platform adapters are mini-composition roots.** `PlatformAdapter` (base class) imports `ConversationHandler`, `AgentCoordinator`, `UserAgentFactory`, `IAMService`. This makes platform adapters untestable in isolation and tightly coupled to the entire application. These dependencies should be injected as ports or passed from the composition root.

2. **`auth_provider_registry.py` creates `FirebaseAuthAdapter` directly.** A service should never instantiate an adapter. The adapter should be created in the composition root and registered into the registry.

3. **`firestore_repo.py` imports `GeminiEmbeddingAdapter`** (concrete) while it already has access to the `EmbeddingService` port. Direct adapter-to-adapter coupling.

4. **Fact management adapters depend on services.** `FactManagementAdapter` imports `FactWriteService` and `SearchEnrichmentService`. The dependency direction is wrong — adapters implement ports, they don't consume services.

### Medium (Architectural Hygiene)

5. **Agents import service types for constructor signatures.** While all dependencies are injected (good), importing concrete service types creates compile-time coupling. Consider promoting `PromptBuilder`, `SearchEnrichmentService`, `HistorySummaryService`, `FactWriteService` to ports if they represent substitutable behavior.

6. **`SlackAdapterFactory`** (`adapters/slack/factory.py`) imports concrete adapters (`GcpTaskQueue`, `FirestoreSessionStore`, `FirestoreEventDedupStore`) and infrastructure (`AgentCoordinator`). This factory belongs in the `composition/` layer, not in `adapters/`.

7. **Duplicate `FirestoreFactRepository` in `main.py`.** Line 288 creates a second instance instead of reusing `container.repository`.

### Minor (Code Quality)

8. **`ConversationHandler` uses `Optional[Any]`** for `consolidation_queue` and `security_port` instead of proper port types.

9. **`LoggerAgent` imports `EnvironmentConfig`** from `config/`. Acceptable for infrastructure agents, but `config` values could be passed as primitives instead.

---

## Recommendations (Prioritized)

| Priority | Action | Impact |
|----------|--------|--------|
| P0 | Inject `ConversationHandler`, `AgentCoordinator`, `UserAgentFactory`, `IAMService` into platform adapters instead of importing them | Decouples adapters from application core |
| P0 | Move adapter creation out of `AuthProviderRegistry` — register pre-built `AuthPort` instances from composition root | Restores services → ports direction |
| P0 | Replace `GeminiEmbeddingAdapter` import in `firestore_repo.py` with the `EmbeddingService` port | Eliminates adapter-to-adapter coupling |
| P1 | Move `SlackAdapterFactory` to `composition/` or inject all deps from main.py | Correct layer placement |
| P1 | Invert dependency in fact management adapters — inject `FactWriteService` and `SearchEnrichmentService` as ports | Correct dependency direction |
| P2 | Promote `PromptBuilder` to a port if multiple implementations are foreseeable | Cleaner agent dependencies |
| P2 | Replace `Optional[Any]` with proper port types in `ConversationHandler` | Type safety |
| P2 | Reuse `container.repository` in `main.py` instead of creating a second instance | Consistency |

---

## Conclusion

The hexagonal architecture is **structurally sound** at its core. Domain and ports are pristine. The DI pattern is consistent and transparent. The violations are concentrated in the adapter layer (specifically platform adapters) and are fixable without major restructuring.

The most impactful improvement would be making platform adapters (Slack, Telegram) receive their orchestration dependencies via injection rather than importing them directly. This would eliminate ~12 of the 18 adapter-layer violations in one refactoring pass.

**Score: 7.5/10** — strong foundation, localized violations, clear path to 9+/10.
