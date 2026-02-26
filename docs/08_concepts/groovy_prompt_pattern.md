# Groovy-Style Prompt Engineering Pattern

**Status:** Adopted Standard | **Last Updated:** 2026-02-25
**Target Audience:** AI Developers & Future AI Sessions

## 📖 HowTo: Using This Document

### Purpose

Explains the Groovy DSL pattern for prompt engineering: treating prompts as code for better LLM adherence.

### When to Read

- **For AI Agents:** When creating or modifying prompt tokens or blueprints.
- **For Developers:** When designing agent prompts or debugging LLM behavior.

### When to Update

This document MUST be updated when:

- [ ] Groovy DSL syntax conventions evolve.
- [ ] New prompt patterns are discovered.
- [ ] Better practices for LLM instruction clarity emerge.

### Cross-References

- **Prompt Design System v3:** [../05_building_blocks/prompt_design_system_v3/README.md](../05_building_blocks/prompt_design_system_v3/README.md)
- **Prompt Assembly Guide:** [./prompt_assembly_guide.md](./prompt_assembly_guide.md)
- **Implementation:** `src/services/prompt_v3/prompt_assembly_service.py`

---

## 1. The Core Philosophy

Modern LLMs (Gemini 2.0, GPT-4, Claude) are heavily trained on code. They understand **syntax, inheritance, strict typing, and logical flow** better than they understand loose natural language instructions.

The **Groovy-Style Pattern** exploits this by treating the System Prompt not as a "story" but as a **Class Definition**.

### Why Groovy?

Groovy (and Java) syntax provides specific constructs that map perfectly to Agent behavior:

- **`class Agent extends Base`**: Instantly establishes hierarchy and capability inheritance.
- **`methods`**: Defines repeatable, deterministic protocols (Tools/Agents).
- **`properties`**: Defines static knowledge (Context, Anchors).
- **`{ ... }` Blocks**: Clearly delimits scope, preventing instruction bleeding.

---

## 2. The Architecture: Token-Based v3 System

All prompt content is stored in Firestore as **tokens** — validated, immutable fragments of Groovy DSL. The `universal_agent_v1` blueprint defines the structural template with `{{CLASS_NAME}}` slot placeholders. At assembly time, `PromptAssemblyService` resolves which token goes into each slot using a 4-level priority chain (USER > ACCOUNT > AGENT > SYSTEM) and substitutes the placeholders.

**Result:** The assembled static template is a complete Groovy DSL class definition — identical in structure to the hand-written prompts of the v2 era, but assembled from composable, permission-controlled pieces.

**Resolution order (who can change what):**

- **USER**: Personality tokens — humor preset, archetype, voice, vibe. Users customize their Alek.
- **ACCOUNT**: Account-wide defaults (e.g., family-friendly humor for all users of an account).
- **AGENT**: Agent-specific behavior — cognitive process, output format. E.g., router gets `COGNITIVE_PROCESS_ROUTER`.
- **SYSTEM**: Immutable base — policies, protocols, directives. Cannot be overridden by any user.

See: [Prompt Design System v3](../05_building_blocks/prompt_design_system_v3/README.md)

---

## 3. Syntax Guide & Examples

### A. Defining the Agent

Instead of: *"You are a helpful assistant named Alek..."*
Use:

```groovy
class Alek extends Agent {
  // Inherits and extends the base Agent contract

  archetype {
    role: "Intellectual Sniper"
    baseline: "Precision over volume. One sharp observation > five blunt ones."
  }
}
```

### B. Injecting Knowledge

Instead of: *"Here is some context about the user..."*
Use:

```groovy
  knowledge_base {
    biographical_context: '''
      **Biographical**
      - Born in Kyiv (Jan 01, 2000)
      - Software engineer (Feb 10, 2025)
    '''
  }
```

**Note:** The `knowledge_base` block is appended by code at runtime — it is not part of the static blueprint template. It appears only when content is non-empty.

### C. Defining Protocols (Agent Behaviors)

Instead of: *"When you search memory, you must first..."*
Use:

```groovy
  protocols {
    memory_search {
      trigger: "questions about facts, events, preferences related to user"
      steps: [
        "1. IDENTIFY: What specific information is needed?",
        "2. INVOKE: search_memory(query=..., limit=5)",
        "3. VERIFY: Does the result answer the question?",
        "4. DELIVER: Integrate findings into response"
      ]
    }
  }
```

---

## 4. The "Cognitive Process" Block

This is the most critical component for reasoning. By defining a `cognitive_process` block, we force the LLM to:

1. **Plan** before acting.
2. **Follow** a strict sequence.
3. **Self-Correct** (via Verify/Analyze steps).

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

## 5. Assembled Prompt Structure

The final system instruction sent to the LLM has two parts separated by the cache boundary marker:

**Static prefix** (cached by Anthropic for ~5 min):
```groovy
class Alek extends Agent {

  personality {
    archetype { ... }     // ← token ARCHETYPE_INTELLECTUAL_SNIPER
    vibe { ... }          // ← token VIBE_BATTLE_WEARY
    voice { ... }         // ← token VOICE_APHORISTIC
    humor_engine { ... }  // ← token HUMOR_PRESET_RANEVSKAYA (or user override)
  }

  knowledge_base {
    {{FEW_SHOT_EXAMPLES_DEFAULT}}  // ← resolved at assembly time
  }

  policies { ... }        // ← 6 policy tokens
  protocols { ... }       // ← 2 protocol tokens
  cognitive_process { ... }
  output_format { ... }

}

knowledge_base {
  biographical_context: '''
    **Biographical**
    - ...
  '''

  conversation_history: '''   // ← consolidation agent only
    ...
  '''
}
```

