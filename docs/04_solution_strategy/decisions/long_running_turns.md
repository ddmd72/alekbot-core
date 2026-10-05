# Long-running turns: one absolute clock, notice at 90s, late answer to the main feed

**Date:** 2026-10-05
**Status:** Accepted — implemented on `feat/long-running-turns`

## Context

Smart's turn timeout (300 s) cut work that routinely needed more: Logfire data
(2026-09-21 → 10-04, `delegation.loop`, n=146) showed 19 runs over 240 s, and one
interactive `grok-4.7` run was cancelled by the timeout while still reasoning — the
provider console showed it completed validly moments later, 46,548 output tokens
(~$0.32) billed and thrown away. The existing mitigation, a two-phase timeout path
(Quick answers "a fuller answer could follow", `SmartRetryService` schedules one
background Smart retry), only ever fires *after* a hard cut — it cannot turn a
cancelled run into a finished one, because the work is already gone.

Full design: `docs/10_rfcs/LONG_RUNNING_TURNS_RFC.md`.

## Decision

Give each chat turn one absolute wall-clock deadline (1500 s budget, 120 s wrap-up
reserve, 40 loop turns) instead of a short hard timeout. A `TurnClock`
(`domain/turn_clock.py`) carried in a `ContextVar` is visible to the LLM adapters and
the delegation engine for the lifetime of the turn. At 90 s unanswered, the run posts a
status note and a `[user, notice]` history pair so a second message sent in the
meantime is answered normally, not queued behind the first. When the run finishes, it
posts `[late answer]` plus a permalink to the main feed itself — the run that did the
work writes its own result, because only it knows what was actually said in chat while
it was away (no separate "result turn" that could race the original, per RFC §4's
rejection of the revision-3 fork design). Persistence (registry record, heartbeat,
cancel, chat-since lookup) is `LongTurnService` over a new `LongTurnRegistry` port,
backed by `FirestoreLongTurnRegistry` (`{prefix}long_turns`, TTL on `expires_at`).
Telegram updates move off the inline webhook into a `/worker` Cloud Task
(`task_type=telegram_update`) so a 25-minute turn does not hold the HTTP connection
Telegram will drop anyway. Slack's per-thread worker lock (keyed by `thread_ts or ts`,
not by session) releases at the 90 s mark instead of the end of the task, so a thread
reply sent meanwhile does not retry into a 429. Behavioural rules for the model (what
the notice/wrap-up/chat-since pairs mean, that the running job owns its own question)
live in one new prompt token, `PROTOCOL_LONG_TURNS` — not inline in agent code, per the
repo's "no fallback prompts / behaviour lives in tokens" convention.

## Deltas from the RFC (declared before implementation, owner-approved)

| # | RFC says | Plan does | Why |
|---|----------|-----------|-----|
| D1 | §5.1: OpenAI/Grok adapters must be changed to honour a timeout above their 300 s client ceiling | No adapter change | Verified: OpenAI and Grok already pass `LLMRequest.timeout` as the per-request SDK `timeout` (overrides the client ceiling) and bound SDK retries with an outer `asyncio.wait_for`; Claude and Gemini bound the call with `wait_for`. Setting `LLMRequest.timeout` is enough. |
| D2 | §5.1: no whole-turn transient retry **after the mark** | No whole-turn transient retry for the orchestrator of a clocked chat turn **at all**; the per-call same-provider retry in `_call_llm` stays, guarded by the deadline | A whole-turn retry re-runs every tool call; before the mark it costs up to 90 s and re-does side effects too. The per-call retry already covers the 429/503 blip it was for. One rule instead of a mark-dependent one. |
| D3 | §5.2: wrap-up call "cannot call tools" (`tool_choice: none` on Anthropic, provider-specific mechanics) | Wrap-up = a marker note + the engine dispatches **nothing** on that turn except the terminal tool; tools stay declared | Provider-agnostic, no adapter changes, no Anthropic 400 risk. If the model still calls a tool, the call is not dispatched and the turn ends with what it has. |
| D4 | §5.4: "one Firestore document per long turn" | Same document, behind a new port `LongTurnRegistry` + `FirestoreLongTurnRegistry` | System boundary + test substitution (CLAUDE.md "Port is justified"). |
| D5 | §5.5 / §5.3: "prompt rule" for later turns | The rules (running job owns its question, meaning of the meanwhile / wrap-up / late-answer notes) are one new token `PROTOCOL_LONG_TURNS`, uploaded by the owner and added to Smart's profile | CLAUDE.md: behavioural guidance lives in prompt tokens; no inline prompts in agents. |
| D6 | §5.5: chat since the mark appended "as one user-role context note" | Appended as a **text part on the last user-role message** when the history ends in one (tool results), else as a new user message | Avoids two consecutive user messages, which some providers reject or merge unpredictably. Pinned by wire tests per provider. |

## Rejected alternatives (RFC §4)

| Approach | Verdict |
|----------|---------|
| Raise the limits and present them well | **Chosen** — the one implemented here. |
| Fork with a separate result turn (RFC revision 3) | Rejected: the lock it relied on does not guard the session, "one writer" never held, and the result turn could lose a finished result and knew less than the run that produced it. |
| Keep the two-phase timeout path (Quick now + `SmartRetryService` retry) | Rejected and retired: with a 25 min budget a timeout fires only after the mark, so the path would promise follow-ups that never come. `SmartRetryService`/`smart_timeout_retry` deleted. |
| Model declares the job long | Rejected (owner): non-deterministic — the trigger must be a timer, not a model judgment call. |
| Alek calls himself as a subagent | A different capability, own RFC; the cycle guard refuses Smart → Smart today. |
| Serialize the run into a new Cloud Task, or a Cloud Run Job per turn | Rejected: cost far above need at ~513 s observed p99 vs a ~25 min budget. |

## Accepted regression

**A truly hung call now costs up to the full deadline (~25 min) before the failure
line, instead of 300 s today.** Per-call liveness (streaming + idle timeout) and
provider-side async execution would close this gap but are deliberately not built in
this RFC (RFC §8) — revisit the first time a genuine hang (not a slow-but-progressing
call) is observed in production; it becomes its own RFC rather than a retrofit here.

## Deferred, not in this branch

RFC §7 lists deadline + wrap-up coverage for `ask_alek`, `tell_alek` and background
notifications (`UserNotificationService`) alongside chat turns. This implementation
clocks chat turns only (`SmartResponseAgent`'s top-level engine via
`use_turn_clock=True`; nested delegation engines do not get their own clock). Giving
the gateway/notification paths their own `TurnClock` and budget is an explicit owner
decision for a follow-up task, not assumed here.

## Consequences

- Smart and the clocked chat path gain a `cancel_long_turn` tool and a running-jobs
  note so the model can see and stop its own long work.
- Both platform adapter factories and `UserAgentFactory` take an extra, optional
  dependency (`long_turn_service` / `long_turn_registry`) — `None` is a safe default
  everywhere these are wired for tests or a deployment without the token uploaded yet.
- Deployment has two manual owner steps (Firestore TTL policy on `long_turns`, and
  uploading + enabling `PROTOCOL_LONG_TURNS`) — see `docs/07_deployment/README.md`.
