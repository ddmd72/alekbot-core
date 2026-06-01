make# Provider Resolution — Complete Guide

**Purpose:** Practical reference for configuring models and tiers in Alek-Core.  
**Audience:** Developers and power users customizing agent performance.

**Architecture Overview:** See [Provider Resolution Building Block](../05_building_blocks/provider_resolution/README.md)

---

## Table of Contents

1. [Quick Start](#1-quick-start)
2. [Performance Tiers](#2-performance-tiers)
3. [Provider Configuration](#3-provider-configuration)
4. [Advanced Configuration](#4-advanced-configuration)
5. [Cost Optimization](#5-cost-optimization)
6. [Troubleshooting](#6-troubleshooting)
7. [Code Reference](#7-code-reference)

---

## 1. Quick Start

### 1.1 Default Configuration

By default, all agents use **ECO tier** (cheapest, fastest):

```python
from src.domain.user import UserBotConfig, PerformanceTier

# Default configuration
config = UserBotConfig(
    default_tier=PerformanceTier.ECO
)

# Agents will use:
# router → gemini-flash-lite-latest (ECO)
# quick → gemini-3-flash-preview (BALANCED - agent default)
# smart → claude-opus-4-20250514 (PERFORMANCE - agent default)
```

**Key Point:** Each agent has strategy defaults that override `default_tier` unless you explicitly configure `agent_tiers`.

### 1.2 Upgrade Smart Agent to Best Model

```python
config = UserBotConfig(
    default_tier=PerformanceTier.ECO,  # Keep others cheap
    agent_tiers={
        "smart": PerformanceTier.PERFORMANCE  # Override for smart
    }
)

# Result:
# smart → claude-opus-4-20250514 (best reasoning)
# quick → gemini-3-flash-preview (balanced)
# router → gemini-flash-lite-latest (fast)
```

### 1.3 Switch All Agents to Gemini

```python
config = UserBotConfig(
    provider_preference="gemini"
)

# Result: All agents use Gemini models
# smart → gemini-3-pro-preview (PERFORMANCE tier)
# quick → gemini-3-flash-preview (BALANCED tier)
# router → gemini-flash-lite-latest (ECO tier)
```

---

## 2. Performance Tiers

### 2.1 Tier Comparison

| Tier            | Speed  | Cost   | Quality    | Use Case                                |
| --------------- | ------ | ------ | ---------- | --------------------------------------- |
| **ECO**         | ⚡⚡⚡ | 💰     | ⭐⭐       | Routing, classification, simple queries |
| **BALANCED**    | ⚡⚡   | 💰💰   | ⭐⭐⭐     | General responses, quick analysis       |
| **PERFORMANCE** | ⚡     | 💰💰💰 | ⭐⭐⭐⭐⭐ | Deep reasoning, complex tasks           |

### 2.2 Tier-to-Model Mapping

**Gemini (Google):**

```python
{
    PerformanceTier.ECO: "gemini-flash-lite-latest",
    PerformanceTier.BALANCED: "gemini-3-flash-preview",
    PerformanceTier.PERFORMANCE: "gemini-3-pro-preview"
}
```

**Claude (Anthropic):**

```python
{
    PerformanceTier.ECO: "claude-3-haiku-20240307",
    PerformanceTier.BALANCED: "claude-sonnet-4-5-20250929",
    PerformanceTier.PERFORMANCE: "claude-opus-4-20250514"
}
```

**Cost Comparison (per 1M tokens):**

| Provider | ECO    | BALANCED | PERFORMANCE |
| -------- | ------ | -------- | ----------- |
| Gemini   | $0.075 | $0.075   | $2.50       |
| Claude   | $0.25  | $3.00    | $15.00      |

**Recommendation:** Use Claude for reasoning tasks, Gemini for speed/cost efficiency.

### 2.3 Agent Default Tiers

Each agent has strategy defaults:

```python
AGENT_DEFAULTS = {
    "router": PerformanceTier.ECO,        # Fast classification
    "quick": PerformanceTier.BALANCED,    # Quick responses
    "smart": PerformanceTier.PERFORMANCE, # Deep analysis
    "consolidation": PerformanceTier.PERFORMANCE,
    "web_search": PerformanceTier.BALANCED,
    "memory_search": PerformanceTier.ECO
}
```

**Important:** These defaults override `UserBotConfig.default_tier` unless you set `agent_tiers[agent_type]`.

---

## 3. Provider Configuration

### 3.1 Provider Selection (3-Level Resolution)

The system uses a **3-level priority system** for provider selection:

**Priority Order:**

1. **Per-Agent Provider** (highest) - `agent_providers[agent_type]`
2. **Global Provider Preference** - `provider_preference`
3. **Strategy Default** (lowest) - Agent-specific default from `AgentProviderStrategy`

### 3.2 Global Provider Preference

Set default provider for all agents:

```python
config = UserBotConfig(
    provider_preference="claude"  # Use Claude by default
)
```

### 3.3 Per-Agent Provider Selection

Override provider for specific agents:

```python
config = UserBotConfig(
    provider_preference="gemini",  # Default for most agents
    agent_providers={
        "smart": "claude",           # Use Claude for smart agent
        "consolidation": "claude"    # Use Claude for consolidation
    }
)
```

**Real-World Example:**

```python
# Gemini for fast routing, Claude for deep reasoning
config = UserBotConfig(
    provider_preference="gemini",  # Default: Gemini
    agent_providers={
        "smart": "claude",           # Smart needs context caching
        "consolidation": "claude"    # Consolidation benefits from caching
    },
    agent_tiers={
        "router": PerformanceTier.ECO,
        "smart": PerformanceTier.PERFORMANCE
    }
)

# Result:
# - Router: Gemini Flash Lite (fast + cheap)
# - Quick: Gemini Flash (global preference)
# - Smart: Claude Opus (per-agent override + caching)
# - Consolidation: Claude Opus (per-agent override + caching)
```

**Use Cases:**

- **Gemini:** Faster responses, lower cost, Google Search integration
- **Claude:** Better reasoning, longer context, safer content

### 3.2 Agent Provider Strategies

Each agent has allowed providers:

| Agent         | Default Provider | Allowed Providers      | Why?                      |
| ------------- | ---------------- | ---------------------- | ------------------------- |
| router        | gemini           | gemini, claude         | Speed priority            |
| quick         | gemini           | gemini, claude, openai | Fast general responses    |
| smart         | claude           | claude, openai, gemini | Reasoning priority        |
| web_search    | gemini           | gemini only            | Google Search integration |
| consolidation | claude           | claude, gemini         | Memory extraction quality |
| memory_search | gemini           | gemini, claude         | Fast retrieval            |

**Enforced Providers:**

- **web_search** always uses Gemini (Google Search tool dependency)
- Other agents respect `provider_preference` if allowed

### 3.3 Provider Fallback

If provider fails:

1. **Fallback to lower tier** on same provider
2. **Circuit breaker** disables failing provider for 5 minutes
3. **Retry** with alternative provider from allowed list

```python
# Example fallback chain for smart agent:
# 1. Primary: claude-opus-4-20250514 (PERFORMANCE)
# 2. Fallback 1: claude-sonnet-4-5-20250929 (BALANCED)
# 3. Fallback 2: gemini-3-pro-preview (alternative provider)
```

---

## 4. Configuration Override Levels

### 4.1 Resolution Priority (5 Levels)

Understanding override priority helps avoid confusion when "wrong" model is selected:

```
┌───────────────────────────────────────────────────────────┐
│ LEVEL 1: USER Model Override                             │  ← HIGHEST
│ Where: UserProfile.config.model_overrides                 │
│ Example: {"smart": "claude-opus-4-20250514"}              │
│ Use: Power user wants exact model                         │
└───────────────────────────────────────────────────────────┘
                          ↓
┌───────────────────────────────────────────────────────────┐
│ LEVEL 2: USER Agent Tier                                 │
│ Where: UserProfile.config.agent_tiers                     │
│ Example: {"smart": PerformanceTier.PERFORMANCE}           │
│ Use: User customizes tier per agent                       │
└───────────────────────────────────────────────────────────┘
                          ↓
┌───────────────────────────────────────────────────────────┐
│ LEVEL 3: ACCOUNT Model Override                          │
│ Where: BillingAccount.account_defaults.model_overrides    │
│ Example: {"smart": "claude-sonnet-4-5-20250929"}          │
│ Use: Family/Enterprise mandates model                     │
└───────────────────────────────────────────────────────────┘
                          ↓
┌───────────────────────────────────────────────────────────┐
│ LEVEL 4: ACCOUNT Agent Tier                              │
│ Where: BillingAccount.account_defaults.agent_tiers        │
│ Example: {"smart": PerformanceTier.BALANCED}              │
│ Use: Family shares tier config                            │
└───────────────────────────────────────────────────────────┘
                          ↓
┌───────────────────────────────────────────────────────────┐
│ LEVEL 5: DOMAIN Defaults                                 │  ← LOWEST
│ Where: UserBotConfig() factory defaults                   │
│ router=ECO, quick=BALANCED, smart=PERFORMANCE             │
│ Use: New user with empty config                           │
└───────────────────────────────────────────────────────────┘
```

### 4.2 Who Can Set What

| Level                | Config Location                                   | Who Sets It     | When Used                   |
| -------------------- | ------------------------------------------------- | --------------- | --------------------------- |
| **USER Override**    | `UserProfile.config.model_overrides`              | Individual user | Power user pins exact model |
| **USER Tier**        | `UserProfile.config.agent_tiers`                  | Individual user | User customizes performance |
| **ACCOUNT Override** | `BillingAccount.account_defaults.model_overrides` | Account owner   | Enterprise policy           |
| **ACCOUNT Tier**     | `BillingAccount.account_defaults.agent_tiers`     | Account owner   | Family shared settings      |
| **DOMAIN**           | `UserBotConfig()` factory                         | System          | Default for all new users   |

### 4.3 Family Account Example

**Scenario:** Family account with 3 users

```python
# ACCOUNT Level (set by parent)
account = BillingAccount(
    account_defaults=UserBotConfig(
        agent_tiers={
            "smart": PerformanceTier.BALANCED  # Family uses BALANCED
        }
    )
)

# USER 1: Child uses family defaults
user1 = UserProfile(
    account_id=account.account_id,
    config=UserBotConfig()  # Empty = use ACCOUNT defaults
)
# Result: smart → BALANCED (from ACCOUNT Level 4)

# USER 2: Teenager wants ECO (saves allowance)
user2 = UserProfile(
    account_id=account.account_id,
    config=UserBotConfig(
        agent_tiers={"smart": PerformanceTier.ECO}  # USER override
    )
)
# Result: smart → ECO (USER Level 2 overrides ACCOUNT Level 4)

# USER 3: Parent (power user) wants exact model
user3 = UserProfile(
    account_id=account.account_id,
    config=UserBotConfig(
        model_overrides={"smart": "claude-opus-4-20250514"}
    )
)
# Result: smart → claude-opus-4-20250514 (USER Level 1 wins)
```

**Key Insight:** 99% of family members use ACCOUNT defaults. Only 1% override at USER level.

### 4.4 Debugging "Wrong Model" Issues

**Problem:** "Why is my smart agent using BALANCED instead of PERFORMANCE?"

**Solution:** Check resolution chain:

```python
# Step 1: Check USER model_overrides
override = user.config.get_model_override("smart")
if override:
    print(f"LEVEL 1 USER Override: {override}")  # Wins if exists

# Step 2: Check USER agent_tiers
user_tier = user.config.agent_tiers.get("smart")
if user_tier:
    print(f"LEVEL 2 USER Tier: {user_tier}")  # Wins if exists

# Step 3: Check ACCOUNT model_overrides (if family account)
if account.account_defaults:
    account_override = account.account_defaults.model_overrides.get("smart")
    if account_override:
        print(f"LEVEL 3 ACCOUNT Override: {account_override}")

# Step 4: Check ACCOUNT agent_tiers
if account.account_defaults:
    account_tier = account.account_defaults.agent_tiers.get("smart")
    if account_tier:
        print(f"LEVEL 4 ACCOUNT Tier: {account_tier}")  # ← Likely winner!

# Step 5: Domain defaults (factory)
print(f"LEVEL 5 DOMAIN Default: {UserBotConfig().agent_tiers['smart']}")
```

---

## 5. Advanced Configuration

### 4.1 Per-Agent Tier Overrides

Configure different tiers for each agent:

```python
config = UserBotConfig(
    default_tier=PerformanceTier.ECO,  # Global default
    agent_tiers={
        "smart": PerformanceTier.PERFORMANCE,     # Important agent
        "consolidation": PerformanceTier.PERFORMANCE,  # Required: BALANCED on Claude
                                                       # → Haiku 4.5, which rejects effort
        "quick": PerformanceTier.ECO,              # Cost optimization
    }
    # Other agents inherit default_tier (ECO)
)
```

### 4.2 Model Overrides (Power Users)

Bypass tier mapping with exact model strings:

```python
config = UserBotConfig(
    model_overrides={
        "smart": "claude-opus-4-20250514",  # Exact version
        "quick": "gemini-2.0-flash"         # Specific model
    }
)
```

**Use Cases:**

- **Model pinning:** Lock to specific version for consistency
- **A/B testing:** Compare model performance
- **Cost control:** Force cheaper model temporarily

**Caution:** Model overrides bypass tier logic. Ensure model exists on provider!

### 4.3 Complete Configuration Example

```python
config = UserBotConfig(
    # Provider selection
    provider_preference="claude",  # Prefer Claude

    # Tier defaults
    default_tier=PerformanceTier.ECO,

    # Per-agent tier overrides
    agent_tiers={
        "smart": PerformanceTier.PERFORMANCE,
        "consolidation": PerformanceTier.PERFORMANCE,  # Required on Claude:
                                                       # BALANCED → Haiku, no effort support
        "quick": PerformanceTier.ECO,
    },

    # Power user overrides
    model_overrides={
        "web_search": "gemini-2.0-flash"  # Force specific model
    },

    # Other settings
    temperature=0.7,
    tools_enabled=["search_memory", "ask_web_search_agent"]
)
```

**Resolution Result:**

- `smart` → claude + PERFORMANCE → `claude-sonnet-4-6`
- `consolidation` → claude + PERFORMANCE → `claude-sonnet-4-6`
- `quick` → claude + ECO → `claude-haiku-4-5-20251001`
- `web_search` → gemini (enforced) + override → `gemini-2.0-flash`
- `router` → gemini (strategy default) + ECO → `gemini-2.0-flash`

---

## 5. Cost Optimization

### 5.1 Cost Analysis by Tier

**Scenario:** 100 requests, 1000 tokens each

| Configuration   | Provider | Model                     | Cost   |
| --------------- | -------- | ------------------------- | ------ |
| All ECO         | Gemini   | gemini-2.0-flash          | $0.075 |
| All BALANCED    | Gemini   | gemini-2.0-flash-thinking | $0.075 |
| All PERFORMANCE | Claude   | claude-opus-4-20250514    | $15.00 |
| Smart only PERF | Mixed    | Smart=Opus, others=Flash  | $3.00  |

**Recommendation:** Use PERFORMANCE tier only for `smart` agent. Keep others at ECO/BALANCED.

### 5.2 Cost-Optimized Configuration

```python
# Budget-friendly setup
config = UserBotConfig(
    default_tier=PerformanceTier.ECO,  # Cheap by default
    provider_preference="gemini",      # Cheaper than Claude
    agent_tiers={
        "smart": PerformanceTier.BALANCED  # Still good quality
    }
)

# Estimated cost: ~$0.10 per 100 requests (vs $15 with all PERFORMANCE)
```

### 5.3 Quality-Optimized Configuration

```python
# Maximum quality setup
config = UserBotConfig(
    provider_preference="claude",  # Best reasoning
    agent_tiers={
        "smart": PerformanceTier.PERFORMANCE,
        "consolidation": PerformanceTier.PERFORMANCE,
        "quick": PerformanceTier.BALANCED,
    }
)

# Estimated cost: ~$10 per 100 requests (high quality)
```

### 5.4 Balanced Configuration (Recommended)

```python
# Best cost/quality ratio
config = UserBotConfig(
    default_tier=PerformanceTier.ECO,  # Cheap routing/classification
    provider_preference="claude",      # Quality when needed
    agent_tiers={
        "smart": PerformanceTier.PERFORMANCE,  # Deep analysis
        "quick": PerformanceTier.BALANCED,      # Good responses
    }
)

# Estimated cost: ~$3 per 100 requests (balanced)
```

---

## 6. Troubleshooting

### 6.1 Common Issues

| Issue                        | Cause                            | Solution                                    |
| ---------------------------- | -------------------------------- | ------------------------------------------- |
| Wrong model selected         | Agent strategy overrides default | Set `agent_tiers[agent_type]` explicitly    |
| Provider not available       | Invalid `provider_preference`    | Check allowed providers per agent           |
| Model override ignored       | Typo in agent_type key           | Verify agent_type matches exactly           |
| Unexpected costs             | All agents using PERFORMANCE     | Set `default_tier=ECO`, override smart only |
| Web search fails with Claude | web_search enforces Gemini       | Remove provider_preference for web_search   |

### 6.2 Debugging Configuration

**Check resolved context:**

```python
from src.services.agent_context_builder import AgentContextBuilder

builder = AgentContextBuilder(provider_registry)
context = builder.build("smart", user_profile.config)

print(f"Provider: {context.provider.__class__.__name__}")
print(f"Model: {context.model_name}")
print(f"Tier: {context.tier}")
print(f"Capabilities: {context.capabilities}")

# Output:
# Provider: ClaudeAdapter
# Model: claude-opus-4-20250514
# Tier: PerformanceTier.PERFORMANCE
# Capabilities: ProviderCapabilities(native_tools=True, ...)
```

**Verify tier resolution:**

```python
tier = user_profile.config.get_tier_for_agent("smart")
print(f"Smart agent tier: {tier}")

# Priority check:
# 1. agent_tiers["smart"] (if exists)
# 2. default_tier
# 3. Strategy default (PERFORMANCE for smart)
```

**Check model override:**

```python
override = user_profile.config.get_model_override("smart")
print(f"Model override: {override or 'None'}")

# If override exists, it bypasses tier mapping
```

### 6.3 Testing Configuration

```bash
# Test tier resolution
pytest tests/unit/services/test_agent_context_builder.py -v

# Test specific agent
pytest tests/unit/services/test_agent_context_builder.py::test_smart_agent_tier -v

# Integration test
pytest tests/integration/test_provider_resolution.py -v
```

### 6.4 Logs & Monitoring

**Enable debug logging:**

```python
import logging
logging.getLogger("src.services.agent_context_builder").setLevel(logging.DEBUG)
```

**Check logs for resolution:**

```
DEBUG: Resolving context for agent=smart
DEBUG: Strategy default: provider=claude, tier=PERFORMANCE
DEBUG: User preference: provider=None
DEBUG: Resolved provider: claude
DEBUG: Tier from agent_tiers: PERFORMANCE
DEBUG: Model from tier mapping: claude-opus-4-20250514
DEBUG: Context built: model=claude-opus-4-20250514, tier=PERFORMANCE
```

---

## 7. Code Reference

### 7.1 Domain Models

**PerformanceTier:**

```python
# src/domain/user.py
class PerformanceTier(str, Enum):
    ECO = "eco"
    BALANCED = "balanced"
    PERFORMANCE = "performance"
```

**UserBotConfig:**

```python
# src/domain/user.py
class UserBotConfig(BaseModel):
    provider_preference: Optional[str] = None
    default_tier: PerformanceTier = PerformanceTier.ECO
    agent_tiers: Dict[str, PerformanceTier] = {...}
    model_overrides: Dict[str, str] = {}

    def get_tier_for_agent(self, agent_type: str) -> PerformanceTier:
        return self.agent_tiers.get(agent_type, self.default_tier)

    def get_model_override(self, agent_type: str) -> Optional[str]:
        return self.model_overrides.get(agent_type)
```

### 7.2 Services

**AgentContextBuilder:**

```python
# src/services/agent_context_builder.py
class AgentContextBuilder:
    def build(
        self,
        agent_type: str,
        config: UserBotConfig
    ) -> AgentExecutionContext:
        # Resolution logic
        ...
```

**ProviderRegistry:**

```python
# src/services/provider_registry.py
class ProviderRegistry:
    def get_provider(self, name: str) -> LLMPort:
        return self._providers[name]
```

### 7.3 Adapters

**GeminiAdapter:**

```python
# src/adapters/gemini_adapter.py
class GeminiAdapter(LLMPort):
    MODEL_TIERS = {
        PerformanceTier.ECO: "gemini-flash-lite-latest",
        PerformanceTier.BALANCED: "gemini-3-flash-preview",
        PerformanceTier.PERFORMANCE: "gemini-3-pro-preview"
    }

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        return self.MODEL_TIERS[tier]
```

**ClaudeAdapter:**

```python
# src/adapters/llm/claude_adapter.py
class ClaudeAdapter(LLMPort):
    MODEL_TIERS = {
        PerformanceTier.ECO: "claude-3-haiku-20240307",
        PerformanceTier.BALANCED: "claude-sonnet-4-5-20250929",
        PerformanceTier.PERFORMANCE: "claude-opus-4-20250514"
    }

    def get_model_for_tier(self, tier: PerformanceTier) -> str:
        return self.MODEL_TIERS[tier]
```

### 7.4 Tests

```bash
# Unit tests
tests/unit/domain/test_user.py                      # UserBotConfig tests
tests/unit/services/test_agent_context_builder.py   # Resolution logic tests
tests/unit/adapters/test_gemini_adapter.py          # Gemini tier mapping tests
tests/unit/adapters/test_claude_adapter.py          # Claude tier mapping tests

# Integration tests
tests/integration/test_provider_resolution.py       # E2E resolution tests
```

---

## Related Documentation

**Architecture:**

- [Provider Resolution Building Block](../05_building_blocks/provider_resolution/README.md) — Architecture overview
- [Multi-Agent System](../05_building_blocks/multi_agent_system/README.md) — Agent architecture

**Guides:**

- Operations — See docs_local/guides/OPERATIONS.md (local only)
- Installation — See docs_local/guides/INSTALLATION.md (local only)

**Code:**

- `src/domain/user.py` — PerformanceTier, UserBotConfig
- `src/services/agent_context_builder.py` — Resolution logic
- `src/services/provider_registry.py` — Provider management
- `src/adapters/llm/gemini_adapter.py` — Gemini implementation
- `src/adapters/llm/claude_adapter.py` — Claude implementation

---

**Last Updated:** 2026-02-12  
**Status:** ✅ Production Ready (Per-Agent Provider Selection)
