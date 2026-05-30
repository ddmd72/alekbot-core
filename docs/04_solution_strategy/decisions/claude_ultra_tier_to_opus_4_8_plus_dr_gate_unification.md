# Decision: Upgrade Claude ULTRA tier to opus-4-8 and unify DR runner thinking gate with main adapter

**Status:** Adopted
**Date:** 2026-05-30
**Context:** Anthropic released Claude Opus 4.8 on 2026-05-28 (41 days after 4.7). Same pricing as 4.7 ($5/M input, $25/M output). Benchmark improvements: SWE-Bench Pro 64.3% → 69.2%; ~4× reduction in code-flaw-pass-through. Separately, code inspection revealed a divergence between the main `ClaudeAdapter` (substring gate `("claude-sonnet", "claude-opus")`) and the DR runner agent (exact-match set `{"claude-sonnet-4-6", "claude-opus-4-6"}`) — meaning ULTRA DR runs on `claude-opus-4-7` currently fall to the Haiku-style fallback path (`thinking={enabled, budget=24000}`, `max_tokens=32000`) instead of the high-effort path (`thinking=adaptive`, `effort=high`, `max_tokens=64000`).

## Decision

Bundle two changes into one PR:

1. **ULTRA model swap**: `claude-opus-4-7` → `claude-opus-4-8` in both `ClaudeAdapter.MODEL_TIERS` and `ClaudeDeepResearchAdapter.MODEL_TIERS`.
2. **Unify DR runner gate**: replace `_THINKING_MODELS = {"claude-sonnet-4-6", "claude-opus-4-6"}` exact-set with substring tuple `("claude-sonnet", "claude-opus")` matching `claude_adapter.py:87`. Use `any(m in model for m in self._THINKING_MODELS)` in the gate check.

## Why bundle

- Both touch ULTRA Claude path; one PR, one decision record, one verify pass.
- Unifying the gate is the only way to make the ULTRA → 4-8 swap produce the intended behaviour (adaptive thinking + effort=high). Without the gate fix, ULTRA on 4-8 would still hit the Haiku fallback — a partial migration that locks in the divergence.

## Why substring over expanded exact set

- The main adapter already uses substring — this restores **behavioural parity** between the two Claude code paths instead of maintaining two patterns.
- Future Claude 4-9 / 5-0 / etc. would be auto-included without touching the gate again. The exact-set form already required at least one forgotten update (4-7 not added when MODEL_TIERS migrated to it).
- Risk: if there was a hidden behavioural reason DR runner restricted to 4-6 — e.g. an observed regression on `output_config.effort` with 4-7 — we unmask it. Mitigation: revert is a one-line set restoration; ULTRA DR runs are owner-only and observable post-deploy.

## Why claude-opus-4-8 over staying on 4-7

- Same price.
- Strictly better benchmarks (per Anthropic's own card).
- Opus 4-7 is not in any active deprecations table, so this is voluntary; the upside is asymmetric (free quality bump, easy revert).

## Triggers to revisit

- ULTRA DR runs after deploy show **lower quality** than on 4-7 with Haiku-fallback config → revert to `claude-opus-4-7` + substring gate (keep the gate fix; only revert the model).
- `output_config.effort` on 4-7 or 4-8 returns 400 error → narrow the substring gate (re-introduce exact-set form, this time correctly listing every supported model).
- Anthropic deprecates 4-8 → migrate to next; the substring gate handles it automatically.

## Rejected alternatives

- **Model swap only, leave the gate.** Locks in the documented divergence between adapters for another model version. ULTRA DR still gets Haiku-fallback config on 4-8.
- **Expand the exact set to include 4-7 and 4-8.** Same forgotten-update risk we just hit; doesn't address the architectural inconsistency with main adapter.
- **Stay on 4-7.** No reason given that 4-8 is same price + better benchmarks.

## Scope

- `src/adapters/claude_adapter.py::MODEL_TIERS[ULTRA]` — model swap.
- `src/adapters/claude_deep_research_adapter.py::MODEL_TIERS[ULTRA]` — model swap.
- `src/agents/claude_deep_research_runner_agent.py::_THINKING_MODELS` — exact-set → substring tuple + `any(... in model ...)` gate.
- `src/domain/billing.py::_TOKEN_COSTS` — add `claude-opus-4-8` entry ($5 / $25 / $0.10 / $1.25).
- Update existing test `test_claude_adapter.py::test_claude_model_for_tier_ultra` (per-test permission granted 2026-05-30).
- New tests in DR runner test file for the unified substring gate behaviour on 4-7 and 4-8.
- Doc sweep: `docs/05_building_blocks/smart_agent_execution/COMPLEXITY_EXECUTION_SETTINGS.md`.

## Related

- `openai_ultra_tier_to_gpt_5_5_pro.md` — yesterday's equivalent for OpenAI ULTRA (same shape: free upgrade + same price + decision record).
- `feedback_clean_or_explain.md` — unifying with main adapter is the clean form; leaving the divergence is the rejected partial fix.
