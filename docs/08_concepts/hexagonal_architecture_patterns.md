# Hexagonal Architecture Patterns in Alek-Core

**Last Updated:** 2026-03-05
**Status:** Active Guide

---

## Overview

This document describes hexagonal architecture patterns used in Alek-Core, with practical examples from our codebase. It explains how we maintain clean separation between Domain, Application, and Infrastructure layers.

---

## Core Principle

**Dependency Direction:** Domain → Application → Infrastructure

```
┌──────────────────────────────────────────────────┐
│  Domain Layer (Pure Business Logic)             │
│  - Agents, Entities, Domain Services            │
│  - NO dependencies on Infrastructure             │
└────────────┬─────────────────────────────────────┘
             ↓ depends on
┌──────────────────────────────────────────────────┐
│  Application Layer (Orchestration)               │
│  - Application Services                          │
│  - Coordinates Domain + Infrastructure           │
└────────────┬─────────────────────────────────────┘
             ↓ depends on (via Ports)
┌──────────────────────────────────────────────────┐
│  Infrastructure Layer (Technical Details)        │
│  - Adapters, Repositories, External Services     │
│  - Implements Ports defined by Application       │
└──────────────────────────────────────────────────┘
```

**Key Rule:** Infrastructure NEVER flows up to Domain. Services never import concrete Adapters.

**Import rules (enforced by `make check`):**

```
domain/   → ONLY stdlib, pydantic
ports/    → domain/ + stdlib + ABC
adapters/ → domain/, ports/, config/
services/ → domain/, ports/   ← DO NOT import adapters
agents/   → domain/, ports/
composition/ → anything (bootstrap layer)
```

---

## Pattern 1: Service Layer for Infrastructure Work

### Problem

Domain agents (like ConsolidationAgent) need to perform Infrastructure work (like generating embeddings), which violates hexagonal boundaries.

### Solution

Create Application Service that orchestrates Infrastructure concerns, inject it into Domain via DI.

### Example: FactWriteService

**BEFORE (❌ Violation):**

```python
# ConsolidationAgent (Domain Layer)
class ConsolidationAgent:
    def __init__(self, embedding_service: EmbeddingService):
        self._embedding = embedding_service  # Infrastructure dependency!

    async def _save_facts(self, facts_data):
        for fact in facts_data:
            # Domain agent doing Infrastructure work
            vector = await self._embedding.get_embedding(fact["text"])
            # ... etc
```

**Problem:** Domain → Infrastructure (wrong direction!)

---

**AFTER (✅ Hexagonal):**

```python
# FactWriteService (Application Layer) — src/services/fact_write_service.py
class FactWriteService:
    def __init__(self, repository: FactRepository, embedding_service: EmbeddingService):
        self._repo = repository
        self._embedding = embedding_service

    async def add_facts_batch(self, account_id, user_id, facts_data):
        vectors = await self._generate_multi_vectors(facts_data)
        entities = self._create_entities(account_id, user_id, facts_data, vectors)
        return await self._repo.add_facts(entities)

# ConsolidationAgent (Domain Layer)
class ConsolidationAgent:
    def __init__(self, fact_write_service: FactWriteService):
        self._fact_write_service = fact_write_service  # Application dependency (OK!)

    async def _save_facts(self, account_id, user_id, facts_data):
        return await self._fact_write_service.add_facts_batch(account_id, user_id, facts_data)
```

**Benefits:**

- ✅ Domain → Application → Infrastructure (correct direction!)
- ✅ Agent focuses on business logic (synthesizing facts)
- ✅ Service handles technical concerns (embeddings, persistence)
- ✅ Testable (Service can be mocked)

---

## Pattern 2: Dependency Injection for Cross-Layer Services

### Problem

Infrastructure layer (Repository) needs Application layer services, but creating them internally violates boundaries.

### Solution

Inject Application services into Infrastructure adapters via constructor (DI).

### Example: SmartDeduplicationService in Repository

**BEFORE (❌ Violation — lazy service import):**

```python
# FirestoreFactRepository (Infrastructure Layer)
class FirestoreFactRepository:
    async def add_fact_if_unique(self, fact):
        # ...
        # Infrastructure CREATES Application service lazily!
        from ..services.deduplication_service import SmartDeduplicationService
        dedup_service = SmartDeduplicationService()
        is_duplicate, reason = dedup_service.is_duplicate(...)
```

**Problems:**
- Hidden `services/ → adapters/` dependency (lazy import)
- New instance created on every call

