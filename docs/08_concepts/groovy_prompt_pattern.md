# Groovy-Style Prompt Engineering Pattern

**Status:** Adopted Standard | **Date:** 2026-01-19
**Target Audience:** AI Developers & Future AI Sessions

## 📖 HowTo: Using This Document

### Purpose

Explains the Groovy DSL pattern for prompt engineering: treating prompts as code for better LLM adherence.

### When to Read

- **For AI Agents:** When creating or modifying prompt components.
- **For Developers:** When designing agent prompts or debugging LLM behavior.

### When to Update

This document MUST be updated when:

- [ ] Groovy DSL syntax evolves.
- [ ] New prompt patterns are discovered.
- [ ] Better practices for LLM instruction clarity emerge.

### Cross-References

- **Prompt Component System:** [../05_building_blocks/prompt_component_system/README.md](../05_building_blocks/prompt_component_system/README.md)
- **Prompt Components Guide:** See docs_local/guides/PROMPT_COMPONENTS_GUIDE.md (local only)
- **Implementation:**
  - `src/domain/prompt.py` - Prompt component models
  - `src/adapters/groovy_prompt_assembler.py` - Groovy DSL assembler

---

## 1. The Core Philosophy

Modern LLMs (Gemini 2.0, GPT-4, Claude 3.5) are heavily trained on code. They understand **syntax, inheritance, strict typing, and logical flow** better than they understand loose natural language instructions.

The **Groovy-Style Pattern** exploits this by treating the System Prompt not as a "story" but as a **Class Definition**.

### Why Groovy?

Groovy (and Java) syntax provides specific constructs that map perfectly to Agent behavior:

- **`class Agent extends Base`**: Instantly establishes hierarchy and capability inheritance.
- **`methods`**: Defines repeatable, deterministic protocols (Tools/Agents).
- **`properties`**: Defines static knowledge (Context, Anchors).
- **`{ ... }` Blocks**: clearly delimits scope, preventing instruction bleeding.

---

## 2. The Architecture: 3-Level Resolution

We structure prompts using a hierarchical component system with 3 resolution levels:

### Level 1: SYSTEM Components

- **Purpose:** Base personality, core values, language rules
- **Scope:** Shared across all agents
- **Example:** `kernel` (personality), `core_context` (system-wide knowledge)

### Level 2: AGENT Components

- **Purpose:** Agent-specific behavior, tools, protocols
- **Scope:** Per-agent (e.g., `smart_response`, `router`, `consolidation`)
- **Example:** Agent-specific instructions, cognitive process steps

### Level 3: USER Components

