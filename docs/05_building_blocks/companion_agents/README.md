<!-- living-doc
covers:
  src/domain/channel_binding.py
  src/ports/channel_binding_port.py
  src/adapters/firestore_channel_binding_adapter.py
  src/services/channel_binding_service.py
  src/domain/session_mode.py
  src/domain/companion*.py
  src/ports/companion_*.py
  src/adapters/firestore_companion_*.py
  src/services/companion_*.py
  src/composition/companion_*.py
  src/agents/tutor_agent.py
  src/agents/tutor_extractor_agent.py
  src/agents/domain_researcher_agent.py
-->
# Companion Agents and Bound Channels

## Purpose

This block lets a chat channel be handed to one agent instead of the orchestrator, and gives some of those agents a
memory of their own. Two ideas, one stacked on the other.

A **channel binding** says "every message in this channel goes straight to agent X". It bypasses the Router, so Alek is
not directly reachable there; inside the channel the agent reaches him only by delegation. A bound agent without more
(today `domain_researcher`) is stateless: its history is read from the platform's own channel history and nothing is
written to the owner's session store or memory.

A **companion** is a bound agent whose binding also carries a `CompanionConfig`. Its channel becomes a *session* with a
memory of its own: the turns are stored, a sliding window hands overflow to a companion extractor, and what the extractor
keeps (records about the interaction, a rolling summary) is read back into the agent's prompt. Today the only companion
is the text language tutor.

The point of the split is that the owner's facts are a biography and a companion's records are about a conversation: a
tutor should remember recurring errors and coverage, never the drills, and never write into the biography. So companion
memory is a second subsystem next to the owner's, keyed by **session** (the channel), not by user, and sharing nothing
with the fact store except the embedding port and the RRF fusion function.

What it is not: it does not read or write the owner's facts (the tutor's read side is closed, see Invariants), it is not
the voice companion (Lelik, see [voice](../../10_rfcs/VOICE_COMPANION_RFC.md)), and it is not the email or drive
pipeline.

## Owned data

All Firestore, `us-production`, environment prefix on collection names.

