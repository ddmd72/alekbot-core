# Consolidation model / provider evaluation (2026-07-15)

**Decision:** Keep consolidation on **Claude Sonnet 5** (current default). **Gemini 3.1 Pro** is a
viable cheaper alternative with equal discipline. **Do NOT** move consolidation to OpenAI GPT-5.6
(Terra/Sol) or to Opus 4.8 / Fable 5.

**Status:** Evaluation record (single-run-per-model; see caveats). No code change; this documents
*why* Sonnet 5 stays and what each provider actually does on the consolidation task.

## Harness

`scripts/consolidation/ab_cross_provider.py` — cross-provider A/B over the **real** ConsolidationAgent
(full 3-stage tool loop), with fact **reads hitting live Firestore** and fact **writes intercepted**
(`DryRunFactManagementAdapter`) so nothing is committed. Provider is swapped faithfully by re-resolving
the agent's execution context (`AgentContextBuilder.resolve_for_task` + `provider_override`, caching/
alerting proxies applied) and pinning `_llm` / `model_name`. Captures per-turn usage → cost, and
segments Stage 1 / 2a / 2b from the raw turn transcript.

```bash
# chat = replay the real last-consolidation conversation window (pulled from BigQuery)
python scripts/consolidation/ab_cross_provider.py --mode chat --only claude --claude-model sonnet5
python scripts/consolidation/ab_cross_provider.py --mode chat --only claude --claude-model opus   # → claude-opus-4-8
python scripts/consolidation/ab_cross_provider.py --mode chat --only claude --claude-model fable  # → claude-fable-5
python scripts/consolidation/ab_cross_provider.py --mode chat --only openai --openai-model terra   # gpt-5.6-terra
python scripts/consolidation/ab_cross_provider.py --mode chat --only openai --openai-model sol      # gpt-5.6-sol (ULTRA)
python scripts/consolidation/ab_cross_provider.py --mode chat --only gemini                          # gemini-3.1-pro
python scripts/consolidation/ab_cross_provider.py --mode dedup --limit 10                            # top-N longest facts
```

Run locally with `NO_PROXY='*'` (Charles proxy intermittently breaks the OpenAI SDK connection).
Outputs + per-op reports land in `scripts/memory/consolidation/` (gitignored — PII).

## How consolidation actually runs (three stages, one call each)

`ConsolidationAgent._execute_deliberate_process_v3`:
- **Stage 1** — extract facts from the conversation window (create/update/merge/**discard**).
- **Stage 2a** — cluster review, **only if Stage 1 wrote something**.
- **Stage 2b** — directive-rulebook review, **UNCONDITIONAL** every run; sees the full rulebook and is
  told *"convergence, not churn — leave already-optimal directives alone"*.

The "directive churn" observed on some models is entirely **Stage 2b**. Thinking effort is a fixed
constant (`CONSOLIDATION.thinking_effort = "medium"`), identical for every provider — the comparison is
fair on that axis.

## Findings per provider (behavioral — PII scrubbed)

- **Claude Sonnet 5 (current):** surgical, stays inside the conversation window, **minimal-to-zero
  Stage 2b churn**. Correctly recognizes already-present facts and discards duplicates. Baseline cost.
- **Gemini 3.1 Pro:** matches Claude's discipline — **zero Stage 2b churn** (NO-OP on an already-optimal
  rulebook), captured the window's facts, fast (~190s), cheapest. Strong alternative. (Consolidation was
  historically eval'd on gemini-3.1-pro-preview, so the prompt suits it.)
- **Claude Opus 4.8:** disciplined like Sonnet (Stage 2b ≈ 0–1), richest single fact descriptions, clean
  invalidation of stale facts. **~2–3× the cost** of Sonnet per run (higher price + more turns). No
  quality win worth the price for a background task.
- **Claude Fable 5:** accepted `thinking="medium"` (no 400). Extracted the **most** (caught extra
  window context + one behavioral directive), Stage 2b = 0. But produced a **same-`fact_id` duplicate**
  (updated one fact twice with reworded text) and costs **~5–7×** Sonnet. Marginal gain, top price.
- **OpenAI GPT-5.6-terra (PERFORMANCE):** ~2× faster/cheaper, but **over-churns Stage 2b** (8–11
  directive rewrites per run), **unstable Stage 1 anchoring** — on ~2 of 3 identical-input runs it
  latched onto a behavioral meta-signal (a "don't make assumptions" moment in the chat) and wrote a
  *directive* instead of the concrete facts, sending the whole downstream review into directive-orbit
  and missing the actual window. Also self-duplicates. Noisy and non-deterministic.
- **OpenAI GPT-5.6-sol (ULTRA):** same over-churn as Terra plus **much slower** (dedup ~2× Sonnet; the
  chat leg once ran pathologically long under local retries). Top-tier does not fix the behavior.

**Root pattern:** the over-churn / mis-anchoring is **OpenAI-specific**. The consolidation prompt is
Claude-native and restraint-heavy ("every fact is a commitment", "convergence not churn"). Claude and
Gemini honor that restraint; GPT-5.6 reads "optimise every pass" literally and rewrites the rulebook.
It is a **prompt↔model fit** issue, not model capability — running consolidation on OpenAI would need an
OpenAI-tuned prompt, not just a provider flip.

## Key nuance — more operations ≠ better

A **0-operation run is often the correct no-op**, not a failure. Example: on one Sonnet run the model
searched, found every candidate fact already in memory (exact match, same reported-date) or ephemeral
(weather / one-off troubleshooting), and **discarded all 7 candidates — writing nothing**. Meanwhile
Opus/Fable/another Sonnet draw chose to **UPDATE** the already-present facts with reworded text — mild
churn (Fable even wrote the same `fact_id` twice). So op-count is a poor quality proxy; run-to-run
variance (temp > 0) is mostly the choice between "refresh existing" (churn) and "recognize duplicate →
leave it" (ideal). Judge by *what* was written, not *how much*.

## Cost / token economics (per chat run, corrected pricing)

Cache pricing folds Claude's 0.1× (read) / 1.25× (5-min write) multipliers into absolute $/M
(input/output/cache-read/cache-write): Sonnet 3/15/0.30/3.75, Opus 5/25/0.50/6.25, Fable 10/50/1.00/12.50.
Observed order-of-magnitude per run: **Sonnet ≈ $0.5, Opus ≈ $1.5, Fable ≈ $3+**. Two multipliers stack —
per-token price *and* turn/token volume (Opus/Fable ran ~9–11 LLM calls vs Sonnet's ~4; when Stage 1
writes facts, Stage 2a fires and re-reads the growing history each turn). Gemini is cheaper than Sonnet.
(`billing.py` stores the cache fields as **multipliers**, not $/M — do not read them as absolute.)

## Caveats

- **Single run per model** — temp > 0 gives real run-to-run variance; treat these as directional. For a
  binding verdict, run 3–5 iterations per model and compare distributions.
- Dry-run judges **proposed** operations, not committed state.
- Related bug found while building the harness: consolidation's cross-provider fallback corrupts the
  tool-loop transcript on a transient primary error (silent fallback → gemini mid-loop → `call_id not
  found`). Tracked separately; the harness disables fallback (`pin_provider` nulls it).
