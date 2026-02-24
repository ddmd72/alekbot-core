# RFC: Hexagonal Prompt Caching (Transparent to Agents)

**Status:** PROPOSED
**Date:** 2026-02-24
**Owner:** AI Engineering
**Scope:** AgentContextBuilder, LLMService, ServiceContainer, PromptCacheStrategy
**Goal:** Transparent API-level prompt caching where agents declare only their identity, never touching caching logic.

**Related Building Block:** Provider Resolution
**Related RFC:** ADAPTIVE_ROUTING_CACHE_RFC (partially supersedes Section 8: Cache Strategy)

---

## 1. Problem Statement

### 1.1 Current State

The prompt caching infrastructure exists but is dead code:

- `PromptCacheConfig` is defined in `ports/llm_service.py` with `enabled`, `ttl_seconds`, `cache_scope`, `cache_key`.
- `LLMRequest.cache_config` field exists as `Optional[PromptCacheConfig]`.
- `ClaudeAdapter` already applies `cache_control: {"type": "ephemeral"}` when `cache_config.enabled=True`.
- `GeminiAdapter` raises `ValueError` when caching is requested (fail-fast, correct behavior).

**But no agent passes `cache_config`** to `LLMRequest`. Every agent creates requests with `cache_config=None`.

### 1.2 Design Flaw in Previous Approach

The ADAPTIVE_ROUTING_CACHE_RFC (Section 8) proposed that agents themselves would decide when to cache:

```python
# Old approach (violates encapsulation):
if self.execution_context.capabilities.context_caching:
    cache_config = PromptCacheConfig(enabled=True)
request = LLMRequest(..., cache_config=cache_config)
```

This approach has three problems:

1. **Coupling:** Agents import and construct `PromptCacheConfig` — an infrastructure concern leaks into agent logic.
2. **Duplication:** Every multi-turn agent (Consolidation, Smart) would repeat the same capability check + config creation.
3. **Fragility:** Adding a new caching strategy (e.g., per-tier TTL, per-user scope) requires touching every agent.

### 1.3 Design Principle

> Agents declare **what** they are, not **how** their prompts should be cached.

An agent says "I'm a consolidator." The system decides: "Consolidator + Claude = cache system prompt with ephemeral breakpoint."

This is the same principle that drives `AgentContextBuilder`: agents declare their type, the builder resolves provider + model + tier. Caching is a natural extension of this resolution.

---

## 2. Proposed Architecture

### 2.1 High-Level Flow

```
ServiceContainer (composition root)
  │
  ├─ creates PromptCacheStrategy (stateless service)
  ├─ creates AgentContextBuilder(registry, cache_strategy)
  │
  └─ UserAgentFactory
       └─ context_builder.build("consolidation", user_config)
            │
            ├─ 1. Resolve provider + model + tier (existing logic)
            ├─ 2. strategy.resolve("consolidation", capabilities)
            │      → PromptCacheConfig(enabled=True)
            ├─ 3. Wrap: provider = CachingLLMProxy(provider, cache_config)
            └─ 4. Return AgentExecutionContext(provider=wrapped_provider)

Agent (completely unaware)
  │
  ├─ self.llm = execution_context.provider  ← receives CachingLLMProxy
  ├─ request = LLMRequest(cache_config=None)  ← agent doesn't set it
  └─ response = await self.llm.generate_content(request=request)
       │
       └─ CachingLLMProxy intercepts:
            ├─ request.cache_config is None → inject self._cache_config
            └─ forward to real ClaudeAdapter with cache_config.enabled=True
```

