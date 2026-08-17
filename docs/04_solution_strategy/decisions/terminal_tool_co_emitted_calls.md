# A terminal tool may arrive alongside real work — dispatch it, then return

**Date:** 2026-08-17
**Status:** Accepted
**Scope:** `src/infrastructure/delegation_engine.py` (terminal-tool branch)

## Context

`DelegationEngine.execute()` terminates when the model calls `terminal_tool` (Smart passes
`deliver_response`). The check sat *before* tool execution and returned immediately, discarding
every other tool call in the same batch.

That was harmless while no adapter declared the tool — the state
`IMPLEMENTATION_ROADMAP.md` TD-3 recorded as "dead machinery, prefer deleting it". It stopped being
true on **2026-08-15**: constrained JSON output and function calling compete on xAI (measured 15/15
turns delegating without a text format vs 9/15 with one), so `GrokAdapter` moves the answer off the
text channel and onto a synthesized `deliver_response` function whose parameters are
`_RESPONSE_SCHEMA`. Smart's reply on Grok now arrives *only* through this branch.

Two days later the branch dropped real work. The 2026-08-17 morning briefing
(trace `01a00e52…`, 06:05–06:12 UTC) ran five Smart turns, composed a full Ukrainian newspaper, and
on turn 5 emitted **two** tool calls:

```
tool_calls=[('delegate_to_specialist', …-58), ('deliver_response', …-59)]
→ terminal tool 'deliver_response' received
→ done (328 chars)
```

The engine returned on the terminal call. `create_html_page` was never dispatched — zero
HtmlPageGenerator spans that day, no Cloud Task enqueued, no page. The user got the chat message
promising a digest and nothing else. (The same briefing had succeeded on 08-16: grok-4.6, 203s.)

The model was not malfunctioning. The daily-review / briefing protocol asks for exactly two things —
an HTML report *and* a short chat message — so co-emission is the natural encoding of that
instruction, and it will recur.

## Decision

When the terminal tool is present, dispatch every co-emitted non-terminal call **before** returning,
fold its `delivery_items` / `history_context` / `structured_data` into the result, then return the
terminal answer.

No result is fed back to the model: it has already written its reply. Siblings are dispatched
anyway because the model *committed* to them — an async generation hop must reach its queue, and a
`save_to_memory` / `delete_file` must actually happen. Dropping those is data loss, not an
optimisation.

Siblings are filtered by **name**, not identity, so a duplicate `deliver_response` is discarded
rather than dispatched as an empty-intent delegation.

## Alternatives considered

- **Ignore the terminal tool when siblings are present; execute everything and let the model deliver
  on the next turn.** Most faithful — the model would see the results. Rejected: it buys nothing
  here (the answer is already written), costs a full extra Smart turn (15–60s on grok-4.6, since
  turns measured 5.5/52.3/63.2s), and risks looping to `max_turns` against a model that co-emits by
  habit.
- **Dispatch only `ExecutionMode.ASYNC` siblings, drop SYNC ones as wasted.** Avoids paying for a
  sync result nobody reads. Rejected: it puts registry/mode knowledge in the engine for a rare
  shape, and it silently drops `save_to_memory`-class actions the model committed to. The accepted
  cost is that a co-emitted SYNC call is paid for and its text unread.
- **Delete the machinery per TD-3.** Would have removed Smart's only reply path on Grok.

## Consequences

- A co-emitted SYNC specialist runs and its text result is discarded. Accepted; rare shape.
- TD-3 is closed as **invalid**, with an explicit do-not-delete warning. "Unreachable" was a claim
  about the adapter set of the day, not about the engine — a new provider revived the branch without
  touching the engine.
- `_accumulate_tool_metadata` was extracted so the normal path and the terminal path fold
  tool-result metadata through one implementation and cannot drift.
- Tests: `tests/unit/infrastructure/test_delegation_engine_terminal_siblings.py` (7 cases; 5 fail
  without the fix). Docs corrected in root `CLAUDE.md`, `src/agents/CLAUDE.md`,
  `src/adapters/CLAUDE.md`, `IMPLEMENTATION_ROADMAP.md`.

## Not addressed here

The same morning, Smart timed out on an interactive question (trace `01a00eb17f…`, 07:48–07:53).
That was **not** this defect and not a budget defect. The cause turned out to be a standing
directive misfiring, not a perception or latency failure — see
`decisions/directive_applicability_gate.md`. The circuit breaker and the Quick fallback degraded as
designed.

An earlier revision of this section attributed the timeout to grok-4.6 "going looking for breakwater
geometry instead of asking Maps for `compute_routes`". That description was accurate but its
implication was wrong: it reads as though the model lacked a scale reference. It had one and had
already read it correctly.
