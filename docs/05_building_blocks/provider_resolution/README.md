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
- [ ] `resolve_for_task()` signature or behavior changes.

### Cross-References

- **Provider Resolution Guide:** [../../08_concepts/provider_resolution_guide.md](../../08_concepts/provider_resolution_guide.md)
- **Smart Agent Execution (dynamic routing):** [../smart_agent_execution/README.md](../smart_agent_execution/README.md)
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

> **Tier × provider matrix has sharp edges.** Every adapter maps a tier to a
> concrete model name and not every model accepts every parameter. Concrete
> example: `BALANCED` on Claude resolves to `claude-haiku-4-5-20251001`, which
> rejects `output_config.effort` with HTTP 400. The ConsolidationAgent default
> tier was therefore lifted to `PERFORMANCE` (`claude-sonnet-4-6`) in
> `_DEFAULT_AGENT_TIERS` (`src/domain/user.py`). When you change a default
> tier, also verify that the resulting model accepts every parameter the agent
> sends (effort, thinking type, response_schema, etc.) — see §2.4.

### 2.2 ProviderRegistry

A central service locator that maintains active instances of LLM adapters (Gemini, Claude, etc.).

- **Registration:** Adapters are registered at startup in `main.py`.
- **Lookup:** Services fetch concrete `LLMPort` implementations by name.

### 2.4 Per-model capability gates inside adapters

Beyond strategy/tier resolution, each adapter applies **capability gates**
before serializing the request — silently dropping parameters the resolved
model does not accept rather than crashing on a 400. Authoritative source for
support is the provider's own `models.retrieve()` capability response (verified
2026-04-25 via `client.models.retrieve(<model>).capabilities` for Anthropic).

**ClaudeAdapter** (`src/adapters/claude_adapter.py`):

| Parameter             | Gated to                                | Behavior on unsupported model                |
| --------------------- | --------------------------------------- | -------------------------------------------- |
| `thinking={adaptive}` | `_THINKING_MODELS = sonnet, opus`       | Skipped silently (Haiku has only `enabled`). |
| `output_config.effort`| Same `_THINKING_MODELS` substring check | Dropped silently. Required: API rejects with `400 invalid_request_error: This model does not support the effort parameter.` on Haiku 4.5. Effort and adaptive thinking go together — only Sonnet 4.6 / Opus 4.7 accept both. |
| `web_search_20260209` | `_DYNAMIC_SEARCH_MODELS = sonnet, opus` | Falls back to legacy `web_search_20250305` on Haiku. |

The gate uses substring matching (`"claude-sonnet" in model_name`) rather than
hard-coded model lists so new minor revisions inherit the right behavior. If
Anthropic ever adds effort to Haiku, drop `output_config.effort` from the
gate; until then, **never send effort to a non-thinking model**.

**SDK pin:** `anthropic >= 0.97.0` (see `requirements.txt`). Older versions
lack typed support for the GA `output_config.format` structured outputs API.

### 2.3 AgentProviderStrategy

Defines the default provider and allowed overrides for each agent type.

- **Router:** Default `gemini`, allowed: `["grok", "gemini", "openai"]` (fast inference).
- **Quick:** Default `gemini`, allowed: `["grok", "gemini", "claude", "openai"]` (native tools). Claude supported since 2026-02-23: `AutomaticFunctionCallingConfig` is now conditional on `capabilities.native_tools` (True for Gemini/Grok/OpenAI, False for Claude).
- **Smart:** Default `gemini`, allowed: `["claude", "openai", "gemini", "grok"]` (tool orchestration).
- **Consolidation:** Default `claude`, allowed: `["claude", "gemini"]` (context caching).
- **Postprocessing:** Default `gemini`, allowed: `["gemini"]` — **locked, no override**. Rationale: the agent uses `response_mime_type` (enforces raw JSON), which is not supported by Claude or Grok. Tier (ECO/BALANCED/PERFORMANCE) remains user-configurable via `agent_tiers["postprocessing"]`.
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

### 3.3 Task-Complexity Resolution (Dynamic Routing Path)

When SmartResponseAgent processes a request with a known `TaskComplexity`, it calls
`AgentContextBuilder.resolve_for_task()` instead of `build()`:

```python
def resolve_for_task(
    self,
    agent_type: str,
    config: UserBotConfig,
    settings: ComplexitySettings,       # already merged: user override > default
) -> AgentExecutionContext:
```

The difference from `build()`:

| Concern         | `build()`                                   | `resolve_for_task()`                                |
|-----------------|---------------------------------------------|-----------------------------------------------------|
| **Tier source** | `config.get_tier_for_agent(agent_type)`     | `settings.tier` (from merged ComplexitySettings)    |
| **Provider**    | 3-level resolution (per-agent > pref > default) | Same 3-level, but `settings.provider_override` wins above all if set |
| **Use case**    | Agent startup / default path                | Per-request dynamic override in SmartResponseAgent  |

