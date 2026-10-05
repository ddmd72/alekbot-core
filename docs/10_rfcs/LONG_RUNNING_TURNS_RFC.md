# RFC: Long-running turns — a chat turn may run long, and says so

**Status:** Draft, revision 5 — two independent reviews applied (Fable, 2026-10-05; §11). Ready for implementation planning unless the owner calls another review.
**Date:** 2026-10-05 (first draft 2026-10-04)
**Owner decisions:**
- **Universal, not skill-specific.** Agent Skills exposed the problem; it belongs to every turn that does many tool calls or one long reasoning call.
- **UX first.** Judged by what the owner experiences in chat (§3).
- **Simplest of equivalent designs** (owner, 2026-10-05).
- **The trigger is a timer.** The model never decides that a job is long.
- **The run itself writes the late answer**, having seen what was said in chat meanwhile. No separate result turn.
- **A late answer goes to the main feed, marked as late, linked to the message it answers** — for the owner and for the LLM reading history later.
- **No partial text.** One notice, then the answer (`project_two_phase_answer_dead_branch`).
- **Scope: the turn.** Per-call liveness and provider-side async are a separate topic (§8).

## 1. Problem

A turn that needs minutes is cut by limits set for other reasons, and the work done so far is discarded.

| Limit | Value | Where | When hit |
|-------|-------|-------|----------|
| Smart turn timeout | 300 s | `SmartAgentConfig.timeout_ms`; `base_agent.py` `_execute_with_timeout` | Turn cancelled, work discarded. Then the two-phase timeout path: Quick answers now ("a fuller answer could follow"), `SmartRetryService` schedules one background Smart retry (`smart_timeout_retry`, 360 s) that posts a `[System: delayed follow-up answer delivered]` pair (`agent_fallback_service.py`, `smart_retry_service.py`) |
| Delegation turns | 15 | `SmartAgentConfig.max_delegation_turns` | `max_turns_exhausted` → failure |
| LLM client timeout | 300 s | `AsyncOpenAI(timeout=300.0)` in OpenAI/Grok adapters — **ours** (SDK default 600 s), `max_retries=2` | `APITimeoutError`, SDK retries |
| Slack worker task deadline | unset → Cloud Tasks default 600 s | `gcp_task_queue.enqueue_slack_event` | Cloud Tasks retries the turn |
| Slack queue `alek-bot-tasks-dev` | maxAttempts **100**, maxConcurrentDispatches 5 | Cloud Tasks (europe-west1) | A dead attempt is retried up to 100 times |
| Cloud Run request | 1800 s | `cloudbuild-dev.yaml` | Hard stop |

**The chat is not blocked — except inside a Slack thread.** Slack's worker lock is keyed by `thread_ts or ts` (`slack/http_adapter.py`, intake), not by the session (`user_id:channel`). A top-level message has its own `ts` and never contends; a reply in the same Slack thread gets 429 and is retried with backoff. Telegram has no lock. What the owner experiences is **uncertainty** (a spinner for minutes) and, past 300 s, **loss**.

**Data (Logfire, 2026-09-21 → 10-04, `delegation.loop`, n=146):** p50 42 s; 19 runs over 240 s, nearly all the daily email review (already on a 1500 s SLA). The one interactive run cut (2026-10-04 16:10) was a single `grok-4.7` reasoning call, cancelled by the turn timeout while still thinking; the xAI console shows it then **completed validly** — 46,548 output tokens, ≈ $0.32 — billed and thrown away. At median grok-4.7 throughput (93.5 tokens/s, n=14) it needed ~500 s (~410–770 s).

## 2. Goals and non-goals

**Goals**
- A long turn is never lost: an answer, a partial answer with what remains, or a failure line.
- The owner learns within a fixed window that the turn will take long, and that writing meanwhile is fine — in threads too.
- The owner can ask what is running and cancel it.
- One mechanism for Slack and Telegram and every orchestrator with a tool loop.

