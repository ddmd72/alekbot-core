

  




---








| ----------------------- | -------------------------------- | ------------------------------------------------------ | ------------------------ |















---







```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

class RiskLevel(Enum):
    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

class TrustZone(Enum):
    
    SEMI_TRUSTED = "semi_trusted"  # RAG content, enriched search
    

@dataclass
class ValidationResult:
    
    
    
    patterns_detected: list[str]  # Matched patterns (for logging)
    
    

class SecurityPort(ABC):
    

    @abstractmethod
    async def validate(
        self,
        text: str,
        context: str,
        zone: TrustZone = TrustZone.UNTRUSTED
    ) -> ValidationResult:
        
        pass
```








---








- Conversation history









**Example:**

```python
result = await security_port.validate(
    
    context=f"biographical_user_{user_id}",
    zone=TrustZone.UNTRUSTED
)
```















**Example:**

```python
result = await security_port.validate(
    
    context=f"rag_fact_{fact.id}",
    zone=TrustZone.SEMI_TRUSTED
)
```














**Example:**

```python
result = await security_port.validate(
    
    context="token_creation",
    zone=TrustZone.TRUSTED
)

```



---



### 4.1 RegexSecurityAdapter (Production)









```python




```



```python





```



```python




```



```python
risk_mapping = {
    RiskLevel.SAFE: 0.0,
    RiskLevel.LOW: 0.2,
    RiskLevel.MEDIUM: 0.5,
    RiskLevel.HIGH: 0.8,
    RiskLevel.CRITICAL: 1.0
}
risk_score = risk_mapping.get(highest_risk, 0.0)
```



```python
if highest_risk in [RiskLevel.HIGH, RiskLevel.CRITICAL]:
    action = "blocked"
    

elif highest_risk == RiskLevel.MEDIUM:
    action = "sanitized"
    # Remove detected patterns
    for pattern, _, _ in self.PATTERNS:
        sanitized = re.sub(pattern, "[REDACTED]", sanitized, flags=re.IGNORECASE)


    action = "passed"
    
```



```python
from src.adapters.security.regex_adapter import RegexSecurityAdapter

adapter = RegexSecurityAdapter()


result = await adapter.validate(
    
    context="test",
    zone=TrustZone.UNTRUSTED
)

```

### 4.2 CompositeAdapter (Production)









```python

worst = max(results, key=lambda r: r.risk_score)
return worst
```



**majority_vote:**

```python


for r in results:
    risk_counts[r.risk_level] = risk_counts.get(r.risk_level, 0) + 1


```



**all_pass:**

```python

if all(r.risk_level == RiskLevel.SAFE for r in results):
    
else:
    
```





```python
from src.adapters.security.regex_adapter import RegexSecurityAdapter
from src.adapters.security.composite_adapter import CompositeAdapter


composite = CompositeAdapter(
    
    strategy="worst_case"
)

result = await composite.validate(
    text="Hello world",
    context="test",
    zone=TrustZone.UNTRUSTED
)

```

### 4.3 LLMSecurityAdapter (Placeholder)







```python
class LLMSecurityAdapter(SecurityPort):
    

    def __init__(self):
        

    
        
        
        
```





```python

    





{text}


{
  
  
  
  
}"""

    
        if zone == TrustZone.TRUSTED:
            

        
        
            
            
            
        )
        
```

  


### 4.4 ExternalAPIAdapter (Placeholder)





```python
class ExternalAPIAdapter(SecurityPort):
    

    
        self.api_url = api_url
        self.api_key = api_key
        

    
        
        
        
        

        
```

---

## 5. Integration Points







```python

token = await Token.create(
    
    category=TokenCategory("humor_engine"),
    class_=TokenClass("properties"),
    
    metadata={},
    
)



```













```python

    
    bio_result = await self.security_port.validate(
        text="\n".join(biographical_facts),
        context=f"biographical_user_{user_id}",
        zone=TrustZone.UNTRUSTED
    )
    validated_bio = bio_result.sanitized_text

    
    formatted_convo = self.formatter.format(conversation_history)
    convo_result = await self.security_port.validate(
        
        context=f"conversation_user_{user_id}",
        zone=TrustZone.UNTRUSTED
    )
    validated_convo = convo_result.sanitized_text

    
    
    
```














```python

    

    result = await self.security_port.validate(
        
        context=f"model_output_user_{user_id}",
        
    )

    if result.risk_level == RiskLevel.CRITICAL:
        
        return "[SYSTEM: Response contained unsafe content and was blocked]"

    elif result.risk_level == RiskLevel.HIGH:
        
        return result.sanitized_text

    else:
        
```

  











```python

    

    
    

    
    if self.security_port:
        validated_facts = []
        for fact in facts:
            result = await self.security_port.validate(
                fact.text,
                context=f"rag_fact_{fact.id}",
                
            )
            if result.risk_level not in [RiskLevel.HIGH, RiskLevel.CRITICAL]:
                validated_facts.append(result.sanitized_text)
        return validated_facts

    
    return [f.text for f in facts]
```




 and include


---







```python


@pytest.mark.asyncio

    adapter = RegexSecurityAdapter()

    with pytest.raises(ValueError, match="Security validation failed"):
        await adapter.validate(
            
            context="test",
            zone=TrustZone.UNTRUSTED
        )

@pytest.mark.asyncio

    adapter = RegexSecurityAdapter()

    result = await adapter.validate(
        
        context="test",
        zone=TrustZone.UNTRUSTED
    )

    assert result.risk_level == RiskLevel.MEDIUM
    assert "[REDACTED]" in result.sanitized_text
```





```python


@pytest.mark.asyncio

    

    
        
        
    ]

    with pytest.raises(ValueError, match="Security validation failed"):
        
            agent_type="smart",
            user_id="test_user",
            account_id=None,
            
            conversation_history=[]
        )
```

---





```python
# ✅ CORRECT





```



```python



    logger.warning(
        
        extra={
            "context": context,
            
            
        }
    )
```



```python








```



```python
try:
    
    if result.risk_level == RiskLevel.CRITICAL:
        
    
except Exception as e:
    
    
    
```

---



















**Caching:**

```python
from functools import lru_cache



    
```



```python

    
    
```



```python





    
    
else:
    
```

---









**Solution:**



```python

```










**Solution:**



```python
import unicodedata


    
    
    
    return text
```















---

## 10. Status



**Implemented:**


















  

