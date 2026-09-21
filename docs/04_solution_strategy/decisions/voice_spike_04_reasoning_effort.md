# Spike 0.4 — Reasoning effort: all 5 levels retained the fact, `minimal` recommended on cost/latency

**Date:** 2026-09-21
**Status:** Live (spike complete, real API data)

**Owner's decision (2026-09-21): `medium`, not the spike's own cost/latency-optimal
recommendation of `minimal`.** Deliberate margin, not a disagreement with the data — this probe is
explicitly flagged below (Revisit-if) as a 6-turn short-session test, not the RFC §4.3 long-call
retention scenario Lelik will actually face; `medium` also matches this repo's already-established
convention for Smart (`project_smart_model_eval.md`: `gpt-5.4-mini` + `reasoning: medium` matched
flagship quality on multi-step work at a fraction of the cost). `minimal`'s cost/latency edge over
`medium` on this probe was small (~$0.003, ~0ms p50 difference) — cheap insurance for a real,
untested risk. This is what Slice 1 item 8 should use; the sections below are the spike's own
analysis, left as originally written.

## Question

RFC §4.3 and Slice 1 item 8 (Lelik's session/persona assembly) need a `reasoning_effort` default.
`gpt-realtime-2.1` exposes `"minimal"|"low"|"medium"|"high"|"xhigh"` via `session.reasoning.effort`.
Raising it should help retention over a conversation but costs more (reasoning tokens bill on the
text-output leg per RFC §6, $24/1M) and adds latency. This spike asks: what's the **lowest**
`reasoning_effort` that reliably retains a planted fact across a few filler turns, and what does
each step above that cost in latency and dollars?

## Method

`scripts/voice/test_reasoning_effort_poc.py` (new script, house pattern copied from
`scripts/voice/test_late_function_call_output_poc.py`: standalone `asyncio` + `websockets`,
`load_settings()` for `OPENAI_API_KEY`, no pytest).

**Deviation from the brief, agreed live with the owner:** the brief (`task-5-brief.md`) said to
extend `scripts/voice/test_mulaw_relay_poc.py` (the Twilio Media Stream relay *server*) with a
`REASONING_EFFORT` env var and drive it via real phone calls. Instead this spike used the brief's
own allowed fast path — Step 2: *"for a faster/cheaper first pass, send as text turns over the
same session"*. The relay script has no client mode; retrofitting one would mix two unrelated
concerns into one file for a probe that doesn't need a phone call at all. A new standalone script
matching Task 1's shape was cleaner and is real API data either way.

**Per effort level:** open one text-only Realtime session (`session.update` with `"type":
"realtime", "output_modalities": ["text"], "reasoning": {"effort": <level>}`, model
`gpt-realtime-2.1`, no `OpenAI-Beta` header — GA API, per Task 1's confirmed shape), then run the
brief's exact 6-turn retention probe as sequential `conversation.item.create` + `response.create`
turns, waiting for `response.done` before the next turn:

```
1. "My colleague Ivan Petrov handles the Q3 budget — remember that."
2. "What's the weather like today?"        (filler)
3. "Tell me a fact about Valencia."         (filler)
4. "What time is it in Tokyo right now?"    (filler)
5. "Can you count from one to five?"        (filler)
6. "Who handles the Q3 budget?"             (retention check — correct answer: Ivan Petrov)
```

Latency per turn = wall clock from `response.create` to `response.done` (`time.monotonic()`).
Token usage read from each `response.done`'s `response.usage` (`input_tokens`, `output_tokens`,
and `output_token_details.reasoning_tokens` when present). Cost computed from RFC §6's uncached
text rates only (no prior cache to hit in a single short session): `cost = (input_tokens*4 +
output_tokens*24) / 1_000_000`.

OpenAI only. Per the brief's Step 1, xAI's `grok-voice-think-fast-2.0` reasoning-effort support
was never checked against `docs.x.ai` — not assumed absent, just not verified, so it is skipped
here rather than guessed at. See Revisit-if.

Run: real API calls, 5 effort levels × 6 turns = 30 exchanges, `NO_PROXY='*' python3
scripts/voice/test_reasoning_effort_poc.py` (local Charles Proxy MITM otherwise breaks TLS to
`api.openai.com` — known issue, per-invocation env var bypass, no code change).

## Result

Full run, real output, one session per row:

| effort  | retention | total (6 turns) | p50/turn | input tok | output tok | reasoning tok | cost    |
|---------|-----------|------------------|----------|-----------|------------|----------------|---------|
| minimal | **PASS**  | 5.22s            | 0.92s    | 1909      | 399        | 113            | $0.01721 |
| low     | **PASS**  | 5.09s            | 0.92s    | 1929      | 400        | 118            | $0.01732 |
| medium  | **PASS**  | 5.71s            | 0.90s    | 1908      | 525        | 223            | $0.02023 |
| high    | **PASS**  | 6.59s            | 1.15s    | 1894      | 521        | 240            | $0.02008 |
| xhigh   | **PASS**  | 7.96s            | 1.12s    | 1843      | 553        | 276            | $0.02064 |

**5 of 5 levels passed retention** — every level's final turn ("Who handles the Q3 budget?")
correctly answered "Ivan Petrov" (counted directly from the printed transcript, not estimated).

Reasoning tokens (`output_token_details.reasoning_tokens`) were present in every `response.done`
event across all 30 turns — the split IS exposed by the API for this model, and it climbs
monotonically with effort (113 → 118 → 223 → 240 → 276 total across the 6-turn session), confirming
reasoning tokens are the mechanism driving the output-token/cost growth from `minimal` to `xhigh`.

Total elapsed time and p50 latency both trend upward with effort overall, but not strictly
monotonically at the low end: total goes 5.22s (`minimal`) → 5.09s (`low`, marginally *lower* than
`minimal`) → 5.71s (`medium`) → 6.59s (`high`) → 7.96s (`xhigh`); p50 sits flat around 0.90–0.92s
for `minimal`/`low`/`medium` then steps up to 1.12–1.15s at `high`/`xhigh`. The `minimal`↔`low`
inversion is small-n noise on a single 6-turn run each, not a claim that `low` is reliably faster
than `minimal` — the clear, repeatable signal is the step up at `high`/`xhigh`, not the ordering
within the bottom three.

## Verdict

**All 5 levels retained the fact — no level failed.** Per the brief's own framing ("If no level
both retains and stays under the latency bar... If no level failed, recommend `minimal`"),
**`minimal` is the recommended default for Lelik's session config (Slice 1 item 8).** It is the
cheapest level tested ($0.01721 for the 6-turn probe, vs $0.02064 at `xhigh` — a ~20% cost spread)
and ties `low` for the lowest per-turn p50 (0.92s vs `high`/`xhigh`'s 1.12–1.15s), and it passed
the same retention check every other level passed. (One honest wrinkle: `low`'s total elapsed
across the 6 turns was marginally lower than `minimal`'s — 5.09s vs 5.22s, a 0.13s gap on a 6-turn
sample, i.e. noise-level, not a signal that `low` is meaningfully faster; `minimal` still wins on
cost, which is the tie-breaker.) Raising `reasoning_effort` past `minimal` bought nothing on this
probe except more reasoning tokens, more latency, and more cost.

This is a **cheap, single-shot, six-turn, short-filler probe** — see Revisit-if before treating it
as proof reasoning effort doesn't matter for retention in general.

## Revisit if

- **xAI's reasoning-effort support was never checked.** `grok-voice-think-fast-2.0` may or may not
  expose an equivalent knob (`docs.x.ai`'s voice-agent guide was not consulted this session, per
  the brief's Step 1 scoping) — this spike is OpenAI-only by omission of verification, not by a
  confirmed absence on xAI's side. If xAI is still a live provider candidate at Slice 1, that check
  is still open.
- **This is not the RFC §4.3 scenario it feeds into.** §4.3 worries about retention over a *long
  spoken conversation* (the 70.8% APR figure cited there is for much longer sessions with many more
  turns and real audio, not 6 text turns with 4 one-line fillers). A short probe where every level
  passes is weak evidence that `minimal` holds up over a real 20-minute call with dozens of turns,
  interruptions, and topic drift — it only shows `minimal` isn't obviously worse than `xhigh` on
  *this* probe.
- **If `minimal` is shipped as the default and Lelik is later observed dropping planted facts in
  real sessions,** the first thing to re-run is this same probe with a harder distractor sequence
  (more filler turns, longer session, audio instead of text) before assuming reasoning effort is
  the fix — a short text-only PASS at every level suggests the bottleneck (if one appears in
  production) is more likely elsewhere (prompt assembly, biographical cache, standing directives)
  than the reasoning knob.
- **Text-only, not audio.** This probe never touched the audio legs (`audio_in`/`audio_out`) RFC
  §6 also prices — a real call's cost and latency profile will differ; this spike isolates the
  reasoning-effort variable cleanly but does not stand in for spike 0.3's audio-path latency
  numbers (`voice_spike_03_latency.md`).
