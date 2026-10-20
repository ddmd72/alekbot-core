# Sonnet 5.5 stalls in tool loops under adaptive thinking: no workaround, loud monitoring

**Date:** 2026-10-20
**Status:** Accepted. The per-pass stats alert (`CONSOLIDATION_STATS_ALERTS`) is temporary; the
turn-limit alert is permanent.

## Decision

An Anthropic-side behaviour is not worked around in our adapter. With adaptive thinking in a tool
loop, `claude-sonnet-5-5` sometimes stops answering and emits filler tool calls until the loop's
turn cap.

- The owner removed Sonnet from Smart.
- ConsolidationAgent stays on Sonnet 5.5 for now, under monitoring:
  - **Permanent:** a pass that reaches `MAX_CONSOLIDATION_TURNS` without a final report logs an
    error and posts a 🚨 to the ops sink (`AlertSinkPort`): pass label, model, effort, turns,
    writes so far, repeated turns, the per-turn tool sequence and the first 8 characters of the
    user id. The return value is unchanged (`[]`).
  - **Temporary:** while `CONSOLIDATION_STATS_ALERTS` is on (`os.getenv`, default on; `0`, `false`,
    `no`, `off` disable it), every pass that ends with a report posts one 📊 stats line. Remove it
    once consolidation's model is settled.
- Side fix: the Sonnet 5.5 cache-read price in `billing.py` goes from 0.10x to 0.05x of input.

## Evidence

**Production symptom.** 2026-10-20, a weather turn: Smart on `claude-sonnet-5-5` gathered its data
in about 80 s, then made 34 filler calls (`search_web "dummy"`, then 33 x `compute_math "Return 0."`)
until the turn-40 wrap-up note forced the answer (about 3 minutes in all). The same evening a weekly
reminder hit Smart's 15-turn cap three times in a row (filler such as `search_web "Ignore"`,
"Placeholder skip", repeated workspace views) and succeeded on the 4th Cloud Tasks retry. About
$0.57 of roughly $0.80 went on the failed attempts.

**Rate.** Since 2026-10-14, 5 of 29 Sonnet Smart turns stalled. Zero on Opus 5.5, Haiku 5.5,
GPT-6.1 Sol (50 Smart turns) and GPT-6 Luna.

**Probe.** A short synthetic Smart-like request (4-sentence neutral system prompt, data already
gathered), 5 runs per variant:

| Variant | Result |
|---|---|
| adaptive thinking, medium | 5/5 stall |
| no `output_config.format` | 5/5 stall |
| adaptive, high | 5/5 stall |
| adaptive, xhigh | 5/5 stall (one call used the intent "noop") |
| `between_tools`, medium | 5/5 clean final JSON |

A strict "answer tool" (the vercel/ai #21992 workaround) is rejected by the API for Smart's schema
("compiled grammar is too large"). So the cause is neither `output_config.format` nor our prompts:
it is adaptive thinking on Sonnet 5.5 with client tools.

**`between_tools` is not free.**
- Deep research (1 run each, real prompt, 5-search cap) found the current INE figure in adaptive
  mode but not in `between_tools`, and the narration ("Начну с поиска…") landed in the report text.
- The reminder re-run under `between_tools` finished in 3 turns and 65 s without stalling, but was
  shallower than the adaptive answer: no verification pass, missed the week's top pick, fewer
  prices and sources.
- Consolidation (light synthetic 3-fact batch, real prompt, intercepted writes, 2 runs each) showed
  no difference between the modes.

Sonnet 5 has no `between_tools` (introduced in 5.5; Sonnet 5 used `disabled`).

## Why consolidation is lower risk, but not zero

Facts are written inside the tool calls, so a stall after the writes loses only the summary report.
A stall in the middle of a batch silently drops the facts not yet written, and the batch is still
consumed. Three production runs were clean at the time of writing. Before this change a stall
logged only a WARNING and returned `[]`, so nobody would have noticed.

## Alternatives considered

- **`between_tools` for all Sonnet tool requests.** Rejected: the depth loss above, and narration
  leaking into reports.
- **Repeat/filler detector with an early wrap-up note.** Rejected: the model alternates different
  filler calls, so detection is unreliable.
- **An "answer tool".** Rejected: the grammar is too large for Smart's schema, and the July 2026
  history of the synthesized `respond` tool (leaked serialization) argues against it; see
  `claude_schema_respond_tool.md`.
- **A per-step "finish if ready" hint.** Rejected: Anthropic warns that per-step harness text after
  tool results reads as prompt injection, and it nudges good turns to stop early.
- **Move consolidation off Sonnet now.** Not done: the owner chose Sonnet; the alert makes a
  failure visible instead of preventing it.

## Consequences

- A consolidation stall is visible in Slack within the pass. Nothing retries it automatically; the
  owner decides (switch the model, or revisit `between_tools`).
- While the knob is on, each pass adds one short Slack message (stage 1, 2a, 2b, cluster, email).
  The text carries tool names and counts only: no fact content and no user id beyond 8 characters.
- The alert is best-effort: a missing sink (no `BILLING_SLACK_WEBHOOK_URL`) or a failing post is
  logged at WARNING and never changes the pass result.
- Cost recorded before 2026-10-20 over-reports Sonnet 5.5 cache reads 2x (0.10x applied instead of
  0.05x). For history, reprice from BigQuery `prompt_content` tokens.
- Code: `src/domain/consolidation_pass_stats.py` (pure stats and text),
  `ConsolidationAgent._run_consolidation_loop` (collects and posts). `UserAgentFactory` receives
  `alert_sink` from `main.py` and reads the env knob, then passes both in: REQ-ARCH-01 forbids
  `os.getenv` inside `agents/`, so the call site is the composition layer.
