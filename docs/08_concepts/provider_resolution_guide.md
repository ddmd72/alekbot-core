

  




---

## Table of Contents









---







```python
from src.domain.user import UserBotConfig, PerformanceTier


config = UserBotConfig(
    default_tier=PerformanceTier.ECO
)





```





```python
config = UserBotConfig(
    
    agent_tiers={
        
    }
)

# Result:



```



```python
config = UserBotConfig(
    provider_preference="gemini"
)





```

---






| --------------- | ------ | ------ | ---------- | --------------------------------------- |










| ----------- | ------------------------- | ----------- | ---------------------------- | -------- |














```python

    
    
    
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




```

---







```
┌───────────────────────────────────────────────────────────┐




└───────────────────────────────────────────────────────────┘
                          ↓
┌───────────────────────────────────────────────────────────┐




└───────────────────────────────────────────────────────────┘
                          ↓
┌───────────────────────────────────────────────────────────┐




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
        
    )
)

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
        
    },

    
    temperature=0.7,
    
)
```









---








| --------------- | -------- | ------------------------- | ------ |









```python

config = UserBotConfig(
      # Cheap by default
    
    agent_tiers={
        
    }
)


```



```python

config = UserBotConfig(
    
    agent_tiers={
        "smart": PerformanceTier.PERFORMANCE,
        "consolidation": PerformanceTier.PERFORMANCE,
        "quick": PerformanceTier.BALANCED,
    }
)


```



```python

config = UserBotConfig(
      # Cheap routing/classification
    
    agent_tiers={
        
        
    }
)


```

---

## 6. Troubleshooting




| ---------------------------- | -------------------------------- | ------------------------------------------- |










```python
from src.services.agent_context_builder import AgentContextBuilder









# Output:




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

  

