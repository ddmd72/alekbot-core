# Smart Agent Execution: Dynamic Routing (Building Block)

## Purpose

Describes the per-request execution context resolution system that maps task semantics to
model tier, thinking effort, and provider. Allows per-user, per-complexity customization
without code changes.

## When to Read

- Before modifying `TaskComplexity`, `ComplexitySettings`, or `DEFAULT_COMPLEXITY_SETTINGS`.
- When adding a new `PerformanceTier` or adjusting default complexity → tier mappings.
- When debugging wrong model/tier selection for a specific request.
- When implementing per-user complexity overrides via Firestore.

## When to Update

This document MUST be updated when:

- [ ] A new `TaskComplexity` value is added.
- [ ] `DEFAULT_COMPLEXITY_SETTINGS` mappings change.
- [ ] `TaskExecutionResolver` merge logic changes.
- [ ] `AgentContextBuilder.resolve_for_task()` signature or behavior changes.
- [ ] `ComplexitySettings` fields change (new fields added, coercion rules modified).
- [ ] SmartResponseAgent's override/restore pattern changes.

## Cross-References

- **Operator cheat sheet:** [COMPLEXITY_EXECUTION_SETTINGS.md](COMPLEXITY_EXECUTION_SETTINGS.md)
- **Hybrid Router:** [../hybrid_router/README.md](../hybrid_router/README.md)
- **Provider Resolution:** [../provider_resolution/README.md](../provider_resolution/README.md)
- **RFC:** [../../10_rfcs/TASK_COMPLEXITY_EXECUTION_SETTINGS_RFC.md](../../10_rfcs/TASK_COMPLEXITY_EXECUTION_SETTINGS_RFC.md)

---

## 1. Overview

The dynamic routing system answers one question: **how should this specific request be
executed, given what kind of task it is and who the user is?**

RouterAgent classifies every request into a `TaskComplexity` enum. SmartResponseAgent
uses that classification to dynamically override its own model, tier, and thinking settings
for the duration of that request — then restores defaults.

**Why this matters:** A `small_talk` greeting runs on a cheap ECO model. A `deep_reasoning`
medical analysis runs on PERFORMANCE tier with extended thinking. Same code path, different
cost and capability — transparently resolved per request.

---

## 2. Core Domain Types

### 2.1 TaskComplexity

```python
# src/domain/task_complexity.py
class TaskComplexity(str, Enum):
    SMALL_TALK       = "small_talk"        # Greetings, acks, one-liners
    INFO_SEARCH      = "info_search"       # Factual lookups, quick retrieval
    SIMPLE_ANALYTICS = "simple_analytics"  # Basic analysis, calculations
    DEEP_REASONING   = "deep_reasoning"    # Multi-step reasoning, synthesis
```

Classification is produced by RouterAgent's LLM triage. It is a **semantic** judgment about
the nature of the task, not about message length or token count.

### 2.2 ComplexitySettings

```python
# src/domain/complexity_settings.py
class ComplexitySettings(BaseModel):
    tier: PerformanceTier              # ECO / BALANCED / PERFORMANCE / ULTRA
    thinking_effort: Optional[str]     # "low" | "medium" | "high" | None
    intent_remap: Dict[str, str]       # Dispatch-time intent substitution
    provider_override: Optional[str]   # "claude" | "openai" | "gemini" | None
```

### 2.3 DEFAULT_COMPLEXITY_SETTINGS

System-wide fallback mappings, defined in `src/domain/complexity_settings.py`:

| TaskComplexity      | Tier          | thinking_effort | Notes                          |
|---------------------|---------------|-----------------|--------------------------------|
| `small_talk`        | ECO           | None            | No reasoning needed            |
| `info_search`       | BALANCED      | None            | Retrieval-focused              |
| `simple_analytics`  | BALANCED      | `"low"`         | Light reasoning                |
| `deep_reasoning`    | PERFORMANCE   | `"high"`        | Full reasoning budget          |

---

## 3. Resolution Pipeline

```
message.context["task_complexity"]   (string, set by RouterAgent)
         ↓
TaskExecutionResolver.resolve(context, user_config)
         ↓
  1. Parse → TaskComplexity enum
  2. Fetch DEFAULT_COMPLEXITY_SETTINGS[complexity]
  3. Fetch user_config.complexity_settings_overrides[complexity]  (may be absent)
  4. Merge: user override > default
  5. AgentContextBuilder.resolve_for_task(agent_type, config, merged_settings)
         ↓
  Returns AgentExecutionContext (provider, model_name, tier)
         ↓
TaskExecutionOverride(execution_context, thinking_effort, intent_remap)
```

