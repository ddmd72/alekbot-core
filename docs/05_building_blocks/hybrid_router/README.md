# Hybrid Router (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the intent classification and routing system that directs user queries to the most appropriate agents.

### When to Read

- **For AI Agents:** Before modifying routing rules, triage prompts, or intent classification logic.
- **For Developers:** When troubleshooting incorrect agent selection or tuning the triage performance.

### When to Update

This document MUST be updated when:

- [ ] The triage logic (rule-based vs LLM-based) changes.
- [ ] New routing categories or target agents are added.
- [ ] The triage prompt (managed via PromptBuilder v3 in Firestore) is modified.
- [ ] The complexity threshold for agent selection is adjusted.
- [ ] Integration with `SearchEnrichmentService` changes.

### Cross-References

- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md)
- **Search Enrichment:** [../search_enrichment/README.md](../search_enrichment/README.md)
- **Prompt Design System v3:** [../prompt_design_system_v3/README.md](../prompt_design_system_v3/README.md)

---

## 1. Overview

The **Hybrid Router** is the entry point for all user queries in the Alek-Core agent network. It analyzes incoming messages to determine their intent, complexity, and required tools, then routes them to either the `QuickResponseAgent` or the `SmartResponseAgent`.

**Core Principle:** Use the cheapest and fastest model possible for simple tasks, while reserving powerful models for complex reasoning.

---

## 2. Triage Mechanism

The router uses a hybrid approach combining fast rule-based checks with semantic LLM analysis.

### 2.1 Rule-Based Classification (Fast Path)

For common, unambiguous phrases, the router uses static keyword matching to avoid LLM latency.

- **Simple Phrases:** Greetings ("Hello"), acknowledgments ("Thanks"), and short confirmations.
- **Personal Keywords:** Detects "my", "mine" to flag personal data queries.
- **External Keywords:** Detects "weather", "news", "google" to flag web search needs.

### 2.2 LLM-Based Triage (Semantic Path)

For complex or ambiguous queries, the router uses a lightweight LLM (Gemini Flash, ECO tier) to perform deep classification.

- **Prompt:** assembled via `PromptBuilderPort` (Token System v3, stored in Firestore).
- **Output:** Structured JSON following a strict schema.
- **Vision Support:** If the message contains attachments (images), the router automatically increases the complexity score to ensure the `SmartResponseAgent` (with vision capabilities) is selected.

---

## 3. Routing Logic

### 3.1 Routing Metadata

The triage process produces `RoutingMetadata`, which includes:

- `complexity_score`: 1-10 scale.
- `confidence`: 0.0-1.0 score.
- `user_tone`: Detected tone (friendly, professional, etc.).
- `needs_tools`: List of required capabilities (memory, web, etc.).
- `semantic_lens`: Keywords for search enrichment.

### 3.2 Decision Rules

- **Complexity >= 6:** Route to `SmartResponseAgent`.
- **Confidence < 0.75:** Route to `SmartResponseAgent` (fallback for ambiguity).
- **Otherwise:** Route to `QuickResponseAgent`.

---

## 4. Search Enrichment Integration

The router doesn't just route; it prepares the context for the target agent.

1. **Keyword Extraction:** LLM triage extracts "semantic lenses" and search phrases.
2. **Enrichment:** Calls `SearchEnrichmentService` to perform parallel multi-vector searches.
3. **Context Injection:** The enriched context is attached to the `AgentMessage` sent to the target agent.

---

## 5. Code References

- `src/agents/core/router_agent.py`: Main implementation.
- `src/domain/tone.py`: Tone detection and metadata building.
- `src/ports/prompt_builder_port.py`: Port for triage prompt assembly.
- `src/services/search_enrichment_service.py`: Context enrichment.

---

## 6. Status & Roadmap

**Status:** ✅ Production Ready

### Planned Enhancements

- **Adaptive Thresholds:** Dynamically adjust the complexity threshold based on system load or user tier.
- **Multi-Turn Triage:** Use conversation history more effectively to resolve ambiguous follow-up questions.
- **Local Triage:** Explore small local models (e.g., BERT-based) for even faster rule-based classification.

---

**Last Updated:** 2026-02-10  
**Status:** ✅ Complete  
**Phase:** Documentation Audit Phase 3.3