---

**AFTER (✅ Hexagonal):**

```python
# SmartDeduplicationService lives in domain/ — zero external deps, pure logic
# src/domain/deduplication_service.py

# FirestoreFactRepository (Infrastructure Layer)
class FirestoreFactRepository:
    def __init__(
        self,
        db_client,
        env_config: EnvironmentConfig,
        embedding_service: Optional[EmbeddingService] = None,
        biographical_context_service: Optional[BiographicalContextService] = None,
        dedup_service: Optional[SmartDeduplicationService] = None,  # Injected!
    ):
        self._dedup_service = dedup_service or SmartDeduplicationService()

    async def add_fact_if_unique(self, fact):
        # ...
        is_duplicate, reason = self._dedup_service.is_duplicate(
            fact.text, existing_fact.text, similarity
        )
```

**Where does SmartDeduplicationService live?** In `domain/` — because it has zero external
dependencies (only `re` + `typing` from stdlib). Pure algorithmic logic belongs in Domain,
not Services. Compare: `domain/vector_math.py` (same reasoning).

```
domain/
  deduplication_service.py  ← pure logic, zero deps
  vector_math.py            ← pure math, zero deps
  llm.py                    ← conversation types (Message, MessagePart, ToolCall)
```

---

## Pattern 3: Composition Root Split (ServiceContainer + UserAgentFactory)

### Responsibility

Two composition roots with different scopes:

- **ServiceContainer** (`composition/`) — shared, singleton-per-worker. Creates all infra adapters and shared services once.
- **UserAgentFactory** (`composition/`) — per-user. Creates agents per user, using shared services injected from ServiceContainer. Lives in `composition/` (not `services/`) because it imports agent classes and config — composition-root logic.

### Example

```python
# ServiceContainer — src/composition/service_container.py
class ServiceContainer:
    def __init__(self, config, db_client, env_config, account_repo, overflow_callback):
        # 1. Create Infrastructure adapters (composition layer can import adapters)
        self.embedding_service = GeminiEmbeddingAdapter(api_key=config["GEMINI_API_KEY"])
        self.repository = FirestoreFactRepository(
            db_client, env_config,
            embedding_service=self.embedding_service,
            dedup_service=SmartDeduplicationService(),  # injected
        )

        # 2. Create shared Application services
        self.fact_write_service = FactWriteService(
            repository=self.repository,
            embedding_service=self.embedding_service,
        )

        # 3. Expose factory callable for per-user adapters
        # (FactManagementAdapter needs per-user SearchEnrichmentService)

    def create_fact_management_adapter(
        self, search_enrichment_service: SearchEnrichmentService
    ) -> FactManagementAdapter:
        """Factory for per-user FactManagementAdapter."""
        return FactManagementAdapter(
            repository=self.repository,
            embedding_service=self.embedding_service,
            fact_write_service=self.fact_write_service,
            search_enrichment_service=search_enrichment_service,  # per-user
        )

    def agent_services(self) -> dict:
        return {
            "session_store": self.session_store,        # SessionStore port
            "fact_write_service": self.fact_write_service,
            "fact_management_adapter_factory": self.create_fact_management_adapter,
            # ... other shared services
        }
```

```python
# UserAgentFactory — src/composition/user_agent_factory.py
# Lives in composition/ — legal to import agent classes and config
class UserAgentFactory:
    def __init__(
        self,
        *,
        session_store: SessionStore,           # Port type, not FirestoreSessionStore
        fact_write_service: FactWriteService,  # Shared, injected
        fact_management_adapter_factory: Callable,  # Factory for per-user adapter
        # ... other ports and services
    ):
        self.session_store = session_store
        self.fact_write_service = fact_write_service
        self.fact_management_adapter_factory = fact_management_adapter_factory

    async def _create_and_cache_agents(self, user_id: str):
        # Per-user: SearchEnrichmentService uses user-specific limits
        search_enrichment_service = SearchEnrichmentService(
            repository=self.repository,
            embedding_service=self.embedding_service,
            total_limit=semantic_limit,   # from user config
        )

        # Per-user: FactManagementAdapter depends on SearchEnrichmentService
        fact_management_adapter = self.fact_management_adapter_factory(search_enrichment_service)

        consolidation_agent = ConsolidationAgent(
            fact_write_service=self.fact_write_service,       # shared
            fact_management_port=fact_management_adapter,     # per-user
        )
```

**Key Points:**

