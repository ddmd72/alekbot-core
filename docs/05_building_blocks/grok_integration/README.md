# xAI Grok Integration

**Status:** ✅ Production Ready  
**Date:** 2026-02-12  
**Provider:** xAI (Grok)

---

## Overview

Integration of **xAI Grok** as an LLM provider in Alek-Core with full Hexagonal Architecture compliance.

### Key Features

- ✅ **Native Reasoning Support** - Grok 4.1 fast reasoning models
- ✅ **Function Calling** - Custom tools + native Grok tools
- ✅ **Large Context Window** - 2M tokens
- ✅ **OpenAI-Compatible SDK** - Uses official OpenAI SDK with xAI base URL
- ✅ **Hexagonal Architecture** - Implements `LLMPort` port
- ✅ **Graceful Degradation** - Fallback to Gemini if API key missing

---

## Architecture

### Components

```
┌─────────────────────────────────────────┐
│         Agent Context Builder           │
│   (Provider Selection Strategy)         │
└────────────┬────────────────────────────┘
             │
             ├─→ Router Agent: Grok (ECO tier)
             ├─→ Quick Agent: Grok (BALANCED tier)
             └─→ Smart Agent: Claude (unchanged)

┌─────────────────────────────────────────┐
│          GrokAdapter                    │
│   implements LLMPort port            │
└────────────┬────────────────────────────┘
             │
             └─→ OpenAI SDK → api.x.ai/v1
```

### File Structure

```
src/adapters/grok_adapter.py          # Main adapter (~360 lines)
src/domain/user.py                     # LLMProvider.GROK enum
src/services/agent_context_builder.py # Default provider config
src/config/settings.py                 # XAI_API_KEY loading
```

---

## Configuration

### Model Tiers

```python
MODEL_TIERS = {
    PerformanceTier.ECO: "grok-4-1-fast-non-reasoning",
    PerformanceTier.BALANCED: "grok-4-1-fast-reasoning",
    PerformanceTier.PERFORMANCE: "grok-4-1-fast-reasoning"
}
```

### Default Provider Strategy

**File:** `src/services/agent_context_builder.py`

```python
STRATEGIES = {
    "router": {
        "default_provider": "grok",  # ECO tier
        "fallback": "gemini"
    },
    "quick": {
        "default_provider": "grok",  # BALANCED tier
        "fallback": "gemini"
    },
    "smart": {
        "default_provider": "claude",
        "fallback": "gemini"
    }
}
```

### Environment Variables

**Local Development (.env):**

```bash
XAI_API_KEY=xai-...
```

**Cloud Run (GCP Secret Manager):**

```yaml
# cloudbuild-dev.yaml & cloudbuild-prod.yaml
- secretEnv: ['XAI_API_KEY']

availableSecrets:
  secretManager:
    - versionName: projects/PROJECT_ID/secrets/XAI_API_KEY/versions/latest
      env: XAI_API_KEY
```

---

## Native Tools Support

Grok supports two types of tools:

### 1. Custom Function Calling (Our Tools)

```python
tools = [
    {
        "type": "function",
        "name": "search_memory",
        "description": "Search user's memory",
        "parameters": {...}
    }
]
```

### 2. Native Grok Tools

Supported native tools (pass-through format):

- **web_search** - Real-time web search and browsing
- **code_execution** - Python code execution in sandbox
- **x_search** - Search X posts and threads
- **collections_search** - RAG over uploaded documents

**Example:**

```python
tools = [
    {"type": "web_search"},           # Native Grok tool
    {"type": "function", "name": ...}  # Custom tool
]
```

**Documentation:** https://docs.x.ai/developers/tools/overview

---

## Performance

### Benchmark Results

| Metric                | Grok Reasoning | Gemini Flash Lite | Comparison          |
| --------------------- | -------------- | ----------------- | ------------------- |
| **API Time**          | 2-3s           | ~1s               | Grok 2-3x slower    |
| **Total Latency**     | 8-11s          | 6-8s              | Grok +2-3s overhead |
| **Tokens/Request**    | ~10k           | ~9.4k             | Similar             |
| **Reasoning Quality** | ✅ Yes         | ❌ No             | Grok advantage      |
| **Cost**              | $$             | $                 | Grok more expensive |

### Use Cases

- **Grok (Reasoning):** Complex queries, multi-step reasoning, accuracy-critical
- **Gemini (Fast):** Simple queries, weather/facts, speed-critical

