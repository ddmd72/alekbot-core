# RFC — Migrate OpenAI tiers to the GPT-5.6 family (Luna / Terra / Sol)

- **Status:** Draft (prepared 2026-07-13, autonomous research session)
- **Owner:** Dmytro
- **Scope:** `OpenAIAdapter` tier map + reasoning/cache handling, `billing.py`, wire/contract tests, docs.
- **Type:** Model-tier flip + adapter capability update (same class as
  `decisions/openai_ultra_tier_to_gpt_5_5_pro.md`, larger surface because of the caching API change).
- **Not in scope:** the deep-research adapter (`openai_deep_research_adapter.py`, o3/o4 models — untouched);
  the ECO tier (stays `gpt-5.4-nano`, see §3).

---

## 1. Context & Motivation

GPT-5.6 went GA on 2026-07-09 as a three-tier family (Luna / Terra / Sol), 1M context on all tiers.
Two drivers to migrate now:

1. **Forcing function:** `gpt-5.4-mini` (our current BALANCED OpenAI model) is scheduled for **API
   shutdown 2026-12-11** (announced 2026-06-11; OpenAI-recommended replacement `gpt-5.5`). BALANCED
   must move before then. `gpt-5.4-nano` (ECO) and `gpt-5.4` (PERFORMANCE) are **not** deprecated,
   but we upgrade them together to stay on one generation.
2. **Modernization:** Terra is "GPT-5.5-class at half the price"; Sol is a genuine agentic-tool SOTA
   step over `gpt-5.5-pro` at ~1/6 the price ($5/$30 vs $30/$180).

There is **no `mini`/`nano` in the 5.6 family** — so the cheapest 5.6 tier (Luna, $1/$6) is ~33%
dearer than today's `gpt-5.4-mini` ($0.75/$4.50). This is a real cost step for the BALANCED tier;
accepted because the mini shutdown leaves no cheaper same-gen option, and Luna is still far below
Terra/Sol.

> **Superseded 2026-07-30 — the cost premium this RFC accepted no longer exists.** OpenAI cut
> Luna to **$0.20/$1.20** and Terra to **$2/$12** (Sol unchanged); see
> [`decisions/openai_gpt56_price_cut.md`](../04_solution_strategy/decisions/openai_gpt56_price_cut.md).
> BALANCED is now ~4× *cheaper* than the `gpt-5.4-mini` it replaced, not 33% dearer. The prices
> below are kept as the state of the world when the migration was decided — `billing.py` is the
> live source. Two conclusions inverted by the cut are flagged inline (§2, §3.3).

---

## 2. Target tier mapping

| Tier | Now | Target | API slug |
|------|-----|--------|----------|
| ECO | `gpt-5.4-nano` | **unchanged** | `gpt-5.4-nano` |
| BALANCED | `gpt-5.4-mini` | **Luna** | `gpt-5.6-luna` |
| PERFORMANCE | `gpt-5.4` | **Terra** | `gpt-5.6-terra` |
| ULTRA | `gpt-5.5-pro` | **Sol** | `gpt-5.6-sol` |
| TIER1/2/3 | `gpt-5.4-nano` | **unchanged** | `gpt-5.4-nano` |

ECO stays on nano: there is no 5.6 sub-Luna tier, nano is cheaper ($0.20/$1.25 vs Luna $1/$6) and
not deprecated. Revisit only if OpenAI ships a 5.6 nano.

> **Inverted by the 2026-07-30 cut:** nano and Luna now cost the same on input ($0.20) and nano is
> marginally *dearer* on output ($1.25 vs $1.20), so "nano is cheaper" no longer holds. ECO stays on
> nano on **latency**, measured 2026-07-31: 2.3× faster on the router workload, because Luna spends
> ~183 hidden reasoning tokens per triage at its default effort and overruns `max_tokens=300`.
> Harness: `scripts/validation/ab_router_latency_nano_vs_luna.py`.

