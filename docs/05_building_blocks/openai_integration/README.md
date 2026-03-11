# OpenAI Integration

**Status:** Production Ready
**Date:** 2026-03-03
**Provider:** OpenAI (Chat Completions API)

---

## Overview

Integration of **OpenAI** as a full LLM provider in Alek-Core with 100% feature parity with
GeminiAdapter and ClaudeAdapter. Any agent (Router, Quick, Smart, etc.) can be routed to OpenAI
via `config.provider_preference = "openai"` or per-agent override in UserBotConfig.

### Key Features

- **Native Function/Tool Calling** — parallel tool calls, automatic or manual orchestration
- **JSON Mode** — `response_format=json_object` when `response_mime_type="application/json"`
- **Vision** — base64-encoded images in message content (gpt-5 family supports multimodal)
- **Large Context Window** — 1M tokens (gpt-5 family)
- **Streaming** — full async streaming support
- **Hexagonal Architecture** — implements `LLMPort` port; agents are provider-agnostic

---

## Architecture

### Components

```
AgentContextBuilder
  └─→ ProviderRegistry.get("openai")
        └─→ OpenAIAdapter(LLMPort)
              └─→ openai.AsyncOpenAI → api.openai.com
```

### File Structure

```
src/adapters/openai_adapter.py          # Main adapter
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
    PerformanceTier.ECO: "gpt-5-nano",      # Cheapest/fastest
    PerformanceTier.BALANCED: "gpt-5-mini", # Mid-tier quality
    PerformanceTier.PERFORMANCE: "gpt-5",   # Flagship
}
```

Verify current model IDs at https://platform.openai.com/docs/models.

### Provider Capabilities

```python
CAPABILITIES = ProviderCapabilities(
    native_tools=True,          # Full function calling support
    context_caching=False,      # Not available (as of 2026-03-03)
    vision=True,                # Multimodal (base64 images)
    max_context_window=1047576, # 1M tokens (gpt-5 family)
    supports_system_prompt=True,
    supports_json_mode=True,    # response_format=json_object
)
```

### Sampling Parameter Restriction

The gpt-5 family does not support sampling parameters. Sending `temperature`, `top_p`,
`frequency_penalty`, or `presence_penalty` returns a 400 error from the API.

`_is_reasoning_model(model_name)` gates this via prefix match:

```python
_REASONING_PREFIXES = ("gpt-5", "o1", "o3")
```

- `gpt-5`, `gpt-5-mini`, `gpt-5-nano` — confirmed via empirical 400 error at temperature != default
- `o1`, `o3` families — documented OpenAI restriction

Temperature is excluded from `create_kwargs` for any model whose name starts with one of these prefixes.

### Default Provider Strategy

OpenAI is added to `allowed_providers` in `AgentProviderStrategy` for Router, Quick, and Smart.
It is NOT the default — agents default to Gemini unless the user or agent overrides.

To route an agent to OpenAI:

```python
# Per-user global override (Firestore user config)
config.provider_preference = "openai"

# Per-agent override
config.agent_providers["smart"] = "openai"
```

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

### Tool Calling

Full function calling support. Tool definitions are converted from the domain format
(`{name, description, parameters}`) to OpenAI format (`{type: "function", function: {...}}`).

`tool_choice="required"` is set when `force_tool_use=True`.
`tool_choice` is omitted entirely when no tools are provided (API rejects otherwise).

Tool call IDs are preserved via `thought_signature` field (same pattern as GrokAdapter).

### JSON Mode

When `response_mime_type="application/json"` is set:

```python
response_format = {"type": "json_object"}
```

Note: `response_schema` (Gemini structured output format) is not supported.
Agents using json_schema should continue using Gemini or migrate to OpenAI's
json_schema format in a future adapter update.

### Prompt Cache Boundary

The `<!-- CACHE_BOUNDARY -->` marker used by ClaudeAdapter is stripped from the system
instruction before sending to OpenAI. OpenAI does not support prompt caching — the
full system instruction is sent on every request.

### Vision

Images encoded as `file_data.base64` in MessagePart are converted to OpenAI's
`image_url` format (`data:<mime>;base64,<data>`). Supports `image/*` MIME types only.

**Preferred path:** call `upload_file(path, mime_type)` before building messages — it
encodes the file asynchronously and returns a `MessagePart` with `file_data.base64` set.

**Legacy path** (`file_data.path`): `_convert_messages` is synchronous, so the file
is read with plain `open()` + `base64.b64encode()`. New code should always use `upload_file()`.

---

## Streaming

Streaming is implemented via `client.beta.stream()`. Currently falls back to a non-streamed
follow-up call to obtain tool calls and usage metadata, which is not present in streaming
events. This is acceptable for current agent workloads (tool calling turns are not streamed).

---

## Deployment

### Local Development

