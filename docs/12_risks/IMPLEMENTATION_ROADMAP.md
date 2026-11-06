# Risks and Technical Debt

arc42 §11. The open risks and the open technical-debt items that still hold, each checked against the code.
The file keeps its historical name because code, tests and decision records cite its ids (`TD-6`, `TD-3`, …).
An id is never reused; a closed item keeps one line in [Closed items](#closed-items) so old references still resolve.

When an item closes, move it to the closed list with a pointer to the decision record that closed it. A new item gets
the next free id and says what is wrong, where (file and function, not line numbers), and what the fix is.

---

## Open risks

### SEC-1: No rate limiting on the Cabinet and web APIs [P1]

- No per-user limit on `/api/user/*` and the other Cabinet routes (`src/web/user_cabinet_app.py`). Harmless for one
  user; enumeration and spam become possible once the whitelist admits others.
- Fix: a token bucket per `user_id` on those routes. `src/utils/rate_limiter.py` holds a token bucket, but no code
  uses it today.

### SEC-2: `/auth/refresh` trusts the refresh token's payload [P2]

- `refresh_token_endpoint` (`src/web/oauth_app.py`) builds the new access token from the token's `sub` and
  `account_id` without loading the user (the code carries a TODO for it). A deleted user, or an email removed from
  the whitelist, keeps Cabinet access until the refresh token expires (`REFRESH_TOKEN_TTL`, 24 h by default;
  see `decisions/cabinet_session_24h_cap.md` and `decisions/cabinet_oauth_whitelist_gate.md`).
- Fix: load the user and re-check the whitelist on refresh; refuse if either fails.

### SEC-3: Error responses carry exception text [P2]

- No app-level `errorhandler` is registered, and several Cabinet routes return `{"error": str(e)}`, one of them as a
  500 (`src/web/user_cabinet_app.py`). Exception text can name internals.
- Fix: a fixed message for every 5xx (the detail goes to the log), explicit handlers for 401/403/500 in
  `user_cabinet_app.py`, `oauth_app.py` and `main.py`.

### R-1: Every service assumes a single instance

- The main service, the drive indexer and the voice relay all deploy with `--max-instances=1` (`cloudbuild-dev.yaml`).
  Code depends on it: a Cloud Tasks claim of a dead attempt is taken over by the retry, and a long turn that is
  `running` but not live in this process is reported lost (`decisions/worker_task_dedup.md`,
  `decisions/long_running_turns.md`); the relay must receive both SFU legs of a web call in one process.
- Raising the cap is a redesign, not a config change: relay shards, and a liveness signal that is not "this process".

### R-2: The model-written DOCX script still runs in the main service

- Closed: the DOCX script runs only in the untrusted-content render sandbox; the main image has no Node and no
  in-process path. See
  `docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md` and `decisions/sandbox_render_service.md`.

### R-3: The drive parser can reach the metadata server and the network (closed)

- Closed: the parser runs only in the sandbox light service (zero-role identity, no egress, a separate uid per
  request); no credentialed runtime parses hostile bytes. See `decisions/drive_parser_sandbox_resource_isolation.md`.

### R-4: Session writes soft-fail by default

- `FirestoreSessionStore.append_message*` logs a failed write and returns, unless the caller passes
  `raise_on_error=True` (only `MemoryFactService.confirm` does). A failed write drops that turn from history, and with
  it from consolidation, while the user sees a normal answer. Deliberate (an append failure must not break the reply),
  but invisible to the caller.

### R-5: Most dependencies are unpinned

- `anthropic` and `openai` are pinned (`decisions/openai_cache_write_tokens_location.md`); most other lines of
  `requirements.txt` have no pin or only a floor (`slack_bolt`, `google-cloud-firestore`, `aiohttp`, `quart`, …).
  Every deploy installs the newest release, so the suite that gates a change can run on different versions than
  production. Wire tests mock at the SDK boundary, so they cannot see the gap.

---

## Open technical debt

### TD-5: `update_note` overwrites a reminder's `instruction` in place [P2]

- `FirestoreAgentNoteAdapter.update_note` is a plain field update; `AgentNote` has no version history, so a rewritten
  `instruction` is lost. The instruction is one string, so any small correction replaces all of it.
- It matters because the firing alert (`build_reminder_alert` in `src/services/reminders_service.py`) invites the
  firing agent to update or delete the reminder on every fire. A long-crafted recurring reminder can rewrite itself
  with no way back; the per-change `notify_raw` receipt detects it but cannot restore it.
- Fix, phase 1: snapshot the prior content to an append-only `revisions` subcollection before a content edit (fire-time
  reschedules do not go through `update_note`, so fires add no revisions); add `protected: bool` to `AgentNote` and
  leave the update/delete invitation out of a protected note's alert. Phase 2: refuse content edits to a protected
  note from an autonomous fire, and show a diff for the user to confirm.

### TD-6: `fetch_url` has no Firestore prompt — the inline prompt is the production path [P2]

- `WebSearchAgent._handle_fetch_url` passes `_FALLBACK_FETCH_SYSTEM` unconditionally and never calls
  `build_for_agent()`. Despite its name it is the only prompt `fetch_url` has, an exception to the "no fallback
  prompts" rule, and a whitelist entry in `tests/unit/arch_tech_debt.py`. `fetch_url` carries most of the morning
  briefing's web cost, yet its prompt has no per-user override and every wording change needs a deploy.
- Fix: author a `fetch_url` prompt profile (WebSearchLight's `websearch_light` profile is the nearest model, but it is
  phrased for search, not for a URL fetch), call `build_for_agent()`, return `AgentResponse.failure()` when it fails, then delete the constant and
  the whitelist entry.
- Constraint: the prompt serves two request shapes (`query` + URL, and a bare URL). Wording that covers only the first
  regresses bare-URL fetches badly; tune with `scripts/websearch/tune_fetch_prompt.py`.
- Deferred by the owner (2026-07-29): wording fixed in place for now; see `decisions/websearch_per_intent_tier.md`.

### TD-9: `edit_video` is billed at `generate_video`'s rate [P3]

- `video_generation_delivery.py` prices every delivery with `VIDEO_GENERATE_MODEL` (`grok-imagine-video-1.5`,
  $0.08/s), but `GrokVideoAdapter` runs edits on `_EDIT_MODEL` (`grok-imagine-video`, $0.05/s per xAI's model page).
  The internal ledger over-counts every edit by 60 %; xAI's own charge is right.
- Fix: a `VIDEO_EDIT_MODEL` constant and rate in `domain/billing.py` (as `IMAGE_GENERATE_MODEL` / `IMAGE_EDIT_MODEL`
  already are), passed by the delivery for whichever operation ran. Deferred by the owner (2026-09-20).

### TD-10: Retire `DomainResearcherAgent` [P3]

- The `domain-competency-research` system skill (`src/skills/smart/domain-competency-research/SKILL.md`) does the same
  job in Smart, with web search and memory. The agent is still wired.
- Scope: `src/agents/domain_researcher_agent.py`; `DOMAIN_RESEARCHER` and `Intent.DOMAIN_RESEARCH`
  (`agent_manifest.py`); `DomainResearcherAgentConfig` (`agent_config.py`); its provider strategy
  (`agent_context_builder.py`) and tier entry (`domain/user.py`); the factory builder (`user_agent_factory.py`); the
  Firestore profile and token; the live channel binding (unbind first); mentions in `src/agents/CLAUDE.md` and
  `NEW_AGENT_PLAYBOOK.md`.
- Tests use `domain_researcher` as the example of a non-companion binding
  (`test_conversation_handler_agent_command.py`, `test_conversation_handler_session_mode.py`,
  `test_overflow_routing_service.py`, `test_agent_context_builder_per_agent_provider.py`); a reviewer decides each edit.

### TD-11: No async delegation whose result returns to the orchestrator [P3] (needs an RFC)

- ASYNC means "the result goes to the user": `AgentWorkerHandler` delivers it and nothing hands it back to the caller
  (`decisions/delegate_mode_parameter_removed.md`). A multi-step task that must continue after a slow specialist (use
  a generated page's link, say) has no mechanism.
- Questions for the RFC: where the pending state lives; how the orchestrator is woken (a new turn, or the long-turn
  machinery); what the user sees meanwhile; the 25-min turn clock; what the orchestrator receives (a reference, never
  document content).

### TD-12: `prompt_builder` is optional in most agent constructors [P2]

- About a dozen agents declare `prompt_builder: Optional[PromptBuilderPort] = None` (Compute, Consolidation, DocPlanner,
  DocGenerator, DeepResearch, EmailClassification, MemorySearch, Notes, Router, Tutor, WebSearch, DomainResearcher).
  Production always injects it, but some guard with `if self.prompt_builder:` and run on an empty prompt when it is
  missing (`ComputeAgent.execute`), which hides misconfiguration and contradicts the "fail fast on missing prompts" rule.
- Fix per agent: make the parameter required, call it unconditionally, return `AgentResponse.failure()` when the build
  fails. Tests that build these agents without a prompt builder need one; a reviewer decides each edit.

### TD-13: Single-call failover tries one fallback provider [P3]

- `BaseAgent._call_llm` fails a single call over to `ctx.fallback_provider` only; it does not walk the agent's
  `allowed_providers`, and Smart's provider rotation fires on `TranscriptLockedError` but not on a non-locked
  `BothProvidersUnavailableError`. Deferred when rotation shipped (`decisions/cross_provider_execution_retry.md`,
  "variant 3"): wider blast radius, since every single-call agent goes through this path.

### TD-14: A specialist failure ends the turn as SUCCESS and raises no alert [P1]

- `DelegationEngine` turns an `AgentResponse.failure` into an ordinary tool result ("Specialist agent rejected the
  request … Correct your input and try again", `failed=True`, a warning log only), so the orchestrator apologises and
  the turn finishes with status SUCCESS. `BaseAgent._on_agent_error` only logs. There is no global error reporter, so
  a system fault (an adapter exception, a 4xx/5xx from a store or provider) is invisible to the owner.
- Fix: split the two kinds of failure in the engine. An input error (bad arguments) goes back to the model as today so
  it can correct itself. A system error raises an alert (the ops `AlertSinkPort`, or `UserNotificationService.notify_raw`;
  which one is open) plus a structured log line.

### TD-15: `allowed_intents` is enforced only in the tool declaration, not at dispatch [P2]

- `AgentRegistry` filters the intents an agent is *shown* by `AgentDescriptor.allowed_intents`
  (`agent_registry.py`, `get_available_intents_for`), but neither `DelegationEngine.dispatch` nor the coordinator checks it
  when the call arrives. A model that names an intent outside its list still runs it. `LelikAgent.delegate`
  (`/voice/delegate`) dispatches whatever the realtime model sends; Smart, Quick and Tutor are the same.
- Fix: reject, in the engine's dispatch, any intent not in the calling descriptor's `allowed_intents` (when set), with a
  tool result that says so. Its own small change; `LelikAgent.delegate` is the first caller to cover.

### TD-16: Archived or deleted Slack channels leave their channel binding behind [P3]

- Nothing in `src/` handles `channel_archive` or `channel_deleted`, so a `ChannelBinding` document (id = channel id,
  made by `$agent <type>`) outlives its channel. `FirestoreChannelBindingAdapter._deserialize_companion_config` still
  raises `KeyError` on a stored `companion_config` that lacks `window_threshold` or `batch_size` (the other fields fall
  back to defaults), a trap for anything that iterates the collection.
- Open before building: whether the Slack app receives these events with its current manifest and scopes; delete or
  soft-delete the binding, and whether its companion records stay. A one-time cleanup of existing orphans is a separate,
  smaller task.

### TD-17: Prompt assembly drops `extra_static_blocks` when there is no knowledge base and no preamble [P2]

- In `PromptAssemblyService` (`prompt_v3/prompt_assembly_service.py`), the blocks are appended only when `kb_parts` is
  non-empty, or in the `elif extra_static_blocks and kb_preamble` branch. With `kb_parts` empty and `kb_preamble=False`
  they are silently dropped. Smart's email-triage path calls with `kb_preamble=False`.
- Fix: append the blocks in that case too (after the blueprint, as the postamble branch does). The code is shared by
  every agent, so it needs its own review.

### TD-18: Email search dates carry no user timezone [P3]

- `EmailSearchAgent._parse_date` (`email_search_agent.py`) parses `date_from` / `date_to` with `strptime` and no
  timezone, so the day boundaries ignore the user's timezone and a search can be off by hours at midnight.
- Fix: parse into the user's timezone and convert for the query.

### TD-19: `response_owed` is written but never read [P2]

- `VoiceSessionService` (`voice_session_service.py`) sets `state.response_owed` when a late Alek answer arrives while the
  caller is speaking under a provider-owned turn (xAI), and clears it elsewhere, but no code reads it. Nothing starts the
  owed response, so Lelik stays silent until asked.
- Fix: part of the voice fix batch (the reader of the flag belongs with the turn-ownership logic), not a piecemeal patch.

### TD-20: `_migrate_ownership_fields` can overwrite `account_id`; `write_superseding` has no tenancy check [P3]

- `FirestoreFactRepository._migrate_ownership_fields` (`firestore_repo.py`) takes the legacy branch whenever either of
  `account_id` / `created_by_user_id` is missing and `owner_id` exists, and then overwrites a stored `account_id` with
  `owner_id`. `write_superseding` checks no tenancy between the new version and the documents it closes.
- No stored document is known to hit the first case. Fix: migrate only the missing field; check that a superseding
  write stays inside one account.

### TD-21: The user-facing apology on an agent failure is hard-coded English [P3]

- `AgentFallbackService._APOLOGY_TEXT` (`agent_fallback_service.py`) is the text sent when Smart fails and the fallback
  runs, whatever the user's language.
- Fix: a `LocalizationPort` key with uk, en, fr and es strings.

### TD-22: `email_indexing_jobs` grows without a cleanup [P3]

- `FirestoreEmailJobRepository` (`firestore_email_job_repo.py`) writes one job document per user per run and has no
  delete path.
- Fix: in `create_job`, delete finished jobs older than 30 days except the latest (the Cabinet reads it).

### TD-23: `SkillService._check_text` puts the full `user_id` in the security-check context [P3]

- The context string `skill_{field}_user_{user_id}` (`skill_service.py`) carries the whole id into the validator's logs,
  against the rule that logs show a short form of ids.
- Fix: pass a shortened id (as other log lines do) or a skill name.

### TD-24: `prompt_component_repo` and `component_service` are built and never read [P3]

- `ServiceContainer` constructs both (`service_container.py`) and passes `component_service` to `UserAgentFactory`
  (`user_agent_factory.py`), which stores it; nothing reads it. A wiring test that asserts on it hides an equivalent
  mutant.
- Fix: delete the construction and the parameter, and the test's assertion with them (a reviewer decides the test edit).

### TD-25: A PDF whose text layer is one repeated letter never reaches the reader model [P2]

- `garbage_share` (`domain/document_extraction.py`, used by `model_route`) counts only mojibake and `(cid:N)` patterns,
  so a text layer with a broken font mapping (one letter repeated) passes as good text. `is_machine_text`
  (`domain/drive_text_gate.py`, called from `DriveIndexingService._read_text`) then demotes the record to
  `metadata_only / machine_text` with no reader attempt. Such files show in the Cabinet's "could not be read" list, not
  to the bot.
- Fix: route the case to the reader. Before building, measure the reader's real per-page cost on a few pages: a long
  book may exceed the monthly reader page cap.

### TD-26: A scope change re-enumerates the whole Google account (design round in progress) [P2]

- On any change of a connection's folder scope, `DriveIndexingService` restarts the enumeration from zero
  (`scope … changed; re-enumerating`, MULTI_DRIVE_RFC §11.3 and E71). The state keeps only a hash of the folder set
  (`scope_key`), so no diff of old and new scope is possible. The restart re-walks files already walked; time and API
  quota only, no model spend.
- Open design, not to be built piecemeal: walk only the added folders' subtrees on a widening and drop from the index on a
  narrowing; the cases to cover include access to a subfolder first and to its parent later, and counting scope per file
  instead of per folder. One model that may cover all of it: the index is built by walking the chosen folders, so a feed
  item's verdict is local (its parent is in the set). The planner already has subtree re-listing (`reobserve_pending`) and
  a moved-folder rescope cascade a diff could reuse. Also for that round: `ITEM_CALLS_PER_TICK`
  (`domain/drive_index_params.py`) looks redundant as a deadline guard, since `item_call_allowed` also checks the time left.

### TD-27: Bot-written `[System: …]` frames reach consolidation in the user role [P2]

- The late answer of a long turn (`turn_clock.py`, saved by `LongTurnService.save_late_answer`), call events, drive-note
  events and the async-document-ready note (`user_notification_service.py`) are saved as user-role messages starting with
  `[System: …]`. `serialize_messages_for_consolidation` sends no author, and the consolidation tokens have a rule only for
  the memory-review frames (`CONSOLIDATION_POLICIES`). Reminders are not framed this way.
- To check first: whether the consolidator turns a late answer's content into the owner's facts (not observed yet). If it
  does, give the `[System:` frames an explicit rule in the consolidation tokens (the "bot never writes to memory itself"
  rule in the root `CLAUDE.md` is the constraint).

### TD-28: History tiering keeps one more full answer than its setting says [P3]

- `BaseAgent._apply_history_tier` (`base_agent.py`) keeps `full_text` while `model_turns_from_end <= turns`, so a
  setting of N keeps N+1 full model turns. Cosmetic unless a cost question arises.
- Fix: `<` instead of `<=`, with the defaults (`history_recent_full_turns`) re-checked.

### TD-29: Telegram replies drop the forum-topic id in a private chat with topics [P3]

- The Bot API lets a private chat with a bot have forum topics (`is_topic_message`, `has_topics_enabled`), but the
  docstring of `TelegramResponseChannel.send_reply` (`adapters/telegram/response_channel.py`) says topics exist only in
  supergroups, and `send_reply`, `send_literal`, `send_late_answer` and the `update_message` fallback drop
  `message_thread_id` while `send_message` and `send_file` pass it. Harmless until topic mode is enabled for the bot.
- Fix: carry `thread_id` through every send path and correct the docstring.

### TD-30: The drive parser child sometimes aborts at start (SIGABRT, cause unknown) [P3]

- The `[DriveExtract]` log shows extractions of markdown, spreadsheet and PDF kinds ending with exit code -6 after no
  measurable time, before any parsing, mostly on Google Docs exports. The files are retried, so none is lost. The child's
  stderr is only hashed (`_stderr_digest`, `adapters/subprocess_content_extractor.py`), so the cause is not in the logs.
- Suspect: the resource limits (process-count or address-space) applied before the interpreter starts. Next step:
  reproduce in the indexer image with the same limits and read stderr there; first check whether it persists now that
  parsing runs in the sandbox light service.

---

## Closed items

| Id | What it was | Closed | Where |
|----|-------------|--------|-------|
| TD-1 | Legacy GCS prompt dump (`PromptDebugLogger`) | removed 2026-06-29 | BigQuery `prompt_content` is the only capture path |
| TD-2 | Cross-provider failover mid delegation loop mixed transcripts | 2026-06-29 | `decisions/transcript_integrity_one_provider.md` |
| TD-2b | `TranscriptLockedError` dropped Smart straight to Quick | 2026-07-13 | `decisions/cross_provider_execution_retry.md` (follow-up: TD-13) |
| TD-3 | "Dead `deliver_response` machinery" | **invalid, 2026-08-17 — do not delete it**: it is Smart's only reply path on Grok; since 2026-10-23 Claude also synthesizes it (one string parameter) for schema + client-tool requests, and Quick and Tutor pass it too | `decisions/terminal_tool_co_emitted_calls.md` |
| TD-4 | `update_fact` overwrote fact text in place | Facts S2, 2026-10-23 (history starts at the S2 deploy) | `decisions/fact_versions_one_writer.md` |
| TD-7 | Cost priced by the agent's default model, not the model that ran | 2026-07-30 | `decisions/billing_per_model_pricing.md` |
| TD-8 | `anthropic` unpinned | pinned 2026-08-11 (the wider sweep is R-5) | `requirements.txt` |
