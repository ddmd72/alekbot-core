# RFC: Unified Knowledge Search

**Status:** DRAFT  
**Date:** 2026-04-08  
**Replaces:** Implicit architecture (separate FactsMemoryAgent + EmailSearchAgent for search)  
**Related:** `GMAIL_EMAIL_INDEXING_RFC.md`, `DELIBERATE_FACT_MANAGEMENT_RFC.md`, `ACP_V2_SIMPLIFIED_RFC.md`

---

## 1. Problem

The orchestrator (Quick/Smart) currently sees five search-related intents:

| Intent | Agent | What it does |
|--------|-------|-------------|
| `search_memory` | FactsMemoryAgent | Vector search across personal facts |
| `search_emails` | EmailSearchAgent | Vector search across indexed emails |
| `get_email_details` | EmailSearchAgent | Fetch full email body by ID |
| `get_email_attachment` | EmailSearchAgent | Download + convert attachment |
| `save_to_memory` | FactsMemoryAgent | Attach fact for consolidation |

Three issues with this:

**1. The orchestrator must guess WHERE the answer lives.** "What did my doctor say about test results?" — is that a fact? An email? Both? The LLM picks a source before knowing the answer, which is a routing decision about data, not user intent.

**2. Implementation details leak into the orchestrator.** `email_id` and `filename` are infrastructure artifacts. The orchestrator juggles `context_schemas` it shouldn't know about, and LLMs fill all context fields regardless of intent (the `text`-in-payload bug fixed 2026-04-08).

**3. Adding a new data source requires manifest surgery.** A future file search or calendar search means new intents, new capability_descriptions, new context_schemas — all visible to the orchestrator's tool declaration, increasing prompt size and decision complexity.

---

## 2. Design Goal

**The orchestrator should decide search DEPTH, not search SOURCE.**

Two intents replace five:

| New intent | Tier | When to use |
|------------|------|-------------|
| `fast_search` | ECO | Simple factual recall: "my blood type", "where do I work" |
| `extended_search` | BALANCED | Multi-source deep dive: "find what the doctor wrote about my results", "invoice from March" |

`save_to_memory` remains unchanged — it is not search.

`get_email_details` and `get_email_attachment` become **internal tools** of the `extended_search` agent. The orchestrator never sees email IDs.

**Mental model shift:** the orchestrator commissions a search by complexity, like it commissions Quick vs Smart by complexity. The search agent decides where to look and how deep to go.

---

## 3. Architecture

### Current flow

```
Orchestrator (Quick/Smart)
    ├── delegate(search_memory, query)        → FactsMemoryAgent (ECO)
    ├── delegate(search_emails, query)        → EmailSearchAgent (BALANCED)
    ├── delegate(get_email_details, email_id) → EmailSearchAgent (BALANCED)
    └── delegate(get_email_attachment, ...)   → EmailSearchAgent (BALANCED)
```

Orchestrator decides source. Multi-step email flow requires 2-3 sequential delegations.

### Proposed flow

```
Orchestrator (Quick/Smart)
    ├── delegate(fast_search, query)      → FastSearchAgent (ECO, single-pass)
    └── delegate(extended_search, query)  → ExtendedSearchAgent (BALANCED, multi-turn)
                                               ├── tool: search_facts(query)
                                               ├── tool: search_emails(query, date_from?, date_to?)
                                               ├── tool: get_email_details(email_id)
                                               ├── tool: get_email_attachment(email_id, filename)
                                               └── [future] tool: search_files(query)
```

Orchestrator decides depth. Extended agent handles multi-step internally.

### Enrichment unchanged

Router enrichment (`SearchEnrichmentService`) continues to pre-fetch biographical context
before routing. It provides the baseline; explicit search delegation provides depth.

---

## 4. FastSearchAgent

**Tier:** ECO (same as current FactsMemoryAgent)  
**Intent:** `fast_search`  
**Execution mode:** SYNC  
**LLM calls:** 1 (key formulation)

### Behaviour

1. Receive raw query from orchestrator
2. LLM key formulation → `{keywords, primary_query, alternative_query, domains}` (existing logic)
3. Parallel search:
   - `SearchEnrichmentService.enrich_context()` — facts RRF (existing, 6-7 vector queries)
   - `EmailSearchService.search()` — email RRF (existing, 7 vector queries)
4. Merge results: facts + email snippets (no full bodies, no attachments)
5. Return combined result string to orchestrator

### What changes vs current FactsMemoryAgent

| Aspect | Current | Proposed |
|--------|---------|----------|
| Intent name | `search_memory` | `fast_search` |
| Data sources | Facts only | Facts + email snippets |
| Email depth | N/A | Snippet-level (subject, from, date, first 200 chars) |
| LLM calls | 1 (key formulation) | 1 (same) |
| Cost | ~1.7K tokens | ~1.7K tokens (same LLM call; email search is vector-only) |

