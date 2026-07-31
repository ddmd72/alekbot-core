# Billing: the ledger prices each model that ran, not the agent's default

**Date:** 2026-07-30
**Status:** Accepted
**Supersedes part of:** `billing_execution_scoped_ledger.md` (scope was correct; price was not)

## Context

`_flush_billing` priced a whole execution with `self.model_name` — the agent's **default**
model from its `AgentExecutionContext` — because `TokenLedger` was a single bucket holding
one set of counters. Two live divergences in opposite directions (TD-7):

- **Smart under-reported.** It resolves its tier per request (`deep_reasoning` → ULTRA →
  `gpt-5.6-sol`, $5/$30) while the default stays BALANCED (`gpt-5.6-luna`, $1/$6).
- **WebSearch `fetch_url` over-reported** since 2026-07-29: the per-intent ECO downgrade runs
  `gpt-5.4-nano` ($0.20/$1.25), still billed as luna.

> Prices above are the ones in force when this defect was found. OpenAI cut luna and terra the
> next day — see [`openai_gpt56_price_cut.md`](openai_gpt56_price_cut.md). The divergence this
> record describes is unaffected: it is about *which* model prices a turn, not what that model
> costs. The reproduction figures below stay at the old prices, or they stop reproducing.

Reproduced to the cent on the 2026-07-30 briefing: counter $1.4458 vs $1.1815 priced at each
call's real model; repricing Smart + fetch_url as luna reproduces $1.4458 exactly. Token
counts were never wrong — BigQuery billable tokens matched `usage.daily_tokens` exactly two
days running.

## Decision

`TokenLedger.by_model: Dict[str, ModelUsage]` — one leg per model, `add(model, …)` from
`_call_llm`, `cost()` sums each leg at its own rates. `_flush_billing` no longer reads
`self.model_name` at all.

The billing basis is `request.model_name` — the model the turn actually ran on — rebound to
`fallback_request.model_name` when a cross-provider failover re-serves the turn. Smart's
provider rotation needs no special handling: it rebuilds the run, and every `_call_llm`
carries its own model.

`QuotaService.record_usage(model=…)` keeps its parameter and receives `dominant_model` (the
costliest leg). It is a label only — no implementation forwards it to
`increment_account_usage` — so the port contract is untouched.

## Alternatives rejected

- **Keep one bucket, pass the *resolved* model instead of the default.** Cheaper, but an
  execution legitimately spans two models (tier escalation, intent downgrade, failover); one
  bucket cannot express that, and the next such split would re-open the same bug.
- **Optional `model` on `add()` with an "unknown" bucket** (zero test edits). Dual bookkeeping
  behind one API — the exact partial fix the repo's clean-or-defer rule excludes.
- **Drop `model` from `QuotaService.record_usage`.** It is dead weight, but removing it is a
  port change touching `BillingAgent` and its tests for no behavioral gain. Out of scope.
- **Aggregate `prompt_tokens`/… properties on the ledger.** Summing token counts across models
  is only meaningful for `total_tokens`; the per-leg aggregates had no production reader and
  were the shape that made the defect natural. Removed.

## Consequences

- `daily_cost` / `monthly_cost` / `total_cost`, the 09:00 Slack summary and the advisory $5
  budget alert now carry the true price. Expect **daily cost to rise** on interactive days —
  the Smart under-report dominated; $5 will trip more often (advisory only, never a gate).
- Historical cost stays wrong in two ways: inflated tokens before 2026-07-28, wrong price
  before 2026-07-30. BigQuery `prompt_content` remains the only historical source.

## Verification

`tests/unit/domain/test_billing_accounting.py` — a two-model execution costs the sum, not
either model's price on all tokens; downgraded leg priced at its own rate; unpriced leg
contributes 0 without voiding the rest; `dominant_model` picks the costliest leg.
`tests/unit/test_base_agent.py::TestFlushBilling` — the flush ignores the agent default in
both directions (escalation and downgrade).
`tests/unit/agents/core/test_base_agent_fallback.py` — a failover books its usage under the
fallback model; a same-provider retry stays on the primary.
