# Hexagonal Architecture Review v2

**Date:** 2026-02-21
**Scope:** Full codebase audit — architecture, code quality, tests, production readiness
**Previous Review:** 7.5/10 (same date, first pass)
**Current Verdict:** 7.5 / 10 — import violations from v1 review remain unfixed; new findings in ports layer

---

## Executive Summary

This is a **repeat review** following the initial assessment. The codebase was audited across all 9 layers (176 Python files), 86 test files, and the bootstrap/composition root.

**What's excellent:** Domain and ports layers remain pristine. Constructor-based DI is consistent. Async discipline is impeccable. Multi-tenant design is deeply integrated. Multi-agent system is well-abstracted. Test coverage is broad and follows proper patterns.

**What remains unfixed:** All 29 import boundary violations from the v1 review are still present. Additionally, this review found **new issues** in the ports layer (duplicate methods, missing abstract decorator, incomplete port contract).

**Key numbers:**
- **176 Python files** in `src/`, **86 test files** in `tests/`
- **0 violations** in domain/ (26 files) — PRISTINE
- **2 bugs + 1 incomplete contract** in ports/ (23 files) — NEW FINDINGS
- **18 import violations** in adapters/ (6 files) — UNCHANGED from v1
- **2 import violations** in services/ (2 files) — UNCHANGED from v1
- **9 import violations** in agents/ (7 files) — UNCHANGED from v1 (gray area, DI-mitigated)

---

## Status of v1 Recommendations

| # | v1 Recommendation | Status | Evidence |
|---|-------------------|--------|----------|
| P0 | Inject deps into platform adapters instead of importing | **NOT FIXED** | `platform/base_adapter.py:8-11` still imports ConversationHandler, AgentCoordinator, UserAgentFactory, IAMService |
| P0 | Move adapter creation out of `AuthProviderRegistry` | **NOT FIXED** | `auth_provider_registry.py:12` still imports `FirebaseAuthAdapter` |
| P0 | Replace `GeminiEmbeddingAdapter` import in `firestore_repo.py` | **NOT FIXED** | `firestore_repo.py:15` still imports concrete adapter |
| P1 | Move `SlackAdapterFactory` to composition/ | **NOT FIXED** | `slack/factory.py:11-18` still imports concrete adapters, services, infrastructure |
| P1 | Invert dependency in fact management adapters | **NOT FIXED** | `fact_management_adapter.py:17-18` still imports services |
| P2 | Promote `PromptBuilder` to port | **NOT DONE** | Agents still import concrete service type |
| P2 | Replace `Optional[Any]` in ConversationHandler | **NOT FIXED** | Still uses `Optional[Any]` |
| P2 | Reuse `container.repository` in main.py | **NOT FIXED** | `main.py:288` still creates duplicate FirestoreFactRepository |

---

## Layer-by-Layer Assessment

### 1. Domain Layer — EXCELLENT (10/10) ✅

**Files:** 26 (including `prompt_v3/` subpackage)
**Violations:** 0
**Change from v1:** None needed — was already perfect.

The domain layer is impeccably clean:
- Imports **only** from stdlib, pydantic, and numpy (for `vector_math.py`)
- Zero imports from adapters, services, config, infrastructure, or utils
- Proper use of Pydantic `BaseModel` for entities, `@dataclass` for value objects, `(str, Enum)` for enums
- Circular dependency between `billing.py` ↔ `user.py` handled correctly via `TYPE_CHECKING` + `model_rebuild()`
- `ResponseChannel` defined as `Protocol` — proper structural typing
- `SecurityPort` defined as `ABC` — classic OOP abstraction

**Notable patterns:**
- `RequestContext` uses `contextvars` for async-safe implicit threading of user/account context
- `SmartDeduplicationService` is pure domain logic (no I/O), correctly placed in domain/
- SCD2 versioning (`valid_from`/`valid_to`/`is_current`) baked into `FactEntity`
- 4D fact taxonomy: Domain × Temporal × State × Priority
- Immutable value objects with `@dataclass(frozen=True)`: Blueprint, Token, SlotExclusion, ProfileSlot, SearchLimits, PromptComponent
- Factory methods: `AgentResponse.success()/.failure()/.cannot_handle()`, `Token.create()`, `SlotExclusion.from_slot_name()`
- Rich type aliases: `TokenId = NewType('TokenId', str)` — semantic typing at zero runtime cost