1. `composition/` imports concrete adapters — that is its job (bootstrap layer)
2. `services/` imports only Ports — never concrete adapter classes
3. Shared services (no per-user deps) → created once in ServiceContainer
4. Per-user services (depend on user config) → created per-request in `_create_and_cache_agents`
5. Per-user adapters with per-user deps → factory callable from ServiceContainer

---

## Pattern 4: Ports & Adapters

### Principle

Application/Domain depend on **Ports** (interfaces), Infrastructure implements **Adapters**.

### Example: EmbeddingService Port

```python
# Port (Application Layer) — src/ports/embedding_service.py
class EmbeddingService(ABC):
    @abstractmethod
    async def get_embedding(self, text: str, task_type: str) -> List[float]:
        pass

# Adapter (Infrastructure Layer) — src/adapters/gemini_embedding_adapter.py
class GeminiEmbeddingAdapter(EmbeddingService):
    async def get_embedding(self, text: str, task_type: str) -> List[float]:
        result = await self.client.aio.models.embed_content(...)
        return result.embedding

# Application Service uses Port — not Adapter!
class FactWriteService:
    def __init__(self, embedding_service: EmbeddingService):  # Port, not Adapter!
        self._embedding = embedding_service
```

### Example: Conversation Types live in Domain

Message-passing types (`Message`, `MessagePart`, `ToolCall`) are domain types — they describe
the conversation model, not LLM infrastructure details.

```python
# src/domain/llm.py — canonical location
class Message(BaseModel):
    role: str
    parts: List[MessagePart]

# src/ports/llm_port.py — re-export for backward compat
from ..domain.llm import Message, MessagePart, ToolCall  # reexport

# src/ports/session_store.py — import from domain, NOT from ports/
from ..domain.llm import Message    # ✅ domain → ok
# from ..ports.llm_port import Message  # ❌ port→port violation!
```

**Benefits:**

- ✅ Application doesn't know about Gemini internals
- ✅ Can swap Gemini for OpenAI without changing Application
- ✅ Testable (Mock the Port, not the Adapter)
- ✅ Domain types are portable — any port can use them without circular imports

---

## Pattern 5: Circular Dependency Resolution

### Problem

Sometimes Application services need each other or Repository, creating circular dependencies.

### Solution

1. Create objects with `None` for circular dependencies
2. Set references after both objects exist

### Example

```python
# Step 1: Create BiographicalContextService with None repository
biographical_context_service = BiographicalContextService(
    repository=None,  # Will be set later
    config_service=config_service,
    account_repo=account_repo,
)

# Step 2: Create Repository (needs biographical_context_service)
repository = FirestoreFactRepository(
    db_client, env_config,
    embedding_service=embedding_service,
    biographical_context_service=biographical_context_service,  # DI
)

# Step 3: Resolve circular dependency
biographical_context_service.set_repository(repository)
```

**Why This Works:**

- Objects created in correct dependency order
- Circular refs set after initialization
- Services work correctly when called (refs exist by then)

---

## Pattern 6: Per-User vs Shared Services

### Principle

Some services are shared (singleton per worker process), others are per-user (different config per user).

### Classification

| Service | Scope | Why |
|---------|-------|-----|
| `GeminiAdapter` | Shared | No user-specific state |
| `FirestoreFactRepository` | Shared | No user-specific state |
| `FactWriteService` | Shared | Depends only on shared repo + embedding |
| `BiographicalContextService` | Shared | Uses system defaults, per-user resolved at call time |
| `SearchEnrichmentService` | Per-user | `total_limit` comes from user config |
| `FactManagementAdapter` | Per-user | Wraps per-user `SearchEnrichmentService` |
| Agents (Quick, Smart, Router...) | Per-user | Cached per user_id |

### Example

```python
# ServiceContainer: creates shared services once
self.fact_write_service = FactWriteService(
    repository=self.repository,
    embedding_service=self.embedding_service,
)

# UserAgentFactory._create_and_cache_agents(): creates per-user
search_enrichment_service = SearchEnrichmentService(
    repository=self.repository,
    embedding_service=self.embedding_service,
    total_limit=semantic_limit,   # ← from user config, varies per user
)
fact_management_adapter = self.fact_management_adapter_factory(search_enrichment_service)
```

---

## Pattern 7: Port-Based Platform Adapter Decoupling

### Problem

Platform adapters (Slack, Telegram) import and instantiate `ConversationHandler` from the handlers layer, creating an adapter → application layer dependency.

