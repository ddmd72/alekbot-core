# OpenAI model tiers migrated to GPT-5.6 family (Luna / Terra / Sol)

**Date:** 2026-07-30
**Status:** Accepted
**Related to:** `openai_gpt56_price_cut.md` (price changes on the same date), `docs/10_rfcs/GPT_5_6_MIGRATION_RFC.md` (full design and rationale)

## Context

GPT-5.6 went GA on 2026-07-09 as a three-tier family (Luna / Terra / Sol). `gpt-5.4-mini`
(our BALANCED tier) is scheduled for **API shutdown 2026-12-11** — a forcing function that
required the migration to complete by year-end.

Full design, cost analysis, and risk verification are in the RFC; this record captures
the single decision: which model gets which tier, and why.

## Decision

### Tier → Model Mapping (DONE 2026-07-30)

| Tier | Previous | New | Rationale |
|---|---|---|---|
| ECO | `gpt-5.4-nano` | `gpt-5.4-nano` | Stays. Measured 2.3× latency win vs Luna on router triage; nano beats Luna even after price cut because Luna spends ~183 hidden reasoning tokens per call at API defaults. Moving ECO to Luna requires explicit `effort=none` (unsupported in the adapter). See `openai_gpt56_price_cut.md` Consequences §3. |
| BALANCED | `gpt-5.4-mini` (deprecated) | **`gpt-5.6-luna`** | Forced by 2026-12-11 shutdown. Luna was $0.20/$1.20 post-cut (−80% vs pre-cut); competitively priced vs Claude Haiku ($1/$5). Reasoning effort defaults to `low`. |
| PERFORMANCE | `gpt-5.4` | **`gpt-5.6-terra`** | Forced consistency; terra was $2/$12 post-cut (−20%). Reasoning effort defaults to `high`. |
| ULTRA | `gpt-5.5-pro` | **`gpt-5.6-sol`** | Forced consistency; sol unchanged at $5/$30. Reasoning effort defaults to `high`. |
| TIER1–3 | `gpt-4o-mini` | `gpt-5.4-nano` | Historical tier, unused in live code; left at ECO tier. |

### Data Validation (2026-07-13, live probe)

- **Reasoning effort floor:** `gpt-5.6-{luna,terra,sol}` all accept `low` / `high` (no floor, unlike `gpt-5.5-pro`'s `medium` floor). Tested against live API.
- **Prompt caching:** `prompt_cache_retention="24h"` is silently ignored by OpenAI (not a 400 error). Works as configured, no regression.
- **Cache multipliers:** Cached input on all three tiers is $0.02 / $0.20 / $0.50 = **0.1× list input** (same as legacy models). No `cache_write` on legacy; all 5.6 models support it at **1.25×** the base output rate.

### Implementation

- `src/adapters/openai_adapter.py::MODEL_TIERS` flipped to Luna/Terra/Sol. `gpt-5.4-mini` kept only in `billing.py` as a rollback row (historical cost).
- `src/domain/complexity_settings.py`: reasoning effort per tier via `_reasoning_effort()`.
- Wire tests + contract validators in `tests/unit/adapters/` and `tests/contracts/adapter_contracts.py`. All passing.
- Rolled back `gpt-5.4-mini` from Azure's "copy of" + OpenAI at the same time; now gone from tier map.

## Alternatives Rejected

- **Move ECO to Luna anyway.** Measured latency is 2.3× slower (triage workload). Requires API support for `effort=none` (does not exist in the adapter). Not worth the complexity for a move that breaks latency SLA.
- **Defer ULTRA migration to later.** Creating unnecessary tech debt — sol is available, cheaper, and no shutdown forcing it. One migration is cleaner than staged.
- **No decision record — RFC alone is sufficient.** Conventions exist to prevent documentation drift; a shipped single-decision like this belongs here for searchability and future audit.

## Consequences

- **Shutdown deadline met:** migration ships 2026-07-30, gpt-5.4-mini EOL is 2026-12-11.
- **Cost swing:** Luna and terra saw same-day cuts (-80% / -20%), making BALANCED/PERFORMANCE 1.8–5× cheaper than before. Spend reported high for 2026-07-30..31 (cost corrections recorded in `openai_gpt56_price_cut.md`).
- **Compatibility:** no caller-facing API change. Router complexity logic, provider strategy, and agent commissioning remain unchanged.
- **ECO latency preserved:** nano stays ECO; the 2.3× latency win is production fact, not a theory. Future re-evaluation of Luna (if API adds `effort=none` support) is a separate decision.

## Verification

- `make test-unit` passes (billing models, adapter contracts, complexity settings).
- `make test-integration` covers OpenAI wire tests (mock SDK boundary, validate JSON schema compliance).
- Live triage latency: `scripts/validation/ab_router_latency_nano_vs_luna.py` (2026-07-31, 56 real calls, nano 2.3× faster). Baseline in repo memory; re-run annually or after major router changes.
- Billing regression: `make test-unit` billing tests confirm no model is unpriced, cache multipliers are in place.
