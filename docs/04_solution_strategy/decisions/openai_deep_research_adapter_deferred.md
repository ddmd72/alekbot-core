# Decision: Defer OpenAIDeepResearchAdapter migration until OpenAI clarifies

**Status:** Deferred with explicit trigger
**Date:** 2026-05-30
**Context:** OpenAI deprecated `o3-deep-research-2025-06-26` and `o4-mini-deep-research-2025-06-26` with shutdown on 2026-07-23, recommended replacement `gpt-5.5-pro`. OpenAI's own documentation is currently contradictory: the Deep Research guide still recommends the dedicated `o3/o4-mini-deep-research` models, the `gpt-5.5-pro` model page does not position itself as a deep research replacement, and there is no published migration guide.

## Decision

Take **no action now**. Keep the adapter on `o3-deep-research-2025-06-26` / `o4-mini-deep-research-2025-06-26` until OpenAI publishes coherent migration guidance. If no guidance lands by **2026-07-16** (one week before shutdown), execute clean removal of the adapter (mirroring the Gemini DR removal from 2026-05-29).

## Why defer rather than migrate now

- **Default deep-research provider is Claude.** `agent_context_builder.py::AgentProviderStrategy["deep_research"].default_provider = "claude"`; the OpenAI path is reachable only when a user explicitly sets `agent_providers["deep_research"] = "openai"` in `UserBotConfig`.
- **Single-user usage, owner-only.** The author uses Deep Research only for themselves and the Claude runner is already configured with reasoning sufficient for their needs. The OpenAI path is kept as a manual A/B alternative they recently unlocked via tier upgrade.
- **Cost gap is significant.** gpt-5.5-pro is $30/M input + $180/M output — roughly 3–4.5× more expensive than o3-deep-research per typical run. Migrating without OpenAI's behavioural guidance risks paying that premium for unverified parity.
- **OpenAI guidance is contradictory.** Deprecation table vs Deep Research guide vs gpt-5.5-pro model page disagree. Acting on the most aggressive interpretation today would lock in an adapter shape that may not match the official path when it does land.

## Why clean removal is the fallback, not "pin and hope"

- The dedicated `*-deep-research-*` models are scheduled to be removed from the API on 2026-07-23. There is no SDK-version escape; the model ID itself stops responding.
- Pinning to the deprecated snapshot past 2026-07-23 would mean a silent failure in the OpenAI fallback path, exactly the kind of speculative-seam-becomes-rotten-code shape `feedback_clean_or_explain.md` rules out.
- Claude DR is already the default and meets the author's needs; the OpenAI fallback is optional, not load-bearing.

## Trigger to revisit (in order)

1. **2026-06-15 — first checkpoint.** Re-read OpenAI deprecations page, deep-research guide, gpt-5.5-pro page. If a coherent migration path exists by then → execute migration (probe + decision record + adapter swap + tests + UAT).
2. **2026-07-16 — final checkpoint.** Last call. Either migration is clearly mapped or we **remove the adapter** with the same scope as `gemini_deep_research_adapter_removal.md`: adapter file, tests, `"openai"` from `deep_research.allowed_providers`, doc sweep, decision record updated to `removed`.

## Rejected alternatives

- **Migrate now to gpt-5.5-pro.** Premature: docs incoherent, cost 3–5× higher, behavioural parity unverified. Likely PR-redo when official guidance lands.
- **Pin to deprecated model and hope shutdown slips.** Violates `feedback_clean_or_explain.md`: a shape that will need re-doing later does not count as clean, and a silent 4xx after 2026-07-23 is the worst end-state.
- **Remove the adapter today.** Premature — there is still 8 weeks for OpenAI to publish coherent guidance that makes migration cheap; the manual-A/B option for the author has standing value if migration is straightforward.

## Related

- `gemini_deep_research_adapter_removal.md` — the precedent template if the 2026-07-16 trigger fires the removal path.
- `feedback_clean_or_explain.md` — clean implementation OR explicit durable deferral. This record is the deferral form for this surface.
