# Prompt Design System v3 (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the token-based prompt assembly system that replaced free-form components with secure, validated templates.

### When to Read

- **For AI Agents:** Before modifying prompt assembly, token library, or profile resolution.
- **For Developers:** When adding tokens, customizing agent prompts, or debugging assembly flow.

### When to Update

This document MUST be updated when:

- [ ] Token library structure or categories change.
- [ ] Blueprint schema or template syntax is modified.
- [ ] Profile resolution logic (4-level priority) changes.
- [ ] The assembly caching strategy or TTL is adjusted.
- [ ] New section types (TOKENIZED, STATIC, RUNTIME) are added.

### Cross-References

- **Complete Guide:** [../../08_concepts/prompt_v3_complete_guide.md](../../08_concepts/prompt_v3_complete_guide.md)
- **Security Validation:** [../security_validation/README.md](../security_validation/README.md)
- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md)

---

## 1. Overview

**Prompt Design System v3** implements a **token-based architecture** where prompts are assembled from pre-approved, validated fragments (tokens) rather than free-form text. This eliminates prompt injection vulnerabilities while enabling granular user customization.

**Core Principle:** Users never inject raw text into system instructions. They only select from a **whitelisted library of tokens**.

---

## 2. Core Components

### 2.1 Tokens

Immutable, pre-approved prompt fragments validated at creation time.

- **Categories:** `humor_engine`, `archetype`, `voice`, `response_style`, `vibe`.
- **Validation:** Every token must pass `SecurityPort` checks before being saved to the library.

### 2.2 Blueprints

Templates defining the structure of an agent's prompt.

- **Universal Blueprint:** Alek-Core uses a single `universal_agent_v1` blueprint for all agents.
- **Placeholders:** Uses `{{TOKENIZED}}` for library tokens and `[[RUNTIME]]` for dynamic data.

### 2.3 Profile Slots

Unified entries that define token assignments at different levels.

- **4-Level Resolution:** `USER` > `ACCOUNT` > `AGENT` > `SYSTEM`.
- **Immutability:** Slots can be marked as `non_overridable` to prevent higher levels from changing them.

---

## 3. Assembly Process

The `PromptAssemblyService` orchestrates the creation of the final prompt.

### 3.1 Static Template Assembly

1. **Profile Resolution:** Loads all 4 profile levels in parallel using `asyncio.gather`.
2. **Slot Mapping:** Merges slots based on priority and immutability rules.
3. **Token Fetching:** Retrieves all assigned tokens in parallel.
4. **Replacement:** Replaces `{{CLASS_NAME}}` placeholders with token content.

### 3.2 Runtime Context Injection

Dynamic data is injected at request time:

1. **Formatting:** `BiographicalFactsFormatter` and `ContextFormatter` prepare the raw data.
2. **Validation:** All runtime data is treated as `UNTRUSTED` and validated via `SecurityPort`.
3. **Injection:** Validated text is placed into `[[BIOGRAPHICAL_CONTEXT]]` and `[[CONVERSATION_HISTORY]]`.

---

## 4. Performance Optimizations

### 4.1 Assembly Cache

To minimize Firestore reads and LLM latency, the service caches the **static template** (steps 1-4 above).

- **TTL:** 24 hours.
- **Key:** `prompt:{agent_type}:acc:{account_id}:usr:{user_id}`.
- **Impact:** 20x speedup on cache hits (110ms → 5ms).

### 4.2 Parallel Execution

All repository calls (profiles, tokens) are parallelized using `asyncio.gather`, reducing cold-start latency by 4-9x.

### 4.3 Cache Management

- **Preloading:** `UserAgentFactory` warms up the cache during agent initialization.
- **Invalidation:** Admin command `$admin_cache_reset` clears the entire assembly cache for debugging.

---

## 5. Code References

- `src/services/prompt_v3/prompt_assembly_service.py`: Main orchestrator.
- `src/domain/prompt_v3/`: Domain models (Token, Blueprint, ProfileSlot).
- `src/ports/prompt_v3/`: Repository interfaces.
- `src/services/prompt_v3/biographical_formatter.py`: Context formatting.
- `src/services/prompt_v3/context_formatter.py`: History formatting.

---

## 6. Status & Roadmap

**Status:** ✅ Production Ready

### Planned Enhancements

- **Per-Agent Blueprints:** Allow different structures for Router vs. Smart agents.
- **Token Versioning:** Support gradual rollout of new personality traits.
- **User Token Creation:** Enable advanced users to submit custom tokens for admin approval.

---

**Last Updated:** 2026-02-10  
**Status:** ✅ Complete  
**Phase:** Documentation Audit Phase 3.12
