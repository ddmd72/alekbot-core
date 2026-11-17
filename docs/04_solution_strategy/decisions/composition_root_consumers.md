# Decision: consumers of the composition root are built whole, from one bundle per subsystem

**Status:** Adopted (2026-11-17). Continues [application_graph_builder.md](application_graph_builder.md), which put the whole
graph in one builder; this record covers what the builder's consumers (handlers, adapter factories, the notification path)
look like. Owner ruling in the same session: Telegram stays all-or-nothing.

## Context

After the builder moved out of `main.py`, the graph still had late wiring: `attach_*` / `register_*` calls that completed an
object after construction, a `/worker` handler with 31 constructor parameters and `_handle_*` methods that each
owned a different feature's dependencies, one language service that both read and wrote, and the service URL and queue
names derived in several modules. A reader could not tell from a constructor what an object needed, and a missing late
call failed only at run time.

## Decision

1. **Option B: a bundle per subsystem, nothing attached after construction.** A subsystem gets one `*_setup.py` returning
   one frozen bundle, and a service is built where its bundle is built. The exceptions are named in
   `tests/unit/architecture/late_wiring_calls_allowlist.txt` with a `permanent` reason (Registry pattern, the agent-factory
   cycle, the Telegram binding below). The consumers take their dependencies at
   construction, and `build_application_graph` is a sequence of thirteen bundle builds (`boot_setup`, `identity_setup`,
   `operations_setup`, `session_routing_setup`, `file_delivery_setup`, `language_setup`, `notification_setup`,
   `companion_setup`, `memory_search_setup`, `agents_setup`, `integrations_setup`, `memory_setup`,
   `background_jobs_setup`) beside `DocumentBackends`; a bundle a builder takes is a bundle built before it, and each
   module's docstring states its ordering constraint. Five private helpers stay in `application_graph.py` on purpose
   (`_build_coordinator`, `_build_generation_registries`, `_build_slack`, `_build_worker_handler`, `_build_telegram`):
   registry fills and the permanent late bindings are allowlisted by path, and tests patch those names there.
   `coordinator.set_agent_factory` stays in the top-level builder, the one real cycle. `ServiceContainer` builds the
   document service from `DocumentBackends` itself.
   Agent services are typed: `AgentServices` (`composition/agent_services.py`) is a frozen bundle passed to
   `UserAgentFactory` as `services=`, and `BaseAgent.bind_runtime(AgentRuntime)` binds the coordinator, the prompt-content
   store and the quota service before registration; nothing in composition writes an agent's privates.
   The bio-cache cycle is untangled: the refresh is `BiographicalContextService.refresh_cache` (port
   `BiographicalCacheRefresher`), built over the repository and taken at construction by every fact-write path (the
   consolidation agent through `AgentServices`, the janitor, the owner's answer, the Cabinet, the memory review, the
   profile step); the repository only saves the cache document and holds no service. To take it without growing, the
   profile step groups its run settings in `FactProfileRunLimits` and the review the four ports it reaches the owner
   through in `FactReviewOwnerChannel`.
2. **`/worker` is a registry of runners.** `WorkerHandler` dispatches through a task registry (`WorkerTask`, a Protocol in
   `domain/worker_task.py`, so handlers still import no `ports/`). The six logic-heavy tasks are runner services in
   `src/services/*_task.py`, built by `composition/worker_setup.py::build_worker_tasks`, each holding only its own
   dependencies: reminders, video polling, deep research failed and webhook, daily email review (start and run), billing
   summary. The handler keeps the light tasks (a call to one service). Handler arity fell from 31 to 19, and the Slack
   worker is passed at construction.
3. **`DisabledTask` only where the honest outcome is a refusal.** When a feature's dependencies are absent, the registry
   holds a Null Object that answers the refusal the task gave before (status and body unchanged), so the handler has no
   `None` guards. It is never a fake success, and a task whose absence should be silent does not get one.
