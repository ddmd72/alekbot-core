# Architecture Review: alek-core

**Date:** 2026-02-18
**Architecture version:** v6.0 (Milestone 4, ~85%)
**Method:** Code inspection based on arc42 documentation

---

## Table of Contents

1. [Overall Assessment](#1-overall-assessment)
2. [Hexagonal Architecture — Audit](#2-hexagonal-architecture--audit)
3. [Critical Implementation Bugs](#3-critical-implementation-bugs)
4. [Engineering Quality](#4-engineering-quality)
5. [Summary Table of Findings](#5-summary-table-of-findings)
6. [Fix Priorities](#6-fix-priorities)

---

## 1. Overall Assessment

### What is Claimed (arc42)

The documentation describes a system built on **Clean/Hexagonal Architecture** with a strict rule: "Domain layer has ZERO external imports". Layers: Domain → Ports (ABC) → Adapters. Dependencies point inward only. Multi-agent system (Actor Model) with 6 agents, dual memory (hot/cold), multi-vector RRF search, provider-agnostic LLM, 5-level security validation.

### What Is Actually There

The project structure **broadly conforms** to the hexagonal model. The split into `domain/`, `ports/`, `adapters/`, `services/`, `handlers/` is correct. However, code inspection revealed **significant violations** of the stated principles and **critical bugs**.

### Summary Scores

| Aspect | Score | Comment |
|--------|-------|---------|
| **Hexagonality** | **C+** | Skeleton is correct, but 12 dependency-direction violations |
| **Domain purity** | **B-** | 3 violations, one critical (import utils.logger) |
| **Engineering quality** | **B** | Good tests, but god-function main.py and duplicates |
| **Security** | **C** | Wildcard CORS + credentials, unauthenticated admin command |
| **Concurrency** | **D+** | Cache races, fire-and-forget without error handling |
| **Data integrity** | **C-** | Potential message loss on overflow, sessions never deleted |
| **Testing** | **B+** | Good behavioral tests, weak integration layer |
| **Documentation** | **A** | Full arc42 across 12 sections, ESSENTIAL_READING.md |

---

## 2. Hexagonal Architecture — Audit

### 2.1 Domain Layer — Purity Violations

Claim: "Domain layer has ZERO external imports" — **FALSE**.

| # | File | Line | Import | Severity |
|---|------|------|--------|----------|
| D1 | `src/domain/tone.py` | 5 | `from ..utils.logger import logger` | **CRITICAL** |
| D2 | `src/domain/vector_math.py` | 13 | `import numpy as np` | HIGH |
| D3 | `src/domain/billing.py` | 5 | `from pydantic import BaseModel, Field` | MEDIUM |
| D3 | `src/domain/consolidation.py` | 3 | `from pydantic import BaseModel, Field` | MEDIUM |
| D3 | `src/domain/entities.py` | 5 | `from pydantic import BaseModel, Field` | MEDIUM |
| D3 | `src/domain/session.py` | 2 | `from pydantic import BaseModel, Field, ConfigDict` | MEDIUM |
| D3 | `src/domain/tool_result.py` | 1 | `from pydantic import BaseModel` | MEDIUM |
| D3 | `src/domain/user.py` | 5 | `from pydantic import BaseModel, Field` | MEDIUM |

**D1** — direct violation: domain depends on utils (an infrastructure layer). Logger is used for `logger.warning("Invalid tone '%s', ...")` in `tone.py:34`.

**D2** — numpy (20MB+ dependency) in domain. The file itself tries to justify this: "numpy isolated in Domain (pure mathematical library)". That is rationalization. It should be moved to `services/` or rewritten using `math`.

**D3** — Pydantic: a debatable point. 6 files use Pydantic, 8 others use `dataclasses`. The inconsistency itself is a problem.

### 2.2 Ports Layer

Generally **clean**. Two structural notes:

- `src/ports/task_queue.py:13` — method `enqueue_slack_event()`. Platform-specific naming in a supposedly platform-agnostic port.
- `src/ports/llm_port.py` — defines DTOs (`Message`, `MessagePart`, `LLMRequest`, `LLMResponse`) that are essentially domain value objects but live in ports. Because of this, `file_service.py` and `session_store.py` import from a sibling port instead of domain.

### 2.3 Dependency Direction — Violations

#### CRITICAL: Service → Concrete Adapter (user_agent_factory.py)

`src/services/user_agent_factory.py` — **the most serious hexagonality violation**. Imports 8+ concrete adapters directly:

```
Line 10: from google.genai import types
Line 16: from ..adapters.firestore_repo import FirestoreFactRepository
Line 17: from ..adapters.firestore_session_store import FirestoreSessionStore
Line 18: from ..adapters.gemini_adapter import GeminiAdapter
Line 19: from ..adapters.claude_adapter import ClaudeAdapter
Line 21: from ..adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
Line 30: from ..adapters.firestore_prompt_repository import FirestorePromptComponentRepository
Line 31: from ..adapters.groovy_prompt_assembler import GroovyPromptAssembler
Line 32: from ..adapters.xml_prompt_assembler import XmlPromptAssembler
```

This is a **composition root** disguised as a service. It should live in `bootstrap/` or `config/`, not in `services/`.

#### HIGH: Other Direction Violations

| File | Line | Import | What is Violated |
|------|------|--------|-----------------|
| `src/services/auth_provider_registry.py` | 12 | `from ..adapters.firebase_auth_adapter import FirebaseAuthAdapter` | Service → Adapter |
| `src/adapters/firestore_repo.py` | 13 | `from ..adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter` | Adapter → Adapter |
| `src/adapters/security/llm_adapter.py` | 15 | `from src.adapters.security.regex_adapter import RegexSecurityAdapter` | Adapter → Adapter |

#### Clean Layers (confirmed)

- **Handlers** (`conversation_handler.py`, `consolidation_handler.py`) — import from `domain`, `ports`, `services`. No violations.
- **Agents** — depend on ports (`LLMPort`, `FactRepository`, `EmbeddingService`). No violations.
- **Web layer** (`oauth_app.py`, `user_cabinet_app.py`) — depends on services and ports. Clean.

---

## 3. Critical Implementation Bugs

### 3.1 Concurrency

#### BUG-01: Race condition in UserAgentFactory cache [CRITICAL]
**`src/services/user_agent_factory.py:172-176`**

```python
async def ensure_agents_for_user(self, user_id: str) -> Dict[str, object]:
    if user_id in self._cache:
        cached = self._cache[user_id]
        if (time.time() - cached["last_used"]) < self._cache_ttl:
            cached["last_used"] = time.time()
            return cached
```

`_cache` is a plain `Dict` with no `asyncio.Lock`. Two concurrent requests from the same user will pass the check simultaneously, create agents twice, and the last write will overwrite the first. Orphaned agents remain in the coordinator.

**Fix:** `self._cache_lock = asyncio.Lock()` + `async with self._cache_lock:` around check-and-set.

#### BUG-02: Silent swallowing of agent registration errors [HIGH]
**`src/services/user_agent_factory.py:420-425`**

```python
for agent in agents:
    try:
        self.coordinator.register_agent(agent)
    except ValueError:
        continue  # silent ignore
```

`ValueError("Agent already registered")` is swallowed. Combined with BUG-01 this means: new agents are written to cache, but the coordinator still holds stale ones → routing to stale objects.

#### BUG-03: Fire-and-forget tasks without error handling [HIGH]
**Multiple files:**
- `src/handlers/consolidation_handler.py:222`
- `src/adapters/firestore_session_store.py:233`
- `src/agents/core/quick_response_agent.py:434`

`asyncio.create_task(...)` without `add_done_callback`. Exceptions in the task are lost forever. Billing, overflow, consolidation — all can silently lose data.

#### BUG-04: create_task in __init__ without event loop [HIGH]
**`src/agents/infrastructure/billing_agent.py:40`, `logger_agent.py:40`**

```python
self._flush_task = asyncio.create_task(self._periodic_flush())
```

`asyncio.create_task()` in `__init__` requires a running event loop. If the object is created outside an async context — `RuntimeError`. There is no `shutdown()` method → the task is never cancelled.

### 3.2 Data Integrity

#### BUG-05: Message loss on overflow [CRITICAL]
**`src/adapters/firestore_session_store.py:156-233`**

Sequence:
1. Transaction: session trimmed, overflow messages extracted
2. `asyncio.create_task(self.overflow_callback(...))` is launched
3. If the callback fails — messages are **lost forever** (removed from session, never reached the consolidation queue)

The overflow callback in `main.py:203-241` catches Exception and only logs it, without re-raise.

**Fix:** Overflow messages must be persisted to the consolidation queue **in the same transaction** as the session trim.

#### BUG-06: Sessions are never deleted [CRITICAL]
**`src/adapters/firestore_session_store.py:258-265`**

```python
async def delete_session(self, session_id: str) -> None:
    self._delete_session(session_id)  # MISSING AWAIT!
```

`_delete_session` is an `async def`. Calling it without `await` returns a coroutine object that is never executed. **The public session-deletion API is completely broken.**

#### BUG-07: Consolidation batches without ordering [HIGH]
**`src/adapters/firestore_consolidation_queue.py:37-39`**

```python
# query = query.order_by("created_at").limit(limit)  # COMMENTED OUT
query = query.limit(limit)
```

Without ordering, consolidation processes batches in arbitrary order. Temporal ordering of facts is broken.

#### BUG-08: No retry mechanism for failed batches [HIGH]
**`src/handlers/consolidation_handler.py:141-142`**

On error a batch receives status `RETRY_PENDING`, but nothing triggers a retry. Batches hang forever if the user sends no new messages.

### 3.3 Error Handling

#### BUG-09: Load failure returns an empty session [CRITICAL]
**`src/adapters/firestore_session_store.py:118-120`**

```python
except Exception as e:
    logger.error(f"Error loading session ...")
    return SessionState()  # Empty! No session_id!
```

Firestore timeout → user loses the entire conversation context. The caller sees "empty new session" and continues as if nothing happened.

#### BUG-10: Raw exceptions are sent to the user [HIGH]
**`src/handlers/conversation_handler.py:406`**

```python
error_text = f"`{str(e)}`"
await response_channel.send_message(error_text, ...)
```

Internal paths, API keys, connection strings — all can end up in Slack/Telegram.

### 3.4 Security

#### SEC-01: Admin command without authentication [CRITICAL]
**`src/agents/core/router_agent.py:280-281`**

```python
if text.strip() == "$admin_cache_reset":
    return await self._handle_admin_cache_reset()
```

**ANY user** can reset the prompt assembly cache for all users in the worker. No role/permission check.

#### SEC-02: Wildcard CORS + Credentials [CRITICAL]
**`main.py:487-490`**

```python
response.headers["Access-Control-Allow-Origin"] = "*"
response.headers["Access-Control-Allow-Credentials"] = "true"
```

`*` with `Allow-Credentials: true` — security misconfiguration. OAuth and Cabinet endpoints are open to cross-origin attacks.

**Fix:** Replace `*` with the specific cabinet origin.

### 3.5 Memory Leaks

#### LEAK-01: Unbounded user cache [CRITICAL]
**`src/services/user_agent_factory.py:75-76`**

```python
self._cache: Dict[str, Dict[str, object]] = {}
self._cache_ttl = 3600
```

The cache grows **without bound**. No max size, no LRU eviction, no periodic cleanup. Expired entries are removed only lazily on the next access by the same user. 1000 users = 6000+ agent objects in memory forever → OOM.

#### LEAK-02: Agent registry is never cleaned [HIGH]
**`src/infrastructure/agent_coordinator.py:29`**

`self.agents: Dict[str, BaseAgent] = {}` — agents are registered but **never removed** when evicted from the factory cache. They accumulate indefinitely.

---

## 4. Engineering Quality

### 4.1 Testing

**119 test files** — impressive coverage for a solo project.

| Aspect | Score |
|--------|-------|
| Behavioral tests (router, IAM, search) | **A** |
| Use of fakes vs mocks (search_enrichment) | **A+** |
| Firestore adapter tests (over-mocked) | **C+** |
| Integration tests (actually unit tests with mocks) | **C** |
| Requirement-based tests (15 files) | **A-** |

**Key problem:** "Integration" tests use `MagicMock(spec=QuickResponseAgent)` — these are not real integration tests. There are no tests using the Firestore emulator.

### 4.2 Code Duplicates

| Duplicate | Where | Recommendation |
|-----------|-------|----------------|
| CircuitBreaker — 3 implementations | `utils/`, `base_agent.py`, `legacy/tools/base.py` | Consolidate into `utils/circuit_breaker.py` |
| `_sanitize_tool_history` | `smart_response_agent.py:711`, `legacy/brain_service.py:435` | Remove from legacy or extract to shared |
| Boilerplate `execute()` | `quick_response_agent.py`, `smart_response_agent.py` | Extract into a `BaseAgent` mixin |

### 4.3 main.py — God Function

**`main.py` — 563 lines, 520-line `async def main()`:**
- Initializes 20+ services
- Defines inline functions (6 of them)
- Duplicates `GeminiAdapter` (lines 84 and 195)
- Duplicates imports (`FirestoreInviteCodeRepository` on lines 36 and 127)
- Mixes infrastructure setup, HTTP routes, and application wiring

**Fix:** Extract into a `Bootstrapper` / `Container` with phases: `create_infrastructure()`, `create_services()`, `create_agents()`, `create_routes()`.

### 4.4 Configuration

**`src/config/settings.py`** — `load_settings()` returns `Dict[str, Any]`. No typing, no validation of required keys (`missing_keys` check is computed but followed by `pass`).

### 4.5 Legacy Isolation

**Excellent isolation.** Searching for `from src.legacy` and `from ..legacy` across all of `src/` yields **zero results**. Legacy is fully disconnected from active code.

### 4.6 Prompt System v3

**Adequately designed.** 3 section types, 4-level resolution, TTL-based caching, security validation. The only concern is the fragile index-based result mapping in `_unified_slots_to_assignments()` (155 lines).

---

## 5. Summary Table of Findings

### Critical (6)

| ID | Category | File | Description |
|----|----------|------|-------------|
| BUG-01 | Concurrency | `user_agent_factory.py:172` | Race condition in cache (no lock) |
| BUG-05 | Data Integrity | `firestore_session_store.py:156-233` | Overflow message loss |
| BUG-06 | Bug | `firestore_session_store.py:258` | Missing `await` — sessions never deleted |
| BUG-09 | Error Handling | `firestore_session_store.py:118` | Load failure → context loss |
| SEC-01 | Security | `router_agent.py:280` | Admin command without authn/authz |
| SEC-02 | Security | `main.py:487` | Wildcard CORS + credentials |
| LEAK-01 | Memory | `user_agent_factory.py:75` | Unbounded cache → OOM |

### High (12)

| ID | Category | File | Description |
|----|----------|------|-------------|
| BUG-02 | Concurrency | `user_agent_factory.py:420` | Silent registration failures |
| BUG-03 | Concurrency | Multiple | Fire-and-forget without error handling |
| BUG-04 | Concurrency | `billing_agent.py:40` | create_task in __init__ |
| BUG-07 | Data Integrity | `firestore_consolidation_queue.py:37` | No ordering on batches |
| BUG-08 | Data Integrity | `consolidation_handler.py:141` | No retry mechanism |
| BUG-10 | Error Handling | `conversation_handler.py:406` | Raw exceptions to user |
| LEAK-02 | Memory | `agent_coordinator.py:29` | Agent registry never cleaned |
| D2 | Architecture | `domain/vector_math.py:13` | numpy in domain layer |
| S1 | Architecture | `user_agent_factory.py:16-32` | Service → 8 concrete adapters |
| S2 | Architecture | `auth_provider_registry.py:12` | Service → concrete adapter |
| ARCH-01 | Architecture | `main.py:84,195` | Duplicate GeminiAdapter |
| ARCH-02 | Engineering | `main.py` | 520-line god function |

### Medium (7+)

| ID | Category | File | Description |
|----|----------|------|-------------|
| D3 | Architecture | 6 domain files | pydantic vs dataclasses inconsistency |
| P1 | Architecture | `ports/task_queue.py:13` | Platform-specific method naming |
| P2 | Architecture | `ports/llm_port.py` | DTOs in port instead of domain |
| BUG-11 | Bug | `firestore_session_store.py:85` | Missing session_id on expired session |
| CONF-01 | Engineering | `config/settings.py` | Untyped Dict + no validation |
| DUP-01 | Engineering | 3 files | 3 CircuitBreaker implementations |
| TEST-01 | Testing | `tests/integration/` | Not real integration tests |

---

## 6. Fix Priorities

### P0 — Immediate (production blockers)

1. **BUG-06:** Add `await` in `delete_session()` — sessions literally never deleted
2. **SEC-01:** Add admin role check for `$admin_cache_reset`
3. **SEC-02:** Replace wildcard CORS with a specific origin
4. **BUG-09:** On load_session error — raise or return with session_id
5. **BUG-10:** Do not send raw exceptions to users

### P1 — Next sprint (stability)

6. **BUG-01:** asyncio.Lock on UserAgentFactory cache
7. **LEAK-01:** Add maxsize + LRU eviction or periodic cleanup
8. **BUG-05:** Overflow messages → consolidation queue in one transaction
9. **BUG-03:** Wrapper for fire-and-forget tasks with error logging
10. **LEAK-02:** Unregister agents on cache eviction

### P2 — Next milestone (architecture)

11. **S1:** Extract UserAgentFactory to `bootstrap/` composition root
12. **ARCH-02:** Split main.py into a Bootstrapper with phases
13. **D1:** Remove `from ..utils.logger` from `domain/tone.py`
14. **D2:** Move `vector_math.py` out of domain
15. **DUP-01:** Consolidate CircuitBreaker into a single implementation
16. **BUG-07:** Enable ordering in consolidation queue
17. **CONF-01:** Replace Dict with a typed Settings dataclass

### P3 — Technical debt

18. **D3:** Unify domain models (pydantic OR dataclasses, not both)
19. **P2:** Move LLM DTOs from ports to domain
20. **TEST-01:** Add real integration tests with the Firestore emulator
21. Remove dead code (`_get_model_for_tier`, legacy directory)
22. Extract common Quick/Smart agent boilerplate into BaseAgent

---

## Conclusion

The project demonstrates **mature architectural thinking** — arc42 documentation, hexagonal structure, multi-agent system, provider-agnostic design, requirement-based tests. For a solo developer this is an impressive level of discipline.

However, the implementation **lags behind the documented intent** in key areas:
- Hexagonality is violated in the most critical file (`user_agent_factory.py`)
- Domain purity is more aspiration than reality
- Concurrency and memory management are serious production-risk areas
- `firestore_session_store.py` is a concentration of critical bugs

**Main recommendation:** Focus on P0/P1 fixes before moving toward new features (Milestone 5). The current bugs in session management and memory leaks represent real production risk.
