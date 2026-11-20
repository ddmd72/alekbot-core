<!-- living-doc
covers:
  src/domain/long_turn.py
  src/domain/turn_clock.py
  src/ports/long_turn_registry.py
  src/adapters/firestore_long_turn_registry.py
  src/services/long_turn_service.py
  src/infrastructure/long_turn_tools.py
-->
# Long-running Turns

## Purpose

A chat turn may need minutes: many tool calls, or one long reasoning call. This block lets such a turn finish instead of
being cut by a short timeout, tells the owner at a fixed moment that the answer will come later, and delivers it as a
**late answer** in the main feed. Three parts carry it:

- **One absolute deadline per turn.** A `TurnClock` is created at the start of a chat turn and read by every layer
  (delegation loop, base agent, Smart, the drive specialist). The LLM adapters never read it: they receive the
  remaining budget as `LLMRequest.timeout`.
- **The mark (`NOTICE_AFTER_S`).** A timer beside the agent swaps the status line for a notice, writes the user message and the
  notice into the session, stores a registry record, and (Slack) releases the Slack worker lock. From then on the turn is
  "marked": the owner may write meanwhile, can ask what runs, and can cancel it.
- **The late answer.** The run that did the work posts its own result, because only it knows what was said in chat
  while it was away. There is no separate result turn.

The trigger is a timer, never a model judgment. What the notes mean to the model (a running job owns its question, what
the meanwhile and wrap-up notes are) is the prompt token `PROTOCOL_LONG_TURNS`, stored in Firestore and uploaded
by the owner; no text in this block's code tells the model how to behave.

What it is not for: bound channels and companion bindings (no clock, today's timeouts), `ask_alek`, `tell_alek` and
background notifications (the owner froze clocking them), and per-call liveness. A hung provider call costs
up to the whole deadline before the failure line; that regression is accepted until a real hang is seen.

## Owned data

**Long-turn record** — Firestore collection `EnvironmentConfig.long_turns_collection`, one document per marked turn,
id = the turn id (`slack:<event_id>`, `telegram:<update_id>` or, for a queued `$geo` question, `geo:<link fingerprint>`, set by the platform adapters; a turn without an id
never takes the long-turn path). The document is `LongTurnRecord`: user, session, a title cut from the question,
`started_at`, `heartbeat_at`, the current `step`, `status` (`running`, `done`, `failed`, `cancelled`) and
`cancel_requested`, plus `expires_at` for the collection's TTL policy (`_TTL` in the adapter; the policy itself is a manual owner step,
`docs/07_deployment/README.md`). Writers: `LongTurnService.mark` (create), the heartbeat loop (liveness, step, reads the
cancel flag), `LongTurnService.finish` (status), `LongTurnRegistry.request_cancel` (the flag). Readers: the retry check
at the start of a turn, Smart's running-jobs note.

**The session** (`SessionStore`, owned by the session layer) gets three kinds of long-turn writes, all atomic pairs
(`append_messages_batch`): the user message plus the notice at the mark; the late-answer pair (a user-role note
`[System: late answer to "<question>" ...]` plus the model answer) at the end; and, on every non-answer outcome (failure,
cancel, lost), the same pair holding the one-line outcome, so history never ends on an open question.

Nothing else is stored. The clock lives in a `ContextVar` for the life of the turn.

## Flows

### Start and the mark

1. A platform event becomes a worker task: Slack through `src/adapters/slack/http_adapter.py::HTTPModeAdapter.handle_queued_event`,
   Telegram through a `telegram_update` task (below). Both set `turn_id` in the context metadata.
2. `src/handlers/conversation_handler.py::ConversationHandler` enables long turns only for the orchestrator flow with an
   identity: a `turn_id`, not a bound channel, a mode that writes the session. Then `LongTurnService.check_retry` runs
   (see Retry).
3. `ConversationHandler._run_agent_turn` creates the clock (`LongTurnService.new_clock`), sets `CURRENT_TURN_CLOCK`,
   and starts the agent as a **child task** (the task copies the context, so the var is reset in the handler right
   after). Cancel and the hard stop cancel that child only; the handler survives and the worker still answers 200.
   The routed `AgentMessage` carries a timeout of budget plus `HARD_STOP_MARGIN_S` as the last-resort stop.