**Non-goals (v1):** work beyond ~25 min; automatic follow-up legs; the running turn taking on new requests; per-call liveness (§8).

## 3. The experience

1. **A short turn** — as today.
2. **A long turn** — at ~90 s the status line becomes: *working on it, will answer when done — you can keep writing.*
3. **Meanwhile** — the owner talks to Alek as usual, in the feed or in the thread. "How is it going?" gets what the job is doing now; "cancel it" cancels. Alek does not answer the running question a second time.
4. **The answer** arrives in the main feed, prefixed **[late answer]** with a link to the message it answers, written knowing what was said meanwhile. Incomplete work says what is done and what remains; "continue" is an ordinary turn.
5. **A failure** is one line, never silence.

## 4. Approaches considered

| Approach | Verdict |
|----------|---------|
| **Raise the limits and present them well** | **Chosen.** Earlier revisions rejected it because "the chat stays blocked"; outside Slack threads it is not (§1), and the thread case is handled at the mark (§5.3). |
| Fork with a separate result turn (revision 3) | Rejected: the lock it relied on does not guard the session, "one writer" never held, and the result turn could lose a finished result and knew less than the run. |
| Keep the two-phase timeout path (Quick now + `SmartRetryService` retry) | Rejected: with a 25 min budget a timeout fires only after the mark; the path would promise follow-ups that never come. Retired (§5.9). |
| Model declares the job long | Rejected (owner): non-deterministic. |
| Alek calls himself as a subagent | A different capability; own RFC. Today the cycle guard refuses Smart → Smart. |
| Serialize the run into a new Cloud Task; Cloud Run Job per turn | Rejected: cost far above need at 513 s observed vs ~25 min budget. |

## 5. Design

### 5.1 One absolute deadline per chat turn

The turn gets one **absolute deadline** at its start (~25 min, under the 1800 s request ceiling) and ~40 loop turns. The deadline travels in the message context, not as a per-attempt timeout, so every layer reads the same clock:
- **LLM calls:** `LLMRequest.timeout = remaining − wrap-up reserve`. The OpenAI/Grok adapters must honour a timeout above their 300 s client ceiling and must not let SDK retries overshoot the deadline (adapter change + wire tests).
- **Whole-turn retries:** after the mark (§5.3) the turn runs with `suppress_transient_retry` and without the L2 cross-provider restart. A 503 at minute 20 must not re-run 20 minutes of side-effecting tools on a fresh clock. A transient failure after the mark goes to wrap-up or the failure line.
- **Hard stop:** `wait_for` at the deadline plus margin, last resort only.

No foreground/background split: a turn still running at 90 s is a long turn by definition.

### 5.2 Wrap-up instead of a hard stop

When the deadline minus the reserve is reached, or loop turns run out, the loop makes one final call that cannot call tools: what is done, what remains. Mechanics differ per provider and are settled in the plan with wire tests: Anthropic needs `tool_choice: none` (a request with `tool_use` history and no `tools` is rejected); on Grok the terminal `deliver_response` is a real function (`delegation_engine.py`). An LLM call still running when the reserve starts is cancelled so the wrap-up has its time.

### 5.3 The 90-second mark

A timer started with the turn fires at ~90 s, beside the loop. It:
1. swaps the status line for the notice (§3.2);
2. appends the pair `[user message, notice]` to the session. The notice is the **model** message, as shown in chat — not a `[System:]` line in the model's mouth, which the model would learn to imitate. The user message keeps the platform event time as `created_at`, not save time, so it sorts before messages sent after it;
3. builds the user parts (cleaned text, file stubs) **before** this write — the end-of-turn path no longer writes them;
4. creates the job record (§5.4);
5. **releases the Slack thread lock**, so replies in the same thread are processed now, not 5–10 min later out of order.

A turn that ends before 90 s does none of this — today's behaviour.

### 5.4 Job record

