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
| [ACP_V2_SIMPLIFIED](./ACP_V2_SIMPLIFIED_RFC.md) | Implemented | Agent registry + sync/async delegation modes — in production. |
| [AGENT_WORKSPACE](./AGENT_WORKSPACE_RFC.md) | Proposed | Smart's own persistent working files (`projects/`, `references/`, `scratch/`, `files/`) in a dedicated versioned GCS bucket. Claude Code-style `workspace_*` local tools (read/write/edit/grep/glob + move/copy/delete/log/diff/restore), an index rendered from the listing into every prompt, writes only on interactive turns and only after the `workspace` system skill is loaded. |
| [DELIBERATE_FACT_MANAGEMENT](./DELIBERATE_FACT_MANAGEMENT_RFC.md) | Implemented | Fact taxonomy, ConsolidationAgent deliberation — in production. |
| [DRIVE_SEARCH_METRICS](./DRIVE_SEARCH_METRICS_RFC.md) | Proposed | User Drive search metrics: per-search events and per-turn facts in BigQuery (30 days), permanent re-aggregable day rows with versioned metric definitions, a pinned LLM judge with anchors, and a weekly build-vs-build Slack report. S0 baseline copy due before 2026-11-09. |
| [EXECUTION_CONTEXT_HEXAGONAL](./EXECUTION_CONTEXT_HEXAGONAL_RFC.md) | Postponed | Domain-level ExecutionContext value object. Postponed — 2-3 params still manageable. Note: `AgentExecutionContext` (LLM provider context) IS implemented separately. |
| [EXTENDED_WEB_SEARCH](./EXTENDED_WEB_SEARCH_RFC.md) | Rejected | Multi-turn loop approach; single Gemini call sufficient. |
| [GMAIL_EMAIL_INDEXING](./GMAIL_EMAIL_INDEXING_RFC.md) | Phase 1–2 ✅ Phase 3 🔄 | LLM-based email indexing, biographical_signal classification, ConsolidationAgent integration UAT validated. |
| [HEXAGONAL_PROMPT_CACHING](./HEXAGONAL_PROMPT_CACHING_RFC.md) | Implemented | Transparent prompt caching via CachingLLMProxy + CACHE_BOUNDARY prefix — in production. |
| [WIDGET_PLAYWRIGHT](./WIDGET_PLAYWRIGHT_RFC.md) | Implemented | HTML widget rendering via Playwright screenshots — in production. |
| [MEMORY_FACT_CONFIRMATION](./MEMORY_FACT_CONFIRMATION_RFC.md) | Accepted, implemented on `feat/memory-propose-facts` | Replaces the `save_to_memory` intent. Smart proposes facts read from a document with the local tool `propose_memory_facts`; the owner reads them in a file and pastes `$memory save <code>`, and the service appends them to the origin session as a user/model pair, so the consolidator reads them as the owner's words. Removes every writer of `consolidation_text`. |
| [NATIVE_TOOLS_INTEGRATION](./NATIVE_TOOLS_INTEGRATION_RFC.md) | Partial | SearchEnrichmentService implemented. Quick delegation loop + WebSearchLightAgent added (session 7). |
| [PROMPT_BUILDER_V4](./PROMPT_BUILDER_V4_RFC.md) | Implemented | Blueprint + ProfileToken override system (4 levels). Code done; Firestore upload pending. |
| [RICH_CONTENT](./RICH_CONTENT_RFC.md) | Implemented V1 | File delivery (md/html/xlsx/docx) via SlackMediaAdapter — in production. |
| [SECRETS_AT_REST](./SECRETS_AT_REST_RFC.md) | Proposed | Application-layer encryption of per-user credentials via a KMS-backed `CipherPort`; migrates plaintext OAuth tokens **and MCP client secrets** in Firestore to ciphertext. Ships first — hard prerequisite for the Telegram session. |
| [SYSTEM_TURN_FINGERPRINT](./SYSTEM_TURN_FINGERPRINT_RFC.md) | Proposed | Lets the model tell which text in a user message was put there by the system: a random per-turn key (three words) in the last block of the system prompt, the rule at its start, and a tag with that key around every code-injected note and anchor of the current turn. History is plain history; the key is scrubbed from replies and tool-call arguments. Covers provenance only; injection-driven actions and reminder bodies are a separate RFC. |
| [TESTING_STRATEGY](./TESTING_STRATEGY_RFC.md) | Active | Testing framework and protocols. |
| [TASKS_AGENT](./TASKS_AGENT_RFC.md) | Implemented | Tasks specialist agent — single `manage_user_tasks` intent, tool-calling loop, Google Tasks adapter. |
| [TELEGRAM_CHANNEL_READER](./TELEGRAM_CHANNEL_READER_RFC.md) | Proposed — gated on spike | Read-only Telegram channel reading via MTProto user session (Telethon); one specialist agent behind one intent, reached by both the scheduled daily digest and on-demand requests. No persistence. Multi-user QR device-linking in Cabinet; session encrypted via `CipherPort`. Blocked on SECRETS_AT_REST + a QR-resumption spike. |
| [VOICE_COMPANION](./VOICE_COMPANION_RFC.md) | Proposed | Spoken front desk for the existing stack: "Lelik" is a voice assistant *to* Alek — clarifies, forwards, narrates the wait — so delegation is his default posture rather than an escalation. Alek answers in text and is never spoken. Raw voice session is discarded; a cheap summary pass writes into the default channel session and consolidates by the ordinary path. New `RealtimeSessionPort` (LLMPort is request/response, and the port carries no audio — media bypasses the backend); Lelik is a handler, not a `BaseAgent`. Transport = two handlers over one service: **one-button web page first** (tests the persona cheaply), telephony/SIP second. Outbound telephony (Alek calling you) is a separate capability needing its own RFC. |
| [WEBSEARCH_STRUCTURED_OUTPUT](./WEBSEARCH_STRUCTURED_OUTPUT_RFC.md) | Draft | Structured JSON output for WebSearchAgent. |

---

---

**Last Updated:** 2026-06-01