```bash
# 1. Add to .env
OPENAI_API_KEY=sk-svcacct-...

# 2. Restart bot
python main.py
```

### Cloud Run (Production)

**Secret already exists in dev project.** For prod:

```bash
echo -n "sk-..." | gcloud secrets create OPENAI_API_KEY \
  --project=<PROD_PROJECT_ID> --data-file=-
```

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

Verify current model IDs at https://platform.openai.com/docs/models.

### Architecture

`AsyncJobPort` (`src/ports/async_job_port.py`) is the port for the kick-off + polling pattern:

```python
class AsyncJobPort(ABC):
    async def submit(self, query: str) -> str: ...         # returns job_id
    async def get_status(self, job_id: str) -> tuple[str, str]: ...
```

Adapters are pure API clients — no Cloud Task or queue logic.
`DeepResearchAgent` owns the orchestration and holds both ports:

```
DeepResearchAgent.execute()
  └─→ AsyncJobPort.submit(query)
        └─→ [OpenAI] client.responses.create(model=..., input=query, background=True,
                                              tools=[{"type": "web_search_preview"}])
              └─→ returns response.id (job_id)
  └─→ TaskQueue.enqueue_deep_research_polling(job_id, user_id, ..., provider="openai")

WorkerHandler (Cloud Task polls every ~30s):
  └─→ async_job_ports["openai"].get_status(job_id)
        └─→ client.responses.retrieve(job_id)
              status: "queued" | "in_progress" → poll again
              status: "completed"              → return response.output_text
              status: "failed"                 → return error
```

### Per-user provider selection

Both adapters are instantiated at startup (when API keys are present).
Provider is selected per-user via Firestore `UserBotConfig`:

```python
# Per-user override (Firestore user config)
config.agent_providers["deep_research"] = "openai"   # route to OpenAI
config.agent_providers["deep_research"] = "gemini"   # route to Gemini (default)
```

No env var required. Switching providers per user requires only a Firestore config change.

---

## Differences from GrokAdapter

| Feature | GrokAdapter | OpenAIAdapter |
|---|---|---|
| Base URL | `api.x.ai/v1` | `api.openai.com` (default) |
| Vision | Not supported | Supported (image_url) |
| JSON mode | Not supported | Supported (json_object) |
| Context window | 2M tokens | 1M tokens (gpt-5 family) |
| Sampling params | Supported | Excluded for gpt-5/o1/o3 prefixes |
| PROMPT_CACHE_BOUNDARY | Not stripped | Stripped |
| DNS pre-check | Yes (diagnostic) | No |

---

## Changelog

### 2026-03-04 — AsyncJobPort hexagonal refactor

- `DeepResearchPort` → `AsyncJobPort` (`src/ports/async_job_port.py`): port named after the
  pattern (kick-off + polling), not the use-case. Methods: `submit(query)` + `get_status(job_id)`.
- Both adapters are now pure API clients — `TaskQueue` dependency removed from constructors.
- `DeepResearchAgent` owns orchestration: calls `job_port.submit()` then
  `task_queue.enqueue_deep_research_polling()`. Agent receives both ports + `provider_name`.
- Per-user provider selection via `UserBotConfig.agent_providers["deep_research"]` (Firestore).
  Both adapters instantiated at startup; `UserAgentFactory` selects based on user config.
  `DEEP_RESEARCH_PROVIDER` env var removed — no global override needed.

### 2026-03-04 — OpenAI Deep Research adapter

- Added `OpenAIDeepResearchAdapter` implementing `AsyncJobPort` via OpenAI Responses API
  (default model `o4-mini-deep-research-2025-06-26`, background mode, web_search_preview tool)
- `OPENAI_DEEP_RESEARCH_MODEL` env var — override default model (e.g. to `o3-deep-research-2025-06-26`)

### 2026-03-04 — Bug fixes + unit tests

- Fixed `run_until_complete` in `_convert_messages()` legacy vision path — was crashing in async context;
  replaced with synchronous `open()` + `base64.b64encode()` (correct for a sync method)
- Fixed `max_tokens` → `max_completion_tokens` (gpt-5 family rejects the old param name)
- Added `_is_reasoning_model()` + `_REASONING_PREFIXES` — excludes temperature for gpt-5/o1/o3
- `tests/unit/adapters/test_openai_adapter.py` — 28 unit tests covering capabilities, tiers,
  `_is_reasoning_model`, `_convert_messages`, `_convert_tools`, and mocked `generate_content` paths

### 2026-03-03 — Initial Integration

- Created `OpenAIAdapter` (~300 lines) with full LLMPort parity
- Registered in `ServiceContainer` + `ProviderRegistry`
- Added to `AgentProviderStrategy` for Router, Quick, Smart
- `OPENAI_API_KEY` loaded in `settings.py` + GCP Secret Manager (dev)