### Solution

1. Define `ConversationHandlerPort` and `PlatformAuthPort` ABCs in `ports/`.
2. `PlatformAdapter` depends only on these ports (no concrete handler import).
3. `SlackAdapterFactory` (in `composition/`) creates the concrete handler and injects it.

```python
# src/ports/conversation_handler_port.py
class ConversationHandlerPort(ABC):
    @abstractmethod
    async def handle_message(self, context: MessageContext, response_channel) -> None: ...

    @abstractmethod
    async def handle_command(self, command: str, context: MessageContext, response_channel) -> Optional[bool]: ...

# src/adapters/platform/base_adapter.py — depends on port, not handler
class PlatformAdapter(ABC):
    def __init__(self, conversation_handler: ConversationHandlerPort,
                 platform_auth: PlatformAuthPort, ...):
        self.conversation_handler = conversation_handler  # Port!
        self.platform_auth = platform_auth                # Port!

# src/composition/slack_adapter_factory.py — creates and injects
class SlackAdapterFactory:
    def create(self) -> SlackAdapter:
        handler = ConversationHandler(...)       # Concrete — legal in composition/
        auth = IAMService(...)                   # Concrete — legal in composition/
        return SocketModeAdapter(
            conversation_handler=handler,        # Injected as ConversationHandlerPort
            platform_auth=auth,                  # Injected as PlatformAuthPort
        )
```

**Key Points:**
- Platform adapters depend only on ports — no import from `handlers/` or `services/`
- `SlackAdapterFactory` lives in `composition/` where concrete imports are legal
- `ConversationHandler` is created once (stateless, safe to reuse)
- Same pattern applies to Telegram via its own factory

---

## Pattern 8: Protocol-Based Decoupling (services/ → infrastructure/)

### Problem

Services layer sometimes needs to call infrastructure components (e.g., `AgentCoordinator`), but `services/` must not import from `infrastructure/`.

### Solution

Define a `typing.Protocol` in the service file. The infrastructure class satisfies the protocol structurally — no explicit inheritance needed.

### Example: AgentFallbackService

```python
# src/services/agent_fallback_service.py
from typing import Protocol

class MessageRouter(Protocol):
    """Protocol for routing agent messages. Implemented by AgentCoordinator."""
    async def route_message(self, message: AgentMessage) -> AgentResponse: ...

class AgentFallbackService:
    def __init__(self, coordinator: MessageRouter):  # Protocol, not AgentCoordinator!
        self._coordinator = coordinator
```

**Key Points:**
- `AgentCoordinator` already has `route_message()` — satisfies the Protocol structurally
- No changes needed at call sites — composition root passes the coordinator as-is
- Same pattern used in `UserNotificationService`

---

## Pattern 9: Feature Flag Centralization in agent_config

### Problem

Agents read `os.getenv()` inline to check feature flags. This violates the principle that agents must not access environment variables directly — secrets and config come via DI.

### Solution

Centralize all feature flags as module-level constants in `src/infrastructure/agent_config.py`. Read once at import time.

### Example

```python
# src/infrastructure/agent_config.py
import os

ENABLE_HISTORY_OPTIMIZATION: bool = os.getenv(
    "ENABLE_HISTORY_OPTIMIZATION", "false"
).lower() in ("true", "1", "yes")

ENABLE_GROUNDING_ATTRIBUTION: bool = os.getenv(
    "ENABLE_GROUNDING_ATTRIBUTION", "false"
).lower() == "true"

# In agents — import the constant, never os.getenv()
from ..infrastructure.agent_config import ENABLE_HISTORY_OPTIMIZATION

if ENABLE_HISTORY_OPTIMIZATION:
    # use compressed history
```

**Benefits:**
- Single source of truth for all feature flags
- Agents never import `os` for config
- Easy to find all flags in one file
- Consistent parsing logic (no scattered `.lower() == "true"` checks)

---

## Pattern 10: Domain Normalization Helpers

### Problem

Multiple adapters duplicate the same normalization logic (e.g., `.lower()` on taxonomy fields). Duplication leads to inconsistency and bugs.

### Solution

Extract shared normalization into a function in `domain/` — pure logic with zero external deps.

### Example: normalize_fact_taxonomy()

```python
# src/domain/entities.py
_FACT_TAXONOMY_FIELDS = ("domain", "temporal_class", "state", "context_priority")

def normalize_fact_taxonomy(metadata: dict) -> dict:
    """Normalize 4D taxonomy fields to lowercase for enum compatibility."""
    result = dict(metadata)
    for field in _FACT_TAXONOMY_FIELDS:
        value = result.get(field)
        if value and isinstance(value, str):
            result[field] = value.lower()
    return result
```

