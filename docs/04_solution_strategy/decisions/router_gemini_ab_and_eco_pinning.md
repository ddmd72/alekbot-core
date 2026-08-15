# Router A/B: Gemini Flash-Lite vs GPT-5.4-nano, and why Gemini ECO is pinned

**Date:** 2026-08-15
**Status:** Measured; ECO pinning implemented. The provider switch itself is NOT made here.
**Scope:** `src/adapters/gemini_adapter.py`, `src/domain/billing.py`,
`scripts/validation/ab_router_gemini_vs_openai.py`, `tests/unit/test_cost_calculator.py`

## Context

The router runs on every user message, so its latency is added to every reply. It sits on
`gpt-5.4-nano` by a user-level `agent_providers["router"]` override; the strategy default
is, and always was, `gemini`. The question was whether a faster option exists.

Public benchmarks answer with TTFT. The router does **not** stream — it waits for a complete
JSON triage — so TTFT is the wrong number. Everything below is end-to-end wall time.

## Method

`scripts/validation/ab_router_gemini_vs_openai.py` replays **real router requests** pulled
from BigQuery `prompt_content` (227 distinct available, oldest 2026-07-16) through both
providers via the production adapters, interleaved, same transport, 40 prompts x 3 repeats
x 2 legs = 240 calls. Models are pinned explicitly; an alias could be repointed between the
measurement and the decision it justifies.

## Results

| | gpt-5.4-nano | gemini-3.5-flash-lite |
|---|---|---|
| latency p50 | 1.52s | **1.10s** |
| latency p95 | 1.94s | **1.33s** |
| stdev | 0.24 | **0.12** |
| valid JSON | 120/120 | 120/120 |
| empty replies | 0 | **0** |

**Self-consistency** — same prompt, 3 identical runs, does the leg answer the same way:

| field | nano | gemini |
|---|---|---|
| `metadata.task_complexity` | **35%** | 85% |
| `needs_memory_search` | 55% | 87.5% |
| `metadata.user_tone` | 57.5% | 90% |
| `search_intent` | 70% | 97.5% |

The headline is not the latency. **The current production router is unstable**: on two
prompts out of three, an identical message gets a different `task_complexity` across
identical runs — a different Smart tier, a different model, a different price for the same
input. Router temperature is already 0.3, so this is not an untuned sampling parameter.

`semantic_lens` and `search_phrase` are near 0% on both legs; they are open-ended text and
were never expected to be reproducible.

**The documented Flash-Lite failure mode did not reproduce.** `src/adapters/CLAUDE.md`
records "schema + Groovy DSL prompt → Flash Lite returns empty responses (22+ tests)". This
run is exactly that combination — 120 calls, zero empties. Evidence, not proof.

## Quality: agreement, not correctness

No ground truth exists for a triage, so the harness reports agreement and dumps every
disagreement for human review. Cross-provider agreement on `task_complexity` was 60%, and
the disagreements are **systematic**: Gemini classifies one step higher than nano
(`small_talk`→`simple_analytics`, `simple_analytics`→`info_search`).

That matters because `ROUTER_COGNITIVE_PROCESS` was retuned on 2026-07-14 specifically to
stop over-escalation, and its tie-breakers (`p6_uncertainty`, `default_low`) already say
"when unsure pick the LOWER level". Gemini escalates **despite** them, so a switch needs a
calibration pass, not just a config flip.

**A caution about reading the dump:** the stored production answer is a reference, not
truth. One case looked like a Gemini failure (`deep_reasoning` in production vs `small_talk`)
until the recorded `reasoning` field was read — nano had classified on *inherited topic from
prior context*, and the message was a deliberate test probe. Worse, nano does not reproduce
its own production answer on the identical stored prompt. Percentages say where to look;
only the dump says who was right.

## Decisions

1. **`GeminiAdapter.MODEL_TIERS[ECO]` is pinned to `gemini-3.5-flash-lite`**, not
   `gemini-flash-lite-latest`. The alias family currently holds six models. A router is the
   worst place for a silent model swap: its calibration is tuned to one model's judgement,
   and an alias moving under it changes `task_complexity` — hence Smart's tier and cost —
   with no deploy and no signal. Blast radius today is nil: no agent currently resolves to
   Gemini ECO. Bump deliberately, re-running the harness.

2. **`gemini-3.5-flash-lite` added to the pricing table.** `calculate_cost` returns `0.0`
   for an unknown model **silently**, and the table was keyed on the alias — so pinning
   without this makes that traffic free on the books.

3. **A test now enforces the invariant**: every model in every adapter's `MODEL_TIERS` must
   be priced. This is the second occurrence of the same defect class (the first was the
   retired `grok-4-1-fast-*` ids, under-reported ~5x), so it gets an assertion rather than
   another docs note.

4. **The provider switch is NOT made here.** The router remains on OpenAI via the user-level
   `agent_providers["router"]` override. Owner elected to calibrate live rather than tune
   the prompt against a labelled set first.

## Not done

- **Calibration of `ROUTER_COGNITIVE_PROCESS` for Gemini's upward shift.** The honest target
  needs labelled ground truth: the 16 disagreeing prompts, judged by the owner, as both a
  tuning target and a regression set. Tuning toward nano's distribution instead would be
  tuning toward a 35%-self-consistent signal.
- **The instability finding is not acted on.** nano's 35% self-consistency on
  `task_complexity` is a live property of production today, independent of any provider
  switch, and nothing here changes it.
