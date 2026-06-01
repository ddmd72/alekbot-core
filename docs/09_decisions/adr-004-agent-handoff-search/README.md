# ADR-004: Agent Handoff Pattern for Web Search

## Status



---

## Context

Grounded web search is a provider-native tool (Gemini: Google Search; OpenAI: `web_search`; Claude: `web_search` +

custom function declarations (`400 INVALID_ARGUMENT`). The orchestrator needs its custom tools (`delegate_to_specialist`,


























---

## Decision

Web search is a **specialist agent** the orchestrator delegates to, not a tool on the orchestrator's own call.

### Rationale












---

## Implementation

### Architecture

```

    
    
    


    
    
    
    
```














## Rejected alternatives



- **Google Custom Search Engine as a custom tool** — snippets too thin, poor relevance without tuning, no synthesis.

```
- **Grounding on the orchestrator's call** — blocked by the provider constraint above.





```

---

## Consequences

### Positive

- Smart can fan out a search and a memory lookup in one tool batch.
- The same pattern carries `WebSearchLightAgent`, Lelik's one-call grounded lookup.



### Negative





### Neutral




---










---




















---

## References





  
  

---

## Revisit when

Every configured orchestrator provider accepts native search alongside function declarations **and** the extra
|------|--------|--------|



