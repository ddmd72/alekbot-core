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

- The untrusted-content sandbox (S1) is built but not deployed; `SANDBOX_RENDER` defaults to `inprocess`, so the
  Node script DocGenerator writes runs next to the main service's credentials. See
  `docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md` and `decisions/sandbox_render_service.md`.

### R-3: The drive parser can reach the metadata server and the network (accepted)

- The parser child runs as the unprivileged `driveparser` user under rlimits: it cannot read the indexer's keys or
  rewrite its code, but it shares the network. Owner-accepted residual until the zero-credential parser service
  exists; see `decisions/drive_parser_sandbox_resource_isolation.md`.

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

---

## Closed items

| Id | What it was | Closed | Where |
|----|-------------|--------|-------|
| TD-1 | Legacy GCS prompt dump (`PromptDebugLogger`) | removed 2026-06-29 | BigQuery `prompt_content` is the only capture path |
| TD-2 | Cross-provider failover mid delegation loop mixed transcripts | 2026-06-29 | `decisions/transcript_integrity_one_provider.md` |
| TD-2b | `TranscriptLockedError` dropped Smart straight to Quick | 2026-07-13 | `decisions/cross_provider_execution_retry.md` (follow-up: TD-13) |
| TD-3 | "Dead `deliver_response` machinery" | **invalid, 2026-08-17 — do not delete it**: it is Smart's only reply path on Grok | `decisions/terminal_tool_co_emitted_calls.md` |
| TD-4 | `update_fact` overwrote fact text in place | Facts S2, 2026-10-23 (history starts at the S2 deploy) | `decisions/fact_versions_one_writer.md` |
| TD-7 | Cost priced by the agent's default model, not the model that ran | 2026-07-30 | `decisions/billing_per_model_pricing.md` |
| TD-8 | `anthropic` unpinned | pinned 2026-08-11 (the wider sweep is R-5) | `requirements.txt` |
