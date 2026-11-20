<!-- living-doc
covers:
  src/adapters/slack/
  src/composition/slack_adapter_factory.py
-->
# Slack Integration

## Purpose

This block is how a Slack message reaches the chat turn and how the answer gets back. Slack talks to the main service
over the **HTTP Events API** only; there is no other transport (`decisions/slack_socket_mode_removal.md`). The block
turns a signed Slack request into a Cloud Task, turns the task into a platform-neutral `MessageContext`, hands it to
the shared `ConversationHandler`, and renders the reply in Slack (mrkdwn, chunking, status message, buttons).

What it is not: it holds no business logic (the turn is `ConversationHandler`, see `docs/06_runtime/README.md`), does
not decide who may talk to the bot (`IAMService`), and does not pick agents. Telegram is a sibling block with the same
shape ([telegram_integration](../telegram_integration/README.md)). The same directory also holds three smaller Slack
adapters used elsewhere: `SlackMediaAdapter` (uploads for notifications), `SlackWebhookAdapter` (the ops alert sink,
`src/composition/operations_setup.py`) and `SlackChannelHistorySource` (see Invariants).

## Owned data

None of its own. What it leans on:

- **Cloud Tasks** — the Slack intake queue `alek-bot-tasks-{queue_suffix}` in `DeployTopology.slack_intake_region`
  (not the main region). A task name derived from the Slack `event_id` is the only dedup of Slack's retries.
- **Task claims** — `/worker` claims every delivery by task name (`decisions/worker_task_dedup.md`).
- **Slack itself as a session store** — for a channel bound with `$agent`/`$primary` the history is read back from the
  channel (`SlackChannelHistorySource.fetch`), not from Firestore.

## Flows

### Inbound event

1. Slack posts to `/slack/events` (`src/adapters/slack/http_adapter.py::HTTPModeAdapter._handle_slack_event`). The
   blueprint is registered with prefix `/slack` on the Quart app shared with OAuth, Cabinet and Telegram (`main.py`).
2. The raw body is read with a cap (`request_body.py::read_capped_body`, `MAX_BODY_BYTES`; over it: 413), the signature
   is verified on those bytes (`signature.py::verify_slack_signature`; failure: 401), and only then is the body parsed
   (not a JSON object: 400). This holds for `url_verification` too, so a forged challenge is never echoed.
3. The event is enqueued (`src/adapters/gcp_task_queue.py::GcpTaskQueue.enqueue_slack_event`) under
   `http_adapter.py::_task_name(event_id)`. `AlreadyExists` is Slack's retry and counts as enqueued. The route answers
   200 and does nothing else: Cloud Run throttles CPU once the response is sent.
4. Cloud Tasks calls `/worker`. `WorkerHandler` claims the delivery, recognises a payload with `event` and `session_id`
   and no `task_type` as a Slack event (`src/handlers/worker_handler.py::WorkerHandler._dispatch`) and calls
   `HTTPModeAdapter.handle_queued_event` (wired in `src/composition/application_graph.py`).
5. `handle_queued_event` drops what the processing path ignores (`_ignored_before_lock`: bot messages, messages without
   a user, subtypes other than plain and `file_share`), takes the lock, then `_process_message_event` or
   `_process_mention_event`: `iam_service.authorize("slack", ...)`, a `reject` posts the IAM message and ends the turn;
   otherwise `_run_authorized_turn` resolves the language, builds `SlackResponseChannel` and either dispatches a `$`
   command (`handle_command`) or runs `handle_message`.

### Session lock and busy events

The lock is `HTTPModeAdapter._session_locks`, keyed by the event's `thread_ts`, else its `ts` (not by the chat session
id). It orders turns inside one instance only.

1. A busy lock is waited for `BUSY_WAIT_S`; a redelivery (Cloud Tasks retry count above 0) finds a busy lock and
   re-queues at once.
2. `_requeue_busy` enqueues the same event again, `BUSY_REQUEUE_DELAY_S` later, under a new deterministic name
   (`_task_name(event_id, requeues)`), counting in `busy_requeues`, up to `BUSY_MAX_REQUEUES`. Then
   `_notify_busy_drop` posts `UIMessage.MESSAGE_NOT_PROCESSED_BUSY` and the message is dropped.
3. A turn that passes the long-turn mark releases the lock early: `ConversationHandler._watch_long_turn` calls
   `SlackResponseChannel.on_long_turn`, which fires the `release_lock` hook once
   (`decisions/long_running_turns.md`).
4. `handle_geo_mark_turn` (the `geo_mark_turn` worker task) takes the same lock; a lock still held after `BUSY_WAIT_S`
   answers 503 so the queue retries.

### Button press

