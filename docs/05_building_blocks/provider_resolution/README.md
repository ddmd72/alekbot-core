# Provider Resolution (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the system for dynamic LLM provider selection, model mapping, and performance tier management.

### When to Read

- **For AI Agents:** Before adding new LLM providers, changing model mappings, or modifying resolution logic.
- **For Developers:** When troubleshooting model selection issues, cost overruns, or provider-specific errors.

### When to Update

This document MUST be updated when:

- [ ] A new LLM provider (e.g., OpenAI, Mistral) is added to the registry.
- [ ] Performance tier definitions (ECO, BALANCED, PERFORMANCE) change.
- [ ] Agent-specific provider strategies are modified.
- [ ] The resolution order in `AgentContextBuilder` is updated.
- [ ] New provider capabilities are introduced.

### Cross-References

- **Provider Resolution Guide:** [../../08_concepts/provider_resolution_guide.md](../../08_concepts/provider_resolution_guide.md)
- **Prompt Cache Strategy:** [../prompt_cache_strategy/README.md](../prompt_cache_strategy/README.md)
- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md)
- **Constraints:** [../../02_constraints/README.md](../../02_constraints/README.md)

---

## 1. Overview

The **Provider Resolution** system decouples Alek-Core's reasoning logic from specific LLM vendors. It allows the system to dynamically choose the best model for a task based on performance requirements, user preferences, and cost constraints.

**Core Principle:** Agents request a **Performance Tier**, and the system resolves it to a concrete **Provider** and **Model**.

---

## 2. Core Components

### 2.1 Performance Tiers

Abstract levels of reasoning capability:

- **ECO:** Fast, low-cost models (e.g., Gemini Flash). Used for routing and simple queries.
- **BALANCED:** Good reasoning at moderate cost (e.g., Gemini Pro). Used for standard tasks.
- **PERFORMANCE:** Top-tier reasoning (e.g., Claude Opus). Used for complex synthesis and tool orchestration.

### 2.2 ProviderRegistry

A central service locator that maintains active instances of LLM adapters (Gemini, Claude, etc.).

- **Registration:** Adapters are registered at startup in `main.py`.
- **Lookup:** Services fetch concrete `LLMService` implementations by name.

### 2.3 AgentProviderStrategy

Defines the default provider and allowed overrides for each agent type.

- **Router:** Default `gemini`, allowed: `["grok", "gemini"]` (fast inference).
- **Quick:** Default `gemini`, allowed: `["grok", "gemini", "claude"]` (native tools). Claude supported since 2026-02-23: `AutomaticFunctionCallingConfig` is now conditional on `capabilities.native_tools` (True for Gemini/Grok, False for Claude).
- **Smart:** Default `gemini`, allowed: `["claude", "openai", "gemini", "grok"]` (tool orchestration).
- **Consolidation:** Default `claude`, allowed: `["claude", "gemini"]` (context caching).
- **Postprocessing:** Default `gemini`, allowed: `["gemini"]` — **locked, no override**. Rationale: `response_schema` (structured JSON output) is a Gemini-only feature; Claude and Grok silently ignore it, causing JSON parse failures in `HistorySummaryService`. Tier (ECO/BALANCED/PERFORMANCE) remains user-configurable via `agent_tiers["postprocessing"]`.
- **MemorySearch:** No dedicated strategy entry — resolved via `build("router", config)`. Always gets the same provider/model as RouterAgent (Gemini Flash by default). Rationale: MemorySearch does key formulation only (small, fast task), not user-facing reasoning.

---

## 3. Resolution Process

The `AgentContextBuilder` orchestrates the resolution of an `AgentExecutionContext` for every request.

### 3.1 Resolution Order

1. **Strategy Lookup:** Get the default provider and allowed overrides for the `agent_type`.
2. **Provider Selection (3-Level Resolution):**
   - **Level 1 (highest priority):** Use `user_config.agent_providers[agent_type]` if set and in allowed list.
   - **Level 2:** Use `user_config.provider_preference` if it's in the allowed list.
   - **Level 3 (lowest priority):** Use the strategy's `default_provider`.
3. **Tier Selection:**
   - Use per-agent tier from `user_config.agent_tiers`.
   - Fallback to `user_config.default_tier`.
4. **Model Selection:**
   - Use `user_config.model_overrides` if present for the agent.
   - Otherwise, call `provider.get_model_for_tier(tier)` to get the vendor-specific model name.

### 3.2 Provider Selection Examples

**Example 1: Per-Agent Override (Level 1)**

```python
config = UserBotConfig(
    provider_preference="gemini",  # Global default
    agent_providers={
        "smart": "claude",  # Per-agent override
        "consolidation": "claude"
    }
)

# smart agent → claude (Level 1 wins)
# quick agent → gemini (Level 2, no Level 1 override)
```

**Example 2: Global Preference (Level 2)**

```python
config = UserBotConfig(
    provider_preference="claude"  # No per-agent overrides
)

# smart agent → claude (Level 2)
# quick agent → claude (Level 2)
# router agent → claude (Level 2, if allowed)
```

**Example 3: Strategy Default (Level 3)**

```python
config = UserBotConfig()  # Empty config

# smart agent → claude (strategy default)
# router agent → gemini (strategy default)
```

### 3.3 Prompt Cache Strategy (Step 5)

After resolving provider, tier, and model, the builder applies the **Prompt Cache Strategy**:

5. **Cache Strategy:** If a `PromptCacheStrategyPort` is configured:
   - Call `strategy.resolve(agent_type, capabilities)`.
   - If it returns a `PromptCacheConfig`, wrap the provider in `CachingLLMProxy`.
   - The proxy transparently injects `cache_config` into every `LLMRequest`.

This step is transparent to agents — they receive a `CachingLLMProxy` (which implements `LLMService`) instead of the raw adapter. See [Prompt Cache Strategy](../prompt_cache_strategy/README.md) for details.

### 3.4 Execution Context

The result is an `AgentExecutionContext` DTO containing:

- Concrete `LLMService` instance (possibly wrapped with `CachingLLMProxy`).
- Resolved `model_name`.
- Target `tier`.
- Provider `capabilities` (vision, tools, caching).

---

## 4. Code References

- `src/domain/user.py`: `PerformanceTier` and `LLMProvider` enums.
- `src/services/provider_registry.py`: Service locator for adapters.
- `src/services/agent_context_builder.py`: Main resolution logic and strategies.
- `src/adapters/gemini_adapter.py`: Gemini-specific tier mapping.
- `src/adapters/claude_adapter.py`: Claude-specific tier mapping.

---

## 5. Status & Roadmap

**Status:** ✅ Production Ready

### Planned Enhancements

- **Dynamic Fallback:** Automatically switch to a secondary provider if the primary is down (Circuit Breaker integration).
- **Cost-Aware Routing:** Select models based on real-time spot pricing or remaining user quota.
- **Capability-Based Discovery:** Route tasks to providers based on specific features (e.g., "needs 1M context" → Gemini).

---

**Last Updated:** 2026-02-24
**Status:** ✅ Complete (Per-Agent Provider Selection + Postprocessing Lock + Prompt Cache Strategy)
**Phase:** Provider Resolution Enhancement + Hexagonal Prompt Caching
**Last Updated:** 2026-02-23
**Status:** ✅ Complete (Per-Agent Provider Selection + Postprocessing Lock + Claude Quick support)
**Phase:** Provider Resolution Enhancement
