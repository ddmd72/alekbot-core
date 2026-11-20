<!-- living-doc
covers:
  src/handlers/conversation_handler.py
  src/handlers/command_services.py
  src/ports/conversation_handler_port.py
-->

# 06 Runtime View

How work moves through the system at runtime: the flows that cross files, layers or processes. Each step names its
entry point. Detail lives with the owner: building blocks under `docs/05_building_blocks/`, RFCs, decision records.
HTTP endpoints: [API_REFERENCE.md](./API_REFERENCE.md).

## 1. A chat turn (Slack / Telegram)

1. **Intake.** The platform adapter verifies the request (`src/adapters/slack/http_adapter.py::HTTPModeAdapter._handle_slack_event`,
   `src/adapters/telegram/webhook_adapter.py::TelegramWebhookAdapter._handle_telegram_update`) and hands the event to
   Cloud Tasks as a **named** task (`src/adapters/gcp_task_queue.py::GcpTaskQueue.enqueue_slack_event`, `enqueue_worker_task`).
   The name derives from the platform's event or update id, so a platform retry meets `ALREADY_EXISTS` and makes no
   second turn. The adapter answers 200 without running the turn (Telegram runs inline only with no queue wired): Cloud Run throttles CPU once a response is sent.
2. **`/worker`.** `src/handlers/worker_handler.py::WorkerHandler.handle` claims a Cloud Tasks delivery by task name (Scheduler and manual calls carry none and are not claimed)
   (`src/ports/task_claim_store.py::TaskClaimStore`; a store error runs the task anyway: a duplicate beats a lost task),
   then dispatches by `task_type`. A queued Slack event has no `task_type`; it goes to
   `HTTPModeAdapter.handle_queued_event`, a Telegram update to `TelegramWebhookAdapter.handle_queued_update`.
   Detail: `decisions/worker_task_dedup.md`.
3. **Per-session serialisation (Slack only).** `handle_queued_event` holds one in-process lock per queued-event `session_id` (the thread ts, else the message ts, else the channel), not per chat session. A turn behind a busy
   session waits `BUSY_WAIT_S`, then is re-queued under a new task name (`BUSY_MAX_REQUEUES` times), then the user is
   told the message was not processed. The lock is released at the long-turn mark, so a later message can run beside a
   long turn.
4. **Authorization.** The adapter authorizes the sender against the whitelist (`src/services/iam_service.py::IAMService.authorize`),
   builds a `MessageContext` and calls the handler through `src/ports/conversation_handler_port.py::ConversationHandlerPort`.
5. **Commands.** The adapter sends a Telegram message or a Slack `message` event that starts with `$` (not an `app_mention`) to `ConversationHandler.handle_command`
   (`src/handlers/conversation_handler.py::ConversationHandler.handle_command`), which answers without an LLM turn.
   The services behind these commands arrive as one optional bundle,
   `src/handlers/command_services.py::CommandServices`; a command whose service is absent answers "unavailable".
6. **Message preparation** (`ConversationHandler.handle_message`). Voice notes are transcribed and enter the turn as the
   user's own text. Other attachments become reference parts through
   `src/services/file_conversion_service.py::FileConversionService.process_attachment`; a download that fails leaves a
   note in the slot, so the agent can tell "no file" from "file lost". The handler then runs inside a `RequestContext`
   (user and account) and calls `src/composition/user_agent_factory.py::UserAgentFactory.ensure_agents_for_user`.
   A channel bound to an agent (`ConversationHandler._resolve_session_mode`) skips the Router: the message goes
   straight to the bound intent, and most bound channels write no session.
7. **Router.** `src/agents/core/router_agent.py::RouterAgent.execute` runs LLM triage (rule-based classification if the
   LLM call fails) beside one memory prefetch: query slots, then a scored fact search outside the biographical
   baseline, skipped when triage says `search_intent=none`
   (`decisions/router_prefetch_once_per_message.md`; mechanics `docs/05_building_blocks/memory/README.md` §4.4).
   It adds the user's active agent notes (self-reminders) and always routes to Smart
   (`RouterAgent._apply_routing_rules`).
8. **Smart.** `src/agents/core/smart_response_agent.py::SmartResponseAgent` runs its tool loop in
   `src/infrastructure/delegation_engine.py::DelegationEngine`. `delegate_to_specialist` goes through
   `DelegationEngine.dispatch` to `src/infrastructure/agent_coordinator.py::AgentCoordinator.handle_delegation`, which
   resolves intent to agent from the manifest (`src/infrastructure/agent_manifest.py`; the caller cannot choose the
   mode, `decisions/delegate_mode_parameter_removed.md`) and refuses a cycle or an over-deep chain. A **SYNC** intent
   runs inline and returns its result. An **ASYNC** intent is enqueued as an `agent_execution` task and returns an
   acknowledgement; `src/handlers/agent_worker_handler.py::AgentWorkerHandler.handle_task` runs it later and delivers
   through `UserNotificationService` to the channel that asked. Local tools are built by Smart and served by the engine itself. Roster and traps: `src/agents/CLAUDE.md`.
9. **Delivery and history.** The handler sends the answer and its delivery items (files, links, rich content), then
   appends the user/model pair to the session `{user_id}:{channel_id}` (`ConversationHandler._save_history_with_retry`).
   A failed history save is logged, never turned into an error reply: the answer is already delivered.

