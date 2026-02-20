# Sliding Window Consolidation (Building Block)

## 📖 HowTo: Using This Document

### Purpose

Describes the memory pipeline that transforms short-term conversation history into long-term structured knowledge.

### When to Read

- **For AI Agents:** Before modifying the consolidation logic, batching strategy, or fact extraction prompt.
- **For Developers:** When troubleshooting missing facts, consolidation delays, or memory usage issues.

### When to Update

This document MUST be updated when:

- [ ] The sliding window threshold or batch size logic changes.
- [ ] The consolidation trigger mechanism (overflow callback) is modified.
- [ ] The `ConsolidationAgent` prompt or synthesis logic changes.
- [ ] New fact types or metadata fields are added to the extraction process.
- [ ] The background processing infrastructure (Cloud Tasks) is updated.

### Cross-References

- **Multi-Agent System:** [../multi_agent_system/README.md](../multi_agent_system/README.md)
- **Biographical Context Cache:** [../biographical_context_cache/README.md](../biographical_context_cache/README.md)
- **Fact Write Service:** [../fact_write_service/README.md](../fact_write_service/README.md)
- **Prompt Design System v3:** [../prompt_design_system_v3/README.md](../prompt_design_system_v3/README.md)

---

## 1. Overview

Alek-Core implements a **Dual Memory System** inspired by human cognitive processes:

1. **Short-Term Memory (Hot):** Recent conversation history (sliding window).
2. **Long-Term Memory (Cold):** Consolidated facts and principles (vector storage).

**Sliding Window Consolidation** is the pipeline that bridges these two systems, ensuring the bot "remembers" important information without being overwhelmed by raw history.

---

## 2. The Memory Pipeline

### 2.1 Step 1: Sliding Window (Hot Storage)

Conversation history is stored in Firestore as a list of messages.

- **Threshold:** Default 100 messages.
- **Mechanism:** `FirestoreSessionStore` monitors the history length.
- **Trigger:** When the threshold is exceeded, an `overflow_callback` is triggered.

### 2.2 Step 2: Batching & Queueing

- **Batch Creation:** The oldest messages (e.g., 50 messages) are extracted into a `ConsolidationBatch`.
- **Queueing:** The batch is saved to the `consolidation_queue` collection with `PENDING` status.
- **Async Trigger:** A background task is enqueued to `Cloud Tasks` to process the user's queue.

### 2.3 Step 3: Synthesis (The Librarian)

The `ConsolidationAgent` (Librarian) processes the batch:

1. **Context Loading:** Loads existing biographical facts and principles to avoid duplicates.
2. **LLM Analysis:** Uses a powerful model (PERFORMANCE tier) with the **Life Chronicler** prompt (v3).
3. **Extraction:** Identifies new facts, events, and guiding principles (anchors).
4. **Deduplication:** LLM compares new findings with existing context to ensure uniqueness.

### 2.4 Step 4: Fact Writing

- **Service:** `FactWriteService` handles the actual persistence.
- **Embeddings:** Generates multi-vector embeddings (text, tags, metadata).
- **SCD Type 2:** Saves facts with temporal metadata (`valid_from`, `is_current`).

### 2.5 Step 5: Cache Refresh

- **Refresh:** The biographical context cache is updated immediately after writing.
- **Invalidation:** `UserPromptBuilder` cache is invalidated to ensure the next response uses the new knowledge.

---

## 3. Core Components

### 3.1 FirestoreSessionStore

Responsible for maintaining the sliding window and triggering consolidation.

- **Key Method:** `append_messages_batch()` checks for overflow.
- **Callback:** `overflow_callback` decouples storage from processing logic.

### 3.2 ConsolidationAgent

The domain specialist for knowledge synthesis.

- **Prompt:** Token-based "Life Chronicler" (v3).
- **Output:** Structured JSON containing `new_facts` and `new_anchors`.
- **Limits:** 3-level resolution for cache limits (USER > ACCOUNT > SYSTEM).

### 3.3 ConsolidationHandler

The application service orchestrating the process.

- **Sequential Processing:** Processes batches one-by-one per user to maintain temporal order.
- **Error Handling:** Implements retry logic (3 attempts) before marking a batch as `FAILED`.

---

## 4. Configuration

### 4.1 Window Settings

Configured in `config/settings.py` under `CONSOLIDATION`:

- `threshold`: Max messages in hot storage (default: 100).
- `batch_size`: Messages to consolidate per batch (default: 50).

### 4.2 Cache Limits

Resolved dynamically via `ConfigurationService`:

- `biographical_cache_limit`: Max facts in context (default: 50).
- `principles_cache_limit`: Max principles/anchors (default: 15).

---

## 5. Code References

- `src/adapters/firestore_session_store.py`: Sliding window logic.
- `src/adapters/firestore_consolidation_queue.py`: Queue implementation.
- `src/handlers/consolidation_handler.py`: Process orchestration.
- `src/agents/consolidation_agent.py`: Synthesis logic.
- `src/services/fact_write_service.py`: Fact persistence.

---

## 6. Per-Turn History Compression (`response_summary`)

The Sliding Window pipeline handles **session-level** consolidation (50+ messages → facts). A complementary mechanism handles **per-turn** compression to keep active session context small.

### 6.1 How It Works

After each SmartAgent response, a lightweight Flash model call compresses the response into a `response_summary` (≤300 chars). This summary is stored in the session history record **instead of** the full response text.

```
Turn N (full response stored)
Turn N+1 → SmartAgent generates response
         → Flash model postprocesses: "User asked X, agent answered Y with context Z 🔬"
         → Session stores summary (280 chars) instead of full text (1800 chars)
```

### 6.2 Two-Level Memory Architecture

| Level | Component | What It Compresses | When |
|-------|-----------|--------------------|------|
| Per-turn | SmartAgent + Flash postprocessing | Single response → ≤300-char summary | After every Smart response |
| Session-level | ConsolidationAgent (this building block) | 50 messages → structured facts | When sliding window overflows |

Per-turn compression keeps **hot storage (Firestore session)** lean for the active conversation. Session-level consolidation moves knowledge to **cold storage (vector search)** for long-term recall.

### 6.3 Configuration

- `ENABLE_HISTORY_OPTIMIZATION=true` — enables per-turn compression (default: `false`).
- `history_recent_full_turns` — last N model turns always use full text (default: 5). Compression only applies to older turns.

### 6.4 `response_summary` Format Rules

Compression LLM instruction (enforced via `response_schema`):
- Max 300 chars. Hard limit.
- Include: key entities, facts, decisions.
- Preserve vibe (irony, tone) — not robotic.
- Preserve or adapt emojis — they carry emotional weight.
- Plain text only. No Markdown formatting.

---

## 7. Status & Roadmap

**Status:** ✅ Production Ready

### Planned Enhancements

- **Incremental Synthesis:** Synthesize facts as they happen rather than in large batches.
- **Conflict Resolution:** Better handling of contradictory information during consolidation.
- **Cross-Session Synthesis:** Identify patterns across multiple sessions for the same user.

---

**Last Updated:** 2026-02-18
**Status:** ✅ Complete
**Phase:** Documentation Audit Phase 3.2
