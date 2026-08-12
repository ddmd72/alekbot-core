# Sonnet 5 introductory pricing made permanent — hold-the-standard-price policy retired

**Date:** 2026-08-12
**Status:** Accepted
**Related to:** `claude_sonnet_5_adoption.md` (which announced the reversion this cancels),
`openai_gpt56_price_cut.md` (the opposite case, decided against this policy)

## Context

Sonnet 5 launched with introductory pricing of $2/$10 per MTok and a scheduled reversion to
$3/$15 on 2026-09-01. `billing.py` deliberately encoded the **standard** $3/$15 rather than the
rate actually in force, so that cost was never *under*-reported and no edit would be needed when
the promo expired. The trade-off was explicit and measured: Sonnet spend read **1.5× high** (July
2026: $16.19 actual vs $24.28 reported).

That policy needed machinery, because the catalogs behind `make check-pricing` publish today's
posted price with no expiry and would otherwise have pushed us onto the promo rate:

- `PRICE_SCHEDULE["claude-sonnet-5"]` — the dated reversion, overriding catalog consensus.
- `HOLD_FINAL_PRICE = {"claude-sonnet-5"}` — marks the deliberate divergence so the audit reports
  it as `confirmed` with the over-report stated, not as `schedule_drift`.

**Anthropic made the introductory rate permanent on 2026-08-12 and cancelled the reversion.**
Verified against the live model overview, which lists Sonnet 5 at $2 / $10 per MTok with no
introductory footnote and no end date.

## Decision

**Encode $2/$10 and retire the hold-the-standard-price policy for this model.**

The policy's entire justification was the pending reversion. With no future change to guard
against, holding $3/$15 would over-report Sonnet spend *permanently* rather than temporarily —
inverting the policy's own goal of an accurate invoice match.

Three coupled changes, which must move together (the verdict path indexes
`PRICE_SCHEDULE[model]` directly, so a `HOLD_FINAL_PRICE` member without a schedule entry is a
`KeyError`):

1. `billing.py` — `claude-sonnet-5` → `{"input": 2.00, "output": 10.00}`.
2. `PRICE_SCHEDULE` — `claude-sonnet-5` entry removed. Nothing left to override consensus with:
   both catalogs already quote $2/$10, which is now simply correct.
3. `HOLD_FINAL_PRICE` — now empty. **The mechanism is kept, not deleted:** an introductory rate
   with a dated reversion is a recurring provider pattern, and the next one wants it.

## Alternatives rejected

- **Keep $3/$15 as a conservative buffer.** The policy was never about conservatism for its own
  sake — it was about matching the invoice after a known reversion. Absent the reversion this is
  just a permanent 1.5× inflation of the most-used Claude tier.
- **Keep the `claude-sonnet-5` schedule entry with only the $2/$10 leg.** The schedule exists to
  *override* catalog consensus. When the catalogs and the permanent price agree, the entry adds a
  maintenance surface that can only rot — the same reasoning the GPT-5.6 entries carry ("drop them
  once both catalogs carry the cut").
- **Delete `HOLD_FINAL_PRICE` entirely.** It is empty, not wrong. Re-deriving it at the next promo
  costs more than the six lines it occupies, and its comment now records why it exists.

## Consequences

- **Historical Sonnet 5 cost is inflated ~1.5× and stays that way.** Recorded counters are not
  rewritten (owner decision, 2026-08-12). For real historical spend, reprice BigQuery
  `prompt_content` tokens at $2/$10. This is the fourth distinct historical-cost defect, after
  the pre-2026-07-28 ledger inflation, the pre-2026-07-30 per-model misattribution, and the
  OpenAI cache-write under-report.
- Reports from this change forward match the invoice for Sonnet 5.
- `make check-pricing` now returns a plain `confirmed` for this model via ordinary consensus,
  with no schedule or policy special-casing.

## Verification

- Live model overview read before editing: $2 / input MTok, $10 / output MTok, no introductory
  footnote. The `claude-api` skill's cached table still showed the superseded "$3.00 ($2.00 intro
  through 2026-08-31)" — a cached table is not a source for a price.
- Eight schedule/hold tests were bound to the real `claude-sonnet-5` entry and broke. They were
  moved to a **synthetic fixture model** (`promo-model`) with every assertion preserved: the
  mechanism is ours and belongs in a unit test, a live price is a fact about the world and does
  not. Verified by mutation — breaking the hold branch still turns four of them red.
- `make test-unit` green; `ruff check src/` clean.
