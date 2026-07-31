# The provider's announced price outranks the price catalogs

**Date:** 2026-07-31
**Status:** Accepted
**Relates to:** `billing_per_model_pricing.md` (what to price), this record (what the price *is*)

## Context

OpenAI cut GPT-5.6 list prices on **2026-07-30**, one day after the per-model pricing fix
landed:

| model | was | now | tier |
|---|---|---|---|
| `gpt-5.6-luna` | $1.00 / $6.00 | **$0.20 / $1.20** (−80%) | BALANCED |
| `gpt-5.6-terra` | $2.50 / $15.00 | **$2.00 / $12.00** (−20%) | PERFORMANCE |
| `gpt-5.6-sol` | $5.00 / $30.00 | unchanged | ULTRA |

Read off the provider's own pricing page. Cached input is quoted at $0.02 / $0.20 / $0.50 —
still exactly 0.1× input, so the `cache_read` and `cache_write` multipliers are untouched.

Both cut models are live tier defaults, so every BALANCED and PERFORMANCE run on OpenAI was
over-reported until the table was corrected. The exposure window is two days.

`make check-pricing` could not simply be re-run against this. Its verdict rule is two-catalog
consensus (LiteLLM + models.dev), and a day after the cut **LiteLLM still quoted the pre-cut
numbers while models.dev quoted luna at a wrong $0.1/$0.6**. Consensus therefore stood against
the correct price: terra would have come back as `consensus_differs` — the one verdict the
runbook marks as actionable — inviting a revert to a stale number.

## Decision

The dated `PRICE_SCHEDULE` in `scripts/validation/price_consensus.py` carries the change
(`effective 2026-07-30`), and the schedule branch now runs **before** the coverage and
agreement checks.

The ordering is the substantive part. The module always claimed "a dated schedule entry wins
over consensus", but the code reached the schedule only after establishing a consensus — so
luna, whose catalogs contradict each other, returned `sources_disagree` and never consulted its
own entry. A schedule entry is a price a human verified at the provider; it must outrank
catalogs that cover the model badly, or not at all.

`schedule_stale` now reads "either the schedule is wrong, or they have not caught up with a
change we verified at the provider" instead of asserting the schedule is at fault.

## Alternatives rejected

- **Edit `billing.py` only.** Leaves the audit demanding a revert on every run. The whole point
  of the audit is to be trusted; one wrong actionable verdict is worse than none.
- **A new `catalog_lag` verdict with a grace window.** More precise — it could distinguish "the
  catalogs lag" from "our schedule is wrong" by checking whether consensus matches a *previous*
  scheduled price. Rejected as unnecessary machinery for a condition that resolves itself in
  days; the reworded `schedule_stale` states both readings and costs nothing.
- **Track the pre-cut price deliberately (the `HOLD_FINAL_PRICE` treatment given to Sonnet 5's
  promo).** That policy exists to never *under*-report while an introductory rate runs out. This
  is the opposite case: a permanent cut with no expiry, where holding the old price only
  inflates every report.

## Consequences

- OpenAI spend reported for 2026-07-30..31 is high — luna 5× and terra 1.25× on those two days.
  Third correction to historical cost in a week (tokens before 2026-07-28, attribution before
  2026-07-30, price on these two days). Any cost comparison spanning them needs BigQuery
  `prompt_content`, repriced.
- `gpt-5.6-terra` shows as `schedule_stale` in the audit until the catalogs refresh. Expected,
  not a defect. **Delete both `PRICE_SCHEDULE` entries once the catalogs carry the cut** — they
  are a bridge over catalog lag, not a permanent record.
- The ECO/BALANCED price gap is gone: `gpt-5.4-nano` ($0.20/$1.25) and Luna ($0.20/$1.20) now
  cost the same on input, and nano is marginally dearer on output. Keeping nano on ECO is now a
  latency argument only. Left as an open question — it needs latency measurements, not a price
  table. Two conclusions in `GPT_5_6_MIGRATION_RFC.md` are inverted by this and flagged there.
