# Gemini sampling parameters are not sent

**Date:** 2026-10-08
**Status:** Accepted
**Related to:** `model_refresh_2026_10.md`, `gemini_thought_parts_split.md`

## Context

Google sent a deprecation notice: requests carrying `thinking_budget`, `temperature`, `top_p` or
`top_k` are deprecated. Per Google's Gemini 3.x guides, the three sampling parameters have had no
effect since Gemini 3.6 Flash, an upcoming API update turns them into `400 INVALID_ARGUMENT`, and
`thinking_budget` will stop being remapped to `thinking_level`.

## Audit

- `thinking_budget` — already absent from `src/`; `GeminiAdapter` sends `thinking_level` only.
  Only a POC script (`scripts/email/test_email_classification_poc.py`) used it.
- `top_p` / `top_k` — never sent.
- `temperature` — **the live offender.** `LLMRequest.temperature` defaults to 0.7 and every agent
  passes it, so every Gemini call (Router, WebSearchLight, ECO/BALANCED agents) forwarded it.

## Decision

`GeminiAdapter.generate_content` no longer puts `temperature` into `GenerateContentConfig`.
`LLMRequest.temperature` stays on the port: Claude (older models) and other providers still use it.
Determinism for JSON agents rests on `response_schema` + `thinking_level`, as Google recommends.
Pinned by `test_deprecated_sampling_and_budget_params_not_sent`.

## Alternatives rejected

- **Per-model gate (like Claude's `_NO_SAMPLING_MODELS`)** — the parameter is a no-op on every Gemini
  3.x model we run; a gate is a list to maintain for zero behavioural difference.
- **Drop `temperature` from `LLMRequest`** — other providers need it; wide blast radius for no gain.
