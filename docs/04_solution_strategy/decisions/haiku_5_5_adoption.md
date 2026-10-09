# Claude Haiku 5.5 replaces Haiku 4.5 on the small tiers

**Date:** 2026-10-08
**Status:** Accepted (deploy + A/B pending)
**Related to:** `temperature_removed.md`, `claude_sonnet_5_adoption.md`, `model_refresh_2026_10.md`

## Context

Haiku 5.5 shipped 2026-10-07 (`claude-haiku-5-5`, 1M context, 128K output). At $0.10 / $0.50 per MTok
(prompts <= 100K; 5x above) it is 10x cheaper than Haiku 4.5 ($1 / $5), and the first Haiku with
adaptive thinking and `effort`. Claude is the default provider for Quick, doc_planner,
doc_generator, tutor_extractor and consolidation, so the small tiers carry real traffic (Quick = ECO).

## Probed live (2026-10-08, `scripts/memory/probe_haiku_5_5.py`, gitignored)

| Request | Result |
|---|---|
| `temperature=0.5` | 400 "deprecated for this model" (`temperature=1` accepted; we send none) |
| `thinking: enabled` / `between_tools` | not supported (capabilities: adaptive + disabled only) |
| `thinking: disabled` | OK at default / `low`; 400 with `effort: xhigh` |
| assistant prefill | 400 |
| forced `tool_choice: any` | OK |
| `output_config.format` (schema) | OK, with and without thinking |
| `web_search_20250305` | OK; `web_search_20260209` accepted but returned a failed search once |
| effort `low` / `medium` / `high` / `xhigh` / `max` | all supported |

## Decision

- ECO / BALANCED / TIER1-3 on `ClaudeAdapter` (and ECO / TIER1-3 on the Deep Research adapter) point at
  `claude-haiku-5-5`. `CLAUDE_SMALL_MODEL` repoints all five tiers at once (rollback to Haiku 4.5 with
  no redeploy), the sibling of `CLAUDE_PERFORMANCE_MODEL`.
- `claude-haiku-5` joins `_THINKING_MODELS` (adaptive + `output_config.effort`) in the adapter and in
  the Deep Research runner, and joins the runner's new-generation set (new tokenizer, ~+30% tokens →
  96K output headroom). The prefix does not match `claude-haiku-4-5-…`.
- **`thinking=None` sends no `thinking` field** on Haiku 5.5 (adaptive at the default `medium` effort),
  not `disabled`. Anthropic's prompting guide warns that with thinking off the model may skip a tool
  call needed under JSON output, and leaks reasoning-like text into user-facing replies (also at
  `low`). Quick and the delegation agents combine tools and `response_schema`, so the safe default wins
  over the few saved thinking tokens at $0.10 / $0.50.
- Web search stays on the legacy `web_search_20250305` for Haiku (`_DYNAMIC_SEARCH_MODELS` unchanged).
- Billing: base rates added. The > 100K-prompt tier (5x) is not modelled, as for `gpt-6.1-sol`; ECO
  prompts sit far below it.

## Open / not done

- **Smart-style validation is pending.** A raw adapter smoke showed that with `tools` + a
  `response_schema` and no thinking, one contrived prompt returned JSON text instead of calling the
  tool. Run `scripts/validation/ab_agent_models.py` (and Quick on real traffic) before relying on it;
  the lever is `CLAUDE_SMALL_MODEL`.
- Refusals: Haiku 5.5 runs safety classifiers and has no server-side fallback; `_raise_on_refusal`
  already maps `stop_reason: "refusal"` to `LLMClientError` (no failover).
- `thinking` blocks are account/prefix-bound (a changed earlier turn 400s a replayed block) — the same
  constraint Sonnet 5.5 already lives with.
- Per-message effort (beta) and the `max_tokens` headroom for thinking at ECO are unexplored.
