# Hybrid Router (Building Block)

## Purpose

Describes the intent classification and routing system that analyzes every user message,
produces semantic routing metadata, enriches context, and routes to SmartResponseAgent.

## When to Read

- Before modifying routing rules, triage prompts, or intent classification logic.
- When troubleshooting incorrect complexity classification or model selection.
- When changing `RoutingMetadata` fields or `build_routing_metadata()`.

## When to Update

This document MUST be updated when:

- [ ] The triage logic (rule-based vs LLM-based) changes.
- [ ] `TaskComplexity` values or their semantic definitions change.
- [ ] `RoutingMetadata` fields are added, removed, or renamed.
- [ ] `build_routing_metadata()` coercion / safety-net logic changes.
- [ ] Integration with `SearchEnrichmentService` or `AgentNotePort` changes.
- [ ] Routing target (currently: always SmartResponseAgent) changes.

## Cross-References

- **Dynamic Execution:** [../smart_agent_execution/README.md](../smart_agent_execution/README.md)
- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md)
- **Search Enrichment:** [../search_enrichment/README.md](../search_enrichment/README.md)
- **Prompt Design System v3:** [../prompt_design_system_v3/README.md](../prompt_design_system_v3/README.md)

---

## 1. Overview

The **Hybrid Router** is the entry point for all user messages. It performs two jobs:

1. **Classify**: LLM triage assigns a `TaskComplexity` (semantic category) and extracts
   context enrichment signals (`search_intent`, `user_tone`). Triage no longer emits search fields; the memory
   prefetch reads its own query slots (§4).
2. **Enrich**: Fetches memory context and active reminders, packages everything into
   `message.context`, and forwards to `SmartResponseAgent`.

**Routing target:** Always `SmartResponseAgent`. Quick routing was deprecated; Quick remains
in code but is no longer reached via the Router.

**Core principle:** Router classifies *what kind of task* this is; execution infrastructure
maps that to *how to run it* (model, tier, thinking) — see
[Smart Agent Execution](../smart_agent_execution/README.md).

---

## 2. Triage Mechanism

### 2.1 Rule-Based Classification (Fast Path)

For common, unambiguous phrases, static keyword matching avoids LLM latency.

- Simple phrases: greetings ("Hello"), acknowledgments ("Thanks"), confirmations.
- Personal keywords: "my", "mine" → flag personal data query.
- External keywords: "weather", "news" → flag web search need.

### 2.2 LLM-Based Triage (Semantic Path)

For everything else, a lightweight LLM (Gemini Flash Lite, ECO tier) performs deep
classification.

- **Prompt:** assembled via `PromptBuilderPort` (token `COGNITIVE_PROCESS_ROUTER`).
- **Output:** structured JSON via `response_schema` (provider-native enforcement).
- **Vision override:** native binary images in the message → force `task_complexity=deep_reasoning`.

#### LLM Triage Response Schema

```json
{
  "needs_memory_search": bool,
  "search_intent": "string — whether memory is searched this turn (none | topic | ...)",
  "metadata": {
    "user_tone": "friendly | professional | urgent | ...",
    "task_complexity": "small_talk | info_search | simple_analytics | deep_reasoning"
  }
}
```

---

## 3. Routing Metadata

### 3.1 RoutingMetadata Fields

Produced by `build_routing_metadata(classification)` in `src/domain/tone.py`:

| Field               | Type                | Description                                         |
|---------------------|---------------------|-----------------------------------------------------|
| `task_complexity`   | `TaskComplexity`    | Semantic category of the task (see §3.2)            |
| `user_tone`         | `str`               | Detected user tone (`friendly`, `professional`, ...) |
| `needs_tools`       | `List[str]`         | Capabilities needed (`search_web`, `search_memory`) |
| `needs_memory_search` | `bool`            | Whether in-request KB retrieval is needed           |
| `reasoning`         | `str`               | LLM explanation of its classification decision      |

**Removed fields (compared to prior design):**
- `complexity_score` (1–10 numeric) — replaced by `task_complexity` enum
- `confidence` (0.0–1.0) — removed; unknown complexity falls back to `simple_analytics`

### 3.2 TaskComplexity Values

| Value               | Semantics                                             | Default Tier    |
|---------------------|-------------------------------------------------------|-----------------|
| `small_talk`        | Greetings, acknowledgments, yes/no, short acks        | ECO             |
| `info_search`       | Factual lookups, quick retrieval, single-answer Q&A   | BALANCED        |
| `simple_analytics`  | Basic analysis, calculations, comparisons             | BALANCED + low thinking |
| `deep_reasoning`    | Multi-step reasoning, synthesis, planning             | PERFORMANCE + high thinking |

**Safety net**: unknown or empty `task_complexity` in LLM output → coerced to `simple_analytics`
by `_coerce_task_complexity()` in `tone.py`.

### 3.3 Context Propagation

`RoutingMetadata` is serialized into `message.context` when forwarding to SmartResponseAgent:

```python
context = {
    **message.context,
    "classification":  classification,             # Raw LLM triage dict
    "routing":         routing_metadata.to_dict(), # Serialized RoutingMetadata
    "task_complexity": routing_metadata.task_complexity.value,  # String for resolver
    "enriched_context": ...,                       # FactSearchService result
    "agent_notes":     ...,                        # Active self-reminders
    "routed_by":       self.agent_id,
}
```

`task_complexity` is passed as a **string value** (not enum) so it survives serialization
through Cloud Tasks queues (used for async paths).

---

## 4. Search Enrichment Integration

The router doesn't just route — it prepares the full context for SmartResponseAgent.

1. **Slots**: in parallel with triage, the slots agent reads the turn's messages into a `FactQuery`
   (`FACT_PROFILE_RFC` §5.1). Triage only decides, through `search_intent`, whether the result is used.
2. **Memory enrichment**: `FactSearchService` runs the scored fact search on those slots
   (`FACT_PROFILE_RFC` §5.3; `FactQuery.message` is the user's latest message). Result is attached as `enriched_context`.
3. **Reminders enrichment**: `AgentNotePort.list_active_notes(user_id)` fetches active
   self-reminders → serialized as `agent_notes: List[dict]` in context.
   Failures are caught and logged — enrichment never blocks routing.
4. **Context injection**: Enriched context is available to SmartResponseAgent's
   `PromptBuilder` (injected as `active_reminders {}` and `query_specific_context {}`
   blocks after `PROMPT_CACHE_BOUNDARY`).

---

## 5. Code Locations

| Concern                         | File                                    |
|---------------------------------|-----------------------------------------|
| Main router implementation      | `src/agents/core/router_agent.py`       |
| `RoutingMetadata` + coercion    | `src/domain/tone.py`                   |
| `TaskComplexity` enum           | `src/domain/task_complexity.py`        |
| Prompt assembly                 | `src/ports/prompt_builder_port.py`      |
| Context enrichment              | `src/services/fact_search_service.py`   |
| Downstream resolution           | `src/services/task_execution_resolver.py` |

---

## 6. Status

**Status:** ✅ Production Ready

**Last Updated:** 2026-04-22
