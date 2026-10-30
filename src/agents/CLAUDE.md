# Agents

Multi-agent system with ActorModel-like coordination — specialists, not one LLM for everything.
Tiers: ECO/BALANCED/PERFORMANCE (tier→model resolution + capability gates live in
[`src/adapters/CLAUDE.md`](../adapters/CLAUDE.md)). This file holds what any edit in `src/agents/` needs: rules,
conventions and per-agent traps. What an agent does lives in `docs/` (map below); intents and tiers live in code (below).

## Structure

- `base_agent.py` — ABC. All agents inherit `BaseAgent`.
- `core/` — orchestrators: `RouterAgent`, `SmartResponseAgent`, `QuickResponseAgent` (fallback/formatter).
- `infrastructure/` — system agents: `BillingAgent`, `LoggerAgent`.
- All other files — specialist agents (map below).

## Rules for every agent

- Agents do NOT access the database directly — only through services/ports.
- Prompts live in Firestore (token + blueprint via PromptBuilder). NO inline or fallback prompts in code — if
  `build_for_agent()` fails, return `AgentResponse.failure()`. Fail fast.
- `AgentExecutionContext` holds model_name, tier, provider — the agent does not select the model itself
  (see `AgentProviderStrategy`).
- `_call_llm(request, turn)` from BaseAgent is the single billing + debug-logging + capture point. Never call the
  provider port directly. Changing capture = edit that method only. Agents bypassing `LLMPort` (native SDK tools, e.g.
  `ClaudeDeepResearchRunnerAgent`) call `_debug_raw_turn(...)`. BigQuery is the only content-capture path.
- CircuitBreaker is built into BaseAgent — do not duplicate.
- Every LLM agent constructor calls `_set_execution_context()`; without it `_call_llm`'s cross-provider failover is
  structurally unreachable (guard: `test_agents_set_execution_context.py`; `decisions/html_page_fallback_context_wiring.md`).
- Multi-turn tool loops: use `infrastructure/delegation_engine.py`, do not hand-roll.
- Log through the lifecycle hooks (`_on_agent_start`, `_on_agent_success(char_count, token_count, output_text)`,
  `_on_agent_error`, `_on_delegation`), not direct `logger.*`; changing infrastructure logging = edit BaseAgent only.
- Tool history: `build_tool_turn(response, tool_results)` (`src/domain/llm.py`; `BaseAgent._build_tool_turn` wraps it).
- Structured-output agents follow the Agent Output Format Standards in [`src/adapters/CLAUDE.md`](../adapters/CLAUDE.md)
  (OUTPUT_FORMAT token, `_parse_response`, retry).
- Tunables live in `src/infrastructure/agent_config.py` as typed dataclasses read as class constants
  (`CONTEXT_WINDOW = QUICK.context_window`); provider selection is separate (`AgentProviderStrategy` in
  `src/services/agent_context_builder.py`).

## Creating a New Agent

Follow [`docs/how_to/NEW_AGENT_PLAYBOOK.md`](../../docs/how_to/NEW_AGENT_PLAYBOOK.md) — mandatory protocol (manifest
`Intent` + `AgentDescriptor` in `ALL_DESCRIPTORS`, `user_agent_factory.py` wiring, eager or lazy,
`src/utils/capabilities.py`).

## Agent Roster

Map: agent → where it is documented (intents: `src/infrastructure/agent_manifest.py`; default tiers:
`_DEFAULT_AGENT_TIERS` in `src/domain/user.py`). Code is in the same-named
`*_agent.py` unless noted.

