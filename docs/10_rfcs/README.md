# RFCs (Request for Comments)

## 📖 Overview

This directory contains Request for Comments (RFC) documents that describe proposed architectural changes, new features, and major refactorings.

### RFC Lifecycle

1. **PROPOSED:** Initial draft for discussion.
2. **ACCEPTED:** Approved for implementation.
3. **IMPLEMENTED:** Feature is live in production.
4. **OBSOLETE:** Superseded by a newer RFC or abandoned.

---

## 🚀 Active RFCs

| RFC | Status | Description |
| --- | ------ | ----------- |
| [ADAPTIVE_ROUTING_CACHE](./ADAPTIVE_ROUTING_CACHE_RFC.md) | Partial | Router-centric enrichment + dedup implemented. Cache §8 superseded by HEXAGONAL_PROMPT_CACHING. |
| [ACP_V2_SIMPLIFIED](./ACP_V2_SIMPLIFIED_RFC.md) | Partial | Agent registry + sync/async delegation modes — implemented. |
| [ACP_V2_AGENT_COMMUNICATION](./ACP_V2_AGENT_COMMUNICATION_RFC.md) | Superseded | Complex version; superseded by ACP_V2_SIMPLIFIED. |
| [DELIBERATE_FACT_MANAGEMENT](./DELIBERATE_FACT_MANAGEMENT_RFC.md) | Implemented | Fact taxonomy, ConsolidationAgent deliberation — in production. |
| [EXECUTION_CONTEXT_HEXAGONAL](./EXECUTION_CONTEXT_HEXAGONAL_RFC.md) | Postponed | Domain-level ExecutionContext value object. Postponed — 2-3 params still manageable. Note: `AgentExecutionContext` (LLM provider context) IS implemented separately. |
| [EXTENDED_WEB_SEARCH](./EXTENDED_WEB_SEARCH_RFC.md) | Rejected | Multi-turn loop approach; single Gemini call sufficient. |
| [GMAIL_EMAIL_INDEXING](./GMAIL_EMAIL_INDEXING_RFC.md) | Phase 1–2 ✅ Phase 3 🔄 | LLM-based email indexing, biographical_signal classification, ConsolidationAgent integration UAT validated. |
| [HEXAGONAL_PROMPT_CACHING](./HEXAGONAL_PROMPT_CACHING_RFC.md) | Implemented | Transparent prompt caching via CachingLLMProxy + CACHE_BOUNDARY prefix — in production. |
| [HTML_CARD_PLAYWRIGHT](./HTML_CARD_PLAYWRIGHT_RFC.md) | Implemented | HTML widget rendering via Playwright screenshots — in production. |
| [NATIVE_TOOLS_INTEGRATION](./NATIVE_TOOLS_INTEGRATION_RFC.md) | Partial | SearchEnrichmentService implemented. Quick delegation loop + WebSearchLightAgent added (session 7). |
| [PROMPT_BUILDER_V4](./PROMPT_BUILDER_V4_RFC.md) | Implemented | Blueprint + ProfileToken override system (4 levels). Code done; Firestore upload pending. |
| [RICH_CONTENT](./RICH_CONTENT_RFC.md) | Implemented V1 | File delivery (md/html/xlsx/docx) via SlackMediaAdapter — in production. |
| [TESTING_STRATEGY](./TESTING_STRATEGY_RFC.md) | Active | Testing framework and protocols. |
| [WEBSEARCH_STRUCTURED_OUTPUT](./WEBSEARCH_STRUCTURED_OUTPUT_RFC.md) | Draft | Structured JSON output for WebSearchAgent. |

---

## 🗄️ Archived RFCs

See [Archived RFCs](../archive/rfcs/) for implemented and obsolete proposals.

---

**Last Updated:** 2026-02-28
**Status:** ✅ Audited
