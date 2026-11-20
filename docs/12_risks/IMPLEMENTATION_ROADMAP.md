<!-- living-doc
covers: none
-->

# Risks and Technical Debt

arc42 §11. The open risks and the open technical-debt items, each checked against the code. The file keeps its
historical name because code, tests and decision records cite its ids (`TD-6`, `TD-3`, ...). An id is never reused;
a cited id that is not listed here is closed, and the decision record or code comment that cites it says why.

An item names what is wrong, where (file and symbol), what would fix it, and the trigger that makes it urgent. When an
item is fixed, delete it; a new item takes the next free id.

---

## Open risks

### SEC-1: No rate limiting on the Cabinet and web APIs [P1]

- No per-user limit on `/api/user/*` and the other Cabinet routes (`src/web/user_cabinet_app.py`). Harmless for one
  user; enumeration and spam become possible once the whitelist admits others.
- Fix: a token bucket per `user_id` on those routes. `src/utils/rate_limiter.py::RateLimiter` is a token bucket, but no
  module imports it.

### SEC-3: Error responses carry exception text [P2]

- No `errorhandler` is registered in `src/web/` or `main.py`, and Cabinet routes return `{"error": str(e)}`, one of
  them as a 500 (`src/web/user_cabinet_app.py`, `unlink_platform`); `src/web/oauth_app.py` does the same with a 400.
  Exception text can name internals.
- Fix: a fixed message for every 5xx (the detail goes to the log) and explicit 401/403/500 handlers in the Quart apps.

### R-1: Every service assumes a single instance

- The main service, the drive indexer and the voice relay all deploy with `--max-instances=1` (`cloudbuild-dev.yaml`).
  Code depends on it: a Cloud Tasks claim of a dead attempt is taken over by the retry, and a long turn that is
  `running` but not live in this process is reported lost (`decisions/worker_task_dedup.md`,
  `decisions/long_running_turns.md`); the relay must receive both SFU legs of a web call in one process.
- Raising the cap is a redesign, not a config change: relay shards, and a liveness signal that is not "this process".

### R-4: Session writes soft-fail by default

- `FirestoreSessionStore.append_message*` (`src/adapters/firestore_session_store.py`) logs a failed write and returns,
  unless the caller passes `raise_on_error=True` (contract: `src/ports/session_store.py`). The chat-turn save
  (`src/handlers/conversation_handler.py`, through `retry_store_write`), the long-turn saves and the memory-confirm,
  fact-review and fact-answer paths pass it. Any other caller's failed write drops that pair from history, and with it
  from consolidation, while the user sees a normal answer.
- Deliberate: an append failure must not break the reply, so a chat-turn save that fails every retry is logged, not
  answered with an error.

### R-5: Many dependencies are unpinned

- `anthropic` and `openai` are pinned (`decisions/openai_cache_write_tokens_location.md`); other lines of
  `requirements.txt` have no pin or only a floor (`slack_bolt`, `google-cloud-firestore`, `aiohttp`, ...). The image
  installs with `pip install -r requirements.txt` (`Dockerfile`), with no lock or constraints file, so every deploy
  takes the newest release and the suite that gates a change can run on different versions than production. Wire
  tests mock at the SDK boundary, so they cannot see the gap.

---

## Open technical debt

### TD-5: `update_note` overwrites a reminder's `instruction` in place [P2]

- `FirestoreAgentNoteAdapter.update_note` (`src/adapters/firestore_agent_note_adapter.py`) is a plain field update;
  `AgentNote` has no version history, so a rewritten `instruction` is lost. The instruction is one string, so any small
  correction replaces all of it.
- It matters because the firing alert (`src/domain/reminder_alert.py::build_reminder_alert`) shows the firing agent the
  reminder's `note_id` and invites it to act on it, including updating it. A long-crafted recurring reminder can
  rewrite itself with no way back.
- Fix, phase 1: snapshot the prior content to an append-only `revisions` subcollection before a content edit; add
  `protected: bool` to `AgentNote` and leave the update/delete invitation out of a protected note's alert. Phase 2:
  refuse content edits to a protected note from an autonomous fire, and show a diff for the user to confirm.