One Firestore document per long turn, keyed by the platform event id: user, session, origin message (id + link), short title, started at, deadline, status (`running` / `done` / `failed` / `cancelled`), **heartbeat** with the current step (loop turn, tool or "thinking"), **cancel flag**, TTL.

- **Heartbeat** — written by the timer task every ~30 s. It is also the cancel poll: when it finds the flag set, it cancels the agent.
- **The agent runs as a child task** of the request handler. Cancel and the hard stop cancel that child only; the handler survives and returns 200. Cancelling the handler's own task would kill the request and make Cloud Tasks retry a cancelled turn.
- **Status and cancel for Smart** — running jobs (title, elapsed, current step) are a block in Smart's dynamic context; "cancel" sets the flag through a small local tool (the `DelegationEngine(local_tools=)` seam).

### 5.5 Chat since the mark

Before **every** LLM call after the mark, the run appends, as one user-role context note, the session messages saved after the turn's own history snapshot (by index / `created_at`, not by the marker — a turn that finished between 0 and 90 s sits before the marker and must still be seen). It uses the stored summaries (`text`), not `full_text`. Append-only, so prompt caching is unharmed; each provider needs a wire test for a text part placed after a tool result.

The run uses this context to shape its answer. It does **not** take on new requests from it.

Later turns get a prompt rule: *a running job owns its question — do not answer it again; it will see what you say but takes no new work; do not promise that it will include anything.*

### 5.6 The late answer

- **One append** at the end: a user-role note `[System: late answer to "<quoted question>" (<time>, <link>)]` carrying any `consolidation_text` produced by `save_to_memory` after the mark (the serializer reads it on user parts) + the model answer.
- **Chat:** main feed, prefixed **[late answer]**, link to the originating message (Slack `chat.getPermalink`; Telegram reply). Delivery items go through the normal `_deliver_item` path — the run is still inside `ConversationHandler`.
- **Invariant:** every session write is an atomic pair (`append_messages_batch`, Firestore transaction). Notifications already write the session independently; nothing here relies on a single writer.

### 5.7 Failure and retries

- **Worker idempotency on entry.** Cloud Tasks retries an attempt whose instance died (OOM, crash) — up to 100 times on the Slack queue — and intake dedup does not cover the worker. The worker looks up the job record by event id: `running` with a fresh heartbeat → return 200, do nothing; `running` with a stale heartbeat → mark `failed`, post one line, return 200. This retry is also the fastest death detector: seconds, not minutes.
- **Application errors** are already swallowed with 200 by the worker; after the mark they produce the failure line, never a Quick answer dropped into chat without context.
- **Deploys do not kill a running turn.** Cloud Run lets in-flight requests on the old revision finish; SIGTERM reaches an instance only when it is idle. No SIGTERM path, no delayed check task.

### 5.8 Telegram moves to Cloud Tasks (prerequisite)

An inline webhook cannot hold a 25 min turn: once Telegram drops the connection, the request loses its CPU. The webhook keeps `update_id` dedup, answers 200 and enqueues the update; the worker sends the status message, downloads files (`get_file`) and transcribes voice. `/worker` routing for Telegram tasks is new (today it falls through to the Slack worker). Ordering is no worse than today. Closes the "Telegram webhook blocking" backlog item.

Both platforms' worker tasks get `dispatch_deadline=1800`.

### 5.9 Retire the two-phase timeout path

With one deadline, Smart times out only after the mark, where §5.2 and §5.7 own the outcome. Removed: the "fuller answer may follow" branch of `AgentFallbackService`, `SmartRetryService`, the `smart_timeout_retry` task type and its `[System: delayed follow-up answer delivered]` notes. Quick fallback stays for failures **before** the mark. Its tests are changed by a reviewer, per the test rule.

## 6. Parameters and constraints