An interactive message is posted by `interactive_message_adapter.py::SlackInteractiveMessageAdapter` through
`InteractiveMessagePort`: the binding rides in the actions block's `block_id` (`binding_codec.py::encode`/`decode`, the
one codec for poster and route). A press arrives at `/slack/interactivity`
(`interactivity_route.py::SlackInteractivityRoute.handle`): signature on the raw body, then parse, then enqueue an
`interactive_choice` worker task on the **main** queue (not the Slack intake queue) with a dedup key from the press, and
answer 200. `WorkerHandler._handle_interactive_choice` applies it and `show_choice` re-renders the message with
`chat.update`. The Slack app needs Interactivity enabled with this route as Request URL. Who builds the messages: the
memory review (`memory/README.md` §5.6) and profile questions.

### Literal text

Text the bot did not write goes out through `ResponseChannel.send_literal` (`src/domain/messaging.py`);
`send_reply` is literal too. On Slack `&`, `<`, `>` are escaped (`mrkdwn_text.py::escape_literal`) and `mrkdwn` is
off, so `<!channel>` or `<@U1>` is shown, not obeyed. `UserNotificationService.notify_raw(..., literal=True)` is the
notification path to it. Interactive messages escape the same way before capping to `MAX_TEXT_CHARS`.

## Invariants and traps

- **Verify before parse, on raw bytes.** Both routes use `read_capped_body` and `verify_slack_signature`; the window is
  `MAX_AGE_S` and an empty `SLACK_SIGNING_SECRET` refuses everything (`HTTPModeAdapter.__init__` also refuses to
  start without it). Tests: `tests/unit/adapters/slack/test_http_adapter_route_signature.py`, `tests/unit/adapters/test_slack_interactivity_route.py`.
- **Slack's retry must not make a second turn.** The dedup is the named Cloud Task plus the `/worker` claim; there is no
  Slack-side dedup store. Re-queued copies get their own names (`-r{n}`), so they are not mistaken for the original
  (`tests/unit/adapters/test_intake_release_and_busy_requeue.py`).
- **The lock is per process.** `_session_locks` is a `WeakValueDictionary` of `asyncio.Lock`s; two Cloud Run instances
  do not serialize against each other. Known residuals are listed above `BUSY_CREATE_BACKOFF_S`: a third message
  behind two long turns can exhaust the re-queues; a Cloud Tasks outage during a re-queue loses the message (ERROR
  log); a re-queue that was created after its create call failed can run the turn twice.
- **Do the work before answering 200, or queue it.** Routes only verify and enqueue; anything started after the
  response is throttled or killed.
- **Unauthorized users.** `authorize` runs per processed message; a `reject` posts `decision.message` (the registration
  instruction from `IAMService.get_rejection_message`) and starts no turn. `handle_geo_mark_turn` re-authorizes and
  refuses a user that is not the link's owner.
- **Threads.** `_reply_to_metadata` sets `reply_to_message_id` only for a reply inside a thread, never for a thread's
  own parent message.
- **Bound channels read history from Slack.** `SlackChannelHistorySource.fetch` maps a message to the `model` role when
  it has a `bot_id` or its author is `SLACK_BOT_USER_ID` (an optional knob read with `os.getenv` in
  `SlackAdapterFactory.create_adapter`); `$new`/`$reset` markers cut the history.
- **Slack's own limits drive the code** (§ Slack platform limits): the immediate 200, `MAX_TEXT_CHARS` in the block
  builders, `MAX_BLOCK_ID_CHARS` in the codec, chunking in `SlackResponseChannel.send_long_text`
  (`SLACK_MAX_MESSAGE_LENGTH`, `SLACK_CHUNK_SIZE`).

### Slack platform limits

External facts. Slack expects the event acknowledgement within 3 seconds (hence the immediate 200 and the Cloud Tasks
enqueue), allows about 3000 characters of text per block, and asks bots to stay near one message per second per channel
(status updates are throttled).

## Links

- [`slack_socket_mode_removal.md`](../../04_solution_strategy/decisions/slack_socket_mode_removal.md)
- [`worker_task_dedup.md`](../../04_solution_strategy/decisions/worker_task_dedup.md)
- [`long_running_turns.md`](../../04_solution_strategy/decisions/long_running_turns.md)
- Interactive messages in the memory review: [`memory/README.md`](../memory/README.md) §5.6.
- Sibling: [`telegram_integration/README.md`](../telegram_integration/README.md).

## Where to start reading

- `src/adapters/slack/http_adapter.py::HTTPModeAdapter.handle_queued_event` — the worker side of an event.
- `src/adapters/slack/http_adapter.py::HTTPModeAdapter._handle_slack_event` — the route: verify, enqueue.
- `src/adapters/slack/response_channel.py::SlackResponseChannel` — rendering, chunking, literal text.
- `src/adapters/slack/interactivity_route.py::SlackInteractivityRoute` and
  `src/adapters/slack/interactive_message_adapter.py::SlackInteractiveMessageAdapter` — buttons.
- `src/composition/slack_adapter_factory.py::SlackAdapterFactory.create_adapter` — wiring.