Both `FactManagementAdapter` and `FirestoreFactManagementAdapter` call this instead of duplicating 6 identical normalization blocks.

---

## Pattern 11: Callback Injection for Cross-Handler Decoupling

### Problem

`ConversationHandler` directly imported `process_user_batches_on_overflow` from `consolidation_handler.py`. This creates horizontal coupling between handlers that should be independent.

### Solution

Inject the callback as a constructor parameter. Wire it in the composition layer.

### Example

```python
# src/handlers/conversation_handler.py
class ConversationHandler:
    def __init__(
        self,
        coordinator,
        agent_factory,
        overflow_callback: Optional[Callable[..., Coroutine]] = None,  # injected
        ...
    ):
        self._overflow_callback = overflow_callback

# src/composition/slack_adapter_factory.py (composition root — legal to import both)
from ..handlers.consolidation_handler import process_user_batches_on_overflow

handler = ConversationHandler(
    ...,
    overflow_callback=process_user_batches_on_overflow,
)
```

**Key Points:**
- Handlers don't import each other — zero horizontal coupling
- Composition layer wires the dependency (its job)
- Same callback injected in both SlackAdapterFactory and TelegramAdapterFactory

---

## Common Violations & Fixes

### Violation 1: Agent Calls Infrastructure Directly

```python
# ❌ WRONG
class MyAgent:
    def __init__(self, embedding_service: EmbeddingService):
        self._embedding = embedding_service

    async def process(self, text):
        vector = await self._embedding.get_embedding(text)

# ✅ CORRECT
class MyService:
    def __init__(self, embedding_service: EmbeddingService):
        self._embedding = embedding_service

    async def process_with_vector(self, text):
        return await self._embedding.get_embedding(text)

class MyAgent:
    def __init__(self, my_service: MyService):
        self._service = my_service

    async def process(self, text):
        return await self._service.process_with_vector(text)
```

---

### Violation 2: Repository Creates Services (Lazy Import)

```python
# ❌ WRONG
class MyRepository:
    async def do_complex_thing(self):
        from ..services.my_service import MyService  # lazy import hides violation
        service = MyService()
        return await service.do_work()

# ✅ CORRECT
class MyRepository:
    def __init__(self, my_service: MyService):
        self._service = my_service  # Injected via DI

    async def do_complex_thing(self):
        result = await self._service.do_work()
        await self._save_result(result)
```

---

### Violation 3: Missing Port Abstraction (Concrete Type in Services)

```python
# ❌ WRONG — services/ depends on concrete adapter
class UserAgentFactory:
    def __init__(self, session_store: FirestoreSessionStore):  # concrete!
        self.session_store = session_store

    def get_session_store(self) -> FirestoreSessionStore:     # concrete!
        return self.session_store

# ✅ CORRECT — services/ depends on port
class UserAgentFactory:
    def __init__(self, session_store: SessionStore):           # port!
        self.session_store = session_store

    def get_session_store(self) -> SessionStore:               # port!
        return self.session_store
```

---

### Violation 4: Port → Port Import (Types Should Live in Domain)

```python
# ❌ WRONG — session_store.py (port) imports from llm_port.py (another port)
# src/ports/session_store.py
from ..ports.llm_port import Message  # port→port dependency!

# ✅ CORRECT — both ports import the type from domain/
# src/domain/llm.py — canonical home for conversation types
class Message(BaseModel): ...

# src/ports/session_store.py
from ..domain.llm import Message   # port → domain (OK)

# src/ports/llm_port.py
from ..domain.llm import Message   # re-export for backward compat
```

**Rule:** If multiple ports need the same type, the type belongs in `domain/`, not in one of the ports.

---

### Violation 5: Model Name Strings in Wrong Layers (REQ-ARCH-12)

```python
# ❌ WRONG — hardcoded model name in agent layer
class RouterAgent(BaseAgent):
    def __init__(self, execution_context=None):
        self.model_name = execution_context.model_name if execution_context else "gemini-3-flash-preview"
        #                                                                          ^^^^^^^^^^^^^^^^^^^^
        #                                                                          Violates REQ-ARCH-12!

# ✅ CORRECT — use None; resolution is the adapter's concern
class RouterAgent(BaseAgent):
    def __init__(self, execution_context=None):
        self.model_name = execution_context.model_name if execution_context else None
```