The key formulation LLM call already produces keywords and phrases suitable for both
fact and email vector search. No additional LLM call needed — reuse the same embeddings.

### Output format

```
=== FACTS ===
[fact text]
context: [...]
reported: 2026-03-15
---
[fact text]
...

=== EMAILS ===
[from: sender@example.com | date: 2026-03-20 | subject: Blood test results]
snippet: First 200 chars of email body...
---
[from: ...]
...
```

The orchestrator sees a unified result. No email IDs exposed.

---

## 5. ExtendedSearchAgent

**Tier:** BALANCED  
**Intent:** `extended_search`  
**Execution mode:** SYNC  
**LLM calls:** 2-5 (tool-calling loop)

### Behaviour

Multi-turn agent with internal tools. The LLM decides search strategy based on query context.

```
Turn 1: LLM receives query → calls search_facts + search_emails (parallel)
Turn 2: LLM reviews results → optionally calls get_email_details for relevant emails
Turn 3: LLM reviews details → optionally calls get_email_attachment
Turn N: LLM produces final synthesized answer
```

### Internal tools

| Tool | Source | Description |
|------|--------|-------------|
| `search_facts` | SearchEnrichmentService | Multi-vector RRF across personal knowledge base |
| `search_emails` | EmailSearchService | Multi-vector RRF across indexed email archive |
| `get_email_details` | EmailSearchService | Fetch full body of a specific email by ID |
| `get_email_attachment` | EmailSearchService | Download and convert email attachment to text |

These are **not** manifest intents. They are tool declarations internal to ExtendedSearchAgent,
implemented as direct service calls (not coordinator delegations).

### Key formulation

The ExtendedSearchAgent uses its own LLM (BALANCED tier) for key formulation as part of
Turn 1. Unlike FastSearchAgent, it can formulate different keys for facts vs emails
(e.g., broader keywords for facts, date-constrained query for emails).

### Max turns

`MAX_TURNS = 5` — sufficient for search → details → attachment → synthesize.

### Output format

Synthesized natural language response (not structured JSON). The agent has full context
of all sources and produces a coherent answer. The orchestrator receives a ready-to-use
knowledge summary, not raw data.

### Future extensibility

Adding a new data source (e.g., file storage, calendar):
1. Add a new tool to ExtendedSearchAgent (`search_files`, `search_calendar`)
2. Inject the corresponding port/service via constructor
3. Update the agent's system prompt to describe the new tool
4. No manifest changes. No orchestrator prompt changes. No new intents.

---

## 6. Manifest Changes

### Removed descriptors

```python
# These are removed from ALL_DESCRIPTORS:
MEMORY_SEARCH    # search_memory + save_to_memory
EMAIL_SEARCH     # search_emails + get_email_details + get_email_attachment
```

### New descriptors

```python
FAST_SEARCH = AgentDescriptor(
    agent_id="fast_search_agent",
    capabilities={
        Intent.FAST_SEARCH: ExecutionMode.SYNC,
    },
    description="Quick knowledge lookup across all sources",
    capability_descriptions={
        Intent.FAST_SEARCH: (
            "Fast search across personal knowledge base and email archive. "
            "Returns facts and email snippets. Use for simple factual recall: "
            "names, dates, preferences, recent correspondence overview."
        ),
    },
    context_schemas={},  # No structured context — query only
)

EXTENDED_SEARCH = AgentDescriptor(
    agent_id="extended_search_agent",
    capabilities={
        Intent.EXTENDED_SEARCH: ExecutionMode.SYNC,
    },
    description="Deep knowledge search with multi-source correlation",
    capability_descriptions={
        Intent.EXTENDED_SEARCH: (
            "Deep search across all knowledge sources: facts, emails (full body, "
            "attachments). Use when the answer requires reading email content, "
            "cross-referencing sources, or extracting details from attachments. "
            "Returns a synthesized summary — no need for follow-up detail requests."
        ),
    },
    context_schemas={},  # No structured context — query only
)

SAVE_TO_MEMORY = AgentDescriptor(
    agent_id="save_to_memory_agent",
    capabilities={
        Intent.SAVE_TO_MEMORY: ExecutionMode.SYNC,
    },
    description="Save fact to long-term memory",
    capability_descriptions={
        Intent.SAVE_TO_MEMORY: (
            "Save a fact to long-term memory for future recall. "
            "Call when the user explicitly asks to remember something."
        ),
    },
    context_schemas={
        Intent.SAVE_TO_MEMORY: {
            "text": "Detailed, self-contained fact description for consolidation.",
        },
    },
)
```

