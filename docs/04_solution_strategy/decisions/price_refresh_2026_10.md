# Price refresh 2026-10-03: new models, two promos held, Gemini cache 0.1×

**Status:** accepted 2026-10-03. Verified at the providers' pricing pages and live model listings,
cross-checked against LiteLLM and models.dev (`make check-pricing`).

## Decision

- **New entries:** `claude-sonnet-5-5` $2/$10, `claude-opus-5-5` $4/$20 with cache reads at
  **0.05×** (not 0.10×), `grok-4.7` $2/$6. They are priced before any tier points at them, because
  `calculate_cost` returns 0.0 for an unknown id.
- **Two promos, both held at the standard price** (`HOLD_FINAL_PRICE`):
  - `gemini-3.8-flash`, which `gemini-flash-latest` resolves to: $0.75/$3.75 until 2026-12-31,
    then $1.50/$7.50. Its spend reads 2× high until then.
  - `gpt-5.6-sol`: $4/$20 until 2026-11-21. We assume $5/$30 returns; it reads 1.5× high.
  - This is the same policy Sonnet 5 had: a report that is too high is acceptable, a report that
    is too low is not.
- **Gemini cache reads 0.25× → 0.10×** for the 3.x entries. Cached input is now 10% of input
  (3.8-flash $0.075, 3.5-flash-lite $0.03, 3.1-pro $0.20). We were over-reporting cache reads 2.5×.

## Rejected

- *Bill the promo rates.* That is accurate today, but it under-reports silently if the promo
  ends early or the revert date slips. The over-report is stated in the audit verdict instead.
- *Leave Gemini at 0.25×.* That over-reports, so it is "safe", but it is wrong on two sources
  and hides real cache savings.

## Known gap (not fixed)

GPT-5.6 has a long-context tariff (2× input). The page does not state the threshold, and
`billing.py` holds one price per model, so long OpenAI requests are under-reported. Grok has the
same gap above 200k tokens.

## Revisit when

- the audit flags `schedule_stale` on either promo, meaning the provider moved the date;
- 2027-01-01 (Gemini) or 2026-11-22 (Sol), when the hold turns into a plain `confirmed`.
