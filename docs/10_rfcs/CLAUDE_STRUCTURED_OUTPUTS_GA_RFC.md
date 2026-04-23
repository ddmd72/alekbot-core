# Claude Structured Outputs GA Migration RFC

**Status:** Implemented (2026-04-22)  
**Priority:** Medium (not blocking, existing hack works with the 2026-04-22 hotfix)  
**Scope:** `src/adapters/claude_adapter.py` only

---

## Problem

The current `response_schema` path in `ClaudeAdapter` uses a fake `respond` tool to enforce
structured output. This was the only available workaround before the GA release.

The hack has three known failure modes:

1. **thinking + force-respond = 400** — when adaptive thinking is enabled and the model returns
   text without calling tools, the force-respond path appends the response (which contains
   thinking blocks) as an assistant prefill. Claude rejects this with:
   `"This model does not support assistant message prefill."` — fixed by the 2026-04-22 hotfix
   (`and not thinking_param` guard), but the underlying hack remains.

2. **force-respond path is fragile** — depends on the model calling `respond` correctly. When it
   doesn't (thinking mode, grounding mode), the code has conditional workarounds.

3. **tool list pollution** — `respond` appears alongside real delegation tools. The model has to
   pick between `delegate_to_specialist` and `respond` — confusing prompt semantics.

---

## Solution: `output_config.format` (Anthropic GA)

Anthropic released structured outputs as GA. No beta header required.

### New request format

```python
create_kwargs["output_config"] = {
    "format": {
        "type": "json_schema",
        "schema": schema,  # same JSON Schema dict currently passed as response_schema
    }
}
```

When thinking is also active, both fields coexist in one dict:
```python
create_kwargs["output_config"] = {
    "effort": effort,          # thinking
    "format": {                 # structured output
        "type": "json_schema",
        "schema": schema,
    }
}
```

### Compatibility matrix

| Feature              | New path      |
|----------------------|---------------|
| streaming            | ✅            |
| thinking             | ✅            |
| real tool calls      | ✅            |
| message prefilling   | ❌ (not needed)|

---

## What gets deleted

All of the following is dead code after the migration:

```python
# In generate_content():
_schema_tool_active = False
if response_schema and isinstance(response_schema, dict):
    respond_tool_schema = ...
    claude_tools = claude_tools + [{"name": "respond", ...}]
    _schema_tool_active = True
    force_tool_use = True

# Intercept respond tool call (~10 lines)
if _schema_tool_active and llm_response.tool_calls:
    respond_call = next(...)
    if respond_call:
        return LLMResponse(text=json.dumps(...), ...)

# Thinking+schema alarm log (~8 lines)
if _schema_tool_active and not _use_dynamic_search and thinking_param:
    if not llm_response.tool_calls:
        logger.error(...)

# Force-respond path (~40 lines)
if _schema_tool_active and not _use_dynamic_search and not thinking_param:
    if not llm_response.tool_calls and response is not None:
        ...
```

Replaced by ~8 lines:
```python
if response_schema and isinstance(response_schema, dict):
    schema = {k: v for k, v in response_schema.items() if k != "nullable"}
    output_config = create_kwargs.get("output_config", {})
    output_config["format"] = {"type": "json_schema", "schema": schema}
    create_kwargs["output_config"] = output_config
```

And the response is read directly from `llm_response.text` — no interception needed.

---

## Implementation Steps

### Step 1 — Update `generate_content` in `ClaudeAdapter`

1. Remove the `_schema_tool_active` block that injects `respond` tool
2. Add `output_config.format` injection (merge with existing `output_config.effort` if thinking)
3. Remove the `respond` tool interception block (lines ~293–305)
4. Remove the thinking+schema alarm log (added 2026-04-22, no longer needed)
5. Remove the force-respond block entirely (~40 lines)

### Step 2 — Verify response parsing

`_parse_response` returns `LLMResponse.text` from the first text block in the message content.
With `output_config.format`, the model returns JSON directly in a text block — no change needed.

Verify that `SmartResponseAgent._parse_llm_response` and `QuickResponseAgent` handle the
response correctly (they already parse `llm_response.text` as JSON).

### Step 3 — Update `force_tool_use` logic

`force_tool_use` currently gets set to `True` by the `respond` tool injection. After removal,
`force_tool_use` is only set by `request.force_tool_use` (line 111). Verify no agent sets this
flag directly — search for `force_tool_use=True` in agent code.

### Step 4 — Tests

Existing adapter tests in `tests/unit/adapters/test_claude_adapter.py` cover:
- `respond` tool interception
- force-respond path
- thinking + schema combination

All three test groups will need updating to reflect the new `output_config.format` path.
**Per the test rule: get explicit per-test approval before touching each test.**

### Step 5 — Deploy and verify

Deploy to dev. Send a `deep_reasoning` request. Confirm:
- No 400 error
- JSON response received via `output_config.format` path
- No `[ClaudeAdapter] Adaptive thinking enabled` → force-respond sequence in logs

---

## Risk

**Low.** The `output_config.format` path is GA, works with streaming and thinking.
The only risk is model behavior change (model may format JSON slightly differently without the
`respond` tool schema enforcement). Monitor first response in dev before promoting to prod.

---

## Not in scope

- Other adapters (Gemini, OpenAI, Grok) — they have their own native JSON enforcement
- `LLMPort` / `LLMRequest` — `response_schema` field remains as the adapter-agnostic contract;
  only the Claude adapter's internal translation changes