4. `ConversationHandler._watch_long_turn` sleeps `NOTICE_AFTER_S`. If the agent is still running it takes
   `_LongTurn.lock`, sets `marked` on both the state and the clock, stops the status animation, replaces the status
   line with the notice, calls `LongTurnService.mark`, and fires `ResponseChannel.on_long_turn`. The lock is what
   decides a race: whoever takes it first, the mark or the agent finishing, decides whether the answer goes the normal
   way or as a late answer.
5. `LongTurnService.mark` writes the registry record first, then the session pair, each write standing alone and neither
   raising: a failed session write must not cost the record that retry and cancel depend on. The user message keeps the
   platform event time as `created_at`, and the notice is the **model** message (what the chat shows), not a system
   line. The notice's `created_at` is remembered in `TurnClock.own_created_ats` so the run never lists it as "said
   meanwhile".
6. On Slack the `on_long_turn` hook releases the worker lock held since the task began (see the lock trap below)
   (`HTTPModeAdapter` passes `release_lock` into `SlackResponseChannel`). A turn that ends before the mark does none of
   this; it is the ordinary path and writes its history at the end.

### Heartbeat and cancel

After the mark the watcher loops every `HEARTBEAT_S`: `LongTurnService.heartbeat` writes liveness and
`TurnClock.step` and returns whether `cancel_requested` is set. A set flag cancels the agent child task and marks the
state `cancelled`. Smart sees its own other running turns through `SmartResponseAgent._load_running_jobs` (the record
list, excluding the current turn), rendered by `src/infrastructure/long_turn_tools.py::running_job_lines` into a keyed
note on the last history message, and gets the local tool `cancel_long_turn`
(`make_cancel_long_turn_handler`), which calls `request_cancel`. The tool takes a short id (the last characters of the
turn id) and only works on the caller's own running turns. It reports "stops within a heartbeat" because the stop waits for
the next heartbeat.

### Inside the delegation loop

`DelegationEngine.execute(use_turn_clock=True)` is passed only by `SmartResponseAgent`; nested engines get no clock and never read the var.
Code that reads it outside the loop: `BaseAgent` (retry policy and the per-call re-clamp) and the drive specialist's
`src/agents/drive_agent.py::soft_deadline`.

- **Per call:** `SmartResponseAgent` sets `LLMRequest.timeout` to `TurnClock.call_timeout`, the remaining time minus the
  wrap-up reserve. `BaseAgent._call_llm` re-clamps it before each retry or failover, because a clock only shrinks.
- **Chat since the mark:** before every LLM call once marked, `DelegationEngine._append_chat_since` fetches the
  session messages written after `max(snapshot_at, seen_until)` (`TurnClock.fetch_since`, from `new_clock`), drops the
  notice, and appends their stored summaries (`text`, never `full_text`) as a keyed `[System: meanwhile in chat]` note.
  `snapshot_at` is stamped by Smart when it builds the prompt, so a turn that finished before the mark is still
  seen. The note is appended to the last user-role message when the history ends in one (tool results), else added as a
  new message, so two user messages never sit side by side. The run shapes its answer from this; it takes on no new
  request from it.
- **Wrap-up:** when `TurnClock.in_reserve` holds, a tool batch ran out of budget, or the last loop turn is reached, the
  engine appends the wrap-up note and makes one final call with `wrap_up_timeout` (never under `WRAP_UP_FLOOR_S`).
  `DelegationEngine._wrap_up_result` dispatches **nothing** on that call: a terminal tool call wins, otherwise any text
  is the answer, otherwise the result is a failure. Tools stay declared; nothing is provider-specific. A tool batch is
  itself bounded by `asyncio.wait_for` at `call_timeout`, so it cannot eat the wrap-up's time.
