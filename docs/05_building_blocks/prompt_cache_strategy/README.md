# Prompt Cache Strategy (Building Block)

## Purpose

Describes the transparent prompt caching system that applies API-level caching (Claude `cache_control`) without agents knowing about it. Agents declare their identity; the strategy decides what to cache.

### When to Read

- Before modifying how agents interact with LLM providers, or when adding new agent types.
- When troubleshooting cache-related issues, optimizing token costs, or extending caching to new providers.

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

## 2. Defaults

Out of the box — zero configuration required:

| Agent | Default Provider | Caching On? | Reason |
|-------|-----------------|:-----------:|--------|
| `consolidation` | Claude | **Yes** | Claude default + static 8k system prompt across 10 turns |
| `smart` | Gemini | No | Gemini doesn't support `context_caching` |
| `quick` | Gemini | No | Same — activates automatically when provider switches to Claude |
| `router` | Gemini | No | Short classification prompt, not worth it by design |
| `web_search` | Gemini | No | No system prompt to cache |

Caching activates automatically when **two conditions are true simultaneously**:
1. The agent type is in `CACHEABLE_AGENTS` (`consolidation`, `smart`, `quick`)
2. The resolved provider has `capabilities.context_caching = True` (only `ClaudeAdapter`)

---

## 3. Integration Flow

```
AgentContextBuilder.build(agent_type, user_config)
  │
  ├─ 1. Resolve provider via ProviderRegistry
  ├─ 2. Resolve tier + model
  ├─ 3. strategy.resolve(agent_type, capabilities)
  │      ├─ "consolidation" + Claude → PromptCacheConfig(enabled=True)
  │      ├─ "smart" + Claude        → PromptCacheConfig(enabled=True)
  │      ├─ "quick"  + Gemini       → None (Gemini: context_caching=False)
  │      └─ "router" + any          → None (not in CACHEABLE_AGENTS)
  ├─ 4. If config returned: provider = CachingLLMProxy(provider, config)
  └─ 5. Return AgentExecutionContext(provider=wrapped_or_raw)

CachingLLMProxy.generate_content(request=LLMRequest(...))
  ├─ request.cache_config is None → inject PromptCacheConfig(enabled=True)
  └─ forward to ClaudeAdapter → adds cache_control: ephemeral to system_parts
```

---

## 4. Configuration Guide

### 4.1 Enable caching for `smart` or `quick` (switch provider to Claude)

Caching activates automatically once the provider is Claude. Two ways to switch:

**Option A — per user, per agent** (Firestore `UserBotConfig`):

```python
user_config.agent_providers["smart"] = "claude"
# or
user_config.agent_providers["quick"] = "claude"
```

**Option B — per user, global provider preference**:

```python
user_config.provider_preference = "claude"
# All agents that allow "claude" in allowed_providers will use it
```

Default values:
- `user_config.agent_providers` → `{}` (empty — use strategy default)
- `user_config.provider_preference` → `None` (empty — use strategy default)

No code changes needed. Caching activates automatically after provider switch.

---

### 4.2 Add a new agent type to caching

**File:** `src/services/prompt_cache_strategy.py`

```python
CACHEABLE_AGENTS: frozenset = frozenset({"consolidation", "smart", "quick"})
# Add your new type:
CACHEABLE_AGENTS: frozenset = frozenset({"consolidation", "smart", "quick", "my_new_agent"})
```

**Prerequisites:**
- The new agent's default provider must support `context_caching` (currently only Claude).
- Or the agent must be used only with Claude via `agent_providers` override.
- System prompt must be >1024 tokens (Anthropic minimum for caching).

**Also update:**
- `AgentProviderStrategy.STRATEGIES` — add entry if agent type is new (or it falls back to `"quick"` strategy).
- This document (section 2 and 5).

---

### 4.3 Disable caching for a specific agent type

**File:** `src/services/prompt_cache_strategy.py`

```python
# Remove from CACHEABLE_AGENTS:
CACHEABLE_AGENTS: frozenset = frozenset({"consolidation", "quick"})  # smart removed
```

Result: `smart` context will contain raw `ClaudeAdapter` instead of `CachingLLMProxy`.

---

### 4.4 Disable caching for a specific user (route to non-caching provider)

```python
user_config.agent_providers["consolidation"] = "gemini"
```

`AgentContextBuilder` will resolve Gemini → `capabilities.context_caching = False` → strategy returns `None` → no proxy wrapping. Caching silently off for this user.

---

### 4.5 Disable caching globally (all users, all agents)

**File:** `src/composition/service_container.py`

```python
# Remove cache_strategy from builder:
self.context_builder = AgentContextBuilder(self.registry)
# (cache_strategy defaults to None — no wrapping ever happens)
```