- **Purpose:** User-specific context, preferences, facts
- **Scope:** Per-user
- **Example:** `biographical_context` (user's memory), custom preferences

**Resolution Order:** USER > AGENT > SYSTEM (most specific wins)

See: [Prompt Component System](../05_building_blocks/prompt_component_system/README.md)

---

## 3. Syntax Guide & Examples

### A. Defining the Agent

Instead of: _"You are a helpful assistant named Alek..."_
Use:

```groovy
class SmartResponseAgent extends Alek {
  // Inherits all personality traits from 'Alek' automatically

  capabilities: [
    "Complex reasoning",
    "Agent delegation",
    "Context synthesis"
  ]
}
```

### B. Injecting Knowledge

Instead of: _"Here is some context about the user..."_
Use:

```groovy
  knowledge_base {
    user_context: '''
      ${biographical_context}
    '''

    mental_anchors: '''
      ${user_anchors}
    '''

    available_agents: [
      "memory_search_agent",
      "web_search_agent"
    ]
  }
```

**Note:** `${...}` syntax allows runtime variable injection from `PromptBuilder`.

### C. Defining Protocols (Agent Behaviors)

Instead of: _"When you search memory, you must first..."_
Use:

```groovy
  methods {
    /**
     * Protocol for delegating to specialist agents.
     * @param query The user's request
     */
    def delegation_protocol(query) {
      cognitive_process {
        steps: [
          "1. ANALYZE: What information is needed?",
          "2. IDENTIFY: Which agent can provide it?",
          "3. DELEGATE: Route message to specialist agent",
          "4. SYNTHESIZE: Combine results into coherent response"
        ]
      }

      available_agents: [
        memory_search_agent: "For personal facts and user history",
        web_search_agent: "For external information and current events"
      ]
    }
  }
```

---

## 4. The "Cognitive Process" Block

This is the most critical component for reasoning. By defining a `cognitive_process` list inside a method, we force the LLM to:

1.  **Plan** before acting.
2.  **Follow** a strict sequence.
3.  **Self-Correct** (via Verify/Analyze steps).

**Example:**

```groovy
      cognitive_process {
        steps: [
          "1. ANALYZE: Extract Object and Criteria.",
          "2. EXECUTE: Perform search or delegation.",
          "3. VERIFY: Do results match Criteria?",
          "4. DELIVER: Format output with context."
        ]
      }
```

**Why This Works:**

- LLMs trained on code recognize step-by-step procedures
- Numbered lists enforce sequential execution
- Verbs (ANALYZE, EXECUTE, VERIFY) trigger specific reasoning modes

---

## 5. Implementation in v6.0

### Component Structure

**System Components:**

```python
# Stored in Firestore collection: prompt_components
{
  "component_id": "kernel",
  "scope": ComponentScope.SYSTEM,
  "content": "class Alek { ... }",  # Groovy DSL
  "owner_type": OwnerType.SYSTEM,
  "owner_id": "SYSTEM"
}
```

**Agent Components:**

```python
{
  "component_id": "smart_response_agent",
  "scope": ComponentScope.AGENT,
  "content": "class SmartResponseAgent extends Alek { ... }",
  "owner_type": OwnerType.AGENT,
  "owner_id": "smart_response"
}
```

**User Components:**

```python
{
  "component_id": "biographical_context",
  "scope": ComponentScope.USER,
  "content": "${enriched_facts}",  # Dynamic injection
  "owner_type": OwnerType.USER,
  "owner_id": "user_123"
}
```

**Code References:**

- `src/domain/prompt.py:20-35` - `PromptComponent` model
- `src/adapters/firestore_prompt_repository.py` - Storage
- `src/services/prompt_component_service.py` - Resolution logic

### Assembly Process

**Step 1: Component Resolution**

```python
# PromptComponentService resolves components by scope
components = await service.resolve_components(
    scope=ComponentScope.AGENT,
    agent_type="smart_response",
    user_id="user_123"
)
```

**Step 2: Groovy Assembly**

```python
# GroovyPromptAssembler merges components
assembled_prompt = await assembler.assemble(
    components=components,
    runtime_data={"enriched_facts": user_facts}
)
```

**Step 3: Variable Injection**

```python
# Runtime variables injected into ${...} placeholders
final_prompt = assembled_prompt.format(
    biographical_context=user_memory,
    user_anchors=user_principles
)
```

**Code References:**

- `src/adapters/groovy_prompt_assembler.py` - Assembly logic
- `src/services/prompt_builder.py` - High-level builder

---

## 6. Implementation Checklist for Future Sessions

When refactoring or adding new capabilities:

1.  **Inheritance First:** Always extend the base `Alek` class. Do not redefine personality in agent layers.
2.  **Methods, Not Rules:** Define behavioral protocols as `def method_name()`.
3.  **Strict Scoping:** Use `{ ... }` to contain instructions. If an instruction is outside a block, it's global (and dangerous).
4.  **Type Hints (Optional):** Use pseudo-types like `List<String>` or `Map` if output format is strict.
5.  **Comments:** Use `//` for context injection and `/** ... */` for method documentation. The LLM reads these as "developer intent".
6.  **Variable Injection:** Use `${variable_name}` for runtime data injection.
7.  **Cognitive Processes:** Always include explicit step-by-step reasoning for complex protocols.

---

## 7. Why This is Better

- **Reduced Hallucinations:** The model treats the prompt as code to be executed, not text to be improvised.
- **Semantic Clarity:** Separates _Identity_ (Kernel) from _Function_ (Agent protocols).
- **Modularity:** You can swap components independently (USER overrides AGENT overrides SYSTEM).
- **Testability:** Prompts can be versioned, tested, and A/B tested like code.
- **Maintainability:** Changes to agent behavior don't require full prompt rewrites.

---

## 8. Real-World Examples

### Example 1: Router Agent Triage

**Before (Natural Language):**

```
You are a router that classifies user queries. Look at the query and decide
if it's simple or complex. Simple queries go to quick_response, complex go
to smart_response. Consider factors like length, ambiguity, and required tools.
```

**After (Groovy DSL):**

```groovy
class RouterAgent extends Alek {
  role: "Query triage and routing"

  methods {
    def classify_query(user_query) {
      cognitive_process {
        steps: [
          "1. ANALYZE complexity (scale 1-10)",
          "2. ANALYZE tone (casual/professional/technical/urgent)",
          "3. CLASSIFY query type (simple/personal/external)",
          "4. DECIDE routing target based on complexity threshold (≤5 → quick, >5 → smart)"
        ]
      }

      complexity_factors: [
        "Query length and structure",
        "Ambiguity and context requirements",
        "Need for external data or tools",
        "Multi-step reasoning required"
      ]

      routing_rules: {
        quick_response: "Complexity ≤5, factual, no tools needed",
        smart_response: "Complexity >5, analysis, delegation possible"
      }
    }
  }
}
```

**Code Reference:** `src/agents/prompts/triage_router_v1.groovy`

### Example 2: Consolidation Agent

**Before:**

```
Extract facts from conversation history. Look for personal information,
preferences, and important events. Format as structured data.
```

**After:**

```groovy
class ConsolidationAgent extends Alek {
  role: "Life chronicler - extract and structure knowledge"

  methods {
    def consolidate_session(messages) {
      cognitive_process {
        steps: [
          "1. LOAD biographical context for deduplication",
          "2. ANALYZE messages for factual content",
          "3. EXTRACT facts (objective) and anchors (subjective principles)",
          "4. DEDUPLICATE via vector similarity check",
          "5. STRUCTURE output as JSON with embeddings"
        ]
      }

      fact_criteria: {
        objective: "Verifiable statements about reality",
        temporal: "Include explicit timestamps when available",
        atomic: "One fact per entity, no compound statements"
      }

      anchor_criteria: {
        subjective: "Personal values, preferences, principles",
        universal: "Formulate as timeless guidelines",
        actionable: "Guide future behavior and decisions"
      }
    }
  }
}
```

**Code Reference:** `src/agents/prompts/consolidation_v2.prompt`

---

## 9. Common Pitfalls

### ❌ Anti-Pattern: Instruction Bleeding

```groovy
class Agent extends Alek {
  // DON'T: Instructions outside blocks bleed into all contexts
  Always be polite.
  Never refuse requests.

  methods { ... }
}
```

### ✅ Correct Pattern: Scoped Instructions

```groovy
class Agent extends Alek {
  personality_traits: {
    tone: "Professional yet approachable",
    response_style: "Clear, concise, context-aware"
  }

  methods { ... }
}
```

### ❌ Anti-Pattern: Vague Steps

```groovy
cognitive_process {
  steps: [
    "Think about the problem",
    "Do something",
    "Return result"
  ]
}
```

### ✅ Correct Pattern: Explicit Steps

```groovy
cognitive_process {
  steps: [
    "1. IDENTIFY: What specific information is requested?",
    "2. EXECUTE: search_memory(keywords=[...])",
    "3. VERIFY: Do results contain requested information?",
    "4. DELIVER: Format findings with context and citations"
  ]
}
```

---

## 10. Future Evolution

### Planned Enhancements

1. **Typed Variables:** `${user_facts: List<Fact>}` for schema validation
2. **Conditional Blocks:** `if (user_tier == "premium") { ... }`
3. **Template Functions:** `def format_date(timestamp)` for reusable logic
4. **Hot Reload:** Update prompts without restarting agents

---

**Last Updated:** 2026-01-30
**Status:** ✅ Current (v6.0 Prompt Component System uses Groovy DSL)
