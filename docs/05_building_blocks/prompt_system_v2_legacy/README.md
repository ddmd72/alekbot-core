# ⚠️ LEGACY: Prompt Component System v2

**Status:** 🔴 DEPRECATED  
**Replaced By:** [Prompt Design System v3](../prompt_design_system_v3/README.md)  
**Last Production Use:** 2026-02-01

This document describes the v2 component-based system.
New development should use v3 token-based architecture.

---

## 📖 HowTo: Using This Document

### Purpose

Describe the dynamic prompt assembly system (3-level resolution + Groovy DSL) used by all agents.

### When to Read

- **For AI Agents:** Before changing prompt assembly or user overrides.
- **For Developers:** When adding or debugging prompt components.

### When to Update

This document MUST be updated when:

- [ ] Component scopes or templates change.
- [ ] Assembly pipeline changes (Groovy → Markdown, cache behavior).
- [ ] New component repositories or storage formats are introduced.
- [ ] Priority resolution levels change (USER > ACCOUNT > AGENT > SYSTEM).
- [ ] New RFCs propose architectural changes to prompt system.

### Cross-References

- **Target Architecture:** [../../04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md](../../04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md)
- **Prompt Components Guide:** [../../guides/PROMPT_COMPONENTS_GUIDE.md](../../guides/PROMPT_COMPONENTS_GUIDE.md)
- **Provider Resolution:** [../provider_resolution/README.md](../provider_resolution/README.md)
- **Search Enrichment:** [../search_enrichment/README.md](../search_enrichment/README.md)

---

## 1. Overview

The Prompt Component System assembles LLM system prompts dynamically from modular Groovy components.
It replaces monolithic templates with a **4-level priority system** (USER > ACCOUNT > AGENT > SYSTEM), enabling
per-user overrides, account-level customization, per-agent specialization, while keeping a consistent system kernel.

**Current Status:** Production with 4-level resolution (SESSION_26, 2026-02-01).

**Future Evolution:** See RFCs for next-generation token-based architecture (v3):

- [PROMPT_NEXT_GEN_RFC.md](../../10_rfcs/PROMPT_NEXT_GEN_RFC.md) - Initial Pydantic approach (v1)
- [PROMPT_FLEXIBLE_SCHEMA_RFC.md](../../10_rfcs/PROMPT_FLEXIBLE_SCHEMA_RFC.md) - Flexible JSON validation (v2)
- [PROMPT_DESIGN_SYSTEM_RFC.md](../../10_rfcs/PROMPT_DESIGN_SYSTEM_RFC.md) - Token-based design system (v3, recommended)
- [PROMPT_DESIGN_SYSTEM_IMPLEMENTATION_PLAN.md](../../10_rfcs/PROMPT_DESIGN_SYSTEM_IMPLEMENTATION_PLAN.md) - Implementation roadmap

## 2. Core Architecture

### 2.1 Resolution Flow

- Components are resolved in `PromptComponentService` using `PromptComponentRepository.resolve_component()`.
- **Priority:** USER > ACCOUNT > AGENT > SYSTEM (4-level resolution)
  - USER: Highest priority, user-specific overrides
  - ACCOUNT: Account-level customization (multi-tenant support)
  - AGENT: Agent-specific defaults (e.g., smart vs quick)
  - SYSTEM: Global fallback defaults
- Components are filtered by template scopes and sorted by `order`.
- Empty text = fallthrough to next level; `is_enabled=false` = exclude component entirely.

### 2.2 Assembly Flow

- `GroovyPromptAssembler` groups components by scope and assembles a Groovy DSL class.
- The output is validated (balanced braces, class Alek existence).

## 3. Domain Model

- `PromptComponent` (id, scope, order, owner_type, owner_value, is_enabled)
- `PromptTemplate` (scopes, supports_tools)
- `OwnerType`: SYSTEM / AGENT / ACCOUNT / USER (4 levels, highest priority wins)

## 4. Templates & Scopes

Templates live in `src/domain/prompt.py`:

- `TEMPLATE_LIGHT` (Quick)
- `TEMPLATE_FULL` (Smart)
- `TEMPLATE_ROUTER`, `TEMPLATE_WEBSEARCH`, `TEMPLATE_CONSOLIDATION`

Scopes map to Groovy sections:

- CLASS_ROOT, CLASS_PROPERTIES, CLASS_POLICIES
- CLASS_KNOWLEDGE_BASE, CLASS_PROTOCOLS, CLASS_RUNTIME_RULES

## 5. Runtime Injection

`PromptBuilder` injects runtime context after assembly:

- Biographical context
- Enriched search context
- Tone instructions
- Current time

## 6. Storage & Caching

- Components stored in Firestore via `PromptComponentRepository`.
- `PromptComponentService` caches assembled prompts per template/agent/user.
- `PromptBuilder` caches biographical context until invalidation.

## 7. Provider Adaptation

For Claude, `GroovyToMarkdownTransformer` can transform the Groovy prompt into Markdown
without changing the component system.

## 8. Testing & Validation

### 8.1 Integration Tests

End-to-end tests validate 4-level resolution with real Firestore:

- **test_prompt_4level_e2e.py** - Real chain: Firestore → Repository → Service → Builder
  - Scenario 1: SYSTEM + ACCOUNT → ACCOUNT wins
  - Scenario 2: SYSTEM + USER → USER wins
  - Scenario 3: ALL levels → USER wins (highest priority)
- **test_prompt_4level_resolution.py** - Unit tests for `resolve_component()` logic

**Test Components:** Located in `ai_templates/components/account/` and `ai_templates/components/user/`

**Run tests:**

```bash
pytest tests/integration/test_prompt_4level_e2e.py -v -s
```

See `/tests/integration/README_PROMPT_4LEVEL_TESTS.md` for details.

### 8.2 Known Limitations

**Security Backdoor (Deferred to v3):**

- USER/ACCOUNT can override AGENT instructions (not just properties)
- Risk: User could modify agent behavior via prompt injection
- Mitigation: Deferred to RFC v3 (token-based system with immutable slots)
- Current workaround: Trust-based system, no UI for editing raw components

## 9. Code References

- `src/domain/prompt.py`
- `src/services/prompt_component_service.py`
- `src/services/prompt_builder.py`
- `src/adapters/groovy_prompt_assembler.py`

## 10. Status

**Production Ready** - 4-level resolution (USER > ACCOUNT > AGENT > SYSTEM) enabled by default.

**Last Updated:** 2026-02-01 (SESSION_26)
**Status:** ✅ Complete - Multi-tenant support with account-level overrides
