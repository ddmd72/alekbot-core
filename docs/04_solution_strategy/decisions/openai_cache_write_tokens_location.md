# OpenAI `cache_write_tokens` lives in `input_tokens_details`, not `output_tokens_details`

**Date:** 2026-08-11
**Status:** Accepted
**Related to:** `docs/10_rfcs/GPT_5_6_MIGRATION_RFC.md` §3.4 (closes its open caveat),
`gpt_5_6_adoption.md`, `billing_per_model_pricing.md`

## Context

GPT-5.6 bills cache writes at **1.25× the uncached input rate** — the first OpenAI family to charge
for them at all (5.4/5.5 were free). `billing.py` carries `cache_write: 1.25` for luna/terra/sol, and
`calculate_cost` multiplies it by `cache_creation_tokens`, which the adapter must supply.

The migration RFC flagged this as unverified (§3.4):

> ⚠ **Verify the adapter actually surfaces cache-write token counts** … otherwise the 1.25×
> multiplier has nothing to bill and cache-write cost is silently under-reported.

The verification was never run. BigQuery `prompt_content` showed `cache_creation_tokens = 0` on all
1988 gpt-5.6-* rows — impossible if caching was working, since every cold prefix is a write.

## Investigation

The adapter read the field from `response.usage.output_tokens_details`. OpenAI puts it in
`response.usage.input_tokens_details`, alongside `cached_tokens`:

```json
"usage": {
  "input_tokens": 4583,
  "input_tokens_details": { "cached_tokens": 3945, "cache_write_tokens": 4580 },
  "output_tokens_details": { "reasoning_tokens": 0 }
}
```

Confirmed three ways: OpenAI's prompt-caching guide (Responses API section), a live usage object in
an OpenAI forum billing thread with a staff reply, and local parsing of the SDK's `ResponseUsage`.

**Why the SDK schema misled the first diagnosis.** `InputTokensDetails` in the installed pin
(`openai==2.30.0`) declares only `cached_tokens`; `cache_write_tokens` became a declared field in
**2.45.0**. Reading the schema alone suggests the field does not exist. It does — the model sets
`extra="allow"` (`additionalProperties: true`), so the value arrives and `getattr` reaches it even on
old pins. **A generated SDK schema is a lower bound on API fields, never the API contract.**

**Why the unit test did not catch it.** The test built usage from `MagicMock`, which fabricates any
attribute on access, so a lookup in the wrong object still "found" a value. The mock validated the
code against itself.

## Decision

Read `cache_write_tokens` from `input_tokens_details`, next to `cached_tokens`
(`openai_adapter.py`). One line; no SDK bump needed.

The regression test now places the field where OpenAI does and was verified to **fail against the
old lookup** before being accepted — a mock-based test that was never seen red proves nothing.

## Consequences

- OpenAI cache-write cost is billed from this deploy forward. Prior GPT-5.6 spend (from 2026-07-30,
  when luna/terra/sol went live) **under-reports cache writes** — a third distinct historical cost
  defect, after the pre-2026-07-28 ledger inflation and the pre-2026-07-30 per-model misattribution.
  For historical cost, BigQuery `prompt_content` is still the source of truth, but its
  `cache_creation_tokens` column is zero for OpenAI and cannot be repaired retroactively.
- Magnitude is bounded: writes are billed at 1.25× input, and input is the cheap leg
  (luna $0.20/1M). Under-report is real but small relative to output spend.
- `billing.py` needed no change — the rates were right, the counter was empty.

## Follow-up

Verify on the next deploy that `cache_creation_tokens > 0` appears on gpt-5.6-* rows in
`prompt_content`. If it stays zero, the remaining suspects are caching not engaging at all
(prefix < 1024 tokens, unstable `prompt_cache_key`) — not the extraction path.
