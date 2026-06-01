# ADR-005: Router-Centric Enrichment Pattern

## Status



---

## Context

Grounded web search is a provider-native tool (Gemini: Google Search; OpenAI: `web_search`; Claude: `web_search` +

injected a second time.













---

## Decision

Memory prefetch happens **once per message, in the Router**. The Router's triage returns the search inputs

`RouterAgent` calls `SearchEnrichmentService.enrich_context` with the user's biographical baseline, so facts already

```
RouterAgent
  
  
  
  
  
  
  
  
  
  
  
```

### Rationale







---

## Implementation



Mechanics (channels, limits, RRF, deduplication): `docs/05_building_blocks/memory/README.md` §4.3–§4.4.

**Responsibilities:**






- **Agent-owned search** — the original design; duplicate queries per turn and inconsistent context.
- **Search only on demand** (`search_memory` alone) — every personal question would cost a tool round-trip before
the first useful answer.



```python


    keywords=keywords,
    
    
    user_id=user_id
)






```






















---

## Consequences

### Positive

- The Router depends on `SearchEnrichmentService` (an application service behind ports), and the prefetch quality is
bounded by the triage's keywords and phrase.
- The same service backs `search_memory`, the claude.ai connector and the companion context, so all of them rank the
same way.


### Negative





### Neutral




---












---



### Hexagonal Architecture










---




















---

## References




- - **Agent-owned search** — the original design; duplicate queries per turn and inconsistent context. `src/domain/search.py`


---

## Revisit when

Every configured orchestrator provider accepts native search alongside function declarations **and** the extra
|------|--------|--------|