---

## Implementation Details

### GrokAdapter Class

**Location:** `src/adapters/grok_adapter.py`

**Key Methods:**

```python
class GrokAdapter(LLMPort):
    async def generate_content(
        self,
        request: LLMRequest,
        ...
    ) -> LLMResponse:
        # 1. Convert messages to OpenAI format
        # 2. Handle custom + native tools
        # 3. Set tool_choice only if tools exist (bug fix)
        # 4. Call xAI API
        # 5. Parse response
```

### Bug Fixes Applied

**1. tool_choice Bug (Fixed 2026-02-12)**

**Issue:** Setting `tool_choice="auto"` without tools caused 400 error.

**Fix:**

```python
# Before:
tool_choice = "auto" if automatic_function_calling.enabled else None

# After:
tool_choice = "auto" if (openai_tools and automatic_function_calling.enabled) else None
```

**2. Model Tier Bug (Fixed 2026-02-12)**

**Issue:** All tiers used `grok-4-1-fast-reasoning`.

**Fix:**

```python
# Corrected:
ECO: "grok-4-1-fast-non-reasoning"  # Faster, cheaper
BALANCED: "grok-4-1-fast-reasoning"  # Better quality
```

---

## Provider Capabilities

```python
CAPABILITIES = ProviderCapabilities(
    native_tools=True,           # Supports function calling
    context_caching=False,       # Not supported yet
    vision=False,                # Not supported yet
    max_context_window=2000000,  # 2M tokens
    supports_system_prompt=True,
    supports_json_mode=False
)
```

---

## Deployment

### Local Development

```bash
# 1. Add to .env
XAI_API_KEY=xai-...

# 2. Restart bot
python main.py
```

### Cloud Run (Production)

**1. Create Secret:**

```bash
echo -n "xai-..." | gcloud secrets create XAI_API_KEY --data-file=-
```

**2. Deploy:**

```bash
gcloud builds submit --config cloudbuild-prod.yaml
```

**3. Verify:**

```
✅ Grok adapter initialized
```

---

## User Override

Users can override default provider via `config.provider_preference`:

```python
# In Firestore: users/{user_id}/config
config.provider_preference = "gemini"  # Override to Gemini
config.provider_preference = "grok"    # Force Grok
config.provider_preference = None      # Use default (grok)
```

---

## Troubleshooting

### Error: "No module named 'openai'"

**Cause:** Missing `openai` package in venv.

**Fix:**

```bash
source venv/bin/activate
python -m pip install "openai>=1.0.0"
```

### Error: "tool_choice was set but no tools were specified"

**Cause:** Old GrokAdapter version (before 2026-02-12 fix).

**Fix:** Update to latest `grok_adapter.py` (line 118 fix applied).

### Slow Responses

**Expected:** Grok reasoning takes 2-3s (normal).

**Optimization:** Switch Quick Agent to Gemini if speed > quality:

```python
# agent_context_builder.py
"quick": {"default_provider": "gemini"}
```

---

## Future Enhancements

### Planned

- [ ] Native web_search integration for WebSearchAgent
- [ ] Code execution tool for debugging assistance
- [ ] Prompt caching support (when xAI releases)
- [ ] Vision support (when available)

### Experimental

- [ ] Dynamic provider selection based on complexity score
- [ ] A/B testing framework for Grok vs Gemini
- [ ] Cost optimization with tier-based routing

---

## References

- **xAI Documentation:** https://docs.x.ai/
- **Grok API Reference:** https://docs.x.ai/api-reference
- **Native Tools:** https://docs.x.ai/developers/tools/overview
- **RFC:** `docs/10_rfcs/` (future)
- **ADR:** `docs/09_decisions/` (future)

---

## Changelog

### 2026-02-12 - Initial Integration

- ✅ Created GrokAdapter (~360 lines)
- ✅ Updated 9 configuration files
- ✅ Added GCP Secret Manager support
- ✅ Fixed tool_choice bug
- ✅ Fixed model tier mapping
- ✅ Added native tools support
- ✅ Set Grok as default for Router + Quick agents
- ✅ Performance benchmarking completed
- ✅ Production deployment successful

---

## Contact

**Maintainer:** Development Team  
**Status:** ✅ Production Ready  
**Last Updated:** 2026-02-12
