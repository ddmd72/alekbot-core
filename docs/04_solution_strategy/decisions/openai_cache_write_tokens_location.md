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

**Why the SDK schema misled the first diagnosis.** The local venv held `openai==2.30.0`, whose
`InputTokensDetails` declares only `cached_tokens`; `cache_write_tokens` became a declared field in
**2.45.0**. Reading that schema alone suggests the field does not exist. It does — the model sets
`extra="allow"` (`additionalProperties: true`), so the value arrives and `getattr` reaches it even on
old pins. **A generated SDK schema is a lower bound on API fields, never the API contract.**

**Production independently proved the fix.** `requirements.txt` had `openai>=1.0.0` unpinned, so the
image actually runs **2.53.0** (read from the Cloud Build log), where `cache_write_tokens` is a
*required* field of `InputTokensDetails`. A Responses payload omitting it would raise
`pydantic.ValidationError` before reaching our code. Prod served OpenAI Responses calls all day with
zero such errors — so OpenAI does send the field, inside `input_tokens_details`, on every call. That
is stronger evidence than the documentation.

**Why the unit test did not catch it.** The test built usage from `MagicMock`, which fabricates any
attribute on access, so a lookup in the wrong object still "found" a value. The mock validated the
code against itself.

## Decision

Read `cache_write_tokens` from `input_tokens_details`, next to `cached_tokens`
(`openai_adapter.py`). One line; the value arrives on any pin, so no SDK bump was required to fix it.

The regression test now places the field where OpenAI does and was verified to **fail against the
old lookup** before being accepted — a mock-based test that was never seen red proves nothing.

**Pin `openai==2.53.0`** (`requirements.txt`). Not a bump — prod already ran 2.53.0; this records the
version that was arriving by accident and ends the local/prod drift that caused the misdiagnosis. The
package is no longer "for Grok" as its comment claimed: it serves `OpenAIAdapter`,
`OpenAIDeepResearchAdapter`, and `GrokAdapter`. An unpinned dependency is how a breaking SDK change
(2.45.0) reached production without review.

## Consequences

- OpenAI cache-write cost is billed from this deploy forward. Prior GPT-5.6 spend (from 2026-07-30,
  when luna/terra/sol went live) **under-reports cache writes** — a third distinct historical cost
  defect, after the pre-2026-07-28 ledger inflation and the pre-2026-07-30 per-model misattribution.
  For historical cost, BigQuery `prompt_content` is still the source of truth, but its
  `cache_creation_tokens` column is zero for OpenAI and cannot be repaired retroactively.
- Magnitude is bounded: writes are billed at 1.25× input, and input is the cheap leg
  (luna $0.20/1M). Under-report is real but small relative to output spend.
- `billing.py` needed no change — the rates were right, the counter was empty.

## Verification (2026-08-11, post-deploy)

Confirmed in `prompt_content`, split on the deploy boundary:

| period | gpt-5.6-* rows | rows with `cache_creation_tokens > 0` | Σ cache-write |
|---|---|---|---|
| before | 1988 | **0** | 0 |
| after | 1 | **1** | 11 974 |

The first post-fix Sol call: `prompt_tokens = 11977`, `cache_read = 0`,
`cache_creation = 11974` — a cold prefix, so nearly the whole prompt was written to cache and
nothing read. Priced through `calculate_cost("gpt-5.6-sol", …)`, the newly-visible write leg is
**$0.0748 on that one call** ($0.1347 with it, $0.0599 as billed before) — the cache-write leg
more than doubled the input-side cost of a single request.

Incidental confirmation for the SDK pin: the same window's router call on `gpt-5.4-nano` recorded
`cache_write = 0`. Since prod runs `openai==2.53.0`, where `cache_write_tokens` is a *required*
field, that call succeeding proves OpenAI emits the field as `0` for pre-5.6 models rather than
omitting it — which is why the 2.45.0 required-field change is safe across model families.

One call is proof the extraction works, not a basis for estimating steady-state impact. Historical
rows stay zero and cannot be repaired.