**Purity checklist (26/26 files verified):**

| Aspect | Status |
|--------|--------|
| No imports from adapters/ | PASS |
| No imports from services/ | PASS |
| No imports from config/ | PASS |
| No imports from utils/ | PASS |
| No imports from ports/ (except TYPE_CHECKING) | PASS |
| BaseModel for entities | PASS (11 files) |
| @dataclass for value objects | PASS (12 files) |
| Enum inheritance correct | PASS (all use `(str, Enum)`) |
| No I/O operations | PASS |
| Factory methods present | PASS (10+ factories) |
| Immutable value objects (frozen=True) | PASS (7 frozen dataclasses) |

### 2. Ports Layer — GOOD (8/10) ⚠️

**Files:** 23 (including `prompt_v3/` subpackage)
**Import violations:** 0
**Bugs found:** 2 (new in v2)
**Change from v1:** Score DOWNGRADED from 10/10 to 8/10 due to new findings.

Import correctness is 100% — all ports import only from `domain/` + stdlib + ABC/Protocol. However, this deeper review found structural issues:

#### BUG: Duplicate method definitions in `consolidation_queue.py`

```
Lines 10-13:  get_queue_size()        ← first definition
Lines 41-44:  get_queue_size()        ← DUPLICATE (silently overwrites first)

Lines 20-23:  cleanup_old_batches()   ← first definition
Lines 46-49:  cleanup_old_batches()   ← DUPLICATE (silently overwrites first)
```

Python silently uses the last definition. This is harmless at runtime but indicates a copy-paste error and makes the code confusing.

#### BUG: Missing `@abstractmethod` on `SessionStore.append_messages_batch()`

```python
# session_store.py:29 — NOT abstract
async def append_messages_batch(self, session_id: str, messages: list[Message], ...) -> None:
    raise NotImplementedError
```

This method is part of the interface contract but lacks `@abstractmethod`. Subclasses can silently skip implementing it, and the error only surfaces at runtime.

#### Incomplete port contract: `ConsolidationQueue`

The adapter (`FirestoreConsolidationQueue`) implements `enqueue_batch()` and `get_pending_batches()`, but the port doesn't declare them as `@abstractmethod`. This means the port contract is incomplete — consumers rely on methods that aren't in the interface.

#### Minor: ABC vs Protocol inconsistency

20 ports use `ABC`, 2 use `Protocol` (`LogSink`, `TaskQueue`). Not a bug, but inconsistent. Both are valid — `Protocol` for structural typing, `ABC` for explicit contracts. The codebase predominantly uses `ABC`.

#### Port justification

| Port | Implementations | Justified? |
|------|----------------|------------|
| `LLMService` | Gemini, Claude, Grok (3) | **YES** — multiple providers |
| `AuthPort` | Firebase (1) | **YES** — Cognito/Okta/Auth0 planned |
| `SecurityPort` | Regex, LLM, Composite, ExternalAPI (4) | **YES** — pluggable strategies |
| `FactRepository` | Firestore (1) | **YES** — testable substitution |
| `SessionStore` | Firestore (1) | **YES** — testable substitution |
| `EmbeddingService` | Gemini (1) | **YES** — provider-swappable |
| `PromptAssembler` | Groovy (1) | **YES** — format-agnostic design |
| `ConsolidationQueue` | Firestore (1) | **YES** — future Cloud Tasks/SQS |
| `TaskQueue` | GCP (1) | **YES** — cloud-portable |
| All repository ports | Firestore (1 each) | **YES** — testable substitution |

All 22 ports are justified — either by multiple implementations or by testable substitution at system boundaries.

### 3. Adapters Layer — NEEDS WORK (5/10) ⚠️

**Files:** 47
**Violations:** 18 import statements across 6 files
**Change from v1:** UNCHANGED — all violations remain.

41 out of 47 adapter files are architecturally clean. The violations are concentrated in:
- Platform adapters (base, factory, slack/http, slack/socket) — act as mini-composition roots
- Fact management adapters — depend on services instead of ports
- Firestore repo — imports concrete adapter

