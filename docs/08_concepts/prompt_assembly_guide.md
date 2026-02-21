# Prompt Assembly Guide

**Status:** ✅ Active
**Last Updated:** 2026-02-01
**Session:** 25 (Prompt Component Refactoring)

## Overview

This document explains how prompts are assembled in Alek Core using the **component-based architecture** with a **3-level hierarchy** (SYSTEM → AGENT → USER).

## Table of Contents

1. [Core Principles](#core-principles)
2. [Architecture Overview](#architecture-overview)
3. [Component Hierarchy](#component-hierarchy)
4. [Assembly Flow](#assembly-flow)
5. [Agent Patterns](#agent-patterns)
6. [File Structure](#file-structure)
7. [Code Examples](#code-examples)
8. [Troubleshooting](#troubleshooting)

---

## Core Principles

### 1. Prompts as Code

Prompts are treated as **code artifacts** using Groovy DSL syntax:

- Stored in version-controlled files (`ai_templates/components/`)
- Assembled from reusable components
- Follow structured hierarchy (properties → policies → cognitive_process → knowledge_base → protocols → runtime_rules)

### 2. Component-Based Assembly

Instead of monolithic prompt files, prompts are built from **6 component types**:

| Component           | Scope                       | Purpose                       |
| ------------------- | --------------------------- | ----------------------------- |
| `properties`        | `class.Alek.properties`     | Identity & personality        |
| `policies`          | `class.Alek.policies`       | Core rules & constraints      |
| `cognitive_process` | `class.Alek`                | Reasoning engine (root block) |
| `few_shot_examples` | `class.Alek.knowledge_base` | Example interactions          |
| `protocols`         | `class.Alek.protocols`      | Tool usage instructions       |
| `runtime_rules`     | `class.Alek.runtime_rules`  | Platform-specific formatting  |

### 3. Three-Level Hierarchy

Components are resolved with priority:

```
USER (highest priority)
  ↓ (override)
AGENT
  ↓ (override)
SYSTEM (base/fallback)
```

**Example:**

- `cognitive_process` for **smart** agent:
  - Check `ai_templates/components/user/{user_id}/cognitive_process.groovy` ❌ (not found)
  - Check `ai_templates/components/agent/smart/cognitive_process.groovy` ✅ **FOUND** (use this)
  - Fallback to `ai_templates/components/system/cognitive_process.groovy` (skipped, already found)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│ Agent (SmartAgent, QuickAgent, ConsolidationAgent)         │
│ └─ _build_system_prompt()                                   │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│ PromptBuilder                                               │
│ └─ build_for_agent(agent_type, user_id, ...)               │
│    ├─ Loads template (TEMPLATE_FULL, TEMPLATE_LIGHT)       │
│    └─ Calls component_service.get_assembled_prompt()       │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│ PromptComponentService                                      │
│ └─ get_assembled_prompt(template, agent_type, user_id)     │
│    ├─ Resolves components (3-level hierarchy)              │
│    └─ Calls assembler.assemble(template, components)       │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│ GroovyPromptAssembler                                       │
│ └─ assemble(template, components)                           │
│    ├─ Follows template.scopes order                        │
│    ├─ Builds Groovy DSL structure                          │
│    └─ Returns assembled string                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Component Hierarchy

### File Structure

```
ai_templates/
├── manifest.yaml                    # Component definitions
└── components/
    ├── system/                      # SYSTEM level (base)
    │   ├── cognitive_process.groovy
    │   ├── properties.groovy
    │   ├── policies.groovy
    │   ├── few_shot_examples.groovy
    │   ├── protocols.groovy
    │   └── runtime_rules.groovy
    │
    ├── agent/                       # AGENT level (overrides)
    │   ├── smart/
    │   │   └── cognitive_process.groovy  # Smart-specific reasoning
    │   ├── quick/
    │   │   └── cognitive_process.groovy  # Quick-specific reasoning
    │   ├── consolidation/
    │   │   ├── cognitive_process.groovy  # Consolidation-specific
    │   │   └── properties.exclude        # Disable properties for this agent
    │   └── ...
    │
    └── user/                        # USER level (per-user overrides)
        └── {user_id}/
            └── cognitive_process.groovy  # User-specific customization
```

### Component Resolution Logic

Implemented in `FirestorePromptComponentRepository.resolve_component()`:

```python
# 1. Try USER level (if user_id provided)
if user_id:
    component = await self._find_component(component_id, "USER", user_id)
    if component and component.is_enabled:
        return component  # ✅ Found at USER level

# 2. Try AGENT level
component = await self._find_component(component_id, "AGENT", agent_type)
if component and component.is_enabled:
    return component  # ✅ Found at AGENT level

# 3. Fallback to SYSTEM level
component = await self._find_component(component_id, "SYSTEM", None)
if component and component.is_enabled:
    return component  # ✅ Found at SYSTEM level

return None  # ❌ Not found at any level
```

### Exclusion Pattern

Use `.exclude` files to **disable** components at specific levels:

```bash
# Disable properties for consolidation agent
ai_templates/components/agent/consolidation/properties.exclude
```

This creates a component with `is_enabled: false`, preventing fallback to SYSTEM level.

---

## Assembly Flow

### Step-by-Step Process

#### 1. Agent Calls PromptBuilder

```python
# src/agents/core/smart_response_agent.py
async def _build_system_prompt(self, routing_metadata, semantic_context):
    return await self.prompt_builder.build_for_agent(
        agent_type="smart",
        user_id=self.user_id,
        routing_metadata=routing_metadata,
        semantic_context=semantic_context,
        capabilities=self.execution_context.capabilities
    )
```

#### 2. PromptBuilder Selects Template

```python
# src/services/prompt_builder.py
async def build_for_agent(self, agent_type, user_id, ...):
    # Select template based on agent type
    if agent_type == "quick":
        template = TEMPLATE_LIGHT
    elif agent_type == "smart":
        template = TEMPLATE_FULL
    elif agent_type == "consolidation":
        template = TEMPLATE_CONSOLIDATION

    # Get assembled prompt from component service
    return await self._build_with_component_service(
        agent_type=agent_type,
        user_id=user_id,
        template=template,
        ...
    )
```

#### 3. PromptComponentService Resolves Components

```python
# src/services/prompt_component_service.py
async def get_assembled_prompt(self, template, agent_type, user_id):
    components = []

    # Resolve each component in template
    for scope in template.scopes:
        component_id = self._scope_to_component_id(scope)
        component = await self.repository.resolve_component(
            component_id=component_id,
            agent_type=agent_type,
            user_id=user_id
        )
        if component:
            components.append(component)

    # Assemble into final prompt
    return await self.assembler.assemble(template, components)
```

#### 4. GroovyPromptAssembler Builds Groovy DSL

```python
# src/adapters/groovy_prompt_assembler.py
def assemble(self, template, components):
    prompt_parts = [
        "// Generated prompt using template: {template.name}",
        "",
        "class Alek {",
    ]

    # Iterate through template.scopes in order
    for scope in template.scopes:
        scope_components = [c for c in components if c.scope == scope]

        if scope == ComponentScope.CLASS_PROPERTIES:
            prompt_parts.append("    properties {")
            prompt_parts.append("        " + component.content)
            prompt_parts.append("    }")
        elif scope == ComponentScope.CLASS_POLICIES:
            prompt_parts.append("    policies {")
            # ...

    prompt_parts.append("}")
    return "\n".join(prompt_parts)
```

### Template Definitions

Located in `src/domain/prompt.py`:

```python
TEMPLATE_LIGHT = PromptTemplate(
    name="light",
    extends="base",
    scopes=[
        ComponentScope.CLASS_PROPERTIES,       # 1. properties
        ComponentScope.CLASS_POLICIES,         # 2. policies
        ComponentScope.CLASS_ROOT,             # 3. cognitive_process
        ComponentScope.CLASS_KNOWLEDGE_BASE,   # 4. few_shot_examples (if needed)
        ComponentScope.CLASS_PROTOCOLS,        # 5. protocols
        ComponentScope.CLASS_RUNTIME_RULES,    # 6. runtime_rules
    ]
)

TEMPLATE_FULL = PromptTemplate(
    name="full",
    extends="base",
    scopes=[
        ComponentScope.CLASS_PROPERTIES,       # Same structure
        ComponentScope.CLASS_POLICIES,
        ComponentScope.CLASS_ROOT,
        ComponentScope.CLASS_KNOWLEDGE_BASE,
        ComponentScope.CLASS_PROTOCOLS,
        ComponentScope.CLASS_RUNTIME_RULES,
    ]
)

TEMPLATE_CONSOLIDATION = PromptTemplate(
    name="consolidation",
    extends="base",
    scopes=[
        ComponentScope.CLASS_ROOT,  # Only cognitive_process needed
    ]
)
```

---

## Agent Patterns

### Pattern 1: Conversational Agents (Smart, Quick)

**Characteristics:**

- Real-time conversation
- History passed as `messages` parameter to LLM
- System prompt is **constant** (no conversation in prompt)

**Implementation:**

```python
# Agent calls LLM
request = LLMRequest(
    model_name=self.model_name,
    system_instruction=system_prompt,  # ← Prompt WITHOUT history
    messages=debug_history,             # ← Conversation history
    tools=tool_declarations,
    temperature=0.7
)
```

**Key Files:**

- `src/agents/core/smart_response_agent.py`
- `src/agents/core/quick_response_agent.py`

**Templates Used:**

- SmartAgent: `TEMPLATE_FULL`
- QuickAgent: `TEMPLATE_LIGHT`

---

### Pattern 2: Document Analysis Agent (Consolidation)

**Characteristics:**

- Batch processing of old messages
- Analyzes conversation as **document**
- History **injected into system prompt**

**Implementation:**

```python
# Assemble base prompt from components
template = await component_service.get_assembled_prompt(
    template=TEMPLATE_CONSOLIDATION,
    agent_type="consolidation",
    user_id=None
)

# Inject conversation into prompt
final_prompt = template.replace("{CONVERSATION_INPUT}", conv_text) \
                       .replace("{EXISTING_ANCHORS}", anchors) \
                       .replace("{BIOGRAPHICAL_CONTEXT}", bio_context)

# Call LLM with conversation in system prompt
request = LLMRequest(
    model_name=self.model_name,
    system_instruction=final_prompt,  # ← Prompt WITH history
    messages=[],                       # ← Empty (everything in system)
    temperature=0.0
)
```

**Key Files:**

- `src/agents/consolidation_agent.py`
- `scripts/prompt/inspect_real_consolidation_prompt.py`

**Template Used:**

- `TEMPLATE_CONSOLIDATION` (only `cognitive_process`)

---

### Pattern 3: PromptBuilder Agent (Router)

**Characteristics:**

- Prompt assembled via `PromptBuilderPort` (v3 Token System)
- Cached after first load — no repeated Firestore reads
- No file-based prompt loading; all content lives in Firestore

**Implementation:**

```python
async def _load_triage_prompt(self, message: AgentMessage) -> str:
    if self._cached_triage_prompt is None:
        if not self.prompt_builder:
            raise RuntimeError("RouterAgent requires prompt_builder for LLM triage")
        account_id = message.context.get("account_id")
        self._cached_triage_prompt = await self.prompt_builder.build_for_agent(
            agent_type="router",
            user_id=self.user_id,
            account_id=account_id,
            routing_metadata=None
        )
    return self._cached_triage_prompt
```

**Key Files:**

- `src/agents/core/router_agent.py`
- `src/ports/prompt_builder_port.py`

---

## File Structure

### Key Files

| File                                                  | Purpose                                    |
| ----------------------------------------------------- | ------------------------------------------ |
| `src/services/prompt_builder.py`                      | Orchestrates prompt building               |
| `src/services/prompt_component_service.py`            | Resolves components with 3-level hierarchy |
| `src/adapters/groovy_prompt_assembler.py`             | Assembles Groovy DSL from components       |
| `src/adapters/firestore_prompt_repository.py`         | Loads components from Firestore            |
| `src/domain/prompt.py`                                | Template definitions                       |
| `ai_templates/manifest.yaml`                          | Component metadata (scope, order)          |
| `ai_templates/components/`                            | Component source files                     |
| `scripts/prompt/sync_components.py`                   | Sync components to Firestore               |
| `scripts/prompt/inspect_smart_prompt.py`              | Inspect SmartAgent prompt                  |
| `scripts/prompt/inspect_quick_prompt.py`              | Inspect QuickAgent prompt                  |
| `scripts/prompt/inspect_real_consolidation_prompt.py` | Inspect ConsolidationAgent prompt          |

### Agent Files

| Agent              | File                                      | Pattern           |
| ------------------ | ----------------------------------------- | ----------------- |
| SmartAgent         | `src/agents/core/smart_response_agent.py` | Conversational    |
| QuickAgent         | `src/agents/core/quick_response_agent.py` | Conversational    |
| ConsolidationAgent | `src/agents/consolidation_agent.py`       | Document Analysis |
| RouterAgent        | `src/agents/core/router_agent.py`         | Static Prompt     |
| WebSearchAgent     | `src/agents/web_search_agent.py`          | Static Prompt     |

---

## Code Examples

### Example 1: Syncing Components to Firestore

```bash
# Sync all components (SYSTEM + agents) to development
make sync-components-dev

# Sync only SYSTEM components
make sync-components-system-dev

# Sync specific agent
make sync-components-agent-dev AGENT=smart

# Dry-run to see what would be uploaded
make sync-components-dry-run
```

### Example 2: Inspecting Assembled Prompts

```bash
# Inspect SmartAgent prompt for dev user
make inspect-smart-dev

# Inspect QuickAgent prompt for dev user
make inspect-quick-dev

# Inspect ConsolidationAgent prompt for dev user
make inspect-console-dev
```

Reports are saved to `reports/prompt/{date}-{agent}-{user_id}-{time}.md`.

### Example 3: Adding a New Component

1. **Create component file:**

```bash
# Add new component at SYSTEM level
echo "critical_thinking { /* ... */ }" > ai_templates/components/system/critical_thinking.groovy
```

2. **Update manifest:**

```yaml
# ai_templates/manifest.yaml
components:
  critical_thinking:
    scope: "class.Alek.policies"
    order: 35
    description: "Critical thinking protocols"
```

3. **Sync to Firestore:**

```bash
make sync-components-dev
```

4. **Update template (if needed):**

```python
# src/domain/prompt.py
# Add to template.scopes if it's a new scope type
```

### Example 4: Creating Agent-Specific Override

```bash
# Create override for smart agent
mkdir -p ai_templates/components/agent/smart
echo "properties { /* smart-specific properties */ }" > ai_templates/components/agent/smart/properties.groovy

# Sync
make sync-components-agent-dev AGENT=smart
```

### Example 5: Disabling Component for Agent

```bash
# Disable few_shot_examples for quick agent
touch ai_templates/components/agent/quick/few_shot_examples.exclude

# Sync
make sync-components-agent-dev AGENT=quick
```

---

## Troubleshooting

### Issue: Components not loading

**Symptoms:**

- Empty sections in assembled prompt
- Missing `properties`, `policies`, etc.

**Solution:**

1. Check components exist in Firestore:

   ```bash
   # Query Firestore to verify components uploaded
   ```

2. Verify component_service initialized:

   ```python
   # Agent must pass component_service to PromptBuilder
   prompt_builder = PromptBuilder(repo, component_service=component_service)
   ```

3. Check agent uses `build_for_agent()`:

   ```python
   # ✅ Correct
   await self.prompt_builder.build_for_agent(agent_type="smart", ...)

   # ❌ Legacy (don't use)
   await self.prompt_builder.build_system_prompt(mode="full", ...)
   ```

### Issue: Duplicate datetime

**Symptoms:**

- "Current date and time" appears twice in prompt

**Solution:**
PromptBuilder already adds datetime. Agents should NOT add it:

```python
# ✅ Correct
async def _build_system_prompt(self, ...):
    return await self.prompt_builder.build_for_agent(...)

# ❌ Wrong
async def _build_system_prompt(self, ...):
    system_prompt = await self.prompt_builder.build_for_agent(...)
    current_time = datetime.now(...)
    return f"Current date and time is {current_time}.\n\n{system_prompt}"  # ❌ Duplicate!
```

### Issue: Wrong component order

**Symptoms:**

- Components appear in wrong order in assembled prompt

**Solution:**
Order is defined by `template.scopes`. Components follow this order:

1. `properties`
2. `policies`
3. `cognitive_process`
4. `few_shot_examples` (knowledge_base)
5. `protocols`
6. `runtime_rules`

To change order, update template in `src/domain/prompt.py`:

```python
TEMPLATE_FULL = PromptTemplate(
    name="full",
    extends="base",
    scopes=[
        ComponentScope.CLASS_PROPERTIES,      # Order defined here
        ComponentScope.CLASS_POLICIES,
        ComponentScope.CLASS_ROOT,            # cognitive_process
        ComponentScope.CLASS_KNOWLEDGE_BASE,  # few_shot_examples
        ComponentScope.CLASS_PROTOCOLS,
        ComponentScope.CLASS_RUNTIME_RULES,
    ]
)
```

### Issue: Agent-specific component not loading

**Symptoms:**

- Agent uses SYSTEM component instead of agent-specific override

**Solution:**

1. Verify file exists:

   ```bash
   ls ai_templates/components/agent/smart/cognitive_process.groovy
   ```

2. Verify synced to Firestore:

   ```bash
   make sync-components-agent-dev AGENT=smart
   ```

3. Check Firestore collection name matches:
   ```python
   # In inspect script or agent factory
   collection_name=f"{env_config.firestore_collection_prefix}prompt_components"
   ```

---

## Summary

**Key Takeaways:**

1. **Prompts = Code**: Components in `ai_templates/`, version-controlled, assembled dynamically
2. **3-Level Hierarchy**: USER → AGENT → SYSTEM (with priority)
3. **Two Patterns**:
   - **Conversational** (Smart/Quick): system_instruction + messages
   - **Document Analysis** (Consolidation): everything in system_instruction
4. **Assembly Chain**: Agent → PromptBuilder → PromptComponentService → GroovyPromptAssembler
5. **Templates Control Structure**: Order and included components defined in `src/domain/prompt.py`

**Next Steps:**

- Read [Groovy Prompt Pattern](./groovy_prompt_pattern.md) for DSL details
- Original design documented in legacy RFC (archived)
- Implementation notes in legacy session log (archived)
