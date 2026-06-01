make

  




---

## Table of Contents









---







```python
from src.domain.user import UserBotConfig, PerformanceTier


config = UserBotConfig(
    default_tier=PerformanceTier.ECO
)


# router → gemini-flash-lite-latest (ECO)
# quick → gemini-3-flash-preview (BALANCED - agent default)
# smart → claude-opus-4-20250514 (PERFORMANCE - agent default)
```





```python
config = UserBotConfig(
    
    agent_tiers={
        
    }
)

# Result:
# smart → claude-opus-4-20250514 (best reasoning)
# quick → gemini-3-flash-preview (balanced)
# router → gemini-flash-lite-latest (fast)
```



```python
config = UserBotConfig(
    provider_preference="gemini"
)


# smart → gemini-3-pro-preview (PERFORMANCE tier)
# quick → gemini-3-flash-preview (BALANCED tier)
# router → gemini-flash-lite-latest (ECO tier)
```

---






| --------------- | ------ | ------ | ---------- | --------------------------------------- |






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





```python

    
    "quick": PerformanceTier.BALANCED,    # Quick responses
    
    "consolidation": PerformanceTier.PERFORMANCE,
    "web_search": PerformanceTier.BALANCED,
    
}
```



---

















```python
config = UserBotConfig(
    
)
```





```python
config = UserBotConfig(
    provider_preference="gemini",  # Default for most agents
    agent_providers={
        
        
    }
)
```



```python

config = UserBotConfig(
    
    agent_providers={
        
        
    },
    agent_tiers={
        "router": PerformanceTier.ECO,
        "smart": PerformanceTier.PERFORMANCE
    }
)

# Result:




```











| ------------- | ---------------- | ---------------------- | ------------------------- |




















```python

# 1. Primary: claude-opus-4-20250514 (PERFORMANCE)
# 2. Fallback 1: claude-sonnet-4-5-20250929 (BALANCED)
# 3. Fallback 2: gemini-3-pro-preview (alternative provider)
```

---







```
┌───────────────────────────────────────────────────────────┐


│ Example: {"smart": "claude-opus-4-20250514"}              │

└───────────────────────────────────────────────────────────┘
                          ↓
┌───────────────────────────────────────────────────────────┐




└───────────────────────────────────────────────────────────┘
                          ↓
┌───────────────────────────────────────────────────────────┐


│ Example: {"smart": "claude-sonnet-4-5-20250929"}          │

└───────────────────────────────────────────────────────────┘
                          ↓
┌───────────────────────────────────────────────────────────┐




└───────────────────────────────────────────────────────────┘
                          ↓
┌───────────────────────────────────────────────────────────┐




└───────────────────────────────────────────────────────────┘
```




| -------------------- | ------------------------------------------------- | --------------- | --------------------------- |










```python

account = BillingAccount(
    account_defaults=UserBotConfig(
        agent_tiers={
            
        }
    )
)



    account_id=account.account_id,
    
)




    account_id=account.account_id,
    config=UserBotConfig(
        
    )
)




    account_id=account.account_id,
    config=UserBotConfig(
        model_overrides={"smart": "claude-opus-4-20250514"}
    )
)
# Result: smart → claude-opus-4-20250514 (USER Level 1 wins)
```









```python


if override:
    




    



    
    
        



    
    
        



```

---







```python
config = UserBotConfig(
    
    agent_tiers={
        
        
                                                       
        
    }
    
)
```





```python
config = UserBotConfig(
    model_overrides={
        "smart": "claude-opus-4-20250514",  # Exact version
        "quick": "gemini-2.0-flash"         # Specific model
    }
)
```











```python
config = UserBotConfig(
    
    

    
    

    
    agent_tiers={
        "smart": PerformanceTier.PERFORMANCE,
        
                                                       
        "quick": PerformanceTier.ECO,
    },

    
    model_overrides={
        "web_search": "gemini-2.0-flash"  # Force specific model
    },

    
    temperature=0.7,
    
)
```



- `smart` → claude + PERFORMANCE → `claude-sonnet-4-6`
- `consolidation` → claude + PERFORMANCE → `claude-sonnet-4-6`

- `web_search` → gemini (enforced) + override → `gemini-2.0-flash`
- `router` → gemini (strategy default) + ECO → `gemini-2.0-flash`

---








| --------------- | -------- | ------------------------- | ------ |
| All ECO         | Gemini   | gemini-2.0-flash          | $0.075 |
| All BALANCED    | Gemini   | gemini-2.0-flash-thinking | $0.075 |
| All PERFORMANCE | Claude   | claude-opus-4-20250514    | $15.00 |
| Smart only PERF | Mixed    | Smart=Opus, others=Flash  | $3.00  |





```python

config = UserBotConfig(
      # Cheap by default
    
    agent_tiers={
        
    }
)

# Estimated cost: ~$0.10 per 100 requests (vs $15 with all PERFORMANCE)
```



```python

config = UserBotConfig(
    
    agent_tiers={
        "smart": PerformanceTier.PERFORMANCE,
        "consolidation": PerformanceTier.PERFORMANCE,
        "quick": PerformanceTier.BALANCED,
    }
)

# Estimated cost: ~$10 per 100 requests (high quality)
```



```python

config = UserBotConfig(
      # Cheap routing/classification
    
    agent_tiers={
        
        
    }
)

# Estimated cost: ~$3 per 100 requests (balanced)
```

---

## 6. Troubleshooting




| ---------------------------- | -------------------------------- | ------------------------------------------- |










```python
from src.services.agent_context_builder import AgentContextBuilder









# Output:

# Model: claude-opus-4-20250514


```



```python







```



```python




```



```bash






# Integration test

```





```python
import logging

```



```





DEBUG: Model from tier mapping: claude-opus-4-20250514
DEBUG: Context built: model=claude-opus-4-20250514, tier=PERFORMANCE
```

---

## 7. Code Reference





```python
# src/domain/user.py
class PerformanceTier(str, Enum):
    ECO = "eco"
    BALANCED = "balanced"
    PERFORMANCE = "performance"
```



```python
# src/domain/user.py
class UserBotConfig(BaseModel):
    provider_preference: Optional[str] = None
    default_tier: PerformanceTier = PerformanceTier.ECO
    
    

    def get_tier_for_agent(self, agent_type: str) -> PerformanceTier:
        

    def get_model_override(self, agent_type: str) -> Optional[str]:
        return self.model_overrides.get(agent_type)
```





```python

class AgentContextBuilder:
    def build(
        self,
        agent_type: str,
        config: UserBotConfig
    ) -> AgentExecutionContext:
        
        ...
```



```python


    
        return self._providers[name]
```





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



```bash
# Unit tests

tests/unit/services/test_agent_context_builder.py    tests



# Integration tests

```

---

## Related Documentation

**Architecture:**




**Guides:**




**Code:**







---

  

