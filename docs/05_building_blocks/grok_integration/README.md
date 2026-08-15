# xAI Grok Integration

**Status:** ⚠️ Functional, but not a default for any agent
**Last verified against the live API:** 2026-08-14
**Provider:** xAI (Grok)

---

## Overview

`GrokAdapter` implements `LLMPort` against xAI's **Responses API** (`/v1/responses`) using the
OpenAI SDK with a custom `base_url`. No `xai-sdk` dependency — the same pinned `openai` package
serves `OpenAIAdapter`, `OpenAIDeepResearchAdapter`, and this adapter.

**Read [`decisions/grok_revival_2026_08.md`](../../04_solution_strategy/decisions/grok_revival_2026_08.md)
before changing anything here.** It records why this integration sat dead for months (a trailing
newline in the `XAI_API_KEY` secret, misdiagnosed as xAI blocking Google Cloud) and why the
transport moved off Chat Completions.

### What works (verified live, not inferred)

- Function calling, including `tool_choice="required"` and multi-turn tool round-trips
- Server-side search: `{"type": "web_search"}` (and `x_search`) — **Responses API only**
- `instructions` as the system prompt, `temperature`, `reasoning.effort`
- `response_schema` forwarded natively as `text.format.json_schema` (strict=False)
- **Image input** — `input_image` accepted on grok-4.6 and grok-4.3 (minimum 8x8 px)
- `url_citation` annotations on grounded answers → appended as a `*Sources:*` block
- `store` and `prompt_cache_key`
- Automatic prompt caching, reported via `usage.input_tokens_details.cached_tokens`
- Reasoning traces via `reasoning` output items → `LLMResponse.thought_text`

### What does not

- **File upload** — there is no Files API here, so `upload_file()` raises `NotImplementedError`
  and non-image binaries cannot be forwarded (`OpenAIAdapter` has an `input_file` path; this does
  not). Upstream `FileConversionService` normally converts those to text first.
- **Controllable prompt caching** — caching happens, but there is no API surface to steer it, so
  `context_caching=False` and `generate_content` rejects a `cache_config`.
### Role precedence — measure it, don't assume it

**`developer` > `instructions` > `user`**, the same order as OpenAI. Measured 2026-08-15: six runs
per cell, both orderings, conflicting output-language instructions.

`GrokAdapter._extract_turn_anchor` therefore lifts `USER_TURN_SYSTEM_ANCHOR` out of the last user
turn into a `developer` item, mirroring `OpenAIAdapter` — otherwise the anchor sits in the weakest
channel available.

> An earlier version of this document claimed the precedence was *reversed* on xAI. That came from
> a single run with a degenerate instruction ("reply with the word BANANA") and no control for
> ordering — i.e. noise. If you need to re-derive precedence, use a measurable non-degenerate
> signal and test both orderings.

**Not ported from `OpenAIAdapter`:** its large hand-written "PERSONALITY ANCHOR" block, injected
when the system prompt contains `humor_engine`. That text was written and validated against a
specific OpenAI failure mode and has never been tested on Grok.

---

## Model tiers

Defined in `GrokAdapter.MODEL_TIERS` — the single source of truth (no env override).

| Tier | Model | Notes |
|------|-------|-------|
| `ECO`, `BALANCED`, `TIER1/2/3` | `grok-4.3` | $1.25/$2.50 per 1M, 1M context, reasons by default |
| `PERFORMANCE`, `ULTRA` | `grok-4.6` | $2/$6 per 1M, 500k context, reasons by default |

**Both models reason by default** — probed 2026-08-15, a bare request with no `reasoning`
block still returns a `reasoning` item (4.3: 83 tokens, 4.6: 66). Earlier docs said 4.3 did
not; it does.

> ⚠️ **Retired IDs fail silently.** `grok-4-1-fast-reasoning` / `-non-reasoning` are gone from
> `GET /v1/models`, but still return HTTP 200 — xAI serves `grok-4.3` and reports it in the
> response body. A retired ID therefore does not error, it **mis-bills**. Only put IDs in this map
> that `GET /v1/models` actually lists.
>
> xAI has also retired the sub-$1 tier: the cheapest live model is now $1.25/$2.50, which is
> *more* expensive than `gpt-5.6-luna` ($0.20/$1.20) or `gemini-flash-lite` ($0.30/$2.50).

Pricing lives in `src/domain/billing.py`. `cache_read` is a multiplier of input price
(4.6 → 0.25, 4.5 → 0.15, 4.3 → 0.16). **Not modelled:** xAI doubles both input and output once a
prompt reaches 200k tokens, so long-context requests are under-costed 2x.

---

## Where Grok sits in routing

