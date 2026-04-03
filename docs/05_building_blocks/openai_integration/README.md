# OpenAI Integration

**Status:** Production Ready
**Date:** 2026-04-03
**Provider:** OpenAI (Responses API)

---

## Overview

Integration of **OpenAI** as a full LLM provider in Alek-Core with 100% feature parity with
GeminiAdapter and ClaudeAdapter. Any agent (Router, Quick, Smart, etc.) can be routed to OpenAI
via `config.provider_preference = "openai"` or per-agent override in UserBotConfig.

### Key Features

- **Responses API** — modern API with native tool support and agentic capabilities
- **Native Function/Tool Calling** — internally-tagged format, strict by default
- **Web Search** — native `{"type": "web_search"}` with url_citation annotations and agentic search
- **JSON Mode** — `text.format=json_object` when `response_mime_type="application/json"`
- **Vision** — base64-encoded images in input content (gpt-5 family supports multimodal)
- **Large Context Window** — 1M tokens (gpt-5 family)
- **Hexagonal Architecture** — implements `LLMPort` port; agents are provider-agnostic

---

## Architecture

### Components

```
AgentContextBuilder
  └─→ ProviderRegistry.get("openai")
        └─→ OpenAIAdapter(LLMPort)
              └─→ openai.AsyncOpenAI → client.responses.create()
```

### File Structure

```
src/adapters/openai_adapter.py          # Main adapter (Responses API)
src/composition/service_container.py    # Initialization + registry registration
src/composition/user_agent_factory.py   # openai_service constructor param
src/services/agent_context_builder.py   # "openai" added to allowed_providers
src/config/settings.py                  # OPENAI_API_KEY loading
```

---

## Configuration

### Model Tiers

```python
MODEL_TIERS = {
    PerformanceTier.ECO: "gpt-5.4-nano",      # Cheapest/fastest
    PerformanceTier.BALANCED: "gpt-5.4-mini",  # Mid-tier quality
    PerformanceTier.PERFORMANCE: "gpt-5.4",    # Flagship
}
```

Verify current model IDs at https://platform.openai.com/docs/models.

### Provider Capabilities

```python
CAPABILITIES = ProviderCapabilities(
    native_tools=True,
    context_caching=False,
    vision=True,
    max_context_window=1047576,
    supports_system_prompt=True,
    supports_json_mode=True,
    native_grounding=True,  # web_search with url_citation annotations
)
```

### Sampling Parameter Restriction

The gpt-5 family does not support sampling parameters. Sending `temperature`, `top_p`,
`frequency_penalty`, or `presence_penalty` returns a 400 error from the API.

`_is_reasoning_model(model_name)` gates this via prefix match:

```python
_REASONING_PREFIXES = ("gpt-5", "o1", "o3")
```

Temperature is excluded from `create_kwargs` for any model whose name starts with one of these prefixes.

### Default Provider Strategy

OpenAI is added to `allowed_providers` in `AgentProviderStrategy` for Router, Quick, and Smart.
It is NOT the default — agents default to Gemini unless the user or agent overrides.

---

## Environment Variables

**Local Development (.env):**

```bash
OPENAI_API_KEY=sk-svcacct-...
```

**Cloud Run (GCP Secret Manager):**

Secret created under project `$PROJECT_ID` (dev).
`load_settings()` fetches automatically when `.env` value is absent.

---

## Feature Details

### Responses API

The adapter uses OpenAI's Responses API (`client.responses.create()`).

Key parameter mapping from LLMPort domain:
- `system_instruction` → `instructions` (top-level parameter, not in messages)
- `messages` → `input` (list of items)
- `max_tokens` → `max_output_tokens`
- `response_mime_type` / `response_schema` → `text={"format": {"type": "json_object"}}`
- `store=True` — responses stored in OpenAI dashboard for debugging/analysis. OpenAI does not use stored data for training.

### Web Search (Grounding)

When `use_grounding=True`, the adapter:
1. Injects `{"type": "web_search"}` into tools
2. Enables reasoning (`reasoning={"effort": "low"}`) for agentic search — gpt-5.4 defaults
   to `effort: "none"` which disables iterative search/open_page/find_in_page
3. Extracts `url_citation` annotations from response output and appends as `*Sources:*` block

Agentic search means the model performs iterative search, opens pages, and searches within
pages server-side. This is a capability of reasoning models — non-reasoning models do single-pass.