### Intent constants

```python
class Intent:
    # Search (new)
    FAST_SEARCH = "fast_search"
    EXTENDED_SEARCH = "extended_search"
    
    # Memory save (unchanged)
    SAVE_TO_MEMORY = "save_to_memory"
    
    # Removed:
    # SEARCH_MEMORY = "search_memory"
    # SEARCH_EMAILS = "search_emails"
    # GET_EMAIL_DETAILS = "get_email_details"
    # GET_EMAIL_ATTACHMENT = "get_email_attachment"
```

### Orchestrator impact

Both Quick and Smart `allowed_intents=None` — they auto-discover intents from registry.
Removing old intents and adding new ones requires **no code change** in orchestrators.
Prompt token updates needed (see Section 8).

---

## 7. DelegationEngine Changes

### Priority execution rename

Current: DelegationEngine treats `search_memory` as priority (sequential before parallel).

```python
# Current (delegation_engine.py)
_memory_calls = [c for c in calls if c.args.get("intent") == "search_memory"]
```

Change to:

```python
_priority_calls = [c for c in calls if c.args.get("intent") == Intent.FAST_SEARCH]
```

`fast_search` inherits the priority execution semantics: results accumulated in
`memory_context`, passed to subsequent delegations.

### memory_context rename

Consider renaming `memory_context` → `search_context` in delegation_context for clarity.
Non-breaking: internal to DelegationEngine + AgentCoordinator.

---

## 8. Prompt Changes

### Orchestrator prompts (Quick + Smart)

`PROTOCOL_QUICK_AGENT_SELECTION` and `PROTOCOL_SMART_AGENT_SELECTION` tokens need update:

- Remove references to `search_memory`, `search_emails`, `get_email_details`, `get_email_attachment`
- Add `fast_search` and `extended_search` with usage guidance
- Key instruction: "Choose search depth, not search source"

### New agent prompts

| Agent | Blueprint | Profile | Key tokens |
|-------|-----------|---------|------------|
| `fast_search` | `universal_agent_v1` | `fast_search` | `COGNITIVE_PROCESS_FAST_SEARCH`, `OUTPUT_FORMAT_FAST_SEARCH` |
| `extended_search` | `universal_agent_v1` | `extended_search` | `COGNITIVE_PROCESS_EXTENDED_SEARCH` |
| `save_to_memory` | None (zero-LLM) | None | None |

FastSearchAgent reuses the existing MemorySearch key formulation prompt
(`COGNITIVE_PROCESS_MEMORY_SEARCH` + `OUTPUT_FORMAT_MEMORY_SEARCH`) — rename to
`COGNITIVE_PROCESS_FAST_SEARCH` / `OUTPUT_FORMAT_FAST_SEARCH`.

ExtendedSearchAgent needs a new cognitive process token describing multi-source search
strategy, tool selection logic, and synthesis instructions.

---

## 9. save_to_memory Extraction

`save_to_memory` is extracted from FactsMemoryAgent into a standalone `SaveToMemoryAgent`.

**Why separate agent:** save_to_memory is zero-LLM, has nothing in common with search.
Bundling it with FactsMemoryAgent was an accident of history, and it caused the
routing bug (text-in-payload triggering save instead of search).

The agent is trivial: receive text → attach as `consolidation_text` on AgentResponse →
consolidation picks it up in the normal batch cycle.

---

## 10. Dependencies & Injection

### FastSearchAgent constructor

```python
class FastSearchAgent(BaseAgent):
    def __init__(
        self,
        config: AgentConfig,
        search_enrichment: SearchEnrichmentPort,     # Facts RRF
        email_search: EmailSearchService,            # Email RRF (snippet-level)
        execution_context: AgentExecutionContext,     # ECO LLM for key formulation
        prompt_builder: PromptBuilderPort,
        user_id: str,
        account_id: str,
    ): ...
```

### ExtendedSearchAgent constructor

```python
class ExtendedSearchAgent(BaseAgent):
    def __init__(
        self,
        config: AgentConfig,
        search_enrichment: SearchEnrichmentPort,     # Facts RRF
        email_search: EmailSearchService,            # Email search + details + attachments
        execution_context: AgentExecutionContext,     # BALANCED LLM for tool loop
        prompt_builder: PromptBuilderPort,
        user_id: str,
        account_id: str,
    ): ...
```

Both agents receive the same ports. The difference is the LLM tier and execution mode.

### UserAgentFactory changes

Replace `FactsMemoryAgent` + `EmailSearchAgent` creation with `FastSearchAgent` +
`ExtendedSearchAgent` + `SaveToMemoryAgent`. Same ports, different wiring.

---