Edit site: `OpenAIAdapter.MODEL_TIERS` — [`openai_adapter.py:79-87`](../../src/adapters/openai_adapter.py#L79-L87).
Keep `gpt-5.5-pro` in `billing.py` (rollback / historical rows); it just leaves the tier map.

---

## 2.5 Probe results — 2026-07-13 (DECISIVE, live API)

Ran the §7 probes against the live API (Responses API, SDK 2.30.0, minimal calls). Outcomes flip
two of the RFC's "risky" items to "no work":

| Probe | Result |
|-------|--------|
| **Slugs** (`models.list`) | Confirmed exactly `gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.6-sol` (no dated snapshot). |
| **Effort floor** (`none`/`low`/`medium`) | **ALL three tiers accept all three — NO floor.** Sol does NOT floor at `medium` (unlike `gpt-5.5-pro`). → **`_MIN_MEDIUM_EFFORT_PREFIXES` needs NO change.** Main trap cleared. |
| **`prompt_cache_retention="24h"`** on 5.6 | **Accepted, no error (silently ignored)** on all three. → Not a hard break. Leaving it in place is safe (5.4 still honours it; 5.6 ignores → implicit 30m). Model-aware branch is now **optional cleanup, not required.** |
| **`prompt_cache_options`/`prompt_cache_key`** | SDK 2.30.0 does NOT expose them as kwargs (`unexpected keyword argument`), but the **API accepts them via `extra_body`**. So `prompt_cache_key` can be added without an SDK bump. |
| **json_schema `text.format` (strict=False)** | **Works identically** — all three returned clean `{"answer":"ok"}`. No change. |

Net effect: migration reduces to **MODEL_TIERS flip + billing rows** as the hard-required work; the
effort gate is untouched, and caching becomes an *optional* improvement (see §3.3, §4).

---

## 3. API research findings (with justifications)

### 3.1 Reasoning / thinking effort — ⚠ MUST PROBE before ship

- The Responses API takes effort **identically** to 5.4: `reasoning={"effort": "..."}` — **no API
  change**. Our mapping (`thinking` low/medium/high → effort, [`openai_adapter.py:217-233`](../../src/adapters/openai_adapter.py#L217-L233))
  works unchanged. `gpt-5.6-*` starts with `gpt-5` → already matched by `_REASONING_PREFIXES`
  ([`:93`](../../src/adapters/openai_adapter.py#L93)), so temperature-suppression + reasoning enablement
  are automatic.
- 5.6 adds **`max`** (above `xhigh`) and a separate **`reasoning.mode: "pro"`** (independent of effort;
  Sol also has an `ultra` mode = 4 parallel agents). **We adopt none of these** — we only send
  low/medium/high, never `reasoning.mode`, so default `standard` mode is used. No accidental cost blow-up.
- **Effort floor is the migration's #1 trap.** `gpt-5.5-pro` (today's ULTRA) rejects `effort="low"` with
  a 400 (min `medium`) — this exact combo hard-failed Smart on 2026-07-13 (rotation landed ULTRA on
  OpenAI at `low`). We added `_MIN_MEDIUM_EFFORT_PREFIXES=("gpt-5.5-pro",)`
  ([`:99`](../../src/adapters/openai_adapter.py#L99)) to clamp low→medium.
  - Secondary sources (Vellum, aipricing, help.openai) claim **all three 5.6 tiers accept the full
    none→max range**, i.e. Luna/Terra/Sol accept `low`.
  - **BUT** the official reasoning guide does **not** confirm floors ("supported values are
    model-dependent … test experimentally"), and Sol is the pro-class model most likely to floor.
  - **Rule (memory `reference_openai_reasoning_effort_floor`): do not infer a floor — probe it live.**
    Before flipping the map, run the probe in §7 for each tier. If Sol (or any) 400s on `low`, add
    `"gpt-5.6-sol"` to `_MIN_MEDIUM_EFFORT_PREFIXES`. If all accept `low`, no gate change.
- **Grounding-forced `low`** ([`:223-224`](../../src/adapters/openai_adapter.py#L223)) exists because
  `gpt-5.4` at effort `none` disables agentic web search. **Probe whether 5.6 has the same behavior**
  (does `none` disable iterative search on Luna/Terra/Sol?). If not, the forced-low may be unnecessary
  for 5.6 — but keep it (harmless, and it interacts with the floor gate above).
- **Conciseness watch-item:** 5.6 "tends to be more concise by default" and exposes `text.verbosity`
  (low/medium/high). Smart/daily-review answers may come back shorter. Not a blocker; if answers get
  too terse post-migration, set `text.verbosity="high"` for the affected agents. Track after rollout.

### 3.2 JSON schema — unchanged, no work ✅

5.6 supports Structured Outputs via `text.format={"type":"json_schema", …}` (Draft-2020-12), strict
optional — **the same mechanism we already use** at [`openai_adapter.py:173-179`](../../src/adapters/openai_adapter.py#L173-L179)
(`strict:False`, `_to_openai_json_schema` normalization). No change. Grounding + JSON mode remain
mutually exclusive (400) — already handled ([`:171-172`](../../src/adapters/openai_adapter.py#L171)).
**Confirms the ask: json schema is supported exactly as before.**

### 3.3 Prompt caching — the real work ⚠

Answer to "do we need markers like Claude, or dynamic?": **still dynamic/automatic — no markers required.**
5.6 keeps **implicit caching** on by default (default mode `"implicit"` auto-caches the latest-message
prefix; ≥1024-token prefixes). Explicit breakpoints exist but are **opt-in**, unlike Claude's mandatory
`cache_control`. So we do **not** need to add cache markers.

However three concrete changes are required for 5.6:

1. **`prompt_cache_retention` is DEPRECATED for 5.6+.** We currently unconditionally send
   `prompt_cache_retention="24h"` ([`openai_adapter.py:237-238`](../../src/adapters/openai_adapter.py#L237-L238)).
   The doc does not confirm reject-vs-ignore — treat as risky and **stop sending it for `gpt-5.6*`
   models** (model-aware branch). Replacement is `prompt_cache_options.ttl`, whose **only allowed value
   is `"30m"` (also the default)** — so we can simply omit it and rely on the 30m implicit default, or
   send `prompt_cache_options={"ttl":"30m"}` for explicitness.
2. **`prompt_cache_key` — should add.** On 5.6+ OpenAI says you *must* set `prompt_cache_key` "to use
   the more reliable matching for both implicit and explicit caching." Not strictly required (caching
   still works without) but recommended for hit-rate. Derive a stable key from the cacheable prefix —
   e.g. `f"{agent_type}:{account_id}"` (mirrors our static-prompt cache boundary). This is the one net-new
   caching addition.
3. **Cache writes now cost 1.25× input** (were free pre-5.6) — billing change, see §3.4.

**Behavioral regression (call out, don't fix):** 24h retention → 30m max. Our sporadic BALANCED agents
(`web_search`, `maps_search`) run minutes-to-hours apart, so cross-request prefix reuse beyond 30m is
lost. Combined with Luna's higher base input price ($1 vs $0.75) and the new 1.25× write fee, effective
BALANCED input cost rises more than the sticker delta. Acceptable (mini is being shut down anyway), but
budget-track it after rollout.

> **Inverted by the 2026-07-30 cut:** at $0.20 input, Luna's base is now well *below* mini's $0.75,
> so the lost 24h retention no longer compounds a price increase. The retention regression itself
> stands; only its cost consequence is void.

### 3.4 Pricing (per 1M tokens) → `billing.py`

Prices **as of the migration (2026-07-09)**. Superseded by the 2026-07-30 cut — luna `0.20/1.20`,
terra `2.00/12.00`, sol unchanged. `billing.py` is the live source; this table is kept for the
cost analysis above to remain readable.

| Model | input | output | cache_read (0.1×) | cache_write (1.25×) |
|-------|-------|--------|-------------------|---------------------|
| `gpt-5.6-luna` | 1.00 | 6.00 | 0.10 | 1.25 |
| `gpt-5.6-terra` | 2.50 | 15.00 | 0.25 | 3.125 |
| `gpt-5.6-sol` | 5.00 | 30.00 | 0.50 | 6.25 |

`billing.py` already supports a `cache_write` multiplier (comment [`:119`](../../src/domain/billing.py#L119):
"Claude 1.25, others 0"), so the model just needs three new rows **with `cache_write: 1.25`** (unlike the
existing 5.4 rows which omit it → default 0). ⚠ **Verify the adapter actually surfaces cache-write token
counts** in usage: OpenAI reports `cached_tokens` (reads); confirm the Responses API usage object exposes
cache-*write* tokens for 5.6 and that `_parse_response` extracts them — otherwise the 1.25× multiplier has
nothing to bill and cache-write cost is silently under-reported. If not exposed, log the gap and keep
`cache_write` for correctness once OpenAI adds the field.

### 3.5 New 5.6 features we deliberately skip

Programmatic tool calling (model writes JS to orchestrate tools — we own orchestration via
`DelegationEngine`), Multi-agent beta (we have our own network), Persisted reasoning (`reasoning.context`
— interesting for Smart later, separate RFC), `max`/`ultra`/`pro` modes, `text.verbosity` (watch-item
only). None are on the migration path.

---

## 4. Required code changes (concrete)

1. **`openai_adapter.py:79-87`** — flip `MODEL_TIERS`: BALANCED→`gpt-5.6-luna`, PERFORMANCE→`gpt-5.6-terra`,
   ULTRA→`gpt-5.6-sol`. ECO/TIER1-3 unchanged. Update the rationale comment block ([`:67-78`](../../src/adapters/openai_adapter.py#L67-L78)).
2. **`openai_adapter.py:99`** — `_MIN_MEDIUM_EFFORT_PREFIXES`: **NO CHANGE** (probe §2.5: all 5.6 tiers
   accept `low`/`none`). Leave `("gpt-5.5-pro",)` as-is.
3. **`openai_adapter.py:237-238`** — caching. Probe §2.5: `prompt_cache_retention="24h"` is **accepted &
   ignored** on 5.6 (no 400), so no hard requirement. Two options:
   - **(a) Minimal / user-preferred — leave as-is.** Keep unconditional `prompt_cache_retention="24h"`;
     5.4 honours it, 5.6 ignores it (implicit 30m). Zero code change, zero risk. Dead-but-harmless on 5.6.
   - **(b) Cleaner + better hit-rate — recommended.** Add a `_is_5_6_plus(model)` prefix gate: on 5.6 skip
     `prompt_cache_retention` and instead add `extra_body={"prompt_cache_key": f"{agent}:{account_id}"}`
     (SDK 2.30.0 lacks the kwarg — must go via `extra_body`; a later SDK bump exposes it natively). This
     buys the "more reliable matching" OpenAI recommends for 5.6 implicit caching. Still no markers needed
     (implicit stays automatic).
   Pick (b) if we care about BALANCED/PERFORMANCE cache hit-rate; (a) is fine to ship first and revisit.
4. **`billing.py:137-141`** — add the three rows from §3.4 (with `cache_write: 1.25`); update the section
   comment; verify/extend cache-write token extraction in usage parsing (§3.4 caveat).
5. **Optional — rollback ergonomics:** the OpenAI adapter has **no env kill-switch** (unlike Claude's
   `CLAUDE_PERFORMANCE_MODEL`). Consider adding `OPENAI_BALANCED_MODEL`/`OPENAI_PERFORMANCE_MODEL`/
   `OPENAI_ULTRA_MODEL` overrides so a bad tier can be reverted without a redeploy. Nice-to-have; if
   skipped, rollback = revert `MODEL_TIERS` + redeploy.

---

## 5. Testing (mandatory — `docs/how_to/ADAPTER_WIRE_TESTING.md`)

- **Wire tests** (`tests/unit/adapters/`, mock at the SDK boundary): (a) each 5.6 tier resolves to the
  right slug; (b) `reasoning={"effort": …}` still forwarded; (c) json_schema `text.format` unchanged;
  (d) **caching branch** — `gpt-5.6*` request does NOT include `prompt_cache_retention`, DOES include
  `prompt_cache_key` (+ `prompt_cache_options` if we send it), while a `gpt-5.4*` request still sends
  `prompt_cache_retention="24h"`; (e) effort-floor clamp fires only for the models actually gated.
- **Contract validators** (`tests/contracts/adapter_contracts.py`) — extend the OpenAI validators for the
  new caching kwargs.
- **Billing test** — cost rows for luna/terra/sol incl. cache_write.
- Per the tests ABSOLUTE RULE: these are **new** tests; do not modify existing ones without per-test
  approval.

## 6. Docs to update

`src/adapters/CLAUDE.md` (tier→model table, the reasoning-effort-floor note, caching mechanics),
root `CLAUDE.md` (Economics / Smart model note if BALANCED/PERFORMANCE defaults shift), a decision record
`docs/04_solution_strategy/decisions/gpt_5_6_adoption.md` (backward-looking summary once shipped),
`docs/05_building_blocks/openai_integration/README.md`.

---

## 7. Verification / probes (run BEFORE flipping the map)

Use manual-trigger path (CLAUDE.md → Manual Triggers) or a throwaway script hitting `responses.create`.
Project ID / SA from `.env`/memory.

1. **Effort floor probe (critical)** — for each of `gpt-5.6-luna|terra|sol`, send a request with
   `reasoning={"effort":"low"}` and again `"none"`. Record which 400 with `unsupported_value`. → sets §4.2.
2. **Grounding + `none`** — does `none` disable agentic web search on 5.6 (as on 5.4)? → validates the
   grounding-forced-low logic.
3. **Retention deprecation** — send `prompt_cache_retention="24h"` to `gpt-5.6-luna`: does it 400 or
   silently ignore? → confirms how urgent the model-aware branch is (400 = hard break at flip).
4. **json_schema** — one Smart-shaped `text.format` json_schema request per tier parses cleanly.
5. **Slug confirmation** — `client.models.list()` to confirm exact API ids (`gpt-5.6-sol` vs a dated
   snapshot).

Post-flip: full `make check`, then a live Smart request per tier (BigQuery `prompt_content` to confirm
model + non-empty structured output), and a daily-email-review dry run (verbosity watch-item).

---

## 8. Risks & open questions

- **Effort floor unknown until probed** (Sol most likely). Mitigation: §7.1 gate before flip. This is the
  same failure that hard-broke Smart on 2026-07-13 — do not skip.
- **`prompt_cache_retention` reject-vs-ignore on 5.6** unconfirmed — §7.3 probe decides urgency; ship the
  model-aware branch regardless.
- **Cache-write token exposure** in usage — may under-report cost until confirmed (§3.4 caveat).
- **Cost:** BALANCED effective input cost rises (Luna base +33%, 24h→30m cache loss, +1.25× writes).
  Budget-track; still cheaper than Terra and unavoidable (mini shutdown).
- **Conciseness/verbosity** shift — watch Smart/daily-review answer length.
- Independent of this RFC: the live Claude structured-output workaround (#1204) and the
  `ANTHROPIC_LOG` prod flag are separate cleanups.

## 9. Decision gates (CLAUDE.md protocol)

Authoritative source = production adapter code + this RFC (no POC). This is a `MODEL_TIERS` flip of the
same shape as `decisions/openai_ultra_tier_to_gpt_5_5_pro.md`, **plus** a genuine API delta (caching).
No silent simplification: the caching branch and the effort-floor probe are non-optional — both are
where "just swap the strings" would regress or 400.