Full violation table: see v1 review (unchanged).

**What IS excellent in the adapter layer:**
- All I/O is async/await — zero blocking calls found
- `asyncio.to_thread()` used correctly for blocking SDK calls (file upload, embedding)
- Error handling is consistent: log before re-raise, graceful degradation, `exc_info=True`
- All secrets passed via config, none hardcoded
- Constructor injection throughout
- Multi-tenant: `account_id` filtering in all repository queries
- Provider capabilities declared per adapter (`ProviderCapabilities` dataclass)
- Tier-to-model mapping decouples agents from concrete models
- Platform abstraction is clean (Slack HTTP/Socket, Telegram webhook, common ResponseChannel)

### 4. Services Layer — GOOD (8/10) ✅

**Files:** 26
**Violations:** 2 (unchanged from v1)
**Change from v1:** UNCHANGED.

24/26 services follow hexagonal rules perfectly. Remaining violations:
- `auth_provider_registry.py:12` — imports `FirebaseAuthAdapter` (creates adapter instance)
- `user_agent_factory.py:41` — imports `AgentCoordinator` from infrastructure

**Strengths (confirmed in deeper review):**
- 100% constructor injection — no service locators, no singletons
- Excellent parallelization: `asyncio.gather()` in `fact_write_service` (3x embedding), `search_enrichment_service` (6 vector queries), `prompt_assembly_service` (4 profile loads)
- Smart caching with TTL: `PromptBuilder` (24h), `PromptAssemblyService` (24h), `PromptComponentService` (1h)
- Thread-safe shared state: `UserAgentFactory` uses per-user `asyncio.Lock`
- 3-level config resolution: USER override → ACCOUNT default → SYSTEM default (`ConfigurationService`)
- Multi-vector RRF search: 6 parallel queries, Reciprocal Rank Fusion ranking, 0.96 dedup threshold

### 5. Agents Layer — GOOD (7/10) ✅

**Files:** 16
**Violations:** 9 service imports (DI-mitigated, gray area)
**Change from v1:** UNCHANGED.

All agents receive dependencies via constructor. `BaseAgent` itself is clean. The violations are type imports for constructor signatures.

**Architecture highlights:**
- **Router** — rule-based + optional LLM triage, multi-language, routes to Quick or Smart
- **Quick** — flash model, ~2s target, 20 message context window
- **Smart** — pro model, multi-turn delegation (max 5 turns), 60 message window, smart parallel execution (memory sequential → others parallel)
- **Memory** — pure vector search, no LLM, multi-vector RRF
- **WebSearch** — Gemini grounding, separate agent (API limitation)
- **Consolidation** — v3 multi-turn tool use (8-step process), 4D taxonomy, awareness-first
- **Billing/Logger** — fire-and-forget batching with `asyncio.Lock`

**CircuitBreaker** centralized in BaseAgent — auto-recovery with configurable thresholds.

### 6. Handlers Layer — GOOD (8/10) ✅

**Files:** 2
**Change from v1:** UNCHANGED.

- `ConversationHandler` — platform-agnostic orchestrator with graceful degradation (Smart timeout → Quick fallback), output validation against prompt injection, async post-processing
- `ConsolidationHandler` — batch processing with sequential per-user ordering, 3 retry attempts, `RequestContext` wrapping

`Optional[Any]` still used for `consolidation_queue` and `security_port` — should be proper port types.

### 7. Infrastructure Layer — EXCELLENT (9/10) ✅

**Files:** 3
**Change from v1:** UNCHANGED.

- `AgentCoordinator` — registry pattern, explicit + broadcast routing, parallel execution
- `InMemoryQueue` — Actor Model with asyncio.Queue, documented limitations (single-process, no persistence)

### 8. Composition Layer — CORRECT (9/10) ✅

**Files:** 2
**Change from v1:** UNCHANGED.

`ServiceContainer` is the proper composition root. Handles circular deps via `set_repository()`. Returns typed ports. Lazy-loads optional components.

`main.py:288` still creates a duplicate `FirestoreFactRepository` for user cabinet.

### 9. Web Layer — GOOD (8/10) ✅

**Files:** 3 (oauth_app, user_cabinet_app, static/cabinet.html)