**Channel bindings** — collection `{firestore_collection_prefix}channel_bindings` (no `EnvironmentConfig` property),
document id `channel_id`. Fields `channel_id`, `agent_type`, `intent` (the descriptor's first capability), `created_by`
(the user who ran `$agent`), and `companion_config` (null for a stateless bound agent). Writer:
`ChannelBindingService.bind` / `unbind`; reader: every incoming message, through the service's 5-minute cache.
`companion_config` holds the window sizing (`window_threshold`, `batch_size`), `history_recent_full_turns`, `text_mode`
(`summary` or `full`: which text of a model turn goes into an extraction batch) and four read toggles
(`include_own_records`, `include_biographical`, `session_domains`, `include_standing_directives`). Only the two sizes are
required on read; every other field falls back to its dataclass default, so a hand-written document loads.

**Companion records** — `EnvironmentConfig.companion_records_collection`, one document per record, id
`{batch_id}-{index}`. `session_id` (platform-qualified channel key, `{platform}:{channel_id}`) is the retrieval key;
`account_id` (billing and tenancy anchor) is filtered together with it; `created_by_user_id` is attribution; `text`,
`vector`, `tags`, and `domain`, a value defined by each companion type (the tutor's are `grammar_error`,
`vocabulary_gap`, `topic_covered`, `progress_note`). No SCD2 fields: records accumulate, they never supersede.
Writer: `CompanionExtractionService`; reader: `CompanionContextAssemblerService` (vector search, session and account
pre-filter; needs the declared vector index in `config/firestore.indexes.json`).

**Context cache** — `EnvironmentConfig.companion_context_cache_collection`, id `session_id`, one `summary`. Written by
`save_summary` as a full replace of the **last** batch's summary; the extractor is never given the prior summary, so
this is a rolling view of the latest batch, not a cumulative one. Read by the assembler.

**Extraction queue** — `EnvironmentConfig.companion_extraction_queue_collection`, id `batch_id`
(`cbatch_` + the overflow's hand-off key). A `CompanionExtractionBatch` holds the serialized messages, `companion_type`
(the binding's `agent_type`), `status` (`BatchStatus`), `attempts`, `last_error`. Written by the overflow router; read,
retried and deleted by the extraction service. Composite index (session_id, status, created_at) must be READY before the
code that queries it ships.

**The session itself** — a companion channel's history lives in the ordinary session store under
`{platform}:{channel_id}`, not under Alek's `{user_id}:{channel_id}`. Its document's `owner_id` is whoever wrote first.

## Flows

### Bind and unbind

1. `$agent <type>` is handled in `src/handlers/conversation_handler.py::ConversationHandler._handle_agent_command`. Any
   registered agent with capabilities is bindable (the `internal` flag hides an agent from orchestrators, not from
   binding). `$agent off` unbinds; `$agent` shows the binding.
2. The binding is built with `companion_config=descriptor.companion_default_config`
   (`src/infrastructure/agent_registry.py::AgentDescriptor`). Only the `TUTOR` descriptor in
   `src/infrastructure/agent_manifest.py` sets it, so **binding to `tutor` is what switches companion memory on**; there
   is no other way to give a channel a `companion_config` short of editing the Firestore document.
3. `ChannelBindingService.bind` saves through `FirestoreChannelBindingAdapter.save` and updates its cache; bind, unbind
   and `invalidate` advance a per-channel version so a read that raced a bind cannot write the stale value back.
4. A bound channel can never be the primary notification channel (`_handle_primary_command`) and takes no geo marks.

### A companion turn

1. `ConversationHandler._handle_message` loads the binding and calls `_resolve_session_mode`
   (`src/domain/session_mode.py::SessionMode`). Three outcomes: no binding is the default Router flow; a binding without
   `companion_config` is direct delegation with `history_source="platform"` and no session write; a binding with it is
   direct delegation with `history_source="session_store"`, `write_session=True`,
   `write_session_id="{platform}:{channel_id}"`, flat (unthreaded) replies, and `history_recent_full_turns` taken from
   the binding.
2. History is loaded from the session store under `write_session_id`, then
   `coordinator.handle_delegation(intent=mode.route_intent, calling_agent_id="bound_channel")` runs the agent directly.
3. `TutorAgent._converse` builds its prompt (biography and directives off), asks
   `_build_companion_context_block` for the companion context, applies `_apply_history_tier` with the per-message
   `history_recent_full_turns`, and runs its tool loop (`DelegationEngine`) with `search_web` as its only intent. Its
   reply is JSON (`full_response`, `response_summary`); a missing summary is produced by an async
   `HistorySummaryService` fallback.
4. `CompanionContextAssemblerService.assemble_context` fetches in parallel the cached summary and this session's own
   records (one batch embedding for the query phrases, `find_nearest` per phrase, `apply_rrf_ranking` merge). Each
   source fails soft: an error is logged and that part is empty, the turn still answers.
5. The turn is saved to the session store under `mode.write_session_id or context.session_id`
   (`_save_history_with_retry`); a failed save is logged, never an error reply.

### Overflow and extraction

1. `FirestoreSessionStore.append_messages_batch` asks `CompanionWindowResolver.resolve` (wired in
   `src/composition/session_routing_setup.py::build_session_routing`) for the channel's `(window_threshold, batch_size)`;
   an unbound channel, a binding without `companion_config`, or a lookup error returns None and the store's own defaults
   apply.
2. When the window overflows the store hands the batch to `OverflowRoutingService.route_overflow`. It takes the
   channel id from the part of `session_id` after the first `:`, looks up the binding (a lookup failure fails **open**,
   to Alek's path), and routes by `binding.companion_config`: set goes to `_route_to_companion`, otherwise to Alek's
   consolidation queue.
3. `_route_to_companion` serializes with the binding's `text_mode`, writes a `CompanionExtractionBatch` (id
   `cbatch_{handoff_key}`, create-if-absent so a repeated hand-off keeps the first), and enqueues the
   `companion_consolidation` task.
4. `WorkerHandler._handle_companion_consolidation` runs `CompanionExtractionService.process_session_batches` for one
   batch and re-enqueues itself while more remain. The hourly `sweep_companion_consolidation` task re-triggers every
   session that still has any batch in the queue (`docs/07_deployment/SCHEDULERS.md`).
5. For each batch the service marks it PROCESSING, calls the port `CompanionExtractorPort.extract`, whose only
   implementation is `src/composition/companion_extractor_runner.py::CompanionExtractorRunner`. The runner builds the
   extractor agent fresh per batch (`TutorExtractorAgent` for `tutor`), binds the runtime so billing and prompt capture
   work, and runs it directly, bypassing the coordinator. The agent returns judgment only (`records`, `summary`);
   persistence is the service's job.
6. The service drops records without `text` or `domain`, embeds the rest in one billed batch, saves them with
   deterministic ids, replaces the cached summary, and deletes the batch. A failure counts an attempt: under three it
   becomes RETRY_PENDING, at three FAILED with an error log line `EXTRACTION_FAILED`.

## Invariants and traps

- **Two identity models, two session-id shapes.** Alek's sessions are `{user_id}:{channel_id}`, a companion's are
  `{platform}:{channel_id}`. Both are split on the first `:` to recover the channel id, which is why routing keys on the
  channel's *binding at overflow time*, not on the id's shape. Binding a channel that already has a `{user}:{channel}`
  session sends that old session's next overflow to the companion queue.
  Pinned by `tests/unit/services/test_overflow_routing_service.py` and
  `tests/unit/handlers/test_conversation_handler_companion_write_session.py`.
- **The tutor's read side is closed, in code, not by configuration.** `TutorAgent._build_companion_context_block` passes
  `include_biographical=False`, `include_standing_directives=False` and no `user_id`; the assembler fails closed
  without a `user_id`. The tutor's tool loop allows `search_web` only: `search_memory` would reach the owner's whole
  biography around that boundary. `tests/unit/infrastructure/test_registry_internal_allowlist.py` pins that no allowlist
  names an internal intent by accident.
- **`CompanionConfig`'s read toggles and `session_domains` are stored but not consumed.** Nothing outside the assembler
  and the adapter reads them; the tutor does not pass them on. Changing them on a binding changes nothing today. The
  fields that do act: `window_threshold`, `batch_size`, `text_mode` (overflow router), `history_recent_full_turns`
  (session mode).
- **`history_recent_full_turns` is read per message from the current binding**, never from the agent's constructor: one
  `TutorAgent` per user serves every channel that user bound, so a stored value would be wrong for the second channel.
- **`batch_size` must stay below `window_threshold`**, else every overflow empties the window.
- **`SessionMode.write_consolidation` has no reader.** Alek's consolidation stays out of companion sessions because the
  overflow router sends the batch elsewhere, not because of that flag.
- **A re-bind overwrites.** `bind` is create-or-overwrite; there is no refusal of a second binding and no check of who
  binds. Unbinding leaves the companion records, cache and queue in place.
- **Extraction retries are unbounded.** `reset_recoverable_batches` revives every FAILED batch on each call with
  `attempts` reset (the port docstring records this as the open item P15-D5), so a batch that always fails is retried
  at every sweep until someone removes it.
- **The summary cache holds only the last batch.** Do not read it as the whole session.
- **Records ids are deterministic**, so a retry overwrites instead of duplicating.
- **Cycle guard.** A companion that delegates can be called back by what it called; `AgentCoordinator` refuses
  re-entering an agent in `context["_call_chain"]` and caps the depth (`MAX_DELEGATION_DEPTH`), alerting on refusal. An
  agent that rebuilds its context instead of forwarding `message.context` silently resets the chain.
- **`own_records` ride in the static prompt block** with the summary, ahead of the cache boundary, although they change
  every turn: a known, accepted cost to prompt-cache hits (noted in `TutorAgent._build_companion_context_block`).
- **Tutor defaults.** Tier BALANCED, OpenAI, set in `_DEFAULT_AGENT_TIERS` and `agent_context_builder.py` (the extractor
  is PERFORMANCE, Claude); `TutorExtractorAgent` is not manifest-registered. The extractor runner also serves the voice
  companion's summarizer types, which are another block's concern.
- **Adding a companion type** means: a descriptor with `companion_default_config`, an entry in both `_EXTRACTORS` and
  `_TIMEOUTS_MS` of the runner, and its own extractor. The sizes of the tutor do not transfer to a type with very
  different turn lengths.

## Links

- [`COMPANION_AGENTS_RFC.md`](../../10_rfcs/COMPANION_AGENTS_RFC.md) — why two memory subsystems, the session as an
  identity, shared infrastructure versus per-type extractor (a point-in-time record, the code below wins).
- [`BOUND_THREAD_AGENTS_RFC.md`](../../10_rfcs/BOUND_THREAD_AGENTS_RFC.md) — channel binding, `SessionMode`,
  primary and origin channels. Phases 3 to 5 (`$route`, incognito, `allowed_direct_agents`) are not built.
- [`companion_write_path.md`](../../04_solution_strategy/decisions/companion_write_path.md) — how the write side turns
  on: session-mode split, overflow routing, auto-attached default config.
- [`companion_history_recent_full_turns_per_channel.md`](../../04_solution_strategy/decisions/companion_history_recent_full_turns_per_channel.md)
  — why the history depth lives on the binding.
- [`allowlist_names_internal_intent.md`](../../04_solution_strategy/decisions/allowlist_names_internal_intent.md) — an
  explicit allowlist may name an internal intent.
- [`delegation_cycle_guard.md`](../../04_solution_strategy/decisions/delegation_cycle_guard.md) — the call chain and
  depth cap.
- [Agent Registry](../agent_registry/README.md) for descriptors and delegation; [memory](../memory/README.md) for the
  owner's side that this block deliberately does not touch.

## Where to start reading

- `src/handlers/conversation_handler.py::ConversationHandler._resolve_session_mode` — the three modes of a message.
- `src/handlers/conversation_handler.py::ConversationHandler._handle_agent_command` — how a channel gets bound.
- `src/services/overflow_routing_service.py::OverflowRoutingService.route_overflow` — Alek or companion.
- `src/services/companion_extraction_service.py::CompanionExtractionService.process_session_batches` — the write side.
- `src/services/companion_context_assembler_service.py::CompanionContextAssemblerService.assemble_context` — the read side.
- `src/agents/tutor_agent.py::TutorAgent` — the one companion.
- `src/composition/companion_setup.py::build_companions` — how it is wired.