- **Notice window** ~90 s — from interactive turn durations.
- **Budget** ~25 min / ~40 loop turns; **wrap-up reserve** sized from observed wrap-up calls.
- **Queue concurrency:** `maxConcurrentDispatches 5` — each long turn holds a slot for its whole run.
- **Resource exhaustion** (OOM on the shared 1 GiB instance) is out of scope — graceful degradation under resource pressure is its own topic. This RFC's part is only §5.7: a turn killed with its instance is reported, never silently re-run.

## 7. Per entry point

| Entry point | Notice + late answer | Deadline + wrap-up |
|-------------|----------------------|--------------------|
| Chat turn — Slack, Telegram (session store history) | **Yes** | Yes |
| Bound / companion channels (history read from the platform, no session marker) | **No in v1** | Yes |
| `ask_alek` — Lelik's question (SYNC, 600 s) | No — the voice path's own late-answer-to-chat stays | Yes |
| `tell_alek` — Lelik's errand (Cloud Task, 720 s) | No — already background | Yes |
| Background notifications | No | Yes, inside `notification_sla.py` |

## 8. Deliberately not built — and when to revisit

| Not built | Revisit when |
|-----------|--------------|
| Per-call liveness (streaming + idle timeout), provider-side async. **Accepted regression:** a truly hung call costs up to the deadline (~25 min) before the failure line, vs 300 s today. | A hang is seen — first trigger. Own RFC. |
| Automatic follow-up legs; the running turn taking new requests | "Continue" becomes a chore |
| Checkpoint / continuation across requests | Runs hit the deadline |
| Per-user cap on running jobs | Contention seen (note the queue's 5-slot limit, §6) |
| Loop heuristics, per-execution cost cap | A run repeats itself / a cost spike |
| Notice + late answer in bound / companion channels | A bound-channel turn runs long |
| Thread awareness in history (Slack threads and feed are one flat list) | Own change; the late-answer note covers this RFC's case |

## 9. Open questions

1. **Telegram status line** — what the notice replaces there (no editable "thinking" message in every client).
2. **Delivery items sent before the mark** — confirm none is delivered twice or lost when the answer is late.
3. **Lelik asking "what is Alek doing?"** — his warm context has the notice, not the job record. Enough?

## 10. Next

Implementation plan. The plan carries the per-provider work as wire-test tasks: wrap-up mechanics (§5.2), timeouts above the client ceiling (§5.1), text-after-tool-result (§5.5).

## 11. Review record

**Review 1 (Fable, skeptical architect, 2026-10-05) of revision 3.** Design built on two false premises. Accepted: Slack lock is per-thread, not per-session; "one writer" never held → atomic pairs; split budget fails the 16:10 case → one budget; separate result turn can lose a result → the run writes the answer; Telegram needs the queue; instance death detected too slowly; concurrency cap to §8; hung-call regression named; no Quick fallback after the mark. Owner: late answer in the main feed, labelled and linked.

**Review 2 (Fable, 2026-10-05) of revision 4.** Right shape, five holes. Accepted:
- "Before the final call" is not knowable, and the marker is the wrong window → before every call after the mark, by snapshot index (§5.5).
- The real retry threat is instance death with a 100-attempt queue → worker idempotency by event id; delayed check removed (§5.7).
- Cancelling the run's task kills the request → agent as a child task; cancel via the heartbeat-polled flag (§5.4).
- The two-phase timeout path dies silently → retired explicitly (§5.9).
- Per-attempt timeouts and whole-turn retries break the budget → one absolute deadline; no whole-turn retry after the mark (§5.1).
- Slack thread replies are delayed out of order → release the thread lock at the mark (§5.3).
- `save_to_memory` output after the mark has no home → carried on the late-answer note (§5.6).
- Provider-specific wrap-up, notice as model text, platform event time, the "running job owns its question" rule, bound channels scoped (§5.2, §5.3, §5.5, §7).
- Deploy premise verified: Cloud Run finishes in-flight requests on the old revision — SIGTERM path dropped (§5.7).
