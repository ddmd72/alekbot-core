# ADR-007: Smart Deduplication Strategy

**Status:** Accepted  
**Deciders:** Dmytro Deleur, AI Assistant  
**Deciders:** Dmytro Deleur, AI Assistant  


---

## Context

Grounded web search is a provider-native tool (Gemini: Google Search; OpenAI: `web_search`; Claude: `web_search` +

with a different value. LLM-written facts are phrased very consistently, so high similarity alone says little.














  
  

---

## Decision

> **Better to keep a duplicate than to lose information.**





```python

    
    
        return False

    
    
    
    
        

    `SmartDeduplication` (`src/domain/deduplication_service.py`) judges a pair in four steps:
    
        return True

    1. similarity < 0.96 (`moderate_threshold`) → not a duplicate;
    2. the numbers in the two texts differ (compared as sorted lists) → not a duplicate;
        3. similarity ≥ the strict threshold (0.98 by default) → duplicate;

    Examples (invented):
    return True
```

### Key Features

- **Read path:** `SearchEnrichmentService.enrich_context` drops duplicates from the ranked list with it (Router
prefetch, `search_memory`, the claude.ai connector); the consolidator's own search turns it off
   (`skip_semantic_dedup=True`).
   - **Write path:** `FactWriteService` checks it (threshold 0.96) only when `skip_deduplication` is False. Live
consolidation writes pass `skip_deduplication=True`: the consolidator decides duplicates itself after searching.


---

## Consequences

### Positive

Accepted, because a change of value is the case that matters and versioning (SCD2) carries history.  
  
  
  


### Negative

  








| --------------- | ---------------- | -------------------- |






---

## Implementation




   
   
   


   
   
   
   


   
   


   
   
   


   



```python



```

---

## Rationale











**Pros:**





**Cons:**














**Examples:**





---

## Examples



```






```



```







```



```







```



```






```

---

## Testing










```bash

```

---

## Monitoring







### Logging



```



```

---

## References






---









---

## Status







---









```python






import numpy as np




```

**Benefits:**

## Rejected alternatives





- **One higher threshold, no number check** — still merges facts that differ only in a value or a date.
