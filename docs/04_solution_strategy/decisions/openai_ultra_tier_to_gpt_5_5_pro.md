# Decision: Upgrade OpenAI ULTRA tier from gpt-5.4-pro to gpt-5.5-pro

**Status:** Adopted
**Date:** 2026-05-30
**Context:** OpenAI released GPT-5.5 family on 2026-04-24. The 5.5 family is intentionally incomplete — there is `gpt-5.5` and `gpt-5.5-pro`, but no `gpt-5.5-mini` or `gpt-5.5-nano`. OpenAI keeps `gpt-5.4-mini` and `gpt-5.4-nano` as the cheap production-routing tier and positions `gpt-5.4` as a "lower-cost frontier" option. Neither `gpt-5.4` nor `gpt-5.4-pro` is in the deprecations table.

## Decision

Migrate only **ULTRA** tier: `gpt-5.4-pro` → `gpt-5.5-pro`. Keep ECO, BALANCED, PERFORMANCE unchanged.

## Why ULTRA only

- **ULTRA migration is free.** Both `gpt-5.4-pro` and `gpt-5.5-pro` are priced at $30/M input + $180/M output. Same cost, newer model, better benchmarks (SWE-bench 88.7 vs 71.7, ~80% fewer factual errors on LongFact/FactScore). Pure upside.
- **PERFORMANCE migration would double the cost.** `gpt-5.4` → `gpt-5.5` doubles input ($2.50 → $5.00) and output ($15 → $30) pricing. With no deprecation deadline on `gpt-5.4`, paying 2× now is not justified by the quality jump for our workloads.
- **ECO and BALANCED have no 5.5 equivalents.** OpenAI did not release `gpt-5.5-nano` or `gpt-5.5-mini`. Nothing to migrate to.

## Triggers to revisit

- **`gpt-5.4` enters the deprecations table** with a shutdown date → migrate PERFORMANCE to `gpt-5.5` regardless of cost (deadline forces it).
- **OpenAI releases `gpt-5.5-mini` or `gpt-5.5-nano`** → consider BALANCED / ECO migration if pricing is competitive.
- **Measurable quality regression on ULTRA tier post-upgrade** → roll back to `gpt-5.4-pro` (still GA, no deprecation).

## Rejected alternatives

- **Full family swap (ECO/BALANCED/PERFORMANCE/ULTRA all → 5.5).** Impossible: 5.5-nano and 5.5-mini do not exist; would also double PERFORMANCE cost with no forcing deadline.
- **Do nothing.** ULTRA upgrade is free with strict quality upside — there is no rational reason to leave it on the older snapshot.
- **Pin to a specific gpt-5.5-pro snapshot** (e.g. `gpt-5.5-pro-2026-04-24`). The unversioned alias is fine for solo-project cadence; pinning would add maintenance burden without proportional safety gain for a single-user workload.

## Scope

- `src/adapters/openai_adapter.py::MODEL_TIERS[ULTRA]` — string swap.
- `src/domain/billing.py::_TOKEN_COSTS` — add `gpt-5.5-pro` entry ($30 / $180 / no cache).
- New unit test `test_openai_model_for_tier_ultra` (pre-existing test covered ECO/BALANCED/PERFORMANCE only).
- Doc sweep: `docs/05_building_blocks/openai_integration/README.md`, `docs/05_building_blocks/smart_agent_execution/COMPLEXITY_EXECUTION_SETTINGS.md`.

## Related

- `openai_deep_research_adapter_deferred.md` — gpt-5.5-pro is also the deprecation replacement for `o3-deep-research`, but DR migration is deferred until OpenAI clarifies behavioural parity.