### 3.1 Merge Rules

User overrides in `complexity_settings_overrides` win on a per-field basis:

```python
merged_tier     = override.tier             if override else default.tier
merged_thinking = override.thinking_effort  if override else default.thinking_effort
merged_remap    = override.intent_remap     if override else default.intent_remap
merged_provider = override.provider_override if override else default.provider_override
```

A partial override (e.g. only `tier` set) still takes all fields from the user object;
there is no field-level partial merge — the user must provide a complete `ComplexitySettings`.

### 3.2 Error Handling

- **Unknown complexity string** → `ValueError` caught → `logger.warning` → return `None`
- **Missing `task_complexity` in context** → `None` returned early
- **Override is `None`** → SmartResponseAgent skips override, uses default execution context

---

## 4. SmartResponseAgent Override Pattern

Override is applied per-request in `_execute_locked()` and restored in `finally`:

```python
# Snapshot defaults
default_llm  = self.llm
default_model = self.model_name
default_ctx  = self._agent_execution_context

override = self.resolver.resolve(message.context, self.user_config)

if override:
    self.llm        = override.execution_context.provider
    self.model_name = override.execution_context.model_name
    self._set_execution_context(override.execution_context)
    thinking_effort = override.thinking_effort or thinking_effort

try:
    # ... DelegationEngine runs here with overridden settings
finally:
    self.llm        = default_llm
    self.model_name = default_model
    self._agent_execution_context = default_ctx
```

**Scope**: Override is active for the entire delegation loop (all turns of one request).
All specialist calls inherit the same overridden LLM/model/thinking via `DelegationEngine`.

---

## 5. User-Level Overrides (Firestore)

Stored in `UserBotConfig.complexity_settings_overrides` (Firestore path: `config.complexity_settings_overrides`):

```json
{
  "config": {
    "complexity_settings_overrides": {
      "deep_reasoning": {
        "tier": "ultra",
        "thinking_effort": "high",
        "provider_override": "claude",
        "intent_remap": {}
      }
    }
  }
}
```

**Sanitization**: `FirestoreUserRepository._sanitize_user_doc()` drops entries with empty
or missing `tier` before Pydantic validation. `ComplexitySettings` validators coerce
empty strings to `None` for optional fields.

After editing Firestore: run `$admin_cache_reset` to invalidate the user agent pool
(TTL is 3600s without reset).

---

## 6. Dependency Injection

`TaskExecutionResolver` is constructed in `UserAgentFactory` and injected into
`SmartResponseAgent` at creation time:

```python
# composition/user_agent_factory.py
resolver = TaskExecutionResolver(self.context_builder)
smart_agent = create_smart_response_agent(
    execution_context=smart_context,
    resolver=resolver,
    user_config=user_profile.config,
    ...
)
```

`user_config` is per-user (loaded per request), so overrides are always fresh
(subject to the agent pool TTL cache).

---

## 7. Code Locations

| Concern                          | File                                              |
|----------------------------------|---------------------------------------------------|
| `TaskComplexity` enum            | `src/domain/task_complexity.py`                   |
| `ComplexitySettings` model       | `src/domain/complexity_settings.py`               |
| `DEFAULT_COMPLEXITY_SETTINGS`    | `src/domain/complexity_settings.py`               |
| `TaskExecutionResolver`          | `src/services/task_execution_resolver.py`         |
| `TaskExecutionOverride` dataclass| `src/services/task_execution_resolver.py`         |
| `AgentContextBuilder.resolve_for_task` | `src/services/agent_context_builder.py`     |
| Override/restore in SmartAgent   | `src/agents/core/smart_response_agent.py`         |
| Firestore sanitization           | `src/adapters/firestore_user_repo.py`             |
| Router triage output             | `src/agents/core/router_agent.py`                 |
| `RoutingMetadata` + `build_routing_metadata` | `src/domain/tone.py`                |

---

## 8. Status

**Status:** ✅ Production (Phase 1 + Phase 2 complete)

**Not yet implemented:**
- Phase 3: Worker/reminder task hints — complexity context for async workers
- Phase 4: Quick agent deletion (currently kept as dead code, always routes to Smart)

**Last Updated:** 2026-04-22
