# Hexagonal Architecture Inspection — Full Audit

> **Date:** 2026-02-18
> **Scope:** All 159 Python files in `src/`
> **Methodology:** Full import graph, verification of every `from ..` and `import` against dependency rules
> **Standard:** Ports & Adapters (Alistair Cockburn), Dependency Rule (Robert C. Martin)

---

## Table of Contents

- [Architectural Layer Map](#architectural-layer-map)
- [Dependency Rule — how it should be](#dependency-rule--how-it-should-be)
- [Violations Summary (32 total)](#violations-summary)
- [Clean Layers (0 violations)](#clean-layers)
- [CAT-1: services/ → adapters/ (9 violations)](#cat-1-services--adapters-9-violations)
- [CAT-2: services/ → agents/ (6 violations)](#cat-2-services--agents-6-violations)
- [CAT-3: agents/ → services/ (14 violations)](#cat-3-agents--services-14-violations)
- [CAT-4: infrastructure/ → agents/ (1 violation)](#cat-4-infrastructure--agents-1-violation)
- [CAT-5: services/ → infrastructure/ (1 violation)](#cat-5-services--infrastructure-1-violation)
- [CAT-6: domain/ → utils/ (1 violation)](#cat-6-domain--utils-1-violation)
- [CAT-7: Type hints with concrete classes](#cat-7-type-hints-with-concrete-classes)
- [CAT-8: Direct SDK import in service layer](#cat-8-direct-sdk-import-in-service-layer)
- [Root Cause — UserAgentFactory](#root-cause--useragentfactory)
- [Missing Ports](#missing-ports)
- [Recommended Refactoring Plan](#recommended-refactoring-plan)
- [Target Import Map](#target-import-map)

---

## Architectural Layer Map

```
src/
├── domain/           # CORE — entities, value objects, enums, ACP protocol
│   └── prompt_v3/    #        Pydantic models for Prompt Design System
│
├── ports/            # PORTS — abstract interfaces (ABC/Protocol)
│   └── prompt_v3/    #         Ports for Blueprint/Token repositories
│
├── agents/           # AGENTS — domain logic for routing and reasoning
│   ├── core/         #          Router, Quick, Smart
│   └── infrastructure/ #       Billing, Logger (stub)
│
├── services/         # APPLICATION — orchestration, business logic, DI
│   └── prompt_v3/    #             Prompt assembly services
│
├── handlers/         # APPLICATION — entry-point orchestrators
│
├── infrastructure/   # INFRASTRUCTURE — coordinator, message queue
│
├── adapters/         # DRIVEN ADAPTERS — Firestore, Gemini, Claude, Grok
│   ├── platform/     #                   Base adapter
│   ├── prompt_v3/    #                   Firestore prompt repos
│   ├── security/     #                   (TBD)
│   ├── slack/        #                   HTTP + Socket adapters
│   └── telegram/     #                   Webhook adapter
│
├── web/              # DRIVING ADAPTERS — OAuth, Cabinet
├── config/           # CONFIGURATION
├── utils/            # UTILITIES — logging, telemetry, parsers
├── locales/          # LOCALIZATION
└── legacy/           # DEPRECATED — zero imports from active code
```

---

## Dependency Rule — how it should be

```
                    ALLOWED                       FORBIDDEN
                    ───────                       ─────────

domain/     → (nothing, except stdlib)            → adapters, services, agents, infra, utils
ports/      → domain/                             → adapters, services, agents, infra
agents/     → domain/, ports/                     → adapters, services (concrete)
services/   → domain/, ports/                     → adapters (concrete)
handlers/   → domain/, ports/, services/, agents/ → adapters (concrete)
infra/      → domain/, ports/                     → adapters, services, agents
adapters/   → domain/, ports/                     → services, agents, handlers
config/     → (stdlib)                            → domain, services, agents
utils/      → (stdlib)                            → domain, services, agents

COMPOSITION ROOT (main.py) → EVERYTHING (the single place for wiring)
```

**Key principle:** Dependencies flow **inward** (from outer layers toward the core). The core (domain + ports) has no knowledge of outer layers.

---

## Violations Summary

| Category | From → To | Count | Severity | Files |
|-----------|---------------|--------|----------|-------|
| CAT-1 | `services/` → `adapters/` | 9 | **CRITICAL** | `user_agent_factory.py` (8), `auth_provider_registry.py` (1) |
| CAT-2 | `services/` → `agents/` | 6 | **HIGH** | `user_agent_factory.py` (6) |
| CAT-3 | `agents/` → `services/` | 14 | **HIGH** | 6 agent files |
| CAT-4 | `infrastructure/` → `agents/` | 1 | **MEDIUM** | `agent_coordinator.py` |
| CAT-5 | `services/` → `infrastructure/` | 1 | **MEDIUM** | `user_agent_factory.py` |
| CAT-6 | `domain/` → `utils/` | 1 | **MEDIUM** | `tone.py` |
| CAT-7 | Type hints with concrete | 6 | **HIGH** | `user_agent_factory.py` |
| CAT-8 | SDK import in services | 1 | **MEDIUM** | `user_agent_factory.py` |
| **TOTAL** | | **39** | | |

**16 of 39 violations (41%) — in a single file:** `user_agent_factory.py`

---

## Clean Layers

### `domain/` — CLEAN (with 1 exception)

All 17 files in `domain/` + `domain/prompt_v3/` import only:
- `stdlib` (`enum`, `typing`, `dataclasses`, `datetime`, `uuid`, `re`)
- `pydantic`
- Each other (`..domain.agent`, `..domain.entities`)

**Single exception:** `domain/tone.py:5` — see CAT-6.

### `ports/` — PERFECTLY CLEAN

All 20 files in `ports/` + `ports/prompt_v3/` import only:
- `abc.ABC`, `abc.abstractmethod`
- `typing`
- `pydantic`
- `domain/` models

**Zero violations. The ports layer is flawless.**

### `legacy/` — ISOLATED

Zero imports from `legacy/` in active code. Zero imports from active code into `legacy/`.

---

## CAT-1: services/ → adapters/ (9 violations)

**Severity: CRITICAL — Completely bypasses port abstractions**

### File: `src/services/user_agent_factory.py`

```python
# Lines 16-21, 30-32: 8 direct imports of concrete adapters
from ..adapters.firestore_repo import FirestoreFactRepository                  # line 16
from ..adapters.firestore_session_store import FirestoreSessionStore            # line 17
from ..adapters.gemini_adapter import GeminiAdapter                            # line 18
from ..adapters.claude_adapter import ClaudeAdapter                            # line 19
from ..adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter         # line 21
from ..adapters.firestore_prompt_repository import FirestorePromptComponentRepository  # line 30
from ..adapters.groovy_prompt_assembler import GroovyPromptAssembler           # line 31
from ..adapters.xml_prompt_assembler import XmlPromptAssembler                 # line 32
```

**What is violated:** The service layer directly depends on 8 concrete implementations instead of ports.

**Existing ports that SHOULD be used:**

| Concrete Import | Should be Port |
|----------------|-------------------|
| `FirestoreFactRepository` | `ports.repository.FactRepository` |
| `FirestoreSessionStore` | `ports.session_store.SessionStore` |
| `GeminiAdapter` | `ports.llm_service.LLMService` |
| `ClaudeAdapter` | `ports.llm_service.LLMService` |
| `GeminiEmbeddingAdapter` | `ports.embedding_service.EmbeddingService` |
| `FirestorePromptComponentRepository` | `ports.prompt_component_repository.PromptComponentRepository` |
| `GroovyPromptAssembler` | `ports.prompt_assembler.PromptAssembler` |
| `XmlPromptAssembler` | `ports.prompt_assembler.PromptAssembler` |

### File: `src/services/auth_provider_registry.py`

```python
# Line 12: Direct import of Firebase adapter
from ..adapters.firebase_auth_adapter import FirebaseAuthAdapter
```

**Context:** This is a Service Locator for OAuth providers. Inside `_initialize_providers()` (line 59), `FirebaseAuthAdapter()` is instantiated directly. The Service Locator pattern is a composition root responsibility, and it should not live in `services/`.

---

## CAT-2: services/ → agents/ (6 violations)

**Severity: HIGH — Dependency inversion (services depends on agents)**

### File: `src/services/user_agent_factory.py`

```python
# Lines 34-39: Factory knows about concrete agents
from ..agents.core.quick_response_agent import create_quick_response_agent     # line 34
from ..agents.core.smart_response_agent import create_smart_response_agent     # line 35
from ..agents.core.router_agent import create_router_agent                     # line 36
from ..agents.memory_search_agent import MemorySearchAgent                     # line 37
from ..agents.web_search_agent import WebSearchAgent                           # line 38
from ..agents.consolidation_agent import ConsolidationAgent                    # line 39
```

**Why this is a problem:**

In hexagonal architecture, `services/` is the application layer and `agents/` is the domain layer. The application layer should not import from the domain layer FOR INSTANTIATION. Instantiation is the responsibility of the composition root.

**However, there is a nuance:** `UserAgentFactory` is essentially a **composition root** (a factory), not an application service. It creates and wires objects. The problem is not WHAT it does, but WHERE it lives (`services/`).

---

## CAT-3: agents/ → services/ (14 violations)

**Severity: HIGH — Agents depend on concrete services instead of ports**

This is the most widespread category. 6 out of 7 agents import concrete services:

### `agents/consolidation_agent.py` (3 violations)

```python
from ..services.prompt_builder import PromptBuilder           # line 23
from ..services.agent_context_builder import AgentExecutionContext  # line 26
from ..services.fact_write_service import FactWriteService    # line 27
```

### `agents/memory_search_agent.py` (1 violation)

```python
from ..services.search_enrichment_service import SearchEnrichmentService  # line 20
```

### `agents/web_search_agent.py` (2 violations)

```python
from ..services.agent_context_builder import AgentExecutionContext  # line 15
from ..services.prompt_builder import PromptBuilder                # line 16
```

### `agents/core/router_agent.py` (3 violations)

```python
from ...services.search_enrichment_service import SearchEnrichmentService  # line 31
from ...services.prompt_builder import PromptBuilder                       # line 32
from ...services.agent_context_builder import AgentExecutionContext         # line 33
```

### `agents/core/quick_response_agent.py` (3 violations)

```python
from ...services.prompt_builder import PromptBuilder              # line 35
from ...services.agent_context_builder import AgentExecutionContext # line 36
from ...services.cost_calculator import calculate_cost             # line 37
```

### `agents/core/smart_response_agent.py` (2 violations)

```python
from ...services.prompt_builder import PromptBuilder              # line 41
from ...services.agent_context_builder import AgentExecutionContext # line 42
```

### Analysis: which services are used by agents

| Service | Used in | Needs port? |
|--------|---------------|-------------|
| `PromptBuilder` | 5 of 6 agents | **Yes** — `ports.prompt_builder.PromptBuilder` (Protocol) |
| `AgentExecutionContext` | 5 of 6 agents | **Yes** — this is a dataclass, should be in `domain/` |
| `SearchEnrichmentService` | 2 agents | **Yes** — `ports.search_service.SearchService` (Protocol) |
| `FactWriteService` | 1 agent | **Yes** — `ports.fact_writer.FactWriter` (Protocol) |
| `calculate_cost` | 1 agent | **No** — pure function, can go in `domain/billing.py` |

---

## CAT-4: infrastructure/ → agents/ (1 violation)

**Severity: MEDIUM**

### `src/infrastructure/agent_coordinator.py`

```python
from ..agents.base_agent import BaseAgent  # line 11
```

**Context:** `AgentCoordinator` holds `Dict[str, BaseAgent]` and calls `agent.can_handle()`, `agent.execute()`. The dependency on `BaseAgent` is a dependency on a concrete base class.

**Fix:** Define a `Protocol` in `ports/`:

```python
# ports/agent.py
class AgentProtocol(Protocol):
    agent_id: str
    async def can_handle(self, message: AgentMessage) -> bool: ...
    async def execute(self, message: AgentMessage) -> AgentResponse: ...
```

---

## CAT-5: services/ → infrastructure/ (1 violation)

### `src/services/user_agent_factory.py`

```python
from ..infrastructure.agent_coordinator import AgentCoordinator  # line 41
```

**Context:** Factory registers agents in the coordinator. This is again a composition root responsibility.

---

## CAT-6: domain/ → utils/ (1 violation)

**Severity: MEDIUM — The only violation of core purity**

### `src/domain/tone.py`

```python
from ..utils.logger import logger  # line 5

# Used on line 34:
logger.warning("Invalid tone '%s', defaulting to '%s'", tone, cls.FRIENDLY.value)
```

**Why this is a problem:** `utils.logger` imports `os.getenv()`, `telemetry`, `logging_context` — these are infrastructure dependencies. The domain layer should not know about the logger.

**Fix:**
```python
# Option 1: Return the value, let the caller log
@classmethod
def validate(cls, tone: str) -> str:
    if tone in cls._value2member_map_:
        return str(tone)
    return str(cls.FRIENDLY.value)  # No logging in domain

# Option 2: Use stdlib logging (no infrastructure dependency)
import logging
logger = logging.getLogger(__name__)
```

---

## CAT-7: Type hints with concrete classes

**Severity: HIGH — Constructor parameters are bound to concrete adapters**

### `src/services/user_agent_factory.py` — constructor

```python
# Lines 60-63: THREE parameters typed with concrete adapters
def __init__(
    self,
    config: Dict,
    coordinator: AgentCoordinator,
    ...
    session_store: Optional[FirestoreSessionStore] = None,     # <- VIOLATION
    llm_service: Optional[GeminiAdapter] = None,               # <- VIOLATION
    embedding_service: Optional[EmbeddingService] = None,      # <- OK (port)
    repository: Optional[FirestoreFactRepository] = None,      # <- VIOLATION
    ...
):
```

**Should be:**
```python
    session_store: Optional[SessionStore] = None,        # port
    llm_service: Optional[LLMService] = None,            # port
    repository: Optional[FactRepository] = None,         # port
```

### Return type

```python
# Line 547: Returns a concrete type
def get_session_store(self) -> FirestoreSessionStore:    # <- VIOLATION
    return self.session_store

# Should be:
def get_session_store(self) -> SessionStore:
```

### isinstance checks

```python
# Line ~443: Concrete type check
if isinstance(context.provider, ClaudeAdapter) and not self.config.get("ANTHROPIC_API_KEY"):

# Should be: check capabilities through port interface
if context.provider.provider_name == "claude" and not self.config.get("ANTHROPIC_API_KEY"):
```

### Hardcoded adapter instantiation (fallbacks)

```python
# Lines 78-87: If not provided — creates concrete adapters
self.session_store = session_store or FirestoreSessionStore(...)       # line 78
self.llm_service = llm_service or GeminiAdapter(...)                   # line 84
self.claude_service = ClaudeAdapter(...)                               # line 85 (ALWAYS created!)
self.embedding_service = embedding_service or GeminiEmbeddingAdapter(...)  # line 87
self.repository = repository or FirestoreFactRepository(...)           # line 123
self.prompt_component_repo = FirestorePromptComponentRepository(...)   # line 154 (ALWAYS!)
```

**Problem:** Even if the caller passes a port-typed object, `ClaudeAdapter` and `FirestorePromptComponentRepository` are created **unconditionally, every time**.

---

## CAT-8: Direct SDK import in service layer

### `src/services/user_agent_factory.py`

```python
from google.genai import types  # line 10
```

**What this is:** Google Generative AI SDK types. Used for configuring Gemini grounding.

**Why it is a violation:** `google.genai` is an external SDK tied to a specific provider (Gemini). It should be encapsulated in `adapters/gemini_adapter.py`. If grounding moves to a different API tomorrow, the service layer would need to change.

---

## Root Cause — UserAgentFactory

`src/services/user_agent_factory.py` is a **composition root** pretending to be a service.

### What it does (19 responsibilities):

```
COMPOSITION ROOT duties (should be in main.py or src/composition/):
├── Instantiation of adapters (Firestore, Gemini, Claude, Grok)
├── Instantiation of embedding service
├── Instantiation of session store
├── Instantiation of repository + circular DI
├── Instantiation of prompt infrastructure (3 components)
├── Instantiation of ConfigurationService
├── Instantiation of BiographicalContextService
├── Instantiation of SearchEnrichmentService
├── Instantiation of ProviderRegistry
└── Wiring circular dependencies (._repo = ...)

FACTORY duties (correct place: services/):
├── Creation of per-user agents (6 total)
├── Registration of agents in coordinator
└── Caching of agent sets

APPLICATION SERVICE duties (correct place: services/):
├── Loading user profile
├── Resolving LLM provider per-user
├── Building agent execution context
├── Resolving search limits per-user
└── Preloading prompt cache
```

### Import graph visualization (this file only):

```
user_agent_factory.py
├── adapters/
│   ├── firestore_repo.py              <- VIOLATION
│   ├── firestore_session_store.py     <- VIOLATION
│   ├── gemini_adapter.py              <- VIOLATION
│   ├── claude_adapter.py              <- VIOLATION
│   ├── gemini_embedding_adapter.py    <- VIOLATION
│   ├── firestore_prompt_repository.py <- VIOLATION
│   ├── groovy_prompt_assembler.py     <- VIOLATION
│   └── xml_prompt_assembler.py        <- VIOLATION
├── agents/
│   ├── core/quick_response_agent.py   <- VIOLATION
│   ├── core/smart_response_agent.py   <- VIOLATION
│   ├── core/router_agent.py           <- VIOLATION
│   ├── memory_search_agent.py         <- VIOLATION
│   ├── web_search_agent.py            <- VIOLATION
│   └── consolidation_agent.py         <- VIOLATION
├── infrastructure/
│   └── agent_coordinator.py           <- VIOLATION
├── services/ (peer — OK)
│   ├── search_enrichment_service.py
│   ├── biographical_context_service.py
│   ├── fact_write_service.py
│   ├── provider_registry.py
│   ├── agent_context_builder.py
│   ├── configuration_service.py
│   ├── prompt_component_service.py
│   └── user_prompt_builder.py
├── domain/ (OK)
│   ├── agent.py
│   └── user.py
├── ports/ (OK)
│   ├── user_repository.py
│   ├── account_repository.py
│   └── embedding_service.py
├── config/ (OK)
│   ├── environment.py
│   └── settings.py
└── external SDK
    └── google.genai.types              <- VIOLATION
```

**16 violations from 1 file.** Fixing this file eliminates ~41% of all violations.

---

## Missing Ports

The following abstractions are used by agents but have no port definitions:

| Service | Used in | Proposed port |
|--------|---------------|-------------------|
| `PromptBuilder` | 5 agents | `ports/prompt_builder.py: PromptBuilder(Protocol)` |
| `AgentExecutionContext` | 5 agents | Move to `domain/agent.py` (it is a dataclass) |
| `SearchEnrichmentService` | 2 agents | `ports/search_service.py: SearchService(Protocol)` |
| `FactWriteService` | 1 agent | `ports/fact_writer.py: FactWriter(Protocol)` |
| `calculate_cost` | 1 agent | Move to `domain/billing.py` (pure function) |
| `BaseAgent` | coordinator | `ports/agent.py: AgentProtocol(Protocol)` |
| `AgentCoordinator` | handlers | `ports/coordinator.py: Coordinator(Protocol)` (optional) |

---

## Recommended Refactoring Plan

### Phase 1: Move composition root (eliminates 16 violations)

**Step 1.1:** Create `src/composition/` directory:
```
src/composition/
├── __init__.py
├── service_container.py    # Instantiation of adapters + services
└── agent_factory.py        # Creation of per-user agents (uses service_container)
```

**Step 1.2:** Move to `service_container.py`:
- All `from ..adapters.*` imports
- All `from ..agents.*` imports
- All fallback instantiations (`or FirestoreSessionStore(...)`)
- Circular dependency wiring (`._repo = ...`)

**Step 1.3:** Keep in `services/user_agent_factory.py`:
- Per-user agent creation logic (receives dependencies through constructor)
- Agent caching
- Profile resolution
- Type hints → port interfaces only

**Result:** `services/` no longer imports `adapters/`, `agents/`, `infrastructure/`.

### Phase 2: Create missing ports (eliminates 14 violations)

**Step 2.1:** Create ports:
```python
# ports/prompt_builder.py
class PromptBuilder(Protocol):
    async def build_system_prompt(self, agent_type: str, context: dict) -> str: ...
    def invalidate_cache(self, user_id: str) -> None: ...

# ports/search_service.py
class SearchService(Protocol):
    async def enrich_context(self, query: str, ...) -> EnrichedContext: ...

# ports/fact_writer.py
class FactWriter(Protocol):
    async def write_facts(self, facts: List[FactEntity], ...) -> int: ...

# ports/agent.py (extend existing domain/agent.py)
class AgentProtocol(Protocol):
    agent_id: str
    async def can_handle(self, message: AgentMessage) -> bool: ...
    async def execute(self, message: AgentMessage) -> AgentResponse: ...
```

**Step 2.2:** Move `AgentExecutionContext` from `services/agent_context_builder.py` to `domain/agent.py` (it is a value object, not a service).

**Step 2.3:** Move `calculate_cost()` from `services/cost_calculator.py` to `domain/billing.py`.

**Step 2.4:** Update imports in all 6 agents:
```python
# BEFORE:
from ...services.prompt_builder import PromptBuilder
from ...services.agent_context_builder import AgentExecutionContext

# AFTER:
from ...ports.prompt_builder import PromptBuilder
from ...domain.agent import AgentExecutionContext
```

**Result:** `agents/` no longer imports `services/`.

### Phase 3: Clean up domain/ (eliminates 1 violation)

**Step 3.1:** Remove `from ..utils.logger import logger` from `domain/tone.py`.
Use `import logging; logger = logging.getLogger(__name__)` (stdlib).

### Phase 4: Clean up infrastructure/ (eliminates 1 violation)

**Step 4.1:** `agent_coordinator.py` — replace `from ..agents.base_agent import BaseAgent` with `from ..ports.agent import AgentProtocol`.

### Phase 5: Move auth_provider_registry

**Step 5.1:** Move `src/services/auth_provider_registry.py` to `src/composition/auth_provider_registry.py` — this is a composition root (Service Locator), not an application service.

---

## Target Import Map (after refactoring)

```
domain/     → stdlib, pydantic                        <- CLEAN
ports/      → domain/, stdlib, typing, abc            <- CLEAN
agents/     → domain/, ports/, utils/                 <- CLEAN (14 violations fixed)
services/   → domain/, ports/, utils/, peer services/ <- CLEAN (16 violations fixed)
handlers/   → domain/, ports/, services/, infra/      <- OK (application layer)
infra/      → domain/, ports/                         <- CLEAN (1 violation fixed)
adapters/   → domain/, ports/, external SDKs          <- OK (implements ports)
composition/-> EVERYTHING                             <- OK (composition root)
web/        → services/, domain/, ports/              <- OK (driving adapter)
main.py     → composition/                            <- OK (entry point)
```

### Verification script (for CI)

```python
#!/usr/bin/env python3
"""Verify hexagonal architecture dependency rules."""
import ast
import sys
from pathlib import Path

FORBIDDEN_IMPORTS = {
    "domain": ["adapters", "services", "agents", "infrastructure", "handlers", "web"],
    "ports": ["adapters", "services", "agents", "infrastructure", "handlers", "web"],
    "agents": ["adapters", "services"],  # After refactor
    "services": ["adapters", "agents"],  # After refactor
    "infrastructure": ["adapters", "services", "agents"],
}

def check_file(filepath: Path) -> list[str]:
    violations = []
    # Determine layer from path
    parts = filepath.relative_to(Path("src")).parts
    layer = parts[0]

    if layer not in FORBIDDEN_IMPORTS:
        return []

    tree = ast.parse(filepath.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = node.module or ""
            for forbidden in FORBIDDEN_IMPORTS[layer]:
                if f".{forbidden}." in module or module.startswith(f"src.{forbidden}"):
                    violations.append(
                        f"{filepath}:{node.lineno}: {layer}/ imports from {forbidden}/ ({module})"
                    )
    return violations

if __name__ == "__main__":
    all_violations = []
    for py_file in Path("src").rglob("*.py"):
        if "legacy" in py_file.parts:
            continue
        all_violations.extend(check_file(py_file))

    for v in all_violations:
        print(f"VIOLATION: {v}")

    if all_violations:
        print(f"\n{len(all_violations)} architecture violations found!")
        sys.exit(1)
    else:
        print("All architecture dependency rules pass!")
```

---

## Summary

| Metric | Value |
|---------|----------|
| Total violations | **39** |
| Unique violating files | **9** |
| `user_agent_factory.py` alone | **16 (41%)** |
| Clean layers (domain + ports) | **37 files, 1 violation** |
| Agents → adapters (worst case) | **0** — agents do NOT import adapters |
| Agents → services (ports needed) | **14** — Protocol ports needed for 4 services |
| Estimated refactor effort | **Phase 1-2: ~2-3 days, Phase 3-5: ~0.5 days** |

**Key conclusion:** The architectural skeleton is correct. Domain and Ports are clean. Legacy is isolated. Agents never import adapters directly. The core problem is that `UserAgentFactory` combines a composition root and an application service in a single file in the wrong layer. Extracting the composition root and creating 4 ports will eliminate 30 of 39 violations.

---

> **This document is a living document. Mark items [DONE] as fixes are applied.**