| Agent | Documentation |
|---|---|
| Router, Smart | `docs/05_building_blocks/smart_agent_execution/README.md`, `decisions/two_layer_provider_override.md` |
| Quick | `docs/05_building_blocks/quick_agent_delegation/README.md`, `decisions/quick_agent_deferred_deletion.md` |
| WebSearch | `decisions/websearch_per_intent_tier.md` |
| Memory, Consolidation, review pass | `docs/05_building_blocks/memory/README.md` (§5.6 review, §2.7 expiry), `decisions/standing_directives.md`, `decisions/directive_applicability_gate.md`, `docs/10_rfcs/MEMORY_FACT_CONFIRMATION_RFC.md`, `docs/10_rfcs/FACT_LIFECYCLE_RFC.md` |
| EmailSearch, EmailClassification | `docs/05_building_blocks/gmail_email_indexing/README.md` |
| Drive model agents (reader, triage, profiler, passages), DriveTranscriber | `docs/05_building_blocks/user_drive/README.md` §4.16, §4.17, §4.23; `decisions/drive_model_extractor_agents.md`, `decisions/drive_model_outage_defers_model_work.md` |
| Drive (`DriveAgent`), FileManagement | `docs/10_rfcs/DRIVE_SPECIALIST_RFC.md`, `decisions/drive_specialist.md`, `docs/05_building_blocks/user_drive/README.md` §6.7, `docs/05_building_blocks/file_storage/README.md` §3.4, `decisions/drive_content_write_gate.md`, `decisions/drive_open_bound.md`, `docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md` |
| DocPlanner, DocGenerator, PdfGenerator, HtmlPageGenerator | `docs/05_building_blocks/document_generation/README.md`, `decisions/html_page_grok_default.md` |
| Notes / Self-Reminders | `docs/10_rfcs/PROACTIVE_SELF_REMINDERS_RFC.md`, `decisions/reminder_rrule_schedules.md`, `docs/10_rfcs/AGENT_WORKSPACE_RFC.md` §8.3 |
| Tasks | `docs/05_building_blocks/tasks_integration/README.md` |
| DeepResearch | `docs/10_rfcs/DEEP_RESEARCH_RFC.md`, `decisions/deep_research_drive_copy.md` |
| MapsSearch | `docs/10_rfcs/MAPS_SEARCH_RFC.md` |
| Compute | `docs/10_rfcs/COMPUTE_AGENT_RFC.md` |
| ImageGeneration | `docs/10_rfcs/IMAGE_GENERATION_RFC.md`, `decisions/image_edit_multi_reference.md` |
| Tutor, TutorExtractor | `docs/10_rfcs/COMPANION_AGENTS_RFC.md`, `decisions/companion_write_path.md`, `decisions/companion_history_recent_full_turns_per_channel.md` |
| Lelik, LelikSummarizer, AlekGateway | `docs/10_rfcs/VOICE_COMPANION_RFC.md` (§4.7, §4.8, §4.10, §4.15), `decisions/lelik_send_to_chat.md`, `docs/04_solution_strategy/current_implementation/STRUCTURE.md` |

### Traps by agent

- **WebSearch:** JSON output is prompt-only (`WEBSEARCH_OUTPUT_FORMAT`); `use_grounding` makes the OpenAI adapter drop
  `text.format`, and there is no `_parse_response`, so malformed JSON never raises. `confidence` is `len(text)/500`.
- **WebSearch `fetch_url`:** its system prompt is the inline `_FALLBACK_FETCH_SYSTEM` (tracked exception, TD-6); the
  wording is tuned and load-bearing, read the comment before editing.
- **Drive model agents** (`drive_reader`/`drive_triage`/`drive_profiler`/`drive_passages`): their `STRATEGIES` entries need
  `fallback: "gemini"` with `fallback_tier: BALANCED`, else the fallback resolves to flash-lite; Gemini is not in
  `allowed_providers`. They run on the indexer only and are built per call by `composition/` runners (services/ cannot
  import agents/).
- **DriveTranscriber** is not a `_call_llm` agent (a Gemini Batch request is not a call): no retry, failover, ledger scope,
  `prompt_content` row or span. The agent builds the request and parses the answer; `DriveTranscriptRunner` submits and bills.
- **HtmlPageGenerator:** `_call_llm_recitation_aware` retries only on Gemini; a Grok refusal is non-empty text, passes the
  `if not html_code` guard and would be published as the page.
- **Drive specialist:** a `still_running` mutation ends the commission (a later change is `not started`); the status is set
  by code, the model can only lower it. Evals stub drive writes at the `DriveOperations` class
  (`scripts/validation/drive_eval/dry_run_drive.py`), never at `DelegationEngine.dispatch` (the specialist's tools do not
  pass through it). A global `provider_preference: openai` moves it to the rejected `gpt-6-luna`: pin
  `agent_providers["drive"]="claude"`. `DriveAgent` is `NO_RETRY_POLICY` (a retry would repeat a write).
- **Drive refs are tagged:** the model and the history see `drive:<tag>:<item_id>` (the tag names the connection); a ref
  written before the connections has no tag and answers "search for the file again". Prompt examples use the tagged form.
  Settings (indexing, transcription, primary, disconnect) are Cabinet-only; no chat path sets them.
- **Drive open bound:** a refusal is `REFUSED` and never settled by a later open, and arms no content gate (a page does).
- **FileManagement:** `prefetch_file_ref=False`; `delete_file` on a `drive:` ref is refused (route through
  `work_with_drive_files`).
- **Consolidation:** history restarts per stage (Stage 1, 2a, 2b), so Stage 2b cannot see Stage 1.
- **Consolidation `expiry`:** the schema is the module constant `_EXPIRY_SCHEMA`, never read through `self` (tests call
  `_get_tool_declarations` unbound); a new field or example must land in the token and the code schema together
  (`test_consolidation_vocabulary.py`, `test_consolidation_expiry_prompt.py`).