4. **One chat-services bundle.** `ChatServices` and `build_conversation_handler` (`composition/chat_services.py`) build the
   `ConversationHandler` of both platforms; what differs (Slack's channel binding and history source, each platform's rich
   content) is an explicit argument. `PlatformKit` (`composition/platform_kit.py`) carries a platform's media port, rich
   content and channel maker; Slack's kit is handed to the notification service and the channel factory at construction.
5. **Telegram is the one late binding: `register_late_platform`.** Telegram's chat handler and the agent factory take the
   notification service, so the service exists before the Telegram adapter, and the adapter's kit cannot be a constructor
   argument of the service. The same call binds the Telegram queued-update entry on the worker. The call is a
   `permanent` allowlist line.
6. **Language is split by command and query.** `LanguageResolver` reads (UI language, stored preference) and implements
   `LanguageServicePort`; `LanguagePreferenceService` writes and takes the resolver. Readers (platform adapters, the
   notification service, the agent factory, the drive mirror) depend on nothing that notifies or refreshes agents.
7. **`DeployTopology` is the one source of deploy literals.** `composition/deploy_topology.py` derives the service URL, the
   Cloud Tasks queue suffix and the regions once; everything else receives the value. `CLOUD_RUN_SERVICE_URL` is read
   there only, and a ratchet test keeps the literals out of other modules.
8. **Ratchets only shrink.** The AST guards in `tests/unit/architecture/` (constructor arity, late wiring, foreign private
   writes, unread injected dependencies, composition function size, deploy literals) list today's offenders in
   allowlists. A new offender fails, and so does a line whose offence is gone or whose count fell, so the lists are lowered in
   the change that removes the offence.

A small shared service, `BackgroundEmptyAnswerReporter`, serves the empty-answer alert of the reminder, daily-review and
deep-research runners, so that code is not copied between runners.

## Alternatives rejected

- **Option A, leave the late calls and add a lint:** the order of calls stays an invisible contract; a lint can only count
  them.
- **Option C, B plus dissolving `ServiceContainer` and splitting `ConversationHandler` by command family:** deferred, not
  rejected — about twice B's cost and real regression risk in the agent factory's per-user cache; revisit once B has been
  live and only if `ConversationHandler` or the agent factory is still what new code copies. A DI container stays out
  (manual DI in `composition/`).
- **Runner methods on `WorkerHandler`, split into mixins:** the arity and the mixed dependencies stay; only the file
  shrinks.
- **A `DisabledTask` for every absent dependency:** a silent success would hide a misconfigured deploy. Only a refusal the
  caller already received is preserved.
- **Telegram "outbound survives":** hand Telegram's kit to the notification service at construction, so outbound
  notifications keep working even when the Telegram adapter then fails to build. The owner rejected it: Telegram is all or
  nothing, so a half-built platform is never served.
- **Telegram built at construction like Slack:** needs the notification service inside the kit it is built from, a real
  cycle; the named exception is cheaper than a proxy object.
- **One language service with a read-only interface:** the writer still pulls in the notifier and the refresher, which was
  the reason readers needed late wiring.

## Consequences

A constructor lists what an object needs and a missing argument fails at import or build, not on a request. A new
`/worker` task is a runner plus one registry line, with a row in `docs/07_deployment/SCHEDULERS.md`. The cost: more small
classes and one bundle per subsystem to read; the builder fits the composition function-size ceiling.

## Triggers to revisit

- A second platform needs notification-service access at its construction (the Telegram exception would become a pattern:
  then extract the notification path from the chat handler's dependencies).
- `register_late_platform` gains a second caller, or Telegram stops being all-or-nothing.
- A runner needs a dependency another runner already holds in a different shape (then a shared service, not a copy).
- A `DisabledTask` is wanted whose honest outcome is not a refusal.
- `/worker` moves into a blueprint (see the application-graph record).
