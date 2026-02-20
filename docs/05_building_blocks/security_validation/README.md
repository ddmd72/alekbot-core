# Security Validation (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the multi-layer security validation system that protects Alek-Core against prompt injection and malicious content.

### When to Read

- **For AI Agents:** Before implementing new validation logic, adding injection points, or modifying trust zones.
- **For Developers:** When troubleshooting blocked content, tuning regex patterns, or integrating security checks into new services.

### When to Update

This document MUST be updated when:

- [ ] The `SecurityPort` interface or `ValidationResult` schema changes.
- [ ] New validation adapters (e.g., LLM-based, External API) are added.
- [ ] Trust zone definitions or risk level thresholds are modified.
- [ ] New integration points (Layers) are introduced in the system.
- [ ] The pattern library in `RegexSecurityAdapter` is updated.

### Cross-References

- **Prompt Design System v3:** [../prompt_design_system_v3/README.md](../prompt_design_system_v3/README.md)
- **Security Validation Guide:** [../../08_concepts/security_validation_guide.md](../../08_concepts/security_validation_guide.md)
- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md)

---

## 1. Overview

The **Security Validation** system implements a **defense-in-depth** strategy to protect Alek-Core from both direct and indirect prompt injection attacks. It ensures that all untrusted data is sanitized or blocked before it can influence the behavior of the AI agents.

**Core Principle:** All user-provided and model-generated content is treated as **UNTRUSTED** by default.

---

## 2. Five-Layer Defense Strategy

The system validates data at every critical boundary:

1. **Layer 1: Token Creation:** `SecurityPort` validates prompt fragments before they are added to the library.
2. **Layer 2: Assignment Validation:** `Blueprint` enforces permissions on which tokens can be assigned to which slots.
3. **Layer 3: Runtime Injection:** Validates biographical context and conversation history before prompt assembly.
4. **Layer 4: Output Validation:** Validates model responses before they are stored in history or shown to the user.
5. **Layer 5: RAG Validation:** Validates facts retrieved from vector search before they enter the context window.

---

## 3. Architecture

### 3.1 SecurityPort (Port)

A domain-level interface that defines the contract for extensible validation.

- **Method:** `validate(text, context, zone)`
- **Result:** `ValidationResult` containing risk level, sanitized text, and detected patterns.

### 3.2 Trust Zones

- **TRUSTED:** System prompts and admin-vetted tokens. Skip validation.
- **SEMI_TRUSTED:** Facts stored in the database (already validated once). Moderate checks.
- **UNTRUSTED:** Raw user input and model-generated output. Full validation.

---

## 4. Validation Adapters

### 4.1 RegexSecurityAdapter (Production)

The primary MVP implementation using high-performance pattern matching.

- **CRITICAL:** Direct system overrides (e.g., `system: you must...`). **Action: BLOCK.**
- **HIGH:** Instruction manipulation (e.g., `ignore previous instructions`). **Action: BLOCK.**
- **MEDIUM:** Soft manipulation (e.g., `forget everything`). **Action: SANITIZE ([REDACTED]).**

### 4.2 CompositeAdapter (Production)

Aggregates multiple adapters with configurable strategies:

- **worst_case:** Highest risk level from any adapter wins (conservative).
- **majority_vote:** Requires agreement from most adapters.
- **all_pass:** Every adapter must mark the content as SAFE.

---

## 5. Integration Points

### 5.1 Prompt Assembly

The `PromptAssemblyService` calls the `SecurityPort` for every runtime variable injection, ensuring that malicious history or facts cannot hijack the agent's instructions.

### 5.2 Conversation Handling

The `ConversationHandler` uses `validate_model_output()` to check LLM responses. This prevents "indirect injection" where a model is tricked into generating malicious instructions that are then stored and used in future turns.

---

## 6. Code References

- `src/domain/prompt_v3/security.py`: Port and DTO definitions.
- `src/adapters/security/regex_adapter.py`: Pattern matching implementation.
- `src/adapters/security/composite_adapter.py`: Adapter aggregation logic.
- `src/services/prompt_v3/prompt_assembly_service.py`: Runtime validation integration.
- `src/handlers/conversation_handler.py`: Output validation integration.

---

## 7. Status & Roadmap

**Status:** ✅ Production Ready (Layers 1-4)

### Planned Enhancements

- **LLM Judge (Layer 6):** Use a specialized "Security Agent" for semantic risk assessment.
- **External API Integration:** Connect to services like Google Perspective or Azure Content Safety.
- **Validation Caching:** Cache results for identical content to reduce overhead.

---

**Last Updated:** 2026-02-10  
**Status:** ✅ Complete  
**Phase:** Documentation Audit Phase 3.13