**Long turns.** A turn has one clock, `src/domain/turn_clock.py::LONG_TURN_BUDGET_S`. At `NOTICE_AFTER_S` without an
answer the handler (`ConversationHandler._watch_long_turn`) posts a status note and frees the session; the run goes on
as a child task and `ConversationHandler._finish_long_turn` posts the late answer with a link to the original message.
A failed Smart turn falls back to Quick only before that mark (`ConversationHandler._route_with_fallback` →
`src/services/agent_fallback_service.py::AgentFallbackService.try_quick_fallback`); after it the long-turn path owns the
outcome. Detail: `decisions/long_running_turns.md`, `docs/10_rfcs/LONG_RUNNING_TURNS_RFC.md`.

## 2. Memory formation

The session is a sliding window. On overflow `src/adapters/firestore_session_store.py::FirestoreSessionStore` calls its
overflow callback, `src/services/overflow_routing_service.py::OverflowRoutingService`, which stores the oldest batch in
the consolidation queue and enqueues a `consolidation` task (a companion channel goes to its own extraction queue
instead). On `/worker`, `WorkerHandler._handle_consolidation` runs
`src/services/consolidation_service.py::ConsolidationService.process_user_batches` for one batch under the account's
memory-write lease, then re-enqueues itself while batches remain. The bot never writes memory itself: only the
consolidator turns user-role messages into facts. Steps, thresholds, failure modes:
`docs/05_building_blocks/memory/README.md` §3.

## 3. Background work

Everything that is not a chat turn arrives as `POST /worker` (Cloud Tasks or Cloud Scheduler, OIDC-verified by
`src/web/worker_oidc_verifier.py`) and is dispatched by `WorkerHandler` on `task_type`: its own handlers for the light
types, and one runner per heavier type (`src/domain/worker_task.py::WorkerTask`, built by
`src/composition/worker_setup.py::build_worker_tasks`). Task types and schedules: `docs/07_deployment/SCHEDULERS.md`.
Proactive messages (reminders, daily email review, job results) go out through
`src/services/user_notification_service.py::UserNotificationService`, to the origin channel of an async task if it names one, else the primary channel, else the last active one.

Three runtime units run outside the main service:

| Unit | Entry point | Does |
|------|-------------|------|
| Drive indexer | `indexer_main.py` | `drive_sync` ticks for the user drive index (`docs/05_building_blocks/user_drive/README.md`) |
| Deep research job | `job_main.py` | Claude deep research as a Cloud Run Job; it delivers its links itself and the HTML report comes back as an `agent_execution` task (`decisions/cloud_tasks_vs_jobs.md`) |
| Voice relay | `relay_main.py` | Live call audio, section 4 |

## 4. A voice call (Lelik)

The call setup, the relay and the main service are three processes (`docs/10_rfcs/VOICE_COMPANION_RFC.md`,
`VOICE_WEB_TRANSPORT_RFC.md`):

1. **Dial.** A phone call is authenticated by `src/web/voice_webhook_app.py::create_voice_webhook_blueprint` (`/voice/auth`:
   signed Twilio request, caller resolved by bound phone number); a web call starts in
   `src/web/voice_web_call_app.py::create_voice_web_call_blueprint`. Both mint a short-lived single-use call ticket and
   take a one-call-per-user marker.
2. **Relay.** `relay_main.py` serves the audio sockets: Twilio Media Streams
   (`src/handlers/media_stream_handler.py::MediaStreamHandler`), Cloudflare SFU (`src/handlers/sfu_stream_handler.py::SfuStreamHandler`)
   and the page's control socket (`src/handlers/call_control_handler.py::CallControlHandler`). Both audio legs of a call
   must land on one process, so the relay runs one instance (`decisions/voice_web_transport_cloudflare_sfu.md`).
   `src/services/voice_session_service.py::VoiceSessionService.handle_call` drives the realtime provider.
3. **Control plane.** The relay calls back into the main service, OIDC-protected like `/worker`
   (`src/web/voice_control_plane_app.py::create_voice_control_plane_blueprint`): `/voice/session-config` consumes the
   ticket atomically and returns the session config; `/voice/delegate` runs a specialist call through
   `LelikAgent.delegate` (`src/agents/lelik_agent.py`), the same `DelegationEngine` path as a chat turn, and an answer the
   relay stopped waiting for goes to chat once; `/voice/submit-transcript` records usage and cost, hands the transcript
   to the summary consumer, and releases the one-call marker even if usage or summary fails (only while it still names this call; the summary is skipped for voicemail and some modes).

## 5. Failure handling

- **Agents:** `src/agents/base_agent.py::BaseAgent` wraps every execution in a per-agent circuit breaker and timeout
  (`src/domain/agent.py::AgentConfig`).
  A timeout is not retried: the same budget would fail again.
- **Providers:** same-provider retry, then cross-provider failover (`docs/05_building_blocks/provider_resolution/README.md`).
- **The user always gets a displayable message:** a failed primary turn falls back to Quick; if Quick fails too,
  `AgentFallbackService.try_quick_fallback` returns a synthetic apology.
- **Observability:** Cloud Logging for events, Logfire for the delegation cascade, BigQuery `prompt_content` for LLM
  content (`docs/05_building_blocks/observability_strategy/README.md`; the debugging protocol is in the root `CLAUDE.md`).