Factory function DI. No adapter imports. Proper port usage.

---

## New Findings (v2)

### N1: ConsolidationQueue Port — Duplicate Methods and Incomplete Contract

**File:** `src/ports/consolidation_queue.py`
**Severity:** Medium

The port has 2 duplicate method definitions (`get_queue_size`, `cleanup_old_batches`) and is missing `enqueue_batch()` and `get_pending_batches()` which the adapter implements. The port contract doesn't match the actual interface used by consumers.

**Fix:** Remove duplicates, add missing `@abstractmethod` declarations.

### N2: SessionStore — Missing @abstractmethod

**File:** `src/ports/session_store.py:29`
**Severity:** Low

`append_messages_batch()` is not marked `@abstractmethod`. Subclasses can skip implementing it without a compile-time error.

**Fix:** Add `@abstractmethod` decorator.

### N3: Hexagonal Isolation Test is Incomplete

**File:** `tests/unit/test_req_arch_01_hexagonal_isolation.py`
**Severity:** Medium

The test only checks domain/ imports. It does NOT verify:
- Adapters don't import services/handlers/infrastructure
- Services don't import adapters
- Ports don't import adapters

This means the 18 adapter violations would not be caught by CI.

**Fix:** Extend the test to cover all layer boundaries from CLAUDE.md import rules.

### N4: `FactManagementAdapter` is Misplaced

**File:** `src/adapters/fact_management_adapter.py`
**Severity:** Medium (architectural)

Despite being named "adapter" and living in `adapters/`, this class doesn't adapt an external system. It orchestrates `FactRepository`, `EmbeddingService`, `FactWriteService`, and `SearchEnrichmentService`. This is a **service** behavior, not an adapter behavior.

The class implements `FactManagementPort`, which is correct. But its placement in `adapters/` with imports from `services/` creates the boundary violation. It should either:
- Move to `services/` (since it orchestrates services), or
- Accept all services as ports (pure adapter pattern)

Note: `FirestoreFactManagementAdapter` is a duplicate of this class (identical imports, same violation).

### N5: `print()` in `main.py`

**File:** `main.py:41-43`
**Severity:** Trivial

CLAUDE.md says "Do not use `print()` — only `from src.utils.logger import logger`". Lines 41-43 use `print()` for startup banner. This is pre-logger initialization, so it's defensible, but technically violates the rule.

---

## Test Suite Assessment

### Coverage Summary

| Category | Files | Focus |
|----------|-------|-------|
| Unit | 76+ | Domain models, adapters, agents, services, infrastructure |
| Integration | 18 | Agent flows, prompt assembly, quota, repository, security |
| E2E | 1 | User management flow |
| Performance | 1 | Firestore latency |

### What's Good

- **Fixtures** in `conftest.py` follow CLAUDE.md patterns: `mock_env_config`, `mock_llm_service`, `mock_repository`
- **AsyncMock(spec=PortClass)** used consistently — mocks against port interfaces, not adapters
- **Requirement markers** (`@pytest.mark.requirement("REQ-XXX")`) trace tests to requirements
- **Architecture test** `test_req_arch_01_hexagonal_isolation.py` validates domain purity via AST parsing
- **Broad coverage:** Agents, services, adapters, domain, handlers, infrastructure all have dedicated test directories
- **Concurrency tests:** `test_user_agent_factory_concurrency.py`, `test_usage_concurrency.py`
- **Security tests:** `test_req_sec_01_secret_management.py`, `test_req_sec_02_http_security.py`

### What Needs Improvement