## 11. Safety & Limits

| Concern | Mitigation |
|---------|-----------|
| ExtendedSearch cost blowup | `MAX_TURNS = 5` hard cap. BALANCED tier, not PERFORMANCE. |
| FastSearch latency increase (added email) | Email vector search is ~700ms (parallel with facts). No LLM cost added. |
| Email not indexed for user | FastSearchAgent + ExtendedSearchAgent check email availability. If no indexed emails, skip email search silently. |
| Orchestrator calls extended_search for simple queries | Prompt guidance: "Use fast_search unless the query requires reading email content or cross-referencing." Same pattern as Quick/Smart selection. |
| Breaking change for existing conversations | Session history may contain old intent names. DelegationEngine only sees current-turn tool calls, not history. No migration needed. |

---

## 12. Open Questions

**Q1: Should `fast_search` include email snippets by default, or only when email is indexed?**  
A1: Check availability at runtime. If `EmailSearchService` is wired and user has indexed emails — include. Otherwise facts-only. Zero cost when absent (no vector queries dispatched).

**Q2: Should ExtendedSearchAgent return structured data or natural language?**  
A2: Natural language synthesis. The orchestrator needs a ready-to-use knowledge summary,
not raw JSON to re-interpret. The agent has full context of all sources and can produce
a coherent, cross-referenced answer.

**Q3: What happens to `email_search_context` in conversation history?**  
A3: ExtendedSearchAgent returns `history_context` with search metadata (what was searched,
what was found) for conversation continuity. Same pattern, different key name.

**Q4: Eager or lazy agent creation?**  
A4: FastSearchAgent — eager (used frequently, like current FactsMemoryAgent).
ExtendedSearchAgent — lazy (`eager=False`, created on first delegation via AgentFactoryPort).
SaveToMemoryAgent — eager (trivial, zero-LLM).

---

## 13. Files Changed

| File | Change |
|------|--------|
| `src/agents/memory_search_agent.py` | Remove (replaced by fast_search_agent + save_to_memory_agent) |
| `src/agents/email_search_agent.py` | Remove (replaced by extended_search_agent) |
| `src/agents/fast_search_agent.py` | **New.** ECO single-pass search across facts + email snippets |
| `src/agents/extended_search_agent.py` | **New.** BALANCED multi-turn search with internal tools |
| `src/agents/save_to_memory_agent.py` | **New.** Zero-LLM fact save (extracted from FactsMemoryAgent) |
| `src/infrastructure/agent_manifest.py` | Replace MEMORY_SEARCH + EMAIL_SEARCH descriptors with FAST_SEARCH + EXTENDED_SEARCH + SAVE_TO_MEMORY. Update Intent constants. |
| `src/infrastructure/delegation_engine.py` | Rename priority execution from `search_memory` → `fast_search`. Optional: `memory_context` → `search_context`. |
| `src/infrastructure/agent_config.py` | Add `FAST_SEARCH` and `EXTENDED_SEARCH` config dataclasses. |
| `src/services/agent_context_builder.py` | Add `fast_search` (ECO) and `extended_search` (BALANCED) strategies. |
| `src/composition/user_agent_factory.py` | Replace FactsMemoryAgent + EmailSearchAgent with new agents. |
| `src/composition/service_container.py` | Wire new agents (if container-level changes needed). |
| `tests/unit/agents/test_memory_search_agent.py` | Remove (replaced by new test files). |
| `tests/unit/agents/test_email_search_agent.py` | Remove (replaced by new test files). |
| `tests/unit/agents/test_fast_search_agent.py` | **New.** |
| `tests/unit/agents/test_extended_search_agent.py` | **New.** |
| `tests/unit/agents/test_save_to_memory_agent.py` | **New.** |
| Firestore (manual) | New prompt tokens + profiles for fast_search and extended_search. Update orchestrator protocol tokens. |

---

## 14. Implementation Order

1. **SaveToMemoryAgent** — extract from FactsMemoryAgent. Trivial, zero-LLM. Tests.
2. **FastSearchAgent** — port existing FactsMemoryAgent search logic + add email snippet search. Tests.
3. **ExtendedSearchAgent** — new multi-turn agent with internal tools. Prompt design. Tests.
4. **Manifest + Intent constants** — update agent_manifest.py.
5. **DelegationEngine** — rename priority execution.
6. **AgentContextBuilder** — add ECO/BALANCED strategies.
7. **UserAgentFactory** — wire new agents, remove old ones.
8. **Firestore prompts** — create tokens + profiles for new agents; update orchestrator protocols.
9. **Old agent cleanup** — remove memory_search_agent.py, email_search_agent.py, old tests.
10. **E2E validation** — deploy to dev, test both search tiers manually.
