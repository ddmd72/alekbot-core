# Claude Sonnet 5 adoption (PERFORMANCE tier)

**Status:** Accepted — 2026-07
**Scope:** `ClaudeAdapter` (Consolidation + any Claude-tier Smart path) — shipped 2026-07-02.
The Deep-Research path (`ClaudeDeepResearchAdapter` kick-off + `ClaudeDeepResearchRunnerAgent`
native-SDK loop, Cloud Run Job) was migrated in a follow-up — see **Deep Research follow-up** below.

## Decision

Make **`claude-sonnet-5`** the default model for the Claude `PERFORMANCE` tier (was
`claude-sonnet-4-6`). Sonnet 5 is ~Opus-4.8 quality at Sonnet price (intro $2/$10 through
2026-08-31, standard $3/$15), a substantial jump over 4.6.

Rollout is guarded on three levers so a bad model day never becomes an outage:

1. **Instant rollback flag** — `CLAUDE_PERFORMANCE_MODEL` env var overrides the tier→model map at
   adapter construction (`ClaudeAdapter(performance_model=…)` also accepts it). Set it to
   `claude-sonnet-4-6` to revert with no redeploy.
2. **Same-provider runtime fallback** — `_MODEL_FALLBACK = {"claude-sonnet-5":"claude-sonnet-4-6"}`.
   On a **transient/capacity** error (529 / 503 / 5xx) the adapter retries once on Sonnet 4.6
   before the request propagates to the existing cross-provider (`AgentExecutionContext`) failover.
   **Not** applied to 4xx — a 400 is a real incompatibility we must surface, not mask.
3. Both compose: the flag is the operator kill-switch; the fallback is per-request self-healing.

## Breaking changes handled (verified against platform.claude.com migration guide)