1. **Architecture test only checks domain/** — doesn't verify adapter/service/port boundaries (see N3)
2. **No test for ConsolidationHandler** in `tests/unit/handlers/` — only `test_conversation_handler_fallback.py`
3. **E2E coverage is minimal** — 1 test file for the entire system
4. **No mutation testing** or code coverage reports configured

---

## What's Excellent (Summary)

1. **Domain purity is perfect.** 26/26 files, zero leakage, proper model patterns.
2. **Ports are comprehensive.** 22 ports covering every system boundary.
3. **Constructor injection everywhere.** No DI containers, no magic. All wiring in `main.py` + `ServiceContainer`.
4. **Async discipline.** All I/O async/await. `asyncio.Lock` for shared state. Graceful shutdown with task draining.
5. **Multi-tenant from the ground up.** `RequestContext`, `account_id` threading, env-prefixed collections, IAM policies.
6. **Multi-agent coordination.** Smart parallel execution, circuit breakers, fire-and-forget billing.
7. **Production patterns.** SCD2 versioning, RRF ranking, TTL caching, deduplication, prompt assembly pipeline.
8. **Economics-aware.** 70% Flash / 30% Opus split, PerformanceTier abstraction, budget-conscious design.

---

## Recommendations (Updated and Prioritized)

### P0 — Bugs (Fix Now)

| # | Action | File | Impact |
|---|--------|------|--------|
| 1 | Remove duplicate `get_queue_size()` and `cleanup_old_batches()` | `ports/consolidation_queue.py` | Eliminates confusion |
| 2 | Add `enqueue_batch()` and `get_pending_batches()` as `@abstractmethod` | `ports/consolidation_queue.py` | Completes port contract |
| 3 | Add `@abstractmethod` to `append_messages_batch()` | `ports/session_store.py:29` | Enforces interface compliance |

### P1 — Architecture Violations (Fix Soon)

| # | Action | File(s) | Impact |
|---|--------|---------|--------|
| 4 | Inject `ConversationHandler`, `AgentCoordinator`, `UserAgentFactory`, `IAMService` into platform adapters via constructor | `platform/base_adapter.py` | Eliminates 4 violations, decouples adapters from core |
| 5 | Move adapter creation out of `AuthProviderRegistry` — register pre-built `AuthPort` from composition root | `services/auth_provider_registry.py` | Restores services → ports direction |
| 6 | Replace `GeminiEmbeddingAdapter` import with `EmbeddingService` port | `adapters/firestore_repo.py:15` | Eliminates adapter-to-adapter coupling |
| 7 | Move `FactManagementAdapter` to `services/` or make it a pure adapter (accept all deps as ports) | `adapters/fact_management_adapter.py` | Fixes dependency direction |
| 8 | Delete `FirestoreFactManagementAdapter` (duplicate of `FactManagementAdapter`) | `adapters/firestore_fact_management_adapter.py` | Removes dead code |
| 9 | Move `SlackAdapterFactory` to `composition/` | `adapters/slack/factory.py` | Correct layer placement |

### P2 — Hygiene (When Convenient)

| # | Action | File(s) | Impact |
|---|--------|---------|--------|
| 10 | Extend `test_req_arch_01_hexagonal_isolation.py` to cover ALL layer boundaries | `tests/unit/` | CI catches future violations |
| 11 | Reuse `container.repository` in `main.py` instead of creating second instance | `main.py:288` | Consistency |
| 12 | Replace `Optional[Any]` with proper port types | `handlers/conversation_handler.py` | Type safety |
| 13 | Replace `print()` with `logger` | `main.py:41-43` | Consistency with CLAUDE.md |

---

## Score Breakdown

| Layer | Score | Files | Violations | Notes |
|-------|-------|-------|------------|-------|
| Domain | 10/10 | 26 | 0 | Pristine |
| Ports | 8/10 | 23 | 0 imports, 2 bugs | Duplicate methods, missing @abstractmethod |
| Adapters | 5/10 | 47 | 18 | 6 files violate boundaries |
| Services | 8/10 | 26 | 2 | Localized, pragmatic |
| Agents | 7/10 | 16 | 9 | DI-mitigated gray area |
| Handlers | 8/10 | 2 | 0 | Clean orchestration |
| Infrastructure | 9/10 | 3 | 0 | Clean registry |
| Composition | 9/10 | 2 | 0 | Proper composition root |
| Web | 8/10 | 3 | 0 | Factory DI |
| Tests | 7/10 | 86 | — | Broad but incomplete arch test |

**Overall: 7.5/10** — unchanged from v1. Import violations remain. New port-level bugs found but offset by confirmed strengths in deeper analysis.

**Path to 9+/10:** Fix P0 bugs (30 min), fix P1 items 4-6 (one refactoring session), extend architecture test (1 hour). This would eliminate ~25 of the 29 import violations and ensure CI prevents regressions.
