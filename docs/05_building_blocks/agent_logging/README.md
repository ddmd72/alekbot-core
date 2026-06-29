# Agent Logging

**Status:** Production Ready
**Date:** 2026-03-07

---

## Overview

Agent logging is split into two independent layers:

| Layer | Purpose | Destination | Controlled by |
|---|---|---|---|
| **Lifecycle hooks** | Operational logs — start, success, error, delegation | Cloud Logging (stdout) | always on |
| **Content store** | Full LLM prompt/response content + tokens | BigQuery `alek_observability_dev.prompt_content` | `DEBUG_PROMPTS=true` AND `BIGQUERY_PROMPT_DATASET` set |

Both layers are owned by `BaseAgent`. Agents call hooks and `_call_llm()` — they do not import or configure logging infrastructure directly.

---

## Layer 1 — Lifecycle Hooks

### API

```python
self._on_agent_start(text: str)
self._on_agent_success(char_count: int, token_count: int, output_text: str = "")
self._on_agent_error(error: Exception, context: str = "execute")
self._on_delegation(intent: str, query: str = "")
```

### When to call

| Hook | Where | What it logs |
|---|---|---|
| `_on_agent_start(text)` | Top of `execute()` | `[agent_id] start → 'text...'` |
| `_on_agent_success(chars, tokens, output_text)` | Before success return | `✅ [agent_id] done (N chars, M tokens)` |
| `_on_agent_error(error)` | In `except` block of `execute()` | `❌ [agent_id] error in execute: ...` |
| `_on_delegation(intent, query)` | Before each specialist delegation | `[agent_id] → delegate: intent=X query='...'` |

`_on_agent_success(output_text=...)` no longer writes the final user-facing text to any bucket — that GCS dump was dropped in the BigQuery migration (TD-1). Content capture now happens per-LLM-call inside `_call_llm()` via `PromptContentStore.record_turn()` (see Layer 2).

### Rule

Direct `logger.*` calls inside `execute()` or delegation loops are allowed only for **context-specific supplementary information** (phase decisions, result counts, routing choices) that hooks do not cover.

Forbidden in `execute()`:
- Duplicating `_on_agent_start` / `_on_agent_success` / `_on_agent_error`
- `logger.error()` before re-raise (double-logging — let the hook catch the re-raised exception)

---

## Layer 2 — BigQuery Content Store

### Single entry point: `_call_llm()`

```python
response = await self._call_llm(request, turn=0)
```

All agents **must** use `_call_llm()` instead of calling `self.llm` / `self._llm` directly.

`_call_llm()` is the single LLM call site and the single content-capture point:
1. Resolves `self.llm` or `self._llm`
2. Calls `llm.generate_content(request=request)`
3. Records the full request + response to the BigQuery content store via
   `PromptContentStore.record_turn()` (adapter: `BigQueryPromptContentAdapter`)

One row is written per LLM call. `request_text` is populated **even when the call fails** — a
400'd request still produces a row (with empty `response_text`), so failed LLM calls are
inspectable.

### Where it lands

Table: `<project>.alek_observability_dev.prompt_content` — day-partitioned, 30-day TTL.

Columns include: `trace_id`, `span_id`, `timestamp`, `user_id`, `account_id`, `agent_id`,
`agent_type`, `model`, `provider`, `turn`, `request_text`, `response_text`, `tool_calls`,
`prompt_tokens`, `completion_tokens`, `total_tokens`.

Multi-turn agents pass `turn=N` to `_call_llm()` — the turn number is recorded on the row.

### Reading it

Query with `bq` locally (use `--format=json` for the large multi-line `request_text` — CSV
breaks on embedded newlines):

```bash
bq query --project_id=<PROJECT_ID> --use_legacy_sql=false --format=json \
  'SELECT request_text, response_text, tool_calls FROM
   `<PROJECT_ID>.alek_observability_dev.prompt_content`
   WHERE timestamp BETWEEN "<from>" AND "<to>" AND agent_type LIKE "%smart%" ORDER BY turn'
```

### Raw-SDK escape hatch: `_debug_raw_turn()`

Agents that bypass `LLMPort` and call the provider SDK directly (e.g.
`ClaudeDeepResearchRunnerAgent` with native built-in tools) cannot route through `_call_llm()`.
They call `_debug_raw_turn(...)`, which emits a **summary-only `logger.info` line** — no storage
write. This is the only remaining "debug" helper; their full prompts are not captured to BigQuery.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `DEBUG_PROMPTS` | `false` | Master on/off switch for BigQuery content capture |
| `BIGQUERY_PROMPT_DATASET` | _(not set)_ | Dataset for the `prompt_content` table; capture wired only when set |

