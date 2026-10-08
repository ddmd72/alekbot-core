# `temperature` is removed from LLMRequest and every adapter

**Date:** 2026-10-08
**Status:** Accepted
**Related to:** `gemini_sampling_params_removed.md`, `claude_sonnet_5_adoption.md`

## Context

By October 2026 the sampling knob is dead or hostile at almost every provider we call:

- **Gemini** — deprecated; no effect since 3.6 Flash, hard 400 announced (Google notice 2026-10-08).
- **Claude** — Sonnet 5+, Opus 4.7+, Fable and **Haiku 5.5** 400 on a non-default value
  (Haiku 5.5: only `temperature=1` / `top_p=0.99` are accepted, or omit). Adaptive thinking
  needs the API default (1.0) anyway.
- **OpenAI** — `gpt-5` / `gpt-6` / `o*` already 400 on it; the adapter dropped it for the whole
  fleet (every tier is `gpt-6-*`).
- **Grok** — still accepted (xAI Responses API reference: 0–2, no default stated, nothing
  about reasoning models). xAI publishes no recommended value; the 0.7 we passed was ours.

What remained was ~25 agents each passing a value that three of four providers discarded,
plus per-provider gate code (`_NO_SAMPLING_MODELS`, `_REASONING_PREFIXES` for sampling) whose only job
was to throw it away.

## Decision

`LLMRequest.temperature`, every `AgentConfig.temperature` / `delegation_temperature`, every
agent's `TEMPERATURE` constant and `temperature=` kwarg, and the adapters' forwarding are deleted.
No adapter sends any sampling parameter; Grok runs at the provider default.
Behaviour is steered by prompt, `response_schema` and thinking level / effort.

## Consequences

- Agents that set 0.0–0.3 (Compute, history summary, email classification, …) now run at the
  provider default on Grok. They are Gemini/OpenAI in practice, where it was already ignored.
- A future model that needs a sampling knob re-adds it in that adapter only, not on the port.
- **Not touched:** `UserBotConfig.temperature` (domain/user.py) — a persisted, unconsumed per-user
  field that doubles as the example scalar in `ConfigurationService` merge tests. Removing it is a
  separate cleanup (stored Firestore docs keep the key; pydantic ignores extras).

## Alternatives rejected

- **Keep the port field, ignore it in adapters** — leaves 25 agents lying about their behaviour.
- **Per-model gates everywhere** — a list to maintain per provider for zero behavioural gain.