- **No whole-turn retry.** `BaseAgent._effective_retry_policy` returns no retry for the orchestrator of a clocked turn
  (whether marked or not), because a rerun redoes every tool call. The per-call retry in `_call_llm` stays and is
  skipped inside the reserve. `SmartResponseAgent` also refuses the provider rotation on `TranscriptLockedError` once
  marked, and the Quick fallback (`ConversationHandler._route_with_fallback`) runs only before the mark.

### The late answer

`ConversationHandler._finish_long_turn` handles every outcome of a marked turn, and always closes the record in its
`finally` (a record left `running` would read as live until its heartbeat goes stale):

1. Fetch the origin message link once. Cancelled, failed and successful outcomes each pick their fixed UI line or the
   answer.
2. `_close_long_turn_in_session` writes the late-answer pair **before** delivery, so a failed post never loses the
   answer from history. The text goes through the same `_prepare_history_texts` as the normal path, so the sanitised
   text, not the raw one, is what later prompts see.
3. `ResponseChannel.send_late_answer` delivers: Slack posts a header with the origin permalink as its own top-level
   message, then the answer chunks (never a thread, never posted-then-edited); Telegram replies to the origin message with the prefix, then
   edits that message to prefix plus the first chunk and sends the rest as new messages (it is posted-then-edited, unlike
   Slack). Rich content and delivery items follow through the ordinary delivery
   path. A delivery error posts the failure line instead.

### Retry and instance death

`LongTurnService.check_retry` runs at the start of every long-turn-eligible event and returns a `RetryVerdict`
(`LongTurnRecord.verdict`). `NEW`: process. `RUNNING` or `FINISHED`: do nothing, answer 200. `STALE`: the record is
set `failed`, the session is closed with a "lost" line, and one late-answer line is posted. A record that reads
`RUNNING` but whose turn id is not in the handler's in-process `_live_turns` is treated as stale at once, without
waiting for the heartbeat to age: Cloud Tasks redelivers only a failed attempt and the main service runs one instance,
so the attempt that wrote it is dead. A failing registry read is treated as `NEW`; the registry is an aid, not a gate.

### Telegram hand-off