**Dynamic suffix** (sent fresh every request):
```groovy
<!-- CACHE_BOUNDARY -->
current_date_time {
    2026-02-25 14:32 Tuesday (UTC)
    System time is UTC. ...
}

query_specific_context: '''   // ← only when router found relevant facts
    **Query-Specific Context:**
    - User mentioned travel plans last week
'''
```

---

## 6. Implementation Checklist

When creating or modifying tokens:

1. **Inheritance First:** Tokens extend the Alek base class implicitly. Do not redefine personality in non-personality tokens.
2. **Methods, Not Rules:** Define behavioral protocols as named blocks with `steps: [...]`.
3. **Strict Scoping:** Use `{ ... }` to contain instructions. Instructions outside blocks are global.
4. **Comments:** Use `//` for context. The LLM reads comments as developer intent.
5. **No Variable Injection in Tokens:** Tokens are static text. Runtime data (bio, history, datetime) is injected by code after token substitution.
6. **Cognitive Processes:** Always include explicit step-by-step reasoning for complex protocols.

---

## 7. Why This is Better

- **Reduced Hallucinations:** The model treats the prompt as code to execute, not text to improvise.
- **Semantic Clarity:** Separates *Identity* (personality tokens) from *Function* (cognitive process, protocols).
- **Modularity:** Tokens can be swapped independently via 4-level profile resolution.
- **Testability:** Tokens are versioned, validated at creation, and cached in-memory.
- **Cacheability:** Purely static token content enables `cache_control: ephemeral` on Anthropic's API — the 5k-token static prefix is cached for 5 minutes.

---

## 8. Real-World Examples

### Example 1: Router Agent Triage

**Before (Natural Language):**
```
You are a router that classifies user queries. Look at the query and decide
if it's simple or complex. Simple queries go to quick_response, complex go
to smart_response.
```

**After (Groovy DSL token `COGNITIVE_PROCESS_ROUTER`):**

```groovy
cognitive_process {
  steps: [
    "1. ANALYZE complexity (scale 1-10)",
    "2. ANALYZE tone (casual/professional/technical/urgent)",
    "3. CLASSIFY query type (simple/personal/external)",
    "4. DECIDE routing target based on complexity threshold (≤5 → quick, >5 → smart)"
  ]
  complexity_factors: [
    "Query length and structure",
    "Ambiguity and context requirements",
    "Need for external data or tools",
    "Multi-step reasoning required"
  ]
}
```

### Example 2: Consolidation Agent

**After (Groovy DSL token `COGNITIVE_PROCESS_CONSOLIDATION`):**

```groovy
cognitive_process {
  steps: [
    "1. LOAD biographical context for deduplication",
    "2. ANALYZE messages for factual content",
    "3. EXTRACT facts (objective) and anchors (subjective principles)",
    "4. DEDUPLICATE via vector similarity check",
    "5. STRUCTURE output as JSON with embeddings"
  ]
  fact_criteria: {
    objective: "Verifiable statements about reality",
    temporal: "Include explicit timestamps when available",
    atomic: "One fact per entity, no compound statements"
  }
}
```

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

## 10. FORMAT-Before-DRAFT: Output Format Decision Pattern

### The Problem

A common mistake in `cognitive_process` design is placing the visual/format decision
**after** the draft step:

```groovy
// ❌ Anti-pattern: DRAFT → VISUAL (format decision after writing)
cognitive_process {
  steps: [
    "1. ORIENT: ...",
    "2. INTENT: ...",
    "3. DRAFT: Write the answer.",
    "4. VISUAL: Should I add a card?",  // Too late — model already committed to text
    "5. OUTPUT: ..."
  ]
}
```

**Result:** The model treats the card as an optional afterthought. It has already
framed the response as text and rarely switches formats retroactively. For structured
data types (weather, prices, comparisons), the card becomes the exception instead of
the norm.

### The Solution: FORMAT Locks In Before DRAFT

```groovy
// ✅ Correct: FORMAT → DRAFT (format decision before writing)
cognitive_process {
  steps: [
    "1. ORIENT: ...",
    "2. INTENT: ...",
    "3. FORMAT: Before writing anything — decide the delivery format.
       Defaults: weather → html_card. Prices/rates → html_card.
       Multi-day or multi-item data → html_card.
       Conversational reply, opinion, advice, single fact → plain text.
       If the user explicitly asked for text — respect that.
       This decision locks in before drafting.",
    "4. DRAFT: Write full_response AND HTML simultaneously (if FORMAT decided html_card).",
    "5. OUTPUT: ..."
  ]
}
```

**Why this works:**
- The model decides format as a first-class step with concrete defaults
- Defaults are written as domain rules (`weather → html_card`), not as qualities
  (`would a card help?`) — which the model tends to answer "no" for simple cases
- Writing both `full_response` and `html` in one DRAFT step treats them as co-primary,
  not primary + optional

### When to Use Pattern Defaults vs. Pattern Questions

| Approach | Use when |
|---|---|
| `weather → html_card` (domain rule) | You want consistent behavior for a specific data type |
| `"Would a card make this better?"` (quality question) | You want the model to exercise judgment on novel cases |
| Mixed: defaults + override | Most real cognitive processes — rules for known types, judgment for edge cases |

**Implemented in:**
- `firestore_utils/uploads/COGNITIVE_PROCESS_SMART.groovy` — step 4 FORMAT
- `firestore_utils/uploads/COGNITIVE_PROCESS_QUICK.groovy` — step 3 FORMAT (no tools step)

---

**Last Updated:** 2026-02-26
**Status:** ✅ Current (v3 Token System + FORMAT-before-DRAFT pattern)