### TD-6: `fetch_url` has no Firestore prompt; the inline prompt is the production path [P2]

- `WebSearchAgent._handle_fetch_url` (`src/agents/web_search_agent.py`) passes `_FALLBACK_FETCH_SYSTEM`
  unconditionally and never calls `build_for_agent()`. Despite its name it is the only prompt `fetch_url` has: an
  exception to the "no fallback prompts" rule, whitelisted in `tests/unit/arch_tech_debt.py`. It has no per-user
  override, and every wording change needs a deploy.
- Fix: author a `fetch_url` prompt profile, call `build_for_agent()`, return `AgentResponse.failure()` when it fails,
  then delete the constant and the whitelist entry.
- Constraint: the prompt serves two request shapes (`query` + URL, and a bare URL). Wording that covers only the first
  regresses bare-URL fetches; tune with `scripts/websearch/tune_fetch_prompt.py`.
- Owner ruling: deferred, wording stays fixed in place for now (`decisions/websearch_per_intent_tier.md`).

### TD-9: `edit_video` is billed at `generate_video`'s rate [P3]

- `src/services/video_generation_delivery.py::deliver_video` prices every delivery with
  `src/domain/billing.py::VIDEO_GENERATE_MODEL`, but `GrokVideoAdapter.edit_video` runs edits on a different model
  (`src/adapters/grok_video_adapter.py::_EDIT_MODEL`) that has no entry in the pricing table. The internal ledger
  charges edits at the generation rate.
- Fix: a `VIDEO_EDIT_MODEL` constant and rate in `src/domain/billing.py` (as `IMAGE_GENERATE_MODEL` /
  `IMAGE_EDIT_MODEL` already are), passed by the delivery for whichever operation ran. Owner ruling: deferred.

### TD-10: Retire `DomainResearcherAgent` [P3]

- The `domain-competency-research` system skill (`src/skills/smart/domain-competency-research/SKILL.md`) does the same
  job in Smart, with web search and memory. The agent is still wired.
- Scope: `src/agents/domain_researcher_agent.py`; `DOMAIN_RESEARCHER` and `Intent.DOMAIN_RESEARCH`
  (`src/infrastructure/agent_manifest.py`); `DomainResearcherAgentConfig` (`src/infrastructure/agent_config.py`); its
  provider strategy (`src/services/agent_context_builder.py`) and tier entry (`src/domain/user.py`); the factory
  builder (`src/composition/user_agent_factory.py`); the Firestore profile and token; the live channel binding (unbind
  first); mentions in `src/agents/CLAUDE.md` and `docs/how_to/NEW_AGENT_PLAYBOOK.md`.
- Tests use `domain_researcher` as the example of a non-companion binding (`grep -rl domain_researcher tests`); a
  reviewer decides each edit.

### TD-11: No async delegation whose result returns to the orchestrator [P3] (needs an RFC)