An inline webhook cannot hold a long turn. `TelegramWebhookAdapter._handle_telegram_update` verifies, then enqueues a
named `telegram_update` worker task (name = the `update_id`, so Telegram's own retry is a no-op) with the long
dispatch deadline and answers 200. `WorkerHandler._handle_telegram_update` routes it to
`TelegramWebhookAdapter.handle_queued_update`, which processes the update and **never answers 5xx**: a retry would rerun
a turn already written. Slack worker tasks carry the same dispatch deadline (`src/adapters/gcp_task_queue.py`, `enqueue_slack_event`).

## Invariants and traps

- **One clock, read everywhere, owned by one orchestrator.** Only `SmartResponseAgent` starts a wrap-up and the chat-since
  note. `tests/unit/agents/core/test_smart_turn_clock.py`, `tests/unit/infrastructure/test_delegation_engine_wrap_up.py`,
  `tests/unit/infrastructure/test_delegation_engine_clock_bounds.py`.
- **The mark and the agent's end are serialised by `_LongTurn.lock`.** Take that into account in any change to the end of
  `_await_agent_turn`. `tests/unit/handlers/test_conversation_handler_long_turn.py`.
- **The Slack mark releases the lock.** `tests/unit/adapters/test_slack_worker_long_turn.py`.
- **After the mark an application error is the failure line**, never the generic in-thread error and never a Quick answer
  dropped into chat without context.
- **A cancelled child task must not become a cancelled handler.** Only `_LongTurn.cancelled` turns a `CancelledError` into
  a failure response; any other cancellation propagates.
- **Every session write is an atomic pair** (user message plus model message). Notifications write the session
  independently; nothing here relies on a single writer. `tests/unit/services/test_long_turn_service.py`,
  `tests/unit/services/test_long_turn_service_store_pins.py`.
- **A late-answer note is a data marker, not a voice.** The notice is a model message; the meanwhile, wrap-up and running
  jobs notes are keyed system notes (`wrap_system`, `keyed_header`) and the meanwhile text is neutralised
  (`DelegationEngine._append_chat_since`), so chat text cannot forge them. The late-answer note is persisted, so it is
  plain, unkeyed text (`src/domain/turn_clock.py::render_late_answer_note`)
  (`docs/10_rfcs/SYSTEM_TURN_FINGERPRINT_RFC.md`).
- **Registry port contract:** the tenancy, staleness and not-found rules are in the docstring of
  `src/ports/long_turn_registry.py::LongTurnRegistry`. `request_cancel` is read-then-update, not a transaction, and
  `list_running` filters stale heartbeats in code. `tests/unit/adapters/test_long_turn_store_kit.py` pins the Firestore side.
- **The Slack worker lock is per thread, not per chat session.** For message and mention events the lock key is
  `thread_ts or ts` computed at intake (the variable is called `session_id`; its value is not the chat session id), so
  a top-level message never waits and only a reply in the same thread does. The mark releases it so that reply can run.
  `BUSY_WAIT_S` is deliberately above `NOTICE_AFTER_S`: a waiting reply gets the lock once the mark releases it; change
  neither constant alone. Queued `$geo` turns (`HTTPModeAdapter.handle_geo_mark_turn`) lock on the chat session id
  instead. `tests/unit/adapters/test_slack_worker_long_turn.py`.
- **The Slack late answer is never edited.** Its docstring
  (`src/adapters/slack/response_channel.py::SlackResponseChannel.send_late_answer`) gives the reason; the header is also
  its own first post so the unfurl preview sits above the answer.
  `tests/unit/adapters/test_slack_late_answer_no_edit.py`, `tests/unit/adapters/test_slack_late_answer_header_first.py`.
- **`PROTOCOL_LONG_TURNS` ships by token upload, not by deploy.**  The Firestore TTL policy on the collection is also a manual owner step.
- **Constructor dependencies are optional.** `long_turn_service` and `long_turn_registry` default to `None` in
  `ConversationHandler`, `SmartResponseAgent`, `UserAgentFactory` and `ChatServices` (`src/composition/agents_setup.py`
  always builds both); with `None` the turn takes today's path. A test or a
  deployment without the registry silently loses the whole feature, not an error.
- **Clocking is gated by the orchestrator flow with a session store.** Adding another entry point that should run long
  needs its own clock and its own late-answer story; the frozen part of the RFC lists them.

## Links

- [`LONG_RUNNING_TURNS_RFC.md`](../../10_rfcs/LONG_RUNNING_TURNS_RFC.md): the design, kept as history; §5.3–§5.8 carry
  the reasoning behind the mark, the record, the late answer, retry and the Telegram hand-off.
- [`long_running_turns.md`](../../04_solution_strategy/decisions/long_running_turns.md): the decision, the deltas from the
  RFC (D1–D6) and the accepted regression.
- [`background_turn_started_is_final.md`](../../04_solution_strategy/decisions/background_turn_started_is_final.md): the
  background tasks' rule that a started turn is never re-run.
- [`SYSTEM_TURN_FINGERPRINT_RFC.md`](../../10_rfcs/SYSTEM_TURN_FINGERPRINT_RFC.md): the keyed system notes.
- Deployment: [README.md](../../07_deployment/README.md) (Firestore TTL policies), [SCHEDULERS.md](../../07_deployment/SCHEDULERS.md).
- Related blocks: [Slack Integration](../slack_integration/README.md), [Telegram Integration](../telegram_integration/README.md).

## Where to start reading

1. `src/domain/turn_clock.py::TurnClock`: the deadline, the reserve and the notes' shapes.
2. `src/handlers/conversation_handler.py::ConversationHandler._run_agent_turn`: the child task and the watcher.
3. `src/handlers/conversation_handler.py::ConversationHandler._finish_long_turn`: every outcome of a marked turn.
4. `src/infrastructure/delegation_engine.py::DelegationEngine.execute`: chat-since, wrap-up and the budget bound.
5. `src/services/long_turn_service.py::LongTurnService.mark`: what the mark writes.
6. `src/services/long_turn_service.py::LongTurnService.check_retry`: the retry verdict.
7. `src/infrastructure/long_turn_tools.py::make_cancel_long_turn_handler`: Smart's cancel tool.