### Tool Calling

Responses API uses **internally-tagged** format (no nested `function` wrapper):

```python
# Domain format (input)        →  Responses API format (output)
{"name": "search", ...}        →  {"type": "function", "name": "search", ...}
```

`tool_choice="required"` is set when `force_tool_use=True`.

Tool call IDs use `call_id` field (Responses API) stored as `thought_signature` in domain ToolCall.

### JSON Mode

When `response_mime_type="application/json"` is set:

```python
text = {"format": {"type": "json_object"}}
```

### Multi-turn Tool Calling

`raw_content` stores `response.output` (list of output items) for multi-turn conversations.
When loading conversation history, output items are passed through directly to the input:

```python
# Model message with raw_content (list of output items)
if msg.raw_content is not None and isinstance(msg.raw_content, list):
    items.extend(msg.raw_content)
```

Tool results use `function_call_output` type:

```python
{"type": "function_call_output", "call_id": "...", "output": "..."}
```

### Prompt Cache Boundary

The `<!-- CACHE_BOUNDARY -->` marker used by ClaudeAdapter is stripped from the system
instruction before sending to OpenAI.

### Vision

### Files

**Images**: inline base64 as `input_image` (`data:<mime>;base64,<data>`).

**Non-image files** (PDF, DOCX, etc.): uploaded via OpenAI Files API (`client.files.create()`)
→ referenced by `file_id` in input items as `{"type": "input_file", "file_id": "..."}`.
Same pattern as Gemini (`client.files.upload()`). Adapter handles upload transparently
from `file_data.path` (temp file from FileManagementAgent).

---

## Deep Research

OpenAI provides a dedicated Deep Research API via the Responses endpoint.
`OpenAIDeepResearchAdapter` implements `AsyncJobPort` — the same port as
`GeminiDeepResearchAdapter`. Both are pure API clients.

### Model

```python
DEFAULT_DEEP_RESEARCH_MODEL = "o4-mini-deep-research-2025-06-26"  # default (fast/cheap)
# o3-deep-research-2025-06-26 — higher quality, override via OPENAI_DEEP_RESEARCH_MODEL env var
```

### Architecture

`AsyncJobPort` (`src/ports/async_job_port.py`) is the port for the kick-off + polling pattern:

```python
class AsyncJobPort(ABC):
    async def submit(self, query: str) -> str: ...         # returns job_id
    async def get_status(self, job_id: str) -> tuple[str, str]: ...
```

Adapters are pure API clients — no Cloud Task or queue logic.

---

## Changelog

### 2026-04-03 — Migration to Responses API

- `client.chat.completions.create()` → `client.responses.create()`
- `messages` → `instructions` + `input` (Responses API format)
- Tools: externally-tagged `{"type": "function", "function": {...}}` → internally-tagged `{"type": "function", "name": ...}`
- Tool results: `{"role": "tool", "tool_call_id": ...}` → `{"type": "function_call_output", "call_id": ...}`
- Response parsing: `choices[0].message` → `response.output` items + `response.output_text`
- Web search: native `{"type": "web_search"}` with `reasoning={"effort": "low"}` for agentic search
- URL citations: `url_citation` annotations extracted and appended as `*Sources:*` block
- Files: non-image files uploaded via Files API (`file_id`), images inline base64
- JSON mode: `response_format` → `text.format`
- `max_completion_tokens` → `max_output_tokens`
- `store=True` for dashboard visibility (OpenAI does not use stored data for training)
- `raw_content` stores `response.output` (list) for multi-turn
- `native_grounding=True` in CAPABILITIES
- Tests: 31 unit tests rewritten for Responses API

### 2026-03-04 — AsyncJobPort hexagonal refactor

- `DeepResearchPort` → `AsyncJobPort`: port named after the pattern, not the use-case
- Both adapters are now pure API clients
- Per-user provider selection via `UserBotConfig.agent_providers["deep_research"]`

### 2026-03-04 — OpenAI Deep Research adapter

- Added `OpenAIDeepResearchAdapter` implementing `AsyncJobPort` via OpenAI Responses API

### 2026-03-03 — Initial Integration

- Created `OpenAIAdapter` with full LLMPort parity (Chat Completions API)
- Registered in `ServiceContainer` + `ProviderRegistry`