| Change | Handling in `ClaudeAdapter` |
|---|---|
| `temperature`/`top_p`/`top_k` non-default → **400** | `_NO_SAMPLING_MODELS` (`sonnet-5`, `opus-4-7`, `opus-4-8`, `fable`) → the sampling param is **omitted entirely** (Anthropic's recommended path). Also fixes a latent Opus-4.8 risk. Sonnet 4.6 / Opus 4.6 / Haiku keep receiving `temperature` (incl. the forced 1.0 when thinking is on). |
| Adaptive thinking **on by default** when `thinking` omitted (unlike 4.6) | `_ADAPTIVE_DEFAULT_ON_MODELS = ("claude-sonnet-5",)` → sends `thinking:{type:"disabled"}` when no effort is requested, preserving the caller's "no thinking" intent (e.g. `ConsolidationAgent.thinking_effort=None`) and avoiding surprise thinking-token cost / behaviour drift. |
| New tokenizer, **~30% more tokens** | No code change needed for Consolidation (`max_tokens=64_000`, ample headroom). Flagged for any future tight `max_tokens` on a Claude PERFORMANCE path. |
| Manual extended thinking `budget_tokens` → 400 | N/A — the adapter already uses `thinking:{type:"adaptive"}` + `output_config.effort`, never `budget_tokens`. |
| Billing | Added `claude-sonnet-5` to `billing._PRICING_PER_MILLION_TOKENS` at standard $3/$15 (tracks list price, not the temporary promo, so cost is never under-reported). |

## Alternatives considered

- **Keep default 4.6, opt-in via flag only.** Rejected: the point is to ship the quality/price
  win; a flag defaulting off just defers it. The flag still exists for rollback.
- **Hard swap, no flag/fallback.** Rejected: a brand-new model's first days carry capacity-blip
  and unknown-incompatibility risk; rollback = commit+deploy is too slow for a live incident.
- **Cross-provider fallback only (existing claude→gemini).** Kept, but insufficient alone —
  falling straight to Gemini abandons Claude quality on a transient Sonnet-5 blip. The
  same-provider Sonnet-5→Sonnet-4.6 hop keeps quality on Claude first.
- **Force `temperature=1.0` for Sonnet 5** (1.0 is the API default, so accepted). Rejected in
  favour of omitting entirely — unambiguous per the migration guide ("omit these parameters"),
  and it also covers `top_p`/`top_k` should they ever be added to `LLMRequest`.

## Deep Research follow-up (2026-07-02)

Deep Research is cost-sensitive (Opus/Fable are out of budget for long-horizon runs), so Sonnet 5
— ~Opus-4.8 quality at Sonnet price — is the deliberate quality/cost compromise here too.

The DR path is a **separate code surface** from `ClaudeAdapter`: a kick-off adapter
(`ClaudeDeepResearchAdapter`, resolves a model name → Cloud Run Job) plus a runner agent
(`ClaudeDeepResearchRunnerAgent`) that calls the Anthropic SDK **directly** with native built-in
tools (`web_search_20260209` / `web_fetch_20260209` / auto-injected `code_execution`). The same
Sonnet 5 breaking changes therefore had to be re-handled on that native-SDK path:

| Change | Handling in the DR path |
|---|---|
| Model default | `ClaudeDeepResearchAdapter.MODEL_TIERS`: BALANCED + PERFORMANCE `claude-sonnet-4-6` → **`claude-sonnet-5`** (BALANCED is the DR default tier). ECO→Haiku, ULTRA→Opus 4.8 unchanged. |
| `temperature` non-default → 400 | `ClaudeDeepResearchRunnerAgent._research_loop` now **omits `temperature`** for the new-gen set and keeps `temperature=1.0` only for older thinking models (Sonnet 4.6 / Opus 4.6) + Haiku. Also fixes a **latent 400** that was live on the ULTRA/Opus-4.8 path (it was sending `temperature=1.0` to a no-sampling model). |
| New tokenizer (~+30% tokens) | `max_tokens` raised **64K → 96K** for the new-gen set (same-length reports emit ~30% more tokens; 96K stays under the 128K extended-output beta ceiling and only bills actual output). Older thinking models keep 64K. |
| Adaptive thinking | Unchanged — the DR loop already runs `thinking:{type:"adaptive"}` + `output_config:{effort:"high"}`, which Sonnet 5 accepts. DR **wants** thinking on (unlike Consolidation), so no `disabled` gate here. |
| Native tools | No change — `web_search_20260209` / `web_fetch_20260209` were already validated on Sonnet 5 via `ClaudeAdapter._DYNAMIC_SEARCH_MODELS` (WebSearch agent, shipped 2026-07-02). |

**Single new-gen set drives both effects.** The models that reject non-default sampling AND use the
new tokenizer are the same generation, so both behaviours key off one `_NO_SAMPLING_MODELS` tuple.

**Why the constant is duplicated, not shared.** `ClaudeDeepResearchRunnerAgent` lives in `agents/`,
which **REQ-ARCH-12 bars from holding provider model-name strings** (as does `domain/`), and an agent
may not import an adapter or `config/`. There is no layer both the adapter and the runner can import
without breaking isolation, so `_NO_SAMPLING_MODELS` (like the pre-existing `_THINKING_MODELS`) is a
deliberate per-file mirror; the runner is whitelisted in `arch_tech_debt.py::MODEL_NAME_WHITELIST_FILES`
for exactly this reason. A "DRY into domain" refactor would violate the repo's own architecture test.

**Rollback lever (coarse).** `CLAUDE_DEEP_RESEARCH_MODEL` (adapter `model_override`) overrides **every**
tier, not just PERFORMANCE — so it's a break-glass kill-switch: `make dr-rollback` pins all DR tiers to
Sonnet 4.6 (losing tier differentiation until `make dr-forward` removes it). The forward default ships
via the `MODEL_TIERS` code flip with the override left unset, so ULTRA→Opus 4.8 / ECO→Haiku stay intact.

## Validation

- Wire tests (`tests/unit/adapters/test_claude_adapter.py`): temperature omitted for Sonnet 5 /
  present for 4.6; Opus 4.8 omits temperature; env/arg tier override; transient→Sonnet-4.6 fallback;
  no fallback on 4xx; `thinking:{type:"disabled"}` on Sonnet 5 with no effort.
- Consolidation quality: offline A/B dry-run diff (Sonnet 4.6 vs 5) on a fixed batch set — metrics
  only (fact count / dedup / tag+metadata coverage), no live writes. Run as UAT before flipping
  traffic; the `CLAUDE_PERFORMANCE_MODEL` flag makes the A/B a config toggle.