---

### 4.6 Disable caching for one specific LLM call (inside an agent)

If an agent needs to skip caching for a particular request:

```python
request = LLMRequest(
    ...,
    cache_config=PromptCacheConfig(enabled=False),  # explicit override
)
response = await self._llm.generate_content(request=request)
```

`CachingLLMProxy` checks `if not request.cache_config` — a non-None value (even `enabled=False`) is treated as explicit and **never overridden**. This is the only case where an agent touches `PromptCacheConfig` directly.

---

### 4.7 Add a new provider with caching support

1. Implement the adapter, set `CAPABILITIES = ProviderCapabilities(context_caching=True, ...)`.
2. Handle `cache_config.enabled` in the adapter's `generate_content` to apply provider-specific caching headers/params.
3. Register in `ProviderRegistry` (in `ServiceContainer`).
4. No changes to `PromptCacheStrategy` needed — it checks `capabilities.context_caching` automatically.

---

## 5. Caching Benefit by Agent Type

| Agent | System Prompt | Multi-turn | Default Provider | Cached? | Net savings over session |
|-------|--------------|------------|-----------------|:-------:|--------------------------|
| `consolidation` | ~8 000 tokens, 100% static | 10 turns | Claude | **Yes** | ~60–70% input token reduction |
| `smart` | ~4 000 tokens, ~80% static | 5 turns | Gemini (Claude via override) | If Claude | ~50–60% |
| `quick` | ~2 000 tokens, ~80% static | 1 turn | Gemini (Claude via override) | If Claude | Moderate — benefits repeated calls within 5-min window |
| `router` | ~500 tokens | 1 turn | Gemini | No | Not worth overhead |
| `web_search` | Empty | 1 turn | Gemini | No | No benefit |

Cache write penalty: **+25%** on first call. Cache read discount: **−90%** on subsequent calls within TTL.

---

## 6. Core Components

### 6.1 PromptCacheStrategyPort

Abstract interface (`src/ports/prompt_cache_strategy_port.py`):

```python
def resolve(agent_type: str, capabilities: ProviderCapabilities) -> Optional[PromptCacheConfig]
```

Port exists for testable substitution and to allow environment-specific strategies (e.g., `NoCacheStrategy` for dev cost control).

### 6.2 PromptCacheStrategy

Default implementation (`src/services/prompt_cache_strategy.py`). Stateless. Business rules:

- Guard 1: `capabilities.context_caching == False` → `None` immediately.
- Guard 2: `agent_type not in CACHEABLE_AGENTS` → `None`.
- Otherwise: `PromptCacheConfig(enabled=True)`.

### 6.3 CachingLLMProxy

Transparent `LLMService` decorator (`src/services/caching_llm_proxy.py`):

1. Wraps a real provider (e.g., `ClaudeAdapter`).
2. Intercepts `generate_content()`.
3. If `request.cache_config is None` → injects strategy-resolved config via `model_copy()` (immutable).
4. If request has explicit `cache_config` → **never overrides**.
5. Delegates all other methods to inner provider.

### 6.4 PromptCacheConfig fields

Defined in `src/ports/llm_service.py`:

| Field | Default | Status |
|-------|---------|--------|
| `enabled` | `False` | Active — read by `ClaudeAdapter` |
| `ttl_seconds` | `None` | Reserved — TTL managed by provider (Claude ephemeral ≈ 5 min, auto-extended on hit) |
| `cache_scope` | `"user"` | Reserved — future per-scope invalidation |
| `cache_key` | `None` | Reserved — future manual cache key control |

---

## 7. Wiring (Composition Root)

`src/composition/service_container.py`:

```python
self.cache_strategy = PromptCacheStrategy()
self.context_builder = AgentContextBuilder(
    self.registry,
    cache_strategy=self.cache_strategy,  # pass None to disable globally
)
```

`src/services/user_agent_factory.py` calls `context_builder.build(agent_type, user_config)` for each agent. Caching is applied (or not) transparently before the agent is created.

---

## 8. Status & Roadmap

**Status:** Implemented — commit ae280f2, 2026-02-24

### Planned Enhancements

- **Conversation prefix caching:** Apply `cache_control` breakpoints to conversation history (not just system instruction) for multi-turn agents.
- **Per-agent TTL:** Different TTL for consolidation (long sessions) vs quick (short bursts). Uses reserved `ttl_seconds` field.
- **Cache metrics:** Track cache hit/miss rates per agent type. `LLMResponse.cache_metadata` already populated by `ClaudeAdapter`.
- **NoCacheStrategy:** Environment-specific strategy for dev/staging to avoid cache write costs.

---

**Last Updated:** 2026-02-24