- **Consolidation time:** use `domain/local_time.local_stamp` and `_message_time`; never format a time inline.
- **Echo-trap exception:** `Domain_Scope.memory_review_reply` in `CONSOLIDATION_POLICIES` is the one definition; other
  lines refer to it, do not restate it in a token.
- **ImageGeneration:** `NO_RETRY_POLICY` and `max_retries=0` on the adapter's client: a transient 5xx after xAI rendered
  (and billed) must not trigger a second paid render. `edit_image` takes `context_schemas["image_refs"]` (array), not `file_ref`.
- **Tutor:** the agent is a per-user singleton shared by channels, so `history_recent_full_turns` is read per call from
  `message.context`, never fixed at construction. Blueprint `tutor_agent_v1` needs `output_format` in `class_order` for
  `OUTPUT_FORMAT_TUTOR` to render.
- **Lelik `execute()`** must not catch `originate_call` failures: the webhook's own except deletes the ticket and releases
  the one-call marker; swallowing strands both.
- **LelikSummarizer** is not manifest-registered: its ECO tier is pinned in `domain/user.py::_DEFAULT_AGENT_TIERS`
  because `test_every_llm_agent_has_a_default_tier` cannot see it. Its provider has no fallback by decision.
- **AlekGateway:** routes to the Router (not Smart) so `enrich_context` runs; Smart reads only
  `context["current_message_parts"]`, so the commission goes there; cancel `response_summary_task` (nothing writes history
  on this path); `NO_RETRY_POLICY`.

## Drive — Logs

`make fetch-logs`, then `grep '\[DriveAgent\]\|\[DriveOperations\]\|\[DriveSearch\]' alek_debug.log`:

- One `[DriveAgent] user=<8> status=<status> model=<model> ops=<op>:<class>,… turns=N ms=…` line per commission;
  `model=` is the model that ran (from the execution's billing ledger, models joined by `+`; a cross-provider failover is
  the `llm_fallback claude→openai` WARNING). `prompt_content` records the request's model, so it cannot show a failover.
- One `[DriveOperations] <op>: <ref or folder>` line per operation; `opened: <ref> chars=<n>` per whole-file open (before
  the bound), then `open refused: <ref> chars=… passages=… truncated=…`, `open cut: <ref> chars=… kept=… extent=…`, or
  for a PDF or image `opened vision: <ref> mime=… bytes=… pages=…`.
- One `[DriveSearch] user=<8> status=… mode=excerpts|excerpt_range scope=… files=… repeated=… no_text=…` line per
  passages call (counts, no text), next to the search's `mode=query|filter` line.
- A turn-cap exit: `ERROR [DriveAgent] user=<8> hit the turn limit (6) without a report; tools per turn: …`.
- A change refused after passages were read: INFO `[DriveAgent] <kind> refused: passages were read in step N of this run`.
- Indexer lines (`[DriveIndex]`, model agents): `make fetch-logs-indexer`. More: `docs/05_building_blocks/user_drive/README.md` §7.

## Orchestration Patterns

- **AgentDescriptor** (`agent_registry.py`; instances in `agent_manifest.py`) — one per agent. Parts: (A) `capabilities`
  (`internal=True` hides it from LLM tool declarations); (B) `requirements`: `allowed_intents` (`None` = all non-internal)
  and `intent_remap`; (C) `context_schemas`: per-intent typed param contracts. `eager` (default True) creates the agent in
  `ensure_agents_for_user()`, `eager=False` on first delegation via `AgentFactoryPort.create_agent_on_demand()`.
  Specialists register via `ALL_DESCRIPTORS` in `build_application_graph`; orchestrators set a class-level `_descriptor`
  (the coordinator never routes TO them via the registry). Registry rule: an explicit `allowed_intents` may name an
  internal intent for that caller only (Lelik names `ask_alek`; `decisions/allowlist_names_internal_intent.md`).
  Building block: `docs/05_building_blocks/agent_registry/README.md`.
- **Every field a `capability_descriptions` prose names in `payload: {…}` / `context={…}` must be declared in
  `context_schemas`:** `context` is generated from the schemas only, and an undeclared key the prose asks for caused
  64k-token whitespace runaways on OpenAI (`test_manifest_prose_matches_tool_schema.py`,
  `decisions/delegate_tool_contract_and_strict.md`).
- **Intent** — typed string constants (`class Intent` in `agent_manifest.py`). Import `Intent.X`, never a raw string.
- **Specialist delegation** — the LLM has one tool, `delegate_to_specialist(intent, query, context?)`, which works as
  commissioning: the LLM matches purpose to the manifest `capability_descriptions`. Intent names must reflect the
  operation being commissioned (the name is the primary matching signal). `query` is natural language, self-contained and
  goal-oriented — never JSON; structured inputs go in `context` (fields declared in `context_schemas`).