`settings.provider_override` (e.g. `"claude"`) short-circuits the normal 3-level provider
resolution: if set, it is used directly without consulting `agent_providers` or
`provider_preference`.

The returned `AgentExecutionContext` has the same structure as the standard build path.
SmartResponseAgent applies it as a temporary override for the duration of one request,
then restores the default context.

See [Smart Agent Execution](../smart_agent_execution/README.md) for the full override/restore
pattern and `DEFAULT_COMPLEXITY_SETTINGS`.

### 3.4 Prompt Cache Strategy (Step 5)

After resolving provider, tier, and model, the builder applies the **Prompt Cache Strategy**:

5. **Cache Strategy:** If a `PromptCacheStrategyPort` is configured:
   - Call `strategy.resolve(agent_type, capabilities)`.
   - If it returns a `PromptCacheConfig`, wrap the provider in `CachingLLMProxy`.
   - The proxy transparently injects `cache_config` into every `LLMRequest`.

This step is transparent to agents — they receive a `CachingLLMProxy` (which implements `LLMPort`) instead of the raw adapter. See [Prompt Cache Strategy](../prompt_cache_strategy/README.md) for details.

### 3.5 Execution Context

The result is an `AgentExecutionContext` DTO containing:

- Concrete `LLMPort` instance (possibly wrapped with `CachingLLMProxy`).
- Resolved `model_name`.
- Target `tier`.
- Provider `capabilities` (vision, tools, caching).
- `provider_name` — string identifier for logging (e.g. `"gemini"`).
- `fallback_provider` — raw `LLMPort` instance (no caching proxy) for the strategy's `"fallback"` entry, or `None`.
- `fallback_model_name`, `fallback_provider_name` — resolved at build time from the fallback provider.

### 3.6 Runtime Fallback

When a primary provider returns 429 (rate limit) or 503 (unavailable), `BaseAgent._call_llm()`
catches `LLMRateLimitError` / `LLMUnavailableError` and transparently retries with `fallback_provider`.

```python
# BaseAgent._call_llm() — simplified
try:
    response = await llm.generate_content(request=request)
except (LLMRateLimitError, LLMUnavailableError) as e:
    ctx = self._agent_execution_context
    if ctx and ctx.fallback_provider:
        logger.warning("llm_fallback", extra={
            "event": "llm_fallback",
            "primary_provider": ctx.provider_name,
            "fallback_provider": ctx.fallback_provider_name,
            "error_type": "rate_limit" if isinstance(e, LLMRateLimitError) else "unavailable",
            "http_status": e.http_status,
        })
        fallback_request = request.model_copy(update={"model_name": ctx.fallback_model_name})
        response = await ctx.fallback_provider.generate_content(request=fallback_request)
    else:
        raise
```

Key properties of the fallback mechanism:

- **Transparent to agents** — no agent-level awareness. Logic lives entirely in `BaseAgent._call_llm()`.
- **Fallback gets raw provider** (no `CachingLLMProxy`) — cache is useless when switching providers.
- **Single retry** — if fallback also fails, the exception propagates normally (Circuit Breaker records the failure).
- **Structured log** — `event="llm_fallback"` enables GCP Log-based alerts for monitoring provider health.
- **Domain exceptions** — adapters translate SDK-specific errors to `LLMRateLimitError` / `LLMUnavailableError` in `src/domain/exceptions.py`. This preserves hexagonal isolation: `base_agent.py` imports only domain types, never SDK types.

The `"fallback"` key in `AgentProviderStrategy.STRATEGIES` controls which agents have a fallback configured. Agents with `"fallback": None` (e.g. `postprocessing`, `maps_search`) let the error propagate unchanged.

---

## 4. Code References

- `src/domain/user.py`: `PerformanceTier` and `LLMProvider` enums.
- `src/domain/exceptions.py`: `LLMRateLimitError`, `LLMUnavailableError` — domain-level transient error types.
- `src/services/provider_registry.py`: Service locator for adapters.
- `src/services/agent_context_builder.py`: Main resolution logic and strategies (including fallback population).
- `src/agents/base_agent.py`: `_call_llm()` — fallback retry logic; `_set_execution_context()` — context wiring.
- `src/adapters/gemini_adapter.py`: Gemini-specific tier mapping + SDK error wrapping.
- `src/adapters/claude_adapter.py`: Claude-specific tier mapping + SDK error wrapping.
- `src/adapters/openai_adapter.py`: OpenAI gpt-5 family tier mapping + SDK error wrapping.
- `src/adapters/grok_adapter.py`: Grok-specific tier mapping + SDK error wrapping.

---

## 5. Status & Roadmap

**Status:** ✅ Production Ready

### Planned Enhancements

- **Cost-Aware Routing:** Select models based on real-time spot pricing or remaining user quota.
- **Capability-Based Discovery:** Route tasks to providers based on specific features (e.g., "needs 1M context" → Gemini).

---

**Last Updated:** 2026-03-08
**Status:** ✅ Complete (Provider Resolution + Prompt Cache Strategy + Runtime Fallback on 429/503)