- ASYNC means "the result goes to the user": `AgentWorkerHandler` (`src/handlers/agent_worker_handler.py`) delivers it
  and nothing hands it back to the caller (`decisions/delegate_mode_parameter_removed.md`). A multi-step task that must
  continue after a slow specialist (use a generated page's link, say) has no mechanism.
- Questions for the RFC: where the pending state lives; how the orchestrator is woken (a new turn, or the long-turn
  machinery); what the user sees meanwhile; the turn clock; what the orchestrator receives (a reference, never
  document content).

### TD-12: `prompt_builder` is optional in most agent constructors [P2]

- Eleven files under `src/agents/` declare `prompt_builder: Optional[PromptBuilderPort] = None` (grep that
  annotation). Production always injects it, but some guard with `if self.prompt_builder:` and run on an empty prompt
  when it is missing (`src/agents/compute_agent.py`, `ComputeAgent.execute`), which hides misconfiguration and
  contradicts the "fail fast on missing prompts" rule.
- Fix per agent: make the parameter required, call it unconditionally, return `AgentResponse.failure()` when the build
  fails. Tests that build these agents without a prompt builder need one; a reviewer decides each edit.

### TD-13: Single-call failover tries one fallback provider [P3]

- `BaseAgent._call_llm` (`src/agents/base_agent.py`) fails a single call over to `ctx.fallback_provider` only; it does
  not walk the agent's allowed providers. Smart's provider rotation (`src/agents/core/smart_response_agent.py`) fires on
  `TranscriptLockedError` but not on a non-locked `BothProvidersUnavailableError`.
- Owner ruling: deferred as "variant 3" of `decisions/cross_provider_execution_retry.md` (wider blast radius, since
  every single-call agent goes through this path).

### TD-14: A specialist failure raises no alert [P1]

- `DelegationEngine` (`src/infrastructure/delegation_engine.py`) turns an `AgentResponse.failure` into an ordinary
  tool result (`Tool note: Specialist agent rejected the request ...`, `failed=True`) with a warning log, so the
  orchestrator apologises or retries. `BaseAgent._on_agent_error` only logs. The coordinator's `alert_sink` is used
  for delegation-loop refusals only, so a system fault (an adapter exception, a 4xx/5xx from a store or provider) is
  invisible to the owner.
- Fix: split the two kinds of failure in the engine. An input error (bad arguments) goes back to the model as today so
  it can correct itself. A system error raises an alert (`AlertSinkPort`, or `UserNotificationService.notify_raw`;
  which one is open) plus a structured log line.

### TD-15: `allowed_intents` is enforced only in the tool declaration, not at dispatch [P2]

- `AgentRegistry.get_available_intents_for` (`src/infrastructure/agent_registry.py`) filters the intents an agent is
  shown by `AgentDescriptor.allowed_intents`, but neither `DelegationEngine.dispatch` nor
  `AgentCoordinator.handle_delegation` checks it when the call arrives. A model that names an intent outside its list
  still runs it. Smart, Quick and Tutor depend on the declaration alone.
- `LelikAgent.delegate` (`src/agents/lelik_agent.py`) already refuses an intent outside `permitted_intents`.
- Fix: reject, in the engine's dispatch, any intent not in the calling descriptor's `allowed_intents` (when set), with
  a tool result that says so.

### TD-16: Archived or deleted Slack channels leave their channel binding behind [P3]

- Nothing in `src/` handles `channel_archive` or `channel_deleted`, so a `ChannelBinding` document (id = channel id,
  made by `$agent <type>`) outlives its channel. `FirestoreChannelBindingAdapter._deserialize_companion_config`
  (`src/adapters/firestore_channel_binding_adapter.py`) raises `KeyError` on a stored `companion_config` that lacks
  `window_threshold` or `batch_size` (the other fields fall back to defaults), a trap for anything that iterates the
  collection.
- Open before building: whether the Slack app receives these events with its current manifest and scopes; delete or
  soft-delete the binding, and whether its companion records stay. A one-time cleanup of existing orphans is a
  separate, smaller task.

### TD-20: `_migrate_ownership_fields` can overwrite `account_id` [P3]

- `FirestoreFactRepository._migrate_ownership_fields` (`src/adapters/firestore_repo.py`) takes the legacy branch
  whenever either of `account_id` / `created_by_user_id` is missing and `owner_id` exists, and then overwrites a stored
  `account_id` with `owner_id`. No stored document is known to hit this.
- Fix: migrate only the missing field. (The write path is guarded separately: `_require_stored_account` checks the
  stored `account_id` inside the transaction.)

### TD-21: The user-facing apology on an agent failure is hard-coded English [P3]

- `AgentFallbackService._APOLOGY_TEXT` (`src/services/agent_fallback_service.py`) is the text sent when every agent
  fails and the fallback runs, whatever the user's language. The code comment calls it a deliberate language-neutral
  last resort.
- Fix, if the owner wants it localized: a `LocalizationPort` key with uk, en, fr and es strings.

### TD-22: `email_indexing_jobs` grows without a cleanup [P3]

- `FirestoreEmailJobRepository` (`src/adapters/firestore_email_job_repo.py`) writes one job document per user per run
  and has no delete path.
- Fix: in `create_job`, delete finished jobs older than a retention window, except the latest (the Cabinet reads it
  through `get_latest_job`).

### TD-23: `SkillService._check_text` puts the full `user_id` in the security-check context [P3]

- The context string `skill_{field}_user_{user_id}` (`src/services/skill_service.py`) carries the whole id into the
  validator's logs, against the rule that logs show a short form of ids.
- Fix: pass a shortened id (as other log lines do) or a skill name.

### TD-25: A PDF whose text layer is one repeated letter never reaches the reader model [P2]

- `garbage_share` (`src/domain/document_extraction.py`, used by `model_route`) counts only mojibake and `(cid:N)`
  patterns, so a text layer with a broken font mapping passes as good text. `is_machine_text`
  (`src/domain/drive_text_gate.py`, called from `DriveIndexingService._read_text`) can then demote the record to
  `metadata_only / machine_text` with no reader attempt.
- Fix: route the case to the reader. Before building, measure the reader's real per-page cost on a few pages: a long
  book may exceed the monthly reader page cap.

### TD-26: Folder-scope index is still built by enumeration, not by walking the chosen folders [P2]

- Step 1 of `docs/10_rfcs/DRIVE_SCOPE_WALK_RFC.md` is built: a scope change is applied as a diff
  (`DriveIndexingService._apply_scope_change`). A full re-enumeration remains for a scope that covers everything, an
  unfinished enumeration or an invalid stored state (`DriveIndexingService._restart_for_scope`).
- Step 2 (a folder scope built by walking, each feed item judged locally against the stored folder set) is not
  built; the RFC holds the design and its status.

### TD-27: Bot-written `[System: ...]` frames reach consolidation in the user role [P2]

- The late answer of a long turn (`src/domain/turn_clock.py::render_late_answer_note`, saved as a user message by
  `src/services/long_turn_service.py`), call events, drive-note receipts (`src/domain/drive_note_receipt.py`) and the
  async-document-ready note (`src/services/user_notification_service.py`) are saved as user-role messages starting
  with `[System: ...`. `src/domain/consolidation_serialization.py::serialize_messages_for_consolidation` sends the
  role and text only, no author.
- To check first: whether the consolidator turns a late answer's content into the owner's facts (not observed). If it
  does, give the `[System:` frames an explicit rule in the consolidation tokens; the constraint is the "bot never
  writes to memory itself" rule in the root `CLAUDE.md`.

### TD-28: History tiering keeps one more full answer than its setting says [P3]

- `BaseAgent._apply_history_tier` (`src/agents/base_agent.py`) keeps `full_text` while
  `model_turns_from_end <= turns`, so a setting of N keeps N+1 full model turns. Cosmetic unless a cost question
  arises.
- Fix: `<` instead of `<=`, with the defaults (`history_recent_full_turns`) re-checked.

### TD-29: Telegram replies drop the forum-topic id [P3]

- `TelegramResponseChannel.send_reply`, `send_literal` and `send_late_answer`
  (`src/adapters/telegram/response_channel.py`) pass no `message_thread_id`, while `send_message`, `send_file` and the
  chunked senders carry `thread_id`. Harmless while the bot's chats have no forum topics.
- Fix: carry `thread_id` through every send path.

### TD-31: Per-user agents built outside the factory each repeat the billing and capture wiring [P3]

- `DriveReaderRunner`, `CompanionExtractorRunner`, `EmailClassifierRunner` and `EmailCardRunner` (`src/composition/`)
  each build the execution context through `AgentContextBuilder`, construct the agent and call
  `bind_runtime(AgentRuntime(...))` themselves, as `UserAgentFactory` does for its agents. A runner that forgets the
  call bills nothing or records nothing, silently (`_flush_billing` is a no-op without the quota service). Only the
  drive Search v2 runners share a helper, `src/composition/drive_agent_build.py::build_drive_agent`.
- `DriveReaderRunner` and `CompanionExtractorRunner` do not enter `request_user_scope` (only `EmailClassifierRunner`
  does), so their rows carry no `user_id` in telemetry.
- Trigger: the next per-user agent that is not a delegation agent, or any change to how billing or capture is attached
  to an agent.
- Fix: one composition helper that builds a per-user agent with billing, capture and the user scope, used by all
  runners and the factory.