### 2.2 Component Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                     Hexagonal Architecture                    │
│                                                              │
│  ┌─────────┐     ┌──────────────────────┐     ┌──────────┐  │
│  │  Ports   │     │      Services        │     │ Adapters  │  │
│  │         │     │                      │     │          │  │
│  │ Prompt  │◄────│  PromptCache         │     │  Claude  │  │
│  │ Cache   │     │  Strategy            │     │  Adapter │  │
│  │ Strategy│     │  (business rules)    │     │          │  │
│  │ Port    │     │                      │     │          │  │
│  │         │     │  CachingLLM          │────►│          │  │
│  │ LLM     │◄────│  Proxy               │     │  Gemini  │  │
│  │ Service │     │  (transparent wrap)  │     │  Adapter │  │
│  │         │     │                      │     │          │  │
│  │         │     │  AgentContext         │     │          │  │
│  │         │     │  Builder             │     │          │  │
│  │         │     │  (orchestrates)      │     │          │  │
│  └─────────┘     └──────────────────────┘     └──────────┘  │
│                                                              │
│  ┌─────────┐     ┌──────────────────────┐                    │
│  │ Agents  │     │    Composition       │                    │
│  │         │     │                      │                    │
│  │ Quick   │     │  ServiceContainer    │                    │
│  │ Smart   │     │  (wires strategy     │                    │
│  │ Consol. │     │   into builder)      │                    │
│  │ Router  │     │                      │                    │
│  │ WebSrch │     │                      │                    │
│  └─────────┘     └──────────────────────┘                    │
│                                                              │
│  Agents NEVER import PromptCacheConfig.                      │
│  Agents NEVER know if their provider is wrapped.             │
└──────────────────────────────────────────────────────────────┘
```

### 2.3 Import Dependencies (Hexagonal Compliance)

```
ports/prompt_cache_strategy_port.py  →  ports/llm_service.py, stdlib
services/prompt_cache_strategy.py    →  ports/prompt_cache_strategy_port, ports/llm_service
services/caching_llm_proxy.py        →  ports/llm_service, domain/user (for PerformanceTier)
services/agent_context_builder.py    →  ports/, services/caching_llm_proxy (conditional)
composition/service_container.py     →  services/ (composition root, allowed)
```

No import rule violations. Domain layer untouched.

---

## 3. Caching Strategy: Business Rules

### 3.1 Agent-to-Caching Matrix

| Agent Type | System Prompt | Multi-turn | Provider | Caching Decision |
|------------|--------------|------------|----------|-----------------|
| consolidation | 100% static | 10 turns | Claude (default) | **CACHE** — maximum benefit, same prompt reused across all turns |
| smart | ~80% static | 5 turns | Claude/Gemini | **CACHE if provider supports** — good benefit on delegation loop |
| quick | ~80% static | 1 turn | Gemini (default) | **CACHE if provider supports** — moderate benefit on repeated calls within session |
| router | Short, classification | 1 turn | Gemini (default) | **NO CACHE** — prompt is small, single-shot, not worth overhead |
| web_search | Empty/minimal | 1 turn | Gemini (default) | **NO CACHE** — no system prompt to cache |

### 3.2 Guard Clauses

1. **Provider capability check:** If `capabilities.context_caching == False` → return `None`. This prevents caching from being injected into Gemini (which raises `ValueError`) or Grok (which doesn't support it).
2. **Agent type check:** If `agent_type` is not in `{"consolidation", "smart", "quick"}` → return `None`.

### 3.3 Why Not Cache Router?

Router uses Gemini Flash for fast classification. Gemini doesn't support API-level prompt caching. Even if it did, the router prompt is small (~500 tokens) and single-shot — caching overhead would exceed benefit.

### 3.4 Explicit Override Semantics

If an agent or caller explicitly sets `cache_config` on `LLMRequest`, the proxy respects it and does NOT override. This is a safety valve for future cases where agents might need fine-grained control (e.g., disabling caching for a specific call).

---

## 4. New Components

### 4.1 Port: `PromptCacheStrategyPort`

**File:** `src/ports/prompt_cache_strategy_port.py`

```python
from abc import ABC, abstractmethod
from typing import Optional
from .llm_service import ProviderCapabilities, PromptCacheConfig


class PromptCacheStrategyPort(ABC):
    """Port for resolving prompt cache configuration based on agent identity.

    Implements the principle: agents declare WHAT they are,
    the strategy decides HOW (and whether) to cache.
    """

    @abstractmethod
    def resolve(
        self, agent_type: str, capabilities: ProviderCapabilities
    ) -> Optional[PromptCacheConfig]:
        """Resolve cache configuration for a given agent type and provider.

        Args:
            agent_type: Agent identity string (e.g., "consolidation", "smart").
            capabilities: Provider feature flags (context_caching, etc.).

        Returns:
            PromptCacheConfig if caching should be applied, None otherwise.
        """
        pass