`AgentProviderStrategy.STRATEGIES` (`src/services/agent_context_builder.py`) lists grok in
`allowed_providers` for **router**, **quick**, and **smart** only. **No agent defaults to it.**
Three ways a request reaches Grok today:

1. Per-agent override — `UserBotConfig.agent_providers["smart"] = "grok"`
2. Global preference — `UserBotConfig.provider_preference = "grok"`
3. Per-complexity override — `UserBotConfig.complexity_settings_overrides[<complexity>]
   .provider_override = "grok"`. **This is the one that surprises**: it is set per task
   complexity, not per agent, so it can quietly own a whole class of background work
   (this is how the daily briefing ended up on Grok).
4. Smart's provider rotation — grok is **last** in `allowed_providers` for smart, so it is only
   tried after claude, openai, and gemini have all been attempted in one turn

Promoting Grok to a default requires an eval first, as was done for `gpt-5.4-mini`.

---

## Latency — budget for it

Measured on the real Smart delegation workload, grok-4.6 at `reasoning.effort=medium`:

| Context | Latency |
|---------|---------|
| ~20k tokens | 42s |
| ~55k tokens | 25s |
| ~73k tokens | 49s |
| ~100k tokens | **> 60s** |

The client ceiling is **300s** (matching `OpenAIAdapter`; it was 60s until 2026-08-15 and
truncated exactly the turns above). An explicit `LLMRequest.timeout` is forwarded to the SDK
as well as bounding total wall-time, so a caller can go lower or higher deliberately.

Two consequences worth holding onto:

- **A multi-turn Grok run is minutes, not seconds.** Five delegation turns at ~50s each is
  over four minutes of pure inference before any tool work. Background task types must have
  a Cloud Tasks `dispatch_deadline` that can contain that — see `docs/07_deployment/SCHEDULERS.md`.
- **`max_retries=2` multiplies a timeout by three.** Worst case for a single call is 900s.

---

## Configuration

```bash
# .env (local) — loading strips the trailing newline
XAI_API_KEY=xai-...
```

```yaml
# cloudbuild-dev.yaml — Cloud Run mounts the secret VERBATIM
--set-secrets=...,XAI_API_KEY=XAI_API_KEY:latest,...
```

**Create the secret without a trailing newline**, or the Authorization header becomes illegal and
every call dies as a bogus "connection error":

```bash
printf %s "$KEY" | gcloud secrets versions add XAI_API_KEY --data-file=- --project=<PROJECT_ID>
# verify: the last byte must NOT be 0a
gcloud secrets versions access latest --secret=XAI_API_KEY --project=<PROJECT_ID> | xxd | tail -1
```

`ServiceContainer._init_grok` returns `None` and logs `ℹ️ Grok not configured` when the key is
absent — the bot starts fine without it.

---

## Grounding costs real money

Server-side search is not a free bonus. One measured grounded query:

| | Input tokens | Cost |
|---|---|---|
| Plain call | ~200 | ~$0.0003 |
| `use_grounding=True` | ~17,000 across 5 server-side tool calls | ~$0.040 |

JSON mode is suppressed when grounding is on (search and structured output conflict), mirroring
`OpenAIAdapter`.

---

## Prompt overhead xAI adds by itself

Measured 2026-08-14 with a one-character input: **~206 tokens on grok-4.6, ~193 on grok-4.3**
arrive before anything we send. Identical on chat/completions, so it is server-side, not ours.
Our `instructions` and tool declarations stack on top linearly. ~128 of it comes back cached.
The content is not disclosed — the model refuses to reproduce it.

Declaring `{"type": "web_search"}` costs far more: input jumps from ~207 to **~3,900 tokens**
before any searching happens.

## Token accounting

`_parse_response` follows the repo-wide convention that `UsageMetadata.prompt_tokens` holds
**uncached input only**:

```
prompt_tokens     = input_tokens - cached_tokens
cache_read_tokens = cached_tokens
```

Reasoning tokens are billed as output and are included in `total_tokens`, which is why
`total_tokens != prompt_tokens + completion_tokens` on grok-4.6.

`usage.cost_in_usd_ticks` (USD × 1e10) is xAI's own cost figure — useful for reconciling
`billing.py` against the provider, though nothing reads it today.

---

## Testing

Wire tests mock at the SDK boundary `adapter.client.responses.create`
(see [`ADAPTER_WIRE_TESTING.md`](../../how_to/ADAPTER_WIRE_TESTING.md)):

- `tests/unit/adapters/test_grok_adapter.py`
- `tests/integration/adapters/` — reuses `OpenAIResponsesCapturingStub`, shared with `OpenAIAdapter`
- `tests/contracts/adapter_contracts.py` — `"grok"` validators for tool_choice and grounding