**Rule:** Model name string literals (`claude-*`, `gemini-*`, `gpt-*`, `grok-*`) must never appear
in `agents/`, `services/`, `domain/`, or `ports/`. They belong exclusively in adapter `MODEL_TIERS`
dicts and the billing price table. Enforced by REQ-ARCH-12 in `tests/unit/test_req_arch_01_hexagonal_isolation.py`.

**Whitelist** (`tests/unit/arch_tech_debt.py:MODEL_NAME_WHITELIST_FILES`) covers files with legitimate
model name references: `src/domain/billing.py` (pricing data) and intentionally provider-specific
agents such as `claude_deep_research_runner_agent.py`.

---

### Violation 5b: Provider Name Branching in Agents/Services (REQ-ARCH-13)

```python
# ❌ WRONG — agent branches on provider name
class SmartResponseAgent(BaseAgent):
    def _build_request(self):
        if self._provider_name == "gemini":  # provider knowledge in agent!
            return self._build_gemini_request()
        elif self._provider_name == "claude":
            return self._build_claude_request()

# ✅ CORRECT — use capabilities, not provider names
class SmartResponseAgent(BaseAgent):
    def _build_request(self):
        use_tools = self.capabilities.native_tools
        # ... single path, adapter translates to its own format
```

**Rule:** String comparisons against provider names (`"gemini"`, `"claude"`, `"openai"`, `"grok"`)
via `==`, `!=`, `in` must never appear in `agents/` or `services/`. Provider differences are
expressed through `ProviderCapabilities` fields. Enforced by REQ-ARCH-13.

---

### Violation 5c: SDK Exceptions Leaking into Agent Layer

```python
# ❌ WRONG — agent catches SDK-specific exception
from anthropic import RateLimitError  # SDK import in agent!

class SmartResponseAgent(BaseAgent):
    async def _call(self):
        try:
            return await self.llm.generate_content(...)
        except RateLimitError:  # SDK type in agent layer
            ...

# ✅ CORRECT — domain exceptions live in domain/, BaseAgent catches them centrally
# src/domain/exceptions.py
class LLMRateLimitError(LLMError): ...    # domain type — no SDK deps
class LLMUnavailableError(LLMError): ...

# src/adapters/claude_adapter.py — translate at SDK boundary
except anthropic.RateLimitError as e:
    raise LLMRateLimitError(str(e), http_status=429) from e

# src/agents/base_agent.py — catch domain type centrally in _call_llm()
except (LLMRateLimitError, LLMUnavailableError) as e:
    # transparent fallback to secondary provider
```

**Rule:** SDK exception types must never cross the adapter boundary into `agents/` or `services/`.
Adapters translate SDK errors to domain exceptions (`src/domain/exceptions.py`). `BaseAgent._call_llm()`
handles transient errors centrally — individual agents never need to catch them.

---

### Violation 5 (original): isinstance() Type Check in Services Layer

```python
# ❌ WRONG — services/ imports adapter just to do isinstance check
from ..adapters.claude_adapter import ClaudeAdapter

def _resolve_smart_llm(self, user_profile):
    context = self.context_builder.build("smart", user_profile.config)
    if isinstance(context.provider, ClaudeAdapter):  # breaks abstraction!
        raise ValueError("...")

# ✅ CORRECT — use model name (already available on context)
def _resolve_smart_llm(self, user_profile):
    context = self.context_builder.build("smart", user_profile.config)
    if context.model_name.startswith("claude"):  # no adapter import needed
        raise ValueError("...")
```

**Rule:** Never import a concrete adapter into `services/` just to type-check it. Use data already
present on the domain object (model name, provider name, capabilities).

---

### Violation 6: Agent Reads Environment Variables Directly

```python
# ❌ WRONG — agent reads os.getenv() inline
import os

class QuickResponseAgent(BaseAgent):
    def _apply_history_tier(self, history):
        if os.getenv("ENABLE_HISTORY_OPTIMIZATION", "false").lower() == "true":
            # ...

# ✅ CORRECT — feature flag centralized in agent_config
from ..infrastructure.agent_config import ENABLE_HISTORY_OPTIMIZATION

class QuickResponseAgent(BaseAgent):
    def _apply_history_tier(self, history):
        if ENABLE_HISTORY_OPTIMIZATION:
            # ...
```

