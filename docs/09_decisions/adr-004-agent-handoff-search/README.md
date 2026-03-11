# ADR-004: Agent Handoff Pattern for Web Search

## 📖 HowTo: Using This Document

### Purpose
Documents the architectural decision to implement web search via Agent Handoff pattern instead of direct tool integration.

### When to Read
- **For AI Agents:** When modifying web search behavior or adding new search sources.
- **For Developers:** When understanding why WebSearchAgent is a separate agent, not a tool.

### When to Update
This document MUST be updated when:
- [ ] The Agent Handoff pattern changes fundamentally.
- [ ] WebSearchAgent is replaced with a different architecture.
- [ ] New search agents are added using this pattern.

### Cross-References
- **Original RFC:** [../../archive/rfcs/SEARCH_STRATEGY_RFC.md](../../archive/rfcs/SEARCH_STRATEGY_RFC.md)
- **Implementation:** `../../../src/agents/web_search_agent.py`
- **Multi-Agent System:** [../../05_building_blocks/multi_agent_system/README.md](../../05_building_blocks/multi_agent_system/README.md)

---

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