```

**Justification for port:** Testable substitution (mock in unit tests) + potential for environment-specific strategies (e.g., disable caching in dev to reduce costs).

### 4.2 Service: `PromptCacheStrategy`

**File:** `src/services/prompt_cache_strategy.py`

```python
from typing import Optional
from ..ports.prompt_cache_strategy_port import PromptCacheStrategyPort
from ..ports.llm_service import ProviderCapabilities, PromptCacheConfig
from ..utils.logger import logger


class PromptCacheStrategy(PromptCacheStrategyPort):
    """Default prompt cache strategy.

    Business rules:
    - Consolidation, Smart, Quick agents benefit from caching (static/semi-static system prompts).
    - Router and WebSearch do not benefit (short/empty prompts, single-shot).
    - Provider must support context_caching (Claude yes, Gemini/Grok no).
    """

    CACHEABLE_AGENTS: frozenset = frozenset({"consolidation", "smart", "quick"})

    def resolve(
        self, agent_type: str, capabilities: ProviderCapabilities
    ) -> Optional[PromptCacheConfig]:
        if not capabilities.context_caching:
            return None

        if agent_type not in self.CACHEABLE_AGENTS:
            return None

        logger.debug(
            "💾 [PromptCacheStrategy] Caching enabled for agent_type=%s",
            agent_type,
        )
        return PromptCacheConfig(enabled=True)
```

### 4.3 Service: `CachingLLMProxy`

**File:** `src/services/caching_llm_proxy.py`

A transparent decorator implementing `LLMService` that wraps a real provider and auto-injects `cache_config` into every `LLMRequest`.

```python
class CachingLLMProxy(LLMService):
    """Transparent proxy that injects prompt cache config into LLM requests.

    Agents receive this proxy instead of the raw provider.
    They call generate_content() as usual — the proxy enriches
    the request with cache_config before forwarding to the real adapter.
    """

    def __init__(self, inner: LLMService, cache_config: PromptCacheConfig):
        self._inner = inner
        self._cache_config = cache_config

    async def generate_content(self, *, request=None, **kwargs):
        # LLMRequest path (all current agents use this)
        if request is not None:
            if not request.cache_config and self._cache_config:
                request = request.model_copy(
                    update={"cache_config": self._cache_config}
                )
            return await self._inner.generate_content(request=request)

        # Legacy parameter path (defensive)
        effective_cache = kwargs.pop("cache_config", None) or self._cache_config
        return await self._inner.generate_content(
            cache_config=effective_cache, **kwargs
        )

    # All other methods delegate to inner
    def supports_caching(self) -> bool:
        return self._inner.supports_caching()

    async def upload_file(self, path, mime_type):
        return await self._inner.upload_file(path, mime_type)

    def get_capabilities(self):
        return self._inner.get_capabilities()

    def get_model_for_tier(self, tier):
        return self._inner.get_model_for_tier(tier)
```

**Key design decisions:**
- `model_copy(update={...})` — immutable modification of Pydantic model, no mutation of original request.
- Explicit `cache_config` on request is never overridden — safety valve.
- Full `LLMService` interface delegation — proxy is indistinguishable from real adapter.

---

## 5. Integration Points

### 5.1 AgentContextBuilder Modification

**File:** `src/services/agent_context_builder.py`

The builder already knows `agent_type` and `capabilities`. Two changes:

**5.1.1 Constructor:**

```python
def __init__(self, registry, cache_strategy=None):
    self.registry = registry
    self._cache_strategy = cache_strategy  # Optional[PromptCacheStrategyPort]
```

**5.1.2 `build()` method — after provider resolution, before return:**

```python
# Apply caching strategy (transparent to agents)
if self._cache_strategy:
    cache_config = self._cache_strategy.resolve(agent_type, capabilities)
    if cache_config:
        from ..services.caching_llm_proxy import CachingLLMProxy
        provider = CachingLLMProxy(provider, cache_config)
```

Backward compatible: `cache_strategy=None` by default, existing code works unchanged.

### 5.2 ServiceContainer Wiring

**File:** `src/composition/service_container.py`

```python
from ..services.prompt_cache_strategy import PromptCacheStrategy

