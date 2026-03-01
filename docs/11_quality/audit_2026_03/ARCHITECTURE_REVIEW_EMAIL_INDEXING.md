# Architecture Review: Email Indexing & Hexagonal Purity

**Date**: 2026-03-01
**Scope**: Email indexing feature (commits 9d79684..6af59d5), consolidation handler, composition root
**Methodology**: Full code audit of all layers (domain, ports, services, adapters, agents, handlers, composition, main.py)

---

## Executive Summary

Email indexing was implemented across 6 commits in a rapid sprint. The **core hexagonal layers are clean** — domain models, ports (5 email ABCs), adapters (4 Firestore/Gmail), and services (EmailIndexingService, EmailSearchService, EmailEmbeddingRepairService) follow hexagonal architecture rules correctly.

The **problems are concentrated in the composition/wiring layer**:
- `main.py` grew to 793 lines with duplicate adapter instantiations and inline HTTP handlers
- Email indexing pipeline was never added to `ServiceContainer`
- `UserAgentFactory` is misplaced in `services/` (should be `composition/`)
- Several encapsulation leaks between handlers and factories

---

## Findings

### P0-1: Dual Instantiation of Email Adapters

**Location**: `ServiceContainer` (composition/service_container.py:72-83) vs `main.py:406-430`

`ServiceContainer` creates:
- `FirestoreIndexedEmailRepository`
- `FirestoreOAuthCredentialsAdapter`
- `GmailProviderAdapter`
- `EmailSearchService`

`main.py` creates **separate independent instances** of the same classes:
- `FirestoreOAuthCredentialsAdapter` (line 406)
- `FirestoreIndexedEmailRepository` (line 407)
- `GmailProviderAdapter` (line 410-413)
- `FirestoreEmailJobRepository` (line 414)
- `FirestoreEmailExclusionsAdapter` (line 415)
- `EmailClassificationAgent` (line 417-422)
- `EmailIndexingService` (line 423-430)

**Impact**: Two `GmailProviderAdapter` instances with identical credentials. Two `FirestoreIndexedEmailRepository` instances pointing to same collection. Resource waste, potential cache inconsistencies, confusing ownership.

**Root cause**: Email indexing pipeline was wired directly in `main.py` instead of being added to `ServiceContainer`.

**Fix**: Move email indexing pipeline creation into `ServiceContainer`. Remove duplicates from `main.py`.

---

### P0-2: main.py is a 793-line Composition Monolith

**Location**: `main.py`

Contains:
- DI composition (~350 lines)
- Inline HTTP route handlers as closures — email_indexing worker, consolidation worker, watchdog (~70 lines inside `/worker` route at lines 646-722)
- Forward-reference hack via `_agent_factory_ref: list = [None]` (line 296)
- Signal handling and graceful shutdown
- CORS middleware definition
- Warmup routines (Firestore, vector indices)

The inline `/worker` endpoint handles 4 task_types with direct access to 8+ local variables from the enclosing scope. This is untestable and fragile.

**Fix**: Extract worker task handlers into `src/handlers/worker_handler.py`. Keep `main.py` as pure bootstrapping.

---

### P1-1: UserNotificationService Imports from Infrastructure

**Location**: `src/services/user_notification_service.py:18`

```python
from ..infrastructure.agent_coordinator import AgentCoordinator
```

Services layer rule: import only from `domain/`, `ports/`, stdlib. `infrastructure/` is not allowed.

**Impact**: Cannot test `UserNotificationService` without the real `AgentCoordinator`.

**Fix**: Use `typing.Protocol` to decouple — define a `MessageRouter` protocol in the service file.

---

### P1-2: UserAgentFactory Misplaced in services/

**Location**: `src/composition/user_agent_factory.py` (moved from `src/services/`)

Imports:
- 8 agent classes directly (lines 37-45)
- `google.genai.types` (line 18) — vendor SDK in services layer
- `config.environment.EnvironmentConfig` (line 20)
- `config.settings.SearchConfig` (line 188, runtime import)

This is composition-root logic (creating and wiring agents), not a business service.

**Fix**: Move to `src/composition/user_agent_factory.py`. Update ~12 import sites.

---

### P1-3: consolidation_handler Accesses agent_factory Internals

**Location**: `src/handlers/consolidation_handler.py`

```python
indexed_email_repo=agent_factory.indexed_email_repo,  # line 277
await agent_factory.user_repo.get_user(user_id)       # lines 189, 308
```

The handler reaches through `UserAgentFactory` to grab internal adapter references.

**Fix**: Pass `indexed_email_repo` and `user_repo` as explicit parameters to handler functions. Resolve at call site (`main.py`).

---

### P1-4: EmailClassificationAgent Implements a Port

**Location**: `src/agents/email_classification_agent.py:38`

```python
class EmailClassificationAgent(BaseAgent, EmailClassifierPort):
```

In hexagonal architecture, ports are implemented by **adapters**, not agents. This dual inheritance creates role confusion.

**Decision**: Keep as-is — `EmailIndexingService` depends on the port, not the concrete agent. Document as intentional "agent-as-adapter" pattern.

---

### P2-1: Hardcoded Russian System Prompt in consolidation_handler

**Location**: `src/handlers/consolidation_handler.py:68-72`

```python
system_alert = (
    "[system_alert] Система по поручению пользователя просканировала ящик "
    "электронной почты и сделала выборку кандидатов для занесения в базу фактов. ..."
)
```

System prompts should go through the Prompt Builder system, not be hardcoded.

**Fix**: Replace with English constant per CLAUDE.md language rule.

---

### P2-2: Private Function Import Between Services

**Location**: `src/services/email_search_service.py:35`

```python
from ..services.file_conversion_service import (
    convert_file_to_text,
    _truncate_with_alert,   # private function
)
```

**Fix**: Rename `_truncate_with_alert` → `truncate_with_alert` (make it public API).

---

### P2-3: Markdown Code Block Extraction in _parse_response

**Location**: `src/agents/email_classification_agent.py:377-383`

Extracts JSON from markdown code blocks, violating the "No regex fallbacks" rule.

**Context**: In tool-calling mode, `response_mime_type=None` (Gemini cannot combine JSON mode with function calling). Without JSON mode, Gemini may wrap output in markdown. The retry loop would fix this but costs an extra LLM call for a 100-email batch.

**Decision**: Keep extraction. Document loudly as an **explicit, bounded exception** that must NOT be replicated elsewhere.

---

## Fix Plan

| # | Priority | Issue | Effort | Files |
|---|----------|-------|--------|-------|
| 1 | P0 | Dual email adapter instantiation | Medium | `service_container.py`, `main.py` |
| 2 | P0 | main.py decomposition (worker handlers) | Medium | new `worker_handler.py`, `main.py` |
| 3 | P1 | Move UserAgentFactory to composition/ | Small | move 1 file, update ~12 imports |
| 4 | P1 | UserNotificationService infrastructure import | Small | `user_notification_service.py` |
| 5 | P1 | consolidation_handler encapsulation | Small | `consolidation_handler.py`, `main.py` |
| 6 | P2 | Hardcoded Russian prompt | Small | `consolidation_handler.py` |
| 7 | P2 | Private function import | Trivial | 3 files |
| 8 | P2 | _parse_response code block extraction | Trivial | `email_classification_agent.py`, `CLAUDE.md` |

**Execution order**: 1 → 2 → 3 → 5 → 4 → 6 → 7 → 8 (Steps 4, 6, 7, 8 are independent)

**Verification**: `make check` (unit tests + domain purity) after each step.
