# Prompt Assembly Guide

**Status:** ✅ Active
**Last Updated:** 2026-02-25

## Overview

Explains how prompts are assembled in Alek Core using the **v3 token-based system**. The central service is `PromptAssemblyService`. Agents never touch prompt construction directly — they call `PromptBuilderPort.build_for_agent()`.

## Table of Contents

1. [Assembly Chain](#1-assembly-chain)
2. [Two-Phase Assembly](#2-two-phase-assembly)
3. [Assembled Prompt Structure](#3-assembled-prompt-structure)
4. [Agent Patterns](#4-agent-patterns)
5. [build_for_agent API](#5-build_for_agent-api)
6. [Biographical Facts: Static vs Query-Specific](#6-biographical-facts-static-vs-query-specific)
7. [Cache Boundary and Anthropic Prompt Caching](#7-cache-boundary-and-anthropic-prompt-caching)
8. [File Reference](#8-file-reference)
9. [Debugging](#9-debugging)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Assembly Chain

```
Agent (SmartAgent, QuickAgent, ConsolidationAgent)
  └─ build_system_prompt()
       ↓
PromptBuilderPort (port interface)
  └─ UserPromptBuilder (service implementation)
       └─ build_for_agent(agent_type, user_id, routing_metadata, ...)
            ├─ BiographicalContextService.get_biographical_context_cached(account_id)
            └─ PromptAssemblyService.assemble(agent_type, user_id, account_id,
                                               biographical_facts, conversation_history)
                 ├─ PHASE 1: _assemble_static_template()  ← cached 24h
                 │    ├─ Load blueprint (universal_agent_v1) from Firestore
                 │    ├─ Load 4-level profiles in parallel (SYSTEM, AGENT, ACCOUNT, USER)
                 │    ├─ Resolve slot assignments (priority: USER > ACCOUNT > AGENT > SYSTEM)
                 │    ├─ Fetch all assigned tokens in parallel
                 │    ├─ Substitute {{CLASS_NAME}} placeholders
                 │    └─ _normalize_whitespace(): remove empty blocks, collapse blank lines
                 └─ PHASE 2: _inject_runtime_context()  ← every request
                      ├─ Split biographical_facts: static vs semantic_lens (Q-S)
                      ├─ Format + validate both via SecurityPort (UNTRUSTED zone)
                      ├─ Format + validate conversation_history via SecurityPort
                      ├─ Append knowledge_base {} block (bio + history, only if non-empty)
                      └─ Append <!-- CACHE_BOUNDARY --> + current_datetime + Q-S context
```

---

## 2. Two-Phase Assembly

### Phase 1: Static Template (Cached 24h)

Resolves tokens from Firestore and builds the Groovy DSL class definition. The result is stored in an in-memory dict with a 24-hour TTL. Cache key: `prompt:{agent_type}:acc:{account_id}:usr:{user_id}`.

**Cold start:** ~110ms (4 parallel Firestore queries)
**Cache hit:** ~5ms

After Phase 1, the template is a complete Groovy DSL class like:
```groovy
class Alek extends Agent {
  personality {
    archetype { ... }
    vibe { ... }
    humor_engine { ... }
  }
  knowledge_base {
    few_shot_examples { ... }
  }
  policies { ... }
  protocols { ... }
  cognitive_process { ... }
  output_format { ... }
}
```

### Phase 2: Runtime Injection (Every Request)

Takes the cached static template and appends runtime content. Runs on every request — never cached.

Steps:
1. Split `biographical_facts` into static (long-term memory) and semantic (query-specific, tagged `semantic_lens`)
2. Format static facts with `BiographicalFactsFormatter` (domain-grouped Markdown)
3. Validate both via `SecurityPort` (UNTRUSTED zone)
4. Format and validate `conversation_history` via `SecurityPort`
5. Build and append `knowledge_base {}` block if either biographical_context or conversation_history is non-empty
6. Append `<!-- CACHE_BOUNDARY -->`
7. Append `current_date_time {}` (always)
8. Append `query_specific_context` block (only if Q-S facts present)

---

## 3. Assembled Prompt Structure

```
[STATIC PREFIX — cached by Anthropic ~5 min]

class Alek extends Agent {
  personality {
    archetype { ... }         ← ARCHETYPE token
    vibe { ... }              ← VIBE token
    voice { ... }             ← VOICE token
    humor_engine { ... }      ← HUMOR_ENGINE token (user-customizable)
    motto { ... }             ← MOTTO_DEFAULT token
  }
  behaviors { ... }           ← BEHAVIOR_GUIDE token
  knowledge_base {
    few_shot_examples { ... } ← FEW_SHOT_EXAMPLES token
  }
  policies { ... }            ← 6 POLICY tokens
  protocols { ... }           ← 2 PROTOCOL tokens
  cognitive_process { ... }   ← COGNITIVE_PROCESS token (agent-specific)
  output_format { ... }       ← OUTPUT_FORMAT token
  directives { ... }          ← 2 DIRECTIVE tokens
}

knowledge_base {
  biographical_context: '''
    **Biographical**
    - Born in Kyiv (Jan 01, 2000)
    - Software engineer (Feb 10, 2025)

    **Work**
    - ...
  '''

  conversation_history: '''   ← ConsolidationAgent only
    user: ...
    assistant: ...
  '''
}

[DYNAMIC SUFFIX — sent fresh every request]

<!-- CACHE_BOUNDARY -->
current_date_time {
    2026-02-25 14:32 Tuesday (UTC)
    System time is UTC. The user's local time may differ...
}

query_specific_context: '''   ← only when router found semantic facts
    **Query-Specific Context:**
    - User mentioned travel plans last week
'''
```

**Key rules:**
- The `knowledge_base` block is only appended when at least one of `biographical_context` or `conversation_history` is non-empty. No empty wrappers.
- Both sections share one `knowledge_base` block (not two separate blocks).
- `query_specific_context` is only appended when Q-S facts exist.

---

## 4. Agent Patterns

### Pattern A: Conversational (Smart, Quick)

- Conversation history → passed as `messages` parameter to LLM, NOT in system prompt
- `conversation_history=[]` in `assemble()` call → no `conversation_history` section in `knowledge_base`
- Biographical facts → static section (before boundary)
- Q-S context from router enrichment → dynamic section (after boundary)

```python
request = LLMRequest(
    model_name=ctx.model_name,
    system_instruction=system_prompt,  # assembled prompt (no history inside)
    messages=conversation_messages,    # history here
    tools=tool_declarations,
)
```

### Pattern B: Document Analysis (Consolidation)

- History batch (messages to consolidate) → passed as `conversation_history` to `assemble()` → ends up in static `knowledge_base` block (before boundary, gets cached)
- No Q-S context
- The entire consolidation context (instructions + history batch) is in `system_instruction`; `messages=[]`

```python
request = LLMRequest(
    model_name=ctx.model_name,
    system_instruction=system_prompt,  # includes the history batch in knowledge_base
    messages=[],                       # empty — everything is in system prompt
)
```

**Why history in static (cached) for consolidation:** The batch of messages to consolidate is fixed for the entire run. Placing it before the cache boundary means Anthropic caches ~8k tokens on the first call and reads from cache on all subsequent turns. Maximum caching benefit.

---

## 5. `build_for_agent` API

```python
async def build_for_agent(
    self,
    agent_type: str,
    user_id: str,
    account_id: Optional[str] = None,
    routing_metadata: Optional[RoutingMetadata] = None,
    capabilities: Optional[ProviderCapabilities] = None,
    include_biographical: bool = True,
    conversation_history: Optional[List[dict]] = None,
) -> str
```

### `include_biographical` Flag

| Value | Behavior | Use case |
|-------|----------|----------|
| `True` (default) | Fetches `get_biographical_context_cached(account_id)` from Firestore | Smart, Quick, Consolidation |
| `False` | Skips Firestore fetch; `biographical_facts = []` | Router, MemorySearch — no bio slot in their prompts |

Router and MemorySearch skip biographical context to avoid ~1400ms cold fetch for data they never use.

### `routing_metadata` for Q-S context

When `routing_metadata` is passed, `UserPromptBuilder` calls `merge_enriched_context_with_biographical()` to extract facts tagged `semantic_lens` from the routing metadata. These become the query-specific context appended after the cache boundary.

---

## 6. Biographical Facts: Static vs Query-Specific

Facts come from two sources:
1. **Long-term memory** (`BiographicalContextService`) — slow-changing, high-quality, stored in Firestore as `FactEntity`. These go in the **static** section before the boundary.
2. **Query-specific (Q-S) context** — semantic search results from the router, tagged `semantic_lens` by `merge_enriched_context_with_biographical()`. These go in the **dynamic** section after the boundary.

The split happens in `_inject_runtime_context()`:
```python
static_facts  = [f for f in biographical_facts if "semantic_lens" not in f.get("tags", [])]
semantic_facts = [f for f in biographical_facts if "semantic_lens" in f.get("tags", [])]
```

**Why this split:** Long-term biographical facts rarely change (updated every ~30 new messages via consolidation). Placing them before the boundary means they are cached with the static template content. Q-S context changes every request (different query → different semantic search results), so it must be in the dynamic suffix.

---

## 7. Cache Boundary and Anthropic Prompt Caching

`PROMPT_CACHE_BOUNDARY = "<!-- CACHE_BOUNDARY -->"` is defined in `src/ports/llm_service.py`.

When `ClaudeAdapter` receives a request with `cache_config.enabled=True` and the boundary marker is present in `system_instruction`, it splits the instruction into two `system_parts` blocks:

```python
static_part, dynamic_part = system_instruction.split(PROMPT_CACHE_BOUNDARY, 1)
system_parts = [
    {"type": "text", "text": static_part.strip(), "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": dynamic_part.strip()},
]
```

Anthropic caches the static block for ~5 minutes. On subsequent requests within the window, `cache_read_input_tokens` appears in the response metadata instead of paying for those tokens.

**Guard:** `cache_control` is never added to an empty text block (Anthropic returns HTTP 400 in that case). The adapter checks `system_instruction` is non-empty before setting cache config.

See [HEXAGONAL_PROMPT_CACHING_RFC.md](../10_rfcs/HEXAGONAL_PROMPT_CACHING_RFC.md) Section 13 for full details.

---

## 8. File Reference

| File | Purpose |
|------|---------|
| `src/ports/prompt_builder_port.py` | `PromptBuilderPort` ABC — what agents call |
| `src/services/user_prompt_builder.py` | Concrete implementation: fetches bio context, calls assembly service |
| `src/services/prompt_v3/prompt_assembly_service.py` | Two-phase assembly, 4-level token resolution, 24h cache |
| `src/services/prompt_v3/biographical_formatter.py` | Domain-grouped Markdown formatting of biographical facts |
| `src/services/prompt_v3/context_formatter.py` | Conversation history formatting |
| `src/ports/llm_service.py` | `PROMPT_CACHE_BOUNDARY` constant |
| `src/adapters/claude_adapter.py` | Splits at boundary, applies `cache_control: ephemeral` |
| `src/domain/prompt_v3/` | Domain models: Token, Blueprint, ProfileSlot, OwnerType |
| `src/adapters/prompt_v3/` | Firestore repositories for tokens, blueprints, profiles |
| `scripts/migration/update_blueprint_template.py` | Remove `[[...]]` placeholders from blueprint in Firestore |

**Agent files:**

| Agent | File | Pattern |
|-------|------|---------|
| SmartAgent | `src/agents/core/smart_response_agent.py` | Conversational |
| QuickAgent | `src/agents/core/quick_response_agent.py` | Conversational |
| ConsolidationAgent | `src/agents/consolidation_agent.py` | Document Analysis |
| RouterAgent | `src/agents/core/router_agent.py` | No bio context |

---

## 9. Debugging

### Inspect Assembled Prompt

```bash
# E2E inspection script (uses real Firestore, captures output to debug_prompts/)
python scripts/prompt/test_agent_e2e.py --agent smart
```

Captured prompts saved to `debug_prompts/` (gitignored).

### Cache Hits in Logs

```
📦 Cache HIT: prompt:smart:acc:{account_id}:usr:{user_id}
📦 Cache MISS: prompt:smart:acc:{account_id}:usr:{user_id} - assembling from repositories...
✅ Assembled prompt: 5432 chars
```

### Invalidate Cache

```python
# Admin command via Slack
$admin_cache_reset

# Or directly
assembly_service.invalidate_cache()
```

---

## 10. Troubleshooting

| Issue | Cause | Solution |
|-------|-------|---------|
| `KeyError: Blueprint not found` | Blueprint not in Firestore | Run `scripts/migration/create_blueprints.py --upload` |
| `Prompt has {{PLACEHOLDERS}}` | Token not resolved | Check token exists + profile has assignment for that class |
| Empty `policies` / `protocols` sections | `_normalize_whitespace()` removed them | Token missing from assignment → check SYSTEM profile in Firestore |
| `knowledge_base` block missing | No biographical facts | Check `BiographicalContextService` returns facts and cache is warm |
| No `<!-- CACHE_BOUNDARY -->` in prompt | Assembly service bug | Run `test_prompt_assembly_service.py` — `test_boundary_marker_always_appended` |
| Q-S context in static section | Facts not tagged `semantic_lens` | Check `merge_enriched_context_with_biographical()` sets the tag |
| Stale prompts after token change | 24h assembly cache | Run `$admin_cache_reset` or redeploy |
| Duplicate `datetime` in prompt | Old agent code adds it manually | Remove the manual addition — assembler adds `current_date_time` automatically |

---

## Summary

1. **Two phases:** Static template (cached 24h, token resolution) + runtime injection (every request, appended not replaced)
2. **No runtime placeholders in blueprint:** Blueprint is purely static `{{TOKEN_SLOT}}` only
3. **One `knowledge_base` block:** Bio + history share a single block; neither emits empty wrappers
4. **Cache boundary:** Static prefix (blueprint + bio + history for consolidation) cached by Anthropic; dynamic suffix (datetime + Q-S context) sent fresh
5. **Agent differences:** Smart/Quick → bio in static, history in messages; Consolidation → history in static (gets cached), history not in messages
