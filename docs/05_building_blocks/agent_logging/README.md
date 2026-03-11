# Agent Logging

**Status:** Production Ready
**Date:** 2026-03-07

---

## Overview

Agent logging is split into two independent layers:

| Layer | Purpose | Destination | Controlled by |
|---|---|---|---|
| **Lifecycle hooks** | Operational logs — start, success, error, delegation | Cloud Logging (stdout) | always on |
| **Debug bucket** | Full LLM I/O for prompt debugging | GCS bucket or local filesystem | `DEBUG_PROMPTS=true` |

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

`_on_agent_success(output_text=...)` also writes the final user-facing text to the debug bucket (type=output) when `DEBUG_PROMPTS` is enabled.

### Rule

Direct `logger.*` calls inside `execute()` or delegation loops are allowed only for **context-specific supplementary information** (phase decisions, result counts, routing choices) that hooks do not cover.

Forbidden in `execute()`:
- Duplicating `_on_agent_start` / `_on_agent_success` / `_on_agent_error`
- `logger.error()` before re-raise (double-logging — let the hook catch the re-raised exception)

---

## Layer 2 — Debug Bucket

### Single entry point: `_call_llm()`

```python
response = await self._call_llm(request, turn=0)
```

All agents **must** use `_call_llm()` instead of calling `self.llm` / `self._llm` directly.

`_call_llm()` does four things automatically:
1. Resolves `self.llm` or `self._llm`
2. Logs the full `LLMRequest` via `log_llm_request()` → **request file**
3. Calls `llm.generate_content(request=request)`
4. Logs the full `LLMResponse` via `_debug_llm_response()` → **response file**

Agents must **never** call `_debug_prompt()` or `_debug_response()` directly. Those methods exist only for backward compat and are not used.

### Request file format

```
================================================================================
AGENT: websearch
TIMESTAMP: 2026-03-07T12:34:56.123456
MODEL: gemini-3-flash-preview
temperature: 0.7  use_grounding: true
================================================================================

[system]
current_date_time: Saturday, 07 March 2026, 12:34 UTC

class SearchAgent extends GoogleSearchAgent {
  ...
}

[user]
weather in Valencia
```

Multi-turn agents pass `turn=N` to `_call_llm()` — the turn number appears in the header and filename.

### Response file format

Each LLM response is written as JSON:

```json
{
  "text": "...",
  "tool_calls": [
    {"name": "delegate_to_specialist", "args": {"intent": "search_memory", "query": "..."}}
  ],
  "tokens": 1234
}
```

`tool_calls` is omitted when empty. `tokens` is omitted when usage metadata is unavailable.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `DEBUG_PROMPTS` | `false` | Enable debug bucket writes |
| `DEBUG_PROMPTS_BUCKET` | _(not set)_ | GCS bucket name for Cloud Run mode |

When `DEBUG_PROMPTS_BUCKET` is not set, files are written locally to `debug_prompts/`.

File naming: `YYYY-MM-DD_HH-MM-SS_request.txt`, `YYYY-MM-DD_HH-MM-SS_response.txt`, `YYYY-MM-DD_HH-MM-SS_response_tN.txt` (multi-turn).

GCS path format: `{agent_type}/{YYYY-MM-DD}/YYYY-MM-DD_HH-MM-SS_request.txt`

### Production safety

All debug methods are complete no-ops when `DEBUG_PROMPTS=false` — no allocations, no I/O. The check is the first line of each method.

---

## File Structure

```
src/agents/base_agent.py              # _call_llm (single logging entry point),
                                      # _debug_llm_response, lifecycle hooks (_on_agent_*)
src/utils/debug_logger.py             # PromptDebugLogger — log_llm_request, log_response,
                                      # GCS + local backend, file rotation
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
- Call `self._debug_prompt()` or `self._debug_response()` — redundant, logging is in `_call_llm`
- Call `logger.error()` before re-raise in the main except block

---

## Changelog

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