**Rule:** Agents must not call `os.getenv()`. Feature flags go in `agent_config.py` as module-level
constants. Secrets (API keys) come via constructor injection from the composition layer.

---

### Violation 7: Domain Types in Wrong Layer (SearchConfig in config/)

```python
# ❌ WRONG — services import from config/ (infrastructure layer)
# src/services/biographical_context_service.py
from ..config.settings import SearchConfig  # config/ is infrastructure!

# ✅ CORRECT — domain type lives in domain/
# src/services/biographical_context_service.py
from ..domain.settings import SearchConfig  # domain/ → OK
```

**Rule:** If a dataclass is used by services/, it belongs in `domain/` — not in `config/`.
`config/` is infrastructure (environment detection, env vars). Re-export from `config/` is fine
for backward compatibility.

---

## Testing Strategies

### Unit Testing with Mocks

```python
@pytest.mark.asyncio
async def test_fact_write_service():
    mock_repo = Mock(FactRepository)
    mock_embedding = Mock(EmbeddingService)

    mock_embedding.get_embedding = AsyncMock(return_value=[0.1] * 768)
    mock_repo.add_fact_if_unique = AsyncMock(return_value=(True, None))

    service = FactWriteService(
        repository=mock_repo,
        embedding_service=mock_embedding
    )

    facts_data = [{"text": "Test", "tags": ["test"], "type": "event"}]
    saved, skipped = await service.add_facts_batch("acc-1", "user-1", facts_data)

    assert saved == 1
    mock_embedding.get_embedding.assert_called()
```

### Testing Platform Adapters

Since `ConversationHandler` is now a shared instance on `self.conversation_handler`,
replace the instance attribute directly in tests instead of patching the class:

```python
async def test_authorized_user_processed(self, adapter):
    # Replace shared handler instance with mock
    mock_handler = AsyncMock()
    adapter.conversation_handler = mock_handler

    # ... trigger the adapter ...

    mock_handler.handle_message.assert_called_once()
```

---

## Decision Tree: Where Does This Logic Go?

```
Is this BUSINESS LOGIC (domain rules)?
├─ YES → Is it pure algorithmic (stdlib only, no I/O)?
│   ├─ YES → domain/ (e.g., SmartDeduplicationService, cosine_similarity)
│   └─ NO  → agents/ (e.g., ConsolidationAgent, RouterAgent)
└─ NO → Is this ORCHESTRATION (coordinating multiple concerns)?
    ├─ YES → services/ (e.g., FactWriteService, SearchEnrichmentService)
    └─ NO → Infrastructure Layer
        ├─ External API call → adapters/
        └─ Persistence → adapters/ (repo)

Composition/wiring → composition/ (ServiceContainer, UserAgentFactory)
Bootstrap/wiring rules:
  - Shared singletons → ServiceContainer
  - Per-user objects   → UserAgentFactory._create_and_cache_agents()
```

---

## Checklist: Is My Architecture Hexagonal?

- [ ] `domain/` imports only stdlib + pydantic
- [ ] `ports/` imports only `domain/` + stdlib + ABC (no port→port imports!)
- [ ] `services/` imports only `domain/` + `ports/` (no concrete adapter imports!)
- [ ] `adapters/` imports only `domain/`, `ports/`, `config/`
- [ ] Type annotations in `services/` use Port types, not concrete adapter classes
- [ ] No `isinstance(x, ConcreteAdapter)` checks in `services/` layer
- [ ] Shared services created in `ServiceContainer`, per-user in `UserAgentFactory`
- [ ] Stateless handlers (ConversationHandler) created once, not per-request
- [ ] Pure algorithmic logic (zero external deps) lives in `domain/`
- [ ] Domain types shared by multiple ports live in `domain/`, not in one port
- [ ] Agents never call `os.getenv()` — feature flags in `agent_config.py`, secrets via DI
- [ ] Handlers don't import each other — cross-handler dependencies injected as callbacks
- [ ] Domain types used by `services/` live in `domain/`, not in `config/`
- [ ] Duplicated normalization logic extracted into `domain/` helpers
- [ ] No model name string literals (`claude-*`, `gemini-*`, etc.) in `agents/`, `services/`, `domain/`, `ports/` (REQ-ARCH-12)
- [ ] No provider name comparisons (`== "gemini"`, `== "claude"`) in `agents/` or `services/` (REQ-ARCH-13)
- [ ] SDK exceptions translated to domain exceptions at adapter boundary — never leak into agents
- [ ] Tests can mock Infrastructure via Ports
- [ ] `make check` passes: unit tests + domain purity grep

