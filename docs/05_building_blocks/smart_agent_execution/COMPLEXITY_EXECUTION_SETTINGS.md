# Complexity & Execution Settings — Operator Cheat Sheet

Per-request execution parameters for the Smart agent, driven by the task complexity
classification produced by RouterAgent.

---

## Priority Order

```
complexity_settings_overrides  (Firestore, per user)
        ↓  wins if key present
DEFAULT_COMPLEXITY_SETTINGS    (src/domain/complexity_settings.py)
        ↓  fallback
agent_tiers["smart"]           (UserBotConfig, per user)
```

---

## Default Complexity → Execution Mapping

| `TaskComplexity`    | Tier          | `thinking_effort` | Notes                            |
|---------------------|---------------|-------------------|----------------------------------|
| `small_talk`        | `ECO`         | —                 | Fast, cheap; no reasoning needed |
| `info_search`       | `BALANCED`    | —                 | Web/memory retrieval             |
| `simple_analytics`  | `BALANCED`    | `low`             | Light reasoning                  |
| `deep_reasoning`    | `PERFORMANCE` | `high`            | Full reasoning budget            |

---

## Tier → Model Reference

| Tier          | Claude                       | OpenAI            | Gemini                          | Grok                          |
|---------------|------------------------------|-------------------|---------------------------------|-------------------------------|
| `ECO`         | claude-haiku-4-5-20251001    | gpt-5.4-nano      | gemini-2.5-flash-lite-preview   | grok-4-1-fast                 |
| `BALANCED`    | claude-haiku-4-5-20251001    | gpt-5.4-mini      | gemini-2.5-flash-preview-05-20  | grok-4-1-fast                 |
| `PERFORMANCE` | claude-sonnet-4-6            | gpt-5.4           | gemini-2.5-pro-preview-06-05    | grok-4-1-fast                 |
| `ULTRA`       | claude-opus-4-7              | gpt-5.4-pro       | gemini-pro-latest               | grok-4-1-fast-reasoning       |
| `TIER1/2/3`   | claude-haiku-4-5-20251001    | gpt-5.4-nano      | gemini-2.5-flash-lite-preview   | grok-4-1-fast                 |

> **TIER1/2/3** are reserved slots — default to ECO models. Override via
> `complexity_settings_overrides` or `agent_tiers` when you need a custom model
> slot without changing the named tiers.

---

## `thinking_effort` — Provider Mapping

| Value    | Claude                                    | OpenAI                        | Gemini                       |
|----------|-------------------------------------------|-------------------------------|------------------------------|
| `"low"`  | `thinking: {type: enabled, budget: 1024}` | `reasoning_effort: "low"`     | `thinking_config: {mode: ENABLED}` |
| `"medium"`| `thinking: {type: enabled, budget: 8192}`| `reasoning_effort: "medium"`  | `thinking_config: {mode: ENABLED}` |
| `"high"` | `thinking: {type: enabled, budget: 16384}`| `reasoning_effort: "high"`   | `thinking_config: {mode: ENABLED}` |
| `None`   | no thinking block                         | no reasoning                  | no thinking                  |

> **Claude Haiku ignores thinking.** The adapter silently skips the thinking block
> for non-sonnet/opus models. Minimum tier for effective thinking on Claude: `PERFORMANCE`.

---

## Firestore — Where to Write Overrides

Collection (dev): `development_domain_users_v2`
Document ID: `<user_id>`

Relevant fields:

| Field                              | Type                              | Purpose                               |
|------------------------------------|-----------------------------------|---------------------------------------|
| `config.agent_tiers.smart`         | string (`"performance"`, `"ultra"`) | Fallback tier for all smart requests |
| `config.complexity_settings_overrides` | map (TaskComplexity → ComplexitySettings) | Per-complexity overrides         |

---

## Override JSON Structure

```json
{
  "config": {
    "complexity_settings_overrides": {
      "deep_reasoning": {
        "tier": "ultra",
        "thinking_effort": "high",
        "provider_override": "anthropic",
        "intent_remap": {}
      },
      "simple_analytics": {
        "tier": "performance",
        "thinking_effort": "medium",
        "provider_override": null,
        "intent_remap": {}
      }
    }
  }
}
```

### `ComplexitySettings` fields

| Field              | Type                   | Description                                                 |
|--------------------|------------------------|-------------------------------------------------------------|
| `tier`             | `PerformanceTier`      | Which tier (and therefore model) to use                     |
| `thinking_effort`  | `"low"\|"medium"\|"high"\|null` | Reasoning budget. `null` = disabled                |
| `provider_override`| `"anthropic"\|"openai"\|"gemini"\|"grok"\|null` | Force a specific provider      |
| `intent_remap`     | `{intent: intent}`     | Swap intent at dispatch time (e.g. `"search_web": "search_web_light"`) |

---

## Post-Change Workflow

After editing Firestore:

```
$admin_cache_reset
```

This invalidates both the **UserBotConfig** TTL cache and the **user agent pool**
in `UserAgentFactory` — the next request reads fresh values from Firestore.

> Cache TTL is 3600 s. Without the reset, changes take up to 1 hour to take effect.

---

## Code Locations

| Concern                      | File                                          |
|------------------------------|-----------------------------------------------|
| `TaskComplexity` enum        | `src/domain/task_complexity.py`               |
| `ComplexitySettings` model   | `src/domain/complexity_settings.py`           |
| `DEFAULT_COMPLEXITY_SETTINGS`| `src/domain/complexity_settings.py`           |
| `PerformanceTier` enum       | `src/domain/user.py`                          |
| Resolution logic             | `src/services/task_execution_resolver.py`     |
| Adapter model tables         | `src/adapters/{claude,openai,gemini,grok}_adapter.py` — `MODEL_TIERS` |
| Cache invalidation           | `src/composition/user_agent_factory.py` — `invalidate_user_cache()` |
| Admin command handler        | `src/handlers/conversation_handler.py` — `handle_command("admin_cache_reset")` |