The content store is wired (in `src/composition/service_container.py`) only when
`DEBUG_PROMPTS=true` AND `BIGQUERY_PROMPT_DATASET` is set. `DEBUG_PROMPTS` is now purely the
BigQuery capture switch — it no longer controls any GCS path.

### Production safety

When the content store is not wired, `_call_llm()` performs no capture — no allocations, no I/O
beyond the LLM call itself.

---

## File Structure

```
src/agents/base_agent.py                          # _call_llm (single LLM call + capture point),
                                                  # _debug_raw_turn (summary-only, raw-SDK agents),
                                                  # lifecycle hooks (_on_agent_*)
src/adapters/bigquery_prompt_content_adapter.py   # BigQueryPromptContentAdapter
                                                  # (PromptContentStore.record_turn)
```

---

## Adding a New Agent

```python
class MyAgent(BaseAgent):

    async def execute(self, message: AgentMessage) -> AgentResponse:
        text = message.payload.get("text", "")
        self._on_agent_start(text)
        try:
            # ...build request...
            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=[Message(role="user", parts=[MessagePart(text=text)])],
                temperature=self.TEMPERATURE,
            )
            response = await self._call_llm(request)   # ← request + response auto-logged
            # ...process response...
            self._on_agent_success(len(result), token_count)
            return AgentResponse.success(...)
        except Exception as e:
            self._on_agent_error(e)
            return AgentResponse.failure(...)
```

Do **not**:
- Call `self.llm.generate_content()` or `self._llm.generate_content()` directly
- Call `logger.error()` before re-raise in the main except block

---

## Changelog

### 2026-06-29 — Legacy GCS prompt-dump removed (TD-1)

- Deleted `src/utils/debug_logger.py` (`PromptDebugLogger`, `get_debug_logger()`), the
  `DEBUG_PROMPTS_BUCKET` env var, and the local `debug_prompts/` fallback directory.
- Removed `BaseAgent._debug_prompt` / `_debug_response` / `_debug_llm_response` and the
  `_format_history_for_debug` helper.
- LLM content capture is now the BigQuery content store: `PromptContentStore.record_turn()`
  (`BigQueryPromptContentAdapter`) called from `_call_llm()`, landing in
  `alek_observability_dev.prompt_content` (day-partitioned, 30-day TTL).
- Gating is now `DEBUG_PROMPTS=true` AND `BIGQUERY_PROMPT_DATASET` set. `DEBUG_PROMPTS` is purely
  the capture on/off switch — no longer GCS-related.
- `_on_agent_success(output_text=...)` no longer writes a final-text dump anywhere.
- `_debug_raw_turn(...)` retained as a summary-only `logger.info` line for raw-SDK agents.

### 2026-03-07 (2) — File naming simplified; history persistence

- File naming changed to `YYYY-MM-DD_HH-MM-SS_request/response.txt` (no agent prefix)
- `tools:` header shows only tool names (was: full ToolDeclaration str())
- `AgentResponse.history_context` — specialist-driven history persistence (see multi_agent_system § 9)
- `ConversationHandler`: generic `*_context` loop; `rich_content` appended to `full_text`

### 2026-03-07 (1) — Centralized request logging

- `_call_llm()` now logs both request (before) and response (after) automatically
- New `PromptDebugLogger.log_llm_request()` — formats as human-readable file with role-labelled
  sections (`[system]`, `[user]`) and real newlines (no JSON escaping of content)
- All `_debug_prompt()` / `_debug_response()` call sites removed from all agents (16 sites across 10 files)
- WebSearchAgent / WebSearchLightAgent: `fetch_url` payload now requires `query` field
  (natural language description of what to find on the page); `system_instruction` + raw query
  as user message (no bio context injected, datetime prefix in system instruction)

### 2026-03-03 — Initial

- Documented lifecycle hooks and debug bucket layers
- `_call_llm()` added to `BaseAgent` — replaces per-agent `generate_content()` calls
- `_debug_llm_response()` serialises full `LLMResponse` (text + tool_calls + tokens)
- All 11 agent callsites migrated; manual `_debug_response()` after LLM calls removed
