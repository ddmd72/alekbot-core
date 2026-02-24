# Prompt Cache Strategy (Building Block)

## Purpose

Describes the transparent prompt caching system that applies API-level caching (Claude `cache_control`) without agents knowing about it. Agents declare their identity; the strategy decides what to cache.

### When to Read

- **For AI Agents:** Before modifying how agents interact with LLM providers, or when adding new agent types.
- **For Developers:** When troubleshooting cache-related issues, optimizing token costs, or extending caching to new providers.

### When to Update

This document MUST be updated when:

- [ ] A new agent type is added that should benefit from caching.
- [ ] A new LLM provider with caching support is integrated.
- [ ] The caching strategy rules change (which agents cache, TTL policy, etc.).
- [ ] The `CachingLLMProxy` behavior is modified.

### Cross-References

- **RFC:** [../../10_rfcs/HEXAGONAL_PROMPT_CACHING_RFC.md](../../10_rfcs/HEXAGONAL_PROMPT_CACHING_RFC.md)
- **Provider Resolution:** [../provider_resolution/README.md](../provider_resolution/README.md)
- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md)

---

## 1. Overview

The **Prompt Cache Strategy** decouples caching decisions from agent logic. Agents only declare their type ("consolidation", "smart", "quick"). A strategy service resolves whether API-level prompt caching should be applied, and a transparent proxy wraps the LLM provider to inject `cache_config` into every request.

**Core Principle:** Agents declare **what** they are, not **how** their prompts should be cached.

---

## 2. Core Components

### 2.1 PromptCacheStrategyPort

Abstract interface (`src/ports/prompt_cache_strategy_port.py`) with a single method:

```python
def resolve(agent_type: str, capabilities: ProviderCapabilities) -> Optional[PromptCacheConfig]
```

Given an agent type and provider capabilities, returns a `PromptCacheConfig` or `None`.

### 2.2 PromptCacheStrategy

Default implementation (`src/services/prompt_cache_strategy.py`) with business rules:

- **Cacheable agents:** `consolidation`, `smart`, `quick` (static/semi-static system prompts).
- **Non-cacheable agents:** `router`, `web_search` (short/empty prompts, no benefit).
- **Provider guard:** If `capabilities.context_caching` is `False` (Gemini, Grok), returns `None`.

### 2.3 CachingLLMProxy

Transparent `LLMService` decorator (`src/services/caching_llm_proxy.py`) that:

1. Wraps a real LLM provider (e.g., `ClaudeAdapter`).
2. Intercepts `generate_content()` calls.
3. If `LLMRequest.cache_config` is `None`, injects the strategy-resolved config.
4. If the request already has explicit `cache_config`, respects it (never overrides).
5. Delegates all other `LLMService` methods (`supports_caching`, `get_capabilities`, etc.) to the inner provider.

---

## 3. Integration Flow

```
AgentContextBuilder.build(agent_type, user_config)
  │
  ├─ 1. Resolve provider via ProviderRegistry (existing)
  ├─ 2. Resolve tier + model (existing)
  ├─ 3. strategy.resolve(agent_type, capabilities)
  │      ├─ "consolidation" + Claude → PromptCacheConfig(enabled=True)
  │      ├─ "smart" + Claude → PromptCacheConfig(enabled=True)
  │      ├─ "quick" + Gemini → None (Gemini doesn't support caching)
  │      └─ "router" + any → None (router not in cacheable set)
  ├─ 4. If config returned: provider = CachingLLMProxy(provider, config)
  └─ 5. Return AgentExecutionContext(provider=wrapped_or_raw)
```

Agents receive the (possibly wrapped) provider via `execution_context.provider` and are completely unaware of the caching layer.

---

## 4. Caching Benefit by Agent Type

| Agent | System Prompt | Multi-turn | Default Provider | Cached? | Benefit |
|-------|--------------|------------|-----------------|---------|---------|
| consolidation | 100% static | 10 turns | Claude | Yes | Maximum — same prompt reused across all turns |
| smart | ~80% static | 5 turns | Gemini (Claude via override) | If Claude | Good — delegation loop reuses prompt |
| quick | ~80% static | 1 turn | Gemini | If Claude | Moderate — repeated calls in session |
| router | Short | 1 turn | Gemini | No | Not worth it |
| web_search | Empty | 1 turn | Gemini | No | No benefit |

---

## 5. Code References

- `src/ports/prompt_cache_strategy_port.py`: Port interface.
- `src/services/prompt_cache_strategy.py`: Business rules implementation.
- `src/services/caching_llm_proxy.py`: Transparent LLM proxy.
- `src/services/agent_context_builder.py`: Integration point (wraps provider in `build()`).
- `src/composition/service_container.py`: Wiring (creates strategy, passes to builder).
- `src/ports/llm_service.py`: `PromptCacheConfig`, `ProviderCapabilities` models.

---

## 6. Adding a New Cacheable Agent Type

To enable caching for a new agent type:

1. Add the agent type string to `PromptCacheStrategy.CACHEABLE_AGENTS` frozenset.
2. Ensure the agent's default provider supports `context_caching` (check `AgentProviderStrategy`).
3. Update this document.

No changes needed in the agent itself — it remains completely unaware of caching.

---

## 7. Status & Roadmap

**Status:** Production Ready

### Planned Enhancements

- **Conversation prefix caching:** Apply `cache_control` breakpoints to conversation history (not just system instruction) for multi-turn agents.
- **Per-agent TTL:** Different TTL for consolidation (long sessions) vs quick (short bursts).
- **Cache metrics:** Track cache hit/miss rates per agent type via observability system.

---

**Last Updated:** 2026-02-24
**Status:** Initial Implementation
**Phase:** Hexagonal Prompt Caching