# In __init__, after ProviderRegistry setup:
self.cache_strategy = PromptCacheStrategy()
self.context_builder = AgentContextBuilder(
    self.registry,
    cache_strategy=self.cache_strategy,
)
```

### 5.3 ConsolidationAgent Context Fix (Pre-existing Bug)

**File:** `src/services/user_agent_factory.py`

**Bug:** `ConsolidationAgent` receives `smart_context` instead of a dedicated `consolidation_context`. The `context_builder.build("consolidation", ...)` is never called anywhere in the codebase.

**Impact:** The `"consolidation"` strategy in `AgentProviderStrategy.STRATEGIES` (which defaults to Claude) is dead code. Additionally, the caching strategy for `agent_type="consolidation"` would never activate because the context has `agent_type="smart"`.

**Fix:**

```python
# Add after existing context builds:
consolidation_context = self.context_builder.build("consolidation", user_profile.config)

# Change ConsolidationAgent creation:
consolidation_agent = ConsolidationAgent(
    ...,
    execution_context=consolidation_context,  # Was: smart_context
    ...
)
```

---

## 6. Cost Impact Estimate

### 6.1 Claude Prompt Caching Economics

| Metric | Value |
|--------|-------|
| Cache write cost | 1.25x regular input tokens |
| Cache read cost | 0.1x regular input tokens (90% discount) |
| Cache TTL | 5 minutes (ephemeral), auto-extended on hit |
| Min cacheable size | 1024 tokens (system prompt must exceed this) |

### 6.2 ConsolidationAgent (10 turns, Claude Opus)

System prompt: ~8000 tokens. Tool declarations: ~2000 tokens. Conversation grows ~1000 tokens/turn.

| Turn | Without cache | With cache | Input savings |
|------|-------------|-----------|---------------|
| 1 | 10,000 tokens | 12,500 (write penalty) | -25% |
| 2 | 11,000 | 1,100 (read) + 1,000 (new) | ~81% |
| 3 | 12,000 | 1,100 (read) + 2,000 (new) | ~74% |
| 5 | 14,000 | 1,100 (read) + 4,000 (new) | ~64% |
| 10 | 19,000 | 1,100 (read) + 9,000 (new) | ~47% |

**Net over 10 turns: ~60-70% reduction in input token costs.**

### 6.3 SmartResponseAgent (5 turns, Claude Opus)

Similar pattern. ~50-60% reduction when agent delegation reaches 3+ turns.

### 6.4 QuickResponseAgent

Single-shot, but benefits from repeated calls within a 5-minute window (e.g., user sends multiple messages). Cache write on first call, cache read on subsequent calls.

---

## 7. Files Changed

### New Files

| File | Layer | Purpose |
|------|-------|---------|
| `src/ports/prompt_cache_strategy_port.py` | Port | ABC interface for caching strategy |
| `src/services/prompt_cache_strategy.py` | Service | Business rules: agent_type → cache config |
| `src/services/caching_llm_proxy.py` | Service | Transparent LLMService wrapper |

### Modified Files

| File | Change | Complexity |
|------|--------|-----------|
| `src/services/agent_context_builder.py` | Accept `cache_strategy`, wrap provider in `build()` | Low |
| `src/composition/service_container.py` | Create strategy, pass to builder | Low |
| `src/services/user_agent_factory.py` | Fix consolidation context (pre-existing bug) | Low |

### NOT Modified (by design)

| File | Why |
|------|-----|
| Any agent file | Agents are completely unaware of caching |
| `src/adapters/claude_adapter.py` | Already handles `cache_config.enabled` correctly |
| `src/adapters/gemini_adapter.py` | Already fails fast on unsupported caching |
| `src/ports/llm_service.py` | `PromptCacheConfig` and `LLMRequest` already correct |
| `src/domain/*` | Domain layer untouched |

---

## 8. Test Strategy

### 8.1 Unit Tests: Strategy

**File:** `tests/unit/services/test_prompt_cache_strategy.py`

| Test Case | Input | Expected |
|-----------|-------|----------|
| consolidation + caching provider | `agent_type="consolidation"`, `context_caching=True` | `PromptCacheConfig(enabled=True)` |
| smart + caching provider | `agent_type="smart"`, `context_caching=True` | `PromptCacheConfig(enabled=True)` |
| quick + caching provider | `agent_type="quick"`, `context_caching=True` | `PromptCacheConfig(enabled=True)` |
| router (never cached) | `agent_type="router"`, any capabilities | `None` |
| web_search (never cached) | `agent_type="web_search"`, any capabilities | `None` |
| non-caching provider | any `agent_type`, `context_caching=False` | `None` |
| unknown agent type | `agent_type="future_agent"` | `None` |

### 8.2 Unit Tests: Proxy

**File:** `tests/unit/services/test_caching_llm_proxy.py`

| Test Case | Scenario | Assertion |
|-----------|----------|-----------|
| Injects cache_config | `LLMRequest(cache_config=None)` | Inner receives `cache_config.enabled=True` |
| Preserves explicit config | `LLMRequest(cache_config=PromptCacheConfig(enabled=False))` | Inner receives original config |
| Delegates supports_caching | Call `proxy.supports_caching()` | Returns inner's value |
| Delegates get_capabilities | Call `proxy.get_capabilities()` | Returns inner's capabilities |
| Delegates get_model_for_tier | Call `proxy.get_model_for_tier(BALANCED)` | Returns inner's model |

### 8.3 Integration Tests: Builder

**File:** `tests/unit/services/test_agent_context_builder.py` (extend existing)

| Test Case | Scenario | Assertion |
|-----------|----------|-----------|
| Wraps provider when strategy says cache | `build("consolidation")` with Claude-like provider | `provider` is `CachingLLMProxy` |
| No wrapping without strategy | `AgentContextBuilder(registry)` (no strategy) | `provider` is raw |
| No wrapping for non-caching provider | `build("consolidation")` with Gemini-like provider | `provider` is raw |
| No wrapping for router | `build("router")` with caching provider | `provider` is raw |
| Delegation through proxy works | `ctx.provider.get_capabilities()` | Returns correct capabilities |

---

## 9. Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Proxy adds latency | Negligible | Proxy is sync in-memory, no I/O. `model_copy` is ~0.01ms |
| Agent explicitly sets cache_config | Conflict | Proxy respects explicit config, never overrides |
| New agent type not in CACHEABLE_AGENTS | Misses caching | Strategy returns None → safe default. Update CACHEABLE_AGENTS when adding new multi-turn agents |
| Gemini assigned to consolidation via user config | ValueError from adapter | Strategy checks `capabilities.context_caching` → returns None for Gemini |
| ConsolidationAgent context fix changes behavior | Unexpected model change | This is a bug fix — consolidation should use its own strategy (Claude default), not inherit smart's |

---

## 10. Relationship to ADAPTIVE_ROUTING_CACHE_RFC

This RFC **partially supersedes** Section 8 (Adaptive Cache Strategy) of the ADAPTIVE_ROUTING_CACHE_RFC:

- **Section 8.1-8.3 (When to cache, TTL, lifecycle):** Superseded. Caching is now resolved at context-build time, not at runtime by agents. TTL is managed by the provider (Claude's ephemeral = ~5 min auto-extended).
- **Section 10.4 (Caching implications):** Compatible. The rule "cache only static content" is preserved — `cache_control` is applied to system instruction only.
- **Sections 1-7, 9-10 (Routing, search, dedup, injection):** Unaffected. These concern routing logic and prompt assembly, orthogonal to API-level caching.

---

## 11. Implementation Order

1. `src/ports/prompt_cache_strategy_port.py` — port interface
2. `src/services/prompt_cache_strategy.py` — business rules
3. `src/services/caching_llm_proxy.py` — transparent proxy
4. `src/services/agent_context_builder.py` — inject proxy in `build()`
5. `src/composition/service_container.py` — wire strategy
6. `src/services/user_agent_factory.py` — fix consolidation context
7. Tests
8. `make check` — verify domain purity

---

## 12. Decision Summary

- Agents declare identity via `agent_type` (existing).
- `PromptCacheStrategyPort` (new port) resolves `agent_type + capabilities → Optional[PromptCacheConfig]`.
- `CachingLLMProxy` (new service) transparently wraps `LLMService` and injects `cache_config`.
- `AgentContextBuilder.build()` applies strategy and wraps provider when appropriate.
- Agents never import, create, or reference `PromptCacheConfig`.
- Hexagonal purity maintained: no domain changes, no adapter changes, clean import chain.

---

**Next Step:** Approve, implement, update building blocks and CLAUDE.md.