- **Sync/async is the manifest's.** `handle_delegation` dispatches by `manifest.capabilities[intent]` only; the per-call
  `mode` argument is gone and ignored (`decisions/delegate_mode_parameter_removed.md`).
- **DelegationEngine** (`src/infrastructure/delegation_engine.py`) owns the multi-turn loop, tool dispatch via
  AgentCoordinator, memory-first parallel execution and history via `build_tool_turn()`. It does not own LLM parameters
  (the agent builds `LLMRequest`) or response parsing. `dispatch()` is public for a caller with no loop of its own
  (Lelik). Fan-out: `FanoutSpec` on the descriptor. Details: `docs/05_building_blocks/multi_agent_system/README.md`.
- **Pass `context=message.context` whole** to `engine.execute` and to any downstream message. Rebuilding the context
  loses `origin_channel_id`, `session_id` and `_call_chain`, and an agent that does so silently resets the cycle guard
  (that was `notes_agent`'s bug). `AgentCoordinator._refuse_if_looping` refuses re-entering an agent in `_call_chain`;
  `MAX_DELEGATION_DEPTH=8` (`decisions/delegation_cycle_guard.md`). Lelik seeds `_call_chain=[lelik_agent]`.
- **Local tools** (`local_tools`: `use_skill`, `propose_memory_facts`, `workspace`) are built per execution and never
  stored on the agent (concurrent executions, provider rotation); their builders live in `src/infrastructure/`, since
  REQ-ARCH-24 forbids an agent importing a sibling `agents/` module.
- **Terminal tool `deliver_response`** (Smart, Quick, Tutor) is live and provider-dependent (Grok: synthesized function
  with `_RESPONSE_SCHEMA`; Claude with schema + tools: `deliver_response(response: string)`; Gemini/OpenAI: native JSON).
  It is NOT vestigial: TD-3 is closed as invalid, do not "clean up" the machinery
  (`decisions/claude_deliver_response_string_tool.md`). Failure semantics (`empty_answer`) and the log greps are in that
  decision.
- **A terminal tool can arrive with real work in the same batch:** the engine must dispatch every co-emitted
  non-terminal call and fold its `delivery_items`/`history_context` before returning the terminal answer
  (`decisions/terminal_tool_co_emitted_calls.md`).
- **Provider blocks are 200s, not exceptions** (`LLMResponse.finish_reason`). Single-shot generation agents use
  `_call_llm_recitation_aware()`; **never in delegation loops** (the directive assumes the last user message is the
  whole assignment). Only `GeminiAdapter` maps the reason (`decisions/gemini_finish_reason_surfaced.md`).
- **Transcript integrity — one delegation transcript = one provider.** `_call_llm` fails over across providers only when
  `request.messages` is not provider-locked (no `tool_call`/`tool_response` part, no `raw_content`); once locked it retries
  the same provider and otherwise raises `TranscriptLockedError`, never a mixed transcript
  (`decisions/transcript_integrity_one_provider.md`).
- **Cross-provider execution retry (provider rotation) — Smart.** `SmartResponseAgent.execute()` catches
  `TranscriptLockedError` and rotates: rebuilds the run on the next `allowed_provider` at the same tier
  (`resolve_next_provider`, `next_provider_override`), only then falls to the Quick rung. `_run` re-raises the lock,
  `execute` owns the loop. **Read `decisions/cross_provider_execution_retry.md` before touching
  `smart_response_agent.execute` / `_run` / `resolve_next_provider` / `next_provider_override`.**

## Traps from the root CLAUDE.md

- **Token accounting is per execution:** usage goes into the `TokenLedger` of `_execution_billing_scope()` (a `ContextVar`),
  never onto the instance (per-user singletons, `asyncio.gather` fan-out); its `reset(token)` is load-bearing (`decisions/billing_execution_scoped_ledger.md`).
- **Consolidator input:** model parts serialize as `p.text`, never `p.full_text` (verbose + web search context); user parts as
  `p.consolidation_text or p.text` (`domain/consolidation_serialization.py`).
- **Time:** `include_datetime=False` by default; time reaches the model via `_inject_timestamps()` in user messages and a UTC
  prefix on delegation queries. A date in the system prompt changes the cached static prefix.
- **Large static payloads** (a triage batch, a corpus) go through `extra_static_blocks` on `build_for_agent()`, never into the
  user message (pollutes history, bypasses caching; `docs/08_concepts/prompt_assembly_guide.md`).
- **Drive notes:** Smart has no note tool; a note's words go in the `work_with_drive_files` query, never in `file_contents`.