---

## Real-World Examples in Alek-Core

| Pattern | Example | Files |
|---------|---------|-------|
| Service Layer | FactWriteService extracts embedding generation | `src/services/fact_write_service.py` |
| DI into Adapter | SmartDeduplicationService injected into Repository | `src/adapters/firestore_repo.py` |
| DI into Adapter | BiographicalContextService injected into Repository | `src/adapters/firestore_repo.py` |
| Composition Root Split | ServiceContainer (shared) + UserAgentFactory (per-user) | `src/composition/service_container.py`, `src/composition/user_agent_factory.py` |
| Factory Callable | `create_fact_management_adapter()` for per-user adapter | `src/composition/service_container.py` |
| Port Abstraction | `SessionStore` port, `FirestoreSessionStore` adapter | `src/ports/session_store.py`, `src/adapters/firestore_session_store.py` |
| Port/Adapter | `EmbeddingService` port, Gemini adapter | `src/ports/embedding_service.py`, `src/adapters/gemini_embedding_adapter.py` |
| Platform Port | `ConversationHandlerPort` decouples adapters from handlers | `src/ports/conversation_handler_port.py` |
| Platform Port | `PlatformAuthPort` + `IAMDecision` for platform authorization | `src/ports/platform_auth_port.py` |
| Agent Port | `PromptBuilderPort` injected into all 5 agents | `src/ports/prompt_builder_port.py` |
| Agent Port | `FactWritePort` injected into ConsolidationAgent + adapters | `src/ports/fact_write_port.py` |
| Agent Port | `SearchEnrichmentPort` injected into Router + Memory agents | `src/ports/search_enrichment_port.py` |
| Domain Types | `Message`, `MessagePart`, `ToolCall` in domain/ | `src/domain/llm.py` |
| Domain Logic | `SmartDeduplicationService` in domain/ (stdlib only) | `src/domain/deduplication_service.py` |
| Composition Factory | `SlackAdapterFactory` creates adapter with ports injected | `src/composition/slack_adapter_factory.py` |
| Circular Resolution | `BiographicalContextService ↔ FirestoreFactRepository` | `src/composition/service_container.py` |
| Adapter Rename | `FactManagementAdapter` (dropped "Firestore" prefix) | `src/adapters/fact_management_adapter.py` |
| Protocol Decoupling | `MessageRouter` Protocol decouples services/ from infrastructure/ | `src/services/agent_fallback_service.py` |
| Feature Flag Central | `ENABLE_HISTORY_OPTIMIZATION` in `agent_config.py` | `src/infrastructure/agent_config.py` |
| Domain Normalization | `normalize_fact_taxonomy()` eliminates adapter duplication | `src/domain/entities.py` |
| Callback Injection | `overflow_callback` decouples ConversationHandler from consolidation | `src/handlers/conversation_handler.py` |
| Domain Settings | `SearchConfig` moved from config/ to domain/ | `src/domain/settings.py` |
| Domain Exceptions | `LLMRateLimitError`, `LLMUnavailableError` in domain/ — no SDK deps | `src/domain/exceptions.py` |
| SDK Error Wrapping | Adapters translate SDK errors at boundary (REQ-ARCH-12/13) | `src/adapters/*_adapter.py` |
| Arch Tests | REQ-ARCH-12: no model strings; REQ-ARCH-13: no provider comparisons in agents/services | `tests/unit/test_req_arch_01_hexagonal_isolation.py` |
| Fallback Whitelist | Files with legitimate model name refs exempt from REQ-ARCH-12 | `tests/unit/arch_tech_debt.py:MODEL_NAME_WHITELIST_FILES` |

---

## Related Documentation

- **CLAUDE.md** — Import rules (authoritative, checked by `make check`)
- **Building Block:** `docs/05_building_blocks/fact_write_service/README.md`
- **RFC:** `docs/10_rfcs/EXECUTION_CONTEXT_HEXAGONAL_RFC.md`
- **Audit:** `docs/11_quality/audit_2026_02/REVIEW_HEXAGONAL_2026_02_19.md`
- **Review v2:** `docs/reviews/HEXAGONAL_ARCHITECTURE_REVIEW_V2.md`

---

**Last Review:** 2026-03-08 (updated: REQ-ARCH-12/13 model-name/provider-name violations, domain exceptions pattern, SDK error boundary rule)
**Status:** Active Reference ✅
