<!-- living-doc
covers:
  src/services/voice_session_service.py
  src/ports/realtime_session_port.py
  src/adapters/openai_realtime_adapter.py
  src/adapters/xai_realtime_adapter.py
  src/agents/lelik_agent.py
  src/agents/lelik_summarizer_agent.py
  src/agents/alek_gateway_agent.py
  src/services/lelik_persona_service.py
  src/domain/lelik_context.py
  src/services/voice_resume_service.py
  src/composition/voice_resume_setup.py
  src/domain/voice_resume.py
  src/services/call_text_file_service.py
  src/domain/call_text_file.py
  src/domain/call_text_markdown.py
  src/domain/voice_mode.py
  src/domain/voice_language_pair.py
  src/domain/voice_ask_latch.py
  src/domain/voice_live_text_pacer.py
  src/domain/voice_errand.py
  src/domain/voice_delegation_outcome.py
  src/domain/voice_call_note.py
  src/domain/voice_call_buffer.py
  src/domain/voice_provider_profile.py
  src/domain/voice_session_spec.py
  src/domain/voice_turn_ownership.py
  src/domain/voice_mcp_servers.py
  src/domain/remote_tool_server.py
  src/domain/result_links.py
  src/assets/voice/
  scripts/admin/set_voice_languages.py
  scripts/voice/ask_note_probe.py
  scripts/voice/test_callback_roundtrip_poc.py
  scripts/voice/test_late_function_call_output_poc.py
  scripts/voice/test_reasoning_effort_poc.py
  scripts/voice/test_xai_mcp_shape_probe.py
  scripts/voice/test_xai_realtime_protocol_poc.py
-->
# Voice Companion (Lelik)

## Purpose

Lelik is the voice of the exocortex: one realtime speech-to-speech conversation with the owner, over a phone call or a
web call. He talks from a warm prompt (the owner's ordinary facts, the primary chat's recent history, date and place),
and goes to Alek, the text system, only for what the prompt does not hold: mail, tasks, documents, the web, actions.
This block is the call itself: the session loop that sits between the audio and a realtime provider, the two providers
behind one port, what a call is given and may do, its modes, and what it leaves behind.

What it is not. The audio transport (call admission, Twilio, the SFU, the relay's sockets, the page's control channel) is
the voice call transport block. Lelik is not a text agent and not a delegation target: he is `internal` in the manifest
and never offered to Smart. The text system's own voice features (voice messages, transcription) have nothing to do with
a call; `scripts/admin/set_voice_languages.py` belongs to that side.

Two processes carry the block. The **relay** (`relay_main.py`, its own Cloud Run service) runs
`VoiceSessionService`, holds the provider socket and the audio, and keeps no state beyond the call. The **main service**
holds everything durable and every model call except the realtime one: it builds the session, runs delegations, bills,
summarises and delivers. The two meet only on the OIDC-protected `/voice/*` routes.

## Owned data

Nothing in this block has a collection of its own. A call's state is short-lived keys in the voice tickets store
(`FirestoreEphemeralStore`, `EnvironmentConfig.voice_tickets_collection`), plus what the call finally writes into
chat history, drive and billing.

- **Ticket record** (`voice_ticket_keys.ticket_key`) and **call-kind record** (`call_kind_key`): the assembled session config
  (instructions with the owner's facts, tools, `voice` spec, MCP descriptors without tokens, mode, call kind, ids) and
  `{call_kind, mode}`. Written by `VoiceCallSetupService.prepare`. The ticket is **consumed once** by
  `/voice/session-config` (`get_and_delete`), because it carries the facts; the call-kind record is read back at call end
  and by each delegation, and a missing one means the companion call.
- **Delegation result and abandoned markers** (`result_key`, `abandoned_key`, TTL `_LATE_ANSWER_TTL_S`): how an answer the
  relay stopped waiting for still reaches chat, exactly once (see Delegation).
- **Resume record** `voice_resume:{user_id}` (`src/domain/voice_resume.py::resume_key`): the provider's conversation
  handle, the mode and the provider, expiring with `RESUME_TTL_S`. Never a transcript. Written by
  `VoiceResumeService.keep`, read and consumed by `VoiceResumeService.take`.
- **Button-end marker** (`button_end_key`): the caller ended with the hang-up button; such a call is never resumed.
- **Call buffer** (`VoiceCallBuffer`): relay memory only. Turns segmented on `response_created`..`response_done`, usage per
  provider model id, a running transcript. Flushed once at call end through `submit_transcript`, and mined for the
  `call_context` each delegation carries.
- **Chat history**: a call that ends with a note appends two messages to the primary session, the event line and the note
  (`UserNotificationService.notify_call_summary`). That pair is the only durable trace of a companion call; Lelik has no
  memory store and writes no facts (the consolidator reads the note like any history).
- **Billing and observability**: per-model usage through `QuotaService.record_usage`; one `prompt_content` row per turn
  (agent type `lelik`), both written at call end.

## Flows

### Preparing a call

1. A phone callback or a web call asks `VoiceCallSetupService.prepare` for a ticket. `prepare` calls
   `LelikAgent.session_config(mode)` and stores the result as the ticket record and the call-kind record. A mode the
   user's provider has no profile for raises `UnsupportedVoiceMode`: the marker and ticket are released with no alert and
   the route answers 409.
2. `session_config` assembles context with `LelikPersonaService.assemble`: the biographical cache filtered to ordinary
   facts whose primary domain is in the user's `voice_fact_domains`, no standing directives, the primary channel's last
   `HISTORY_MESSAGES` stored summaries, and the clock. Then `PromptBuilder.build_for_agent` builds the instructions from
   the provider's prompt profile for the mode (`VoiceProviderProfile.prompt_profile_for`). A failure fails the call
   before it opens (no empty-prompt call).
3. The provider is the user's `voice_provider` or the default (`resolve_voice_profile`). One `VoiceProviderProfile` carries
   what differs per provider: prompt profile, `VoiceSessionSpec` (provider, turn ownership, voice, effort), per-mode
   profiles, native web search. The spec rides in the session config under `voice`, so the relay never guesses.
4. Tools are declared by `LelikAgent._tool_declarations`: `delegate_to_specialist` over the intents `LELIK` allows, plus
   `send_to_chat`; none at all in an interpreter call. With `LELIK_XAI_NATIVE_SEARCH` on, a provider that searches natively
   (`builtin_web_search`) is given that search and loses the delegated light search.

### The session loop

`VoiceSessionService.handle_call` redeems the ticket (`HttpCallControlPlaneAdapter.fetch_session_config`), pairs MCP
servers with their tokens (`pair_session_servers`), builds the call state and enters `_run_call`, which opens the provider
session from `relay_main.py::_realtime_session_factory` (it refuses an adapter that does not implement the spec's turn
ownership) and runs four concurrent loops: inbound audio forwarding, `_consume_events`, the silence watchdog (relay-owned
turns only) and, on web calls, the control and live-text tasks. The call ends when an audio stream ends or the watchdog
hangs up. Teardown order is fixed and matters: cancel the loops, close the live text and the Ask latch, abandon unheard
delegations, close the provider session, then `submit_transcript` in a `finally`, because that report also releases the
one-call marker and revokes the call's MCP tokens.

**Turn ownership** (`TurnOwnership`, per call, from the spec) decides who speaks when:
- *RELAY* (OpenAI): the provider never replies by itself (`create_response` off). The relay starts every reply in
  `_start_response` right after the caller's turn with the persona anchor (`build_persona_anchor`) ahead of it, owns
  barge-in, sends the caller opening and the silence and waiting notes.
- *PROVIDER* (xAI, the default): the provider replies and stops when talked over. The relay places the anchor once at the
  start (and again after a mid-call note, `_mid_call_note`), starts only the greeting and replies to delegation answers,
  and on barge-in only drops queued audio. The debounce `barge_in_min_speech_s` and the silence watchdog do not apply.

No `if provider ==` lives in the service. What differs is declared on the class: `supported_turn_ownership` and
`late_tool_result_after_caller_turn` on `RealtimeSessionPort`, and the `update_session` and `mcp_servers` capabilities,
which an adapter that cannot do them refuses (`NotImplementedError`, `ValueError`).

**Barge-in** (relay-owned) is `_barge_in`: read what was heard first, then clear the audio, cancel the response only if one
is active, then truncate the item to the heard part. The order is load-bearing (see Invariants).

### Delegation

Lelik has two tools. `delegate_to_specialist` reaches `search_memory`, `search_web_light` and `ask_alek`; `send_to_chat`
is a channel, not a worker (decision `lelik_send_to_chat.md`).

1. `_consume_events` sees a `tool_call` and `_start_delegation` turns it into delegation arguments
   (`src/domain/voice_errand.py::delegation_arguments`: `send_to_chat(request)` becomes the errand intent of the same
   name), attaches the last exchanges of the call as `call_context`, and runs it as a tracked task.
2. `_fetch_answer` posts to `/voice/delegate` (`create_voice_control_plane_blueprint`) and waits up to the
   `delegation_timeout_s` the relay passes in `relay_main.py::main`. The route runs `LelikAgent.delegate_outcome`: the intent
   must be in `permitted_intents`, then `DelegationEngine.dispatch` runs it like any text orchestrator's delegation, inside
   the call's `RequestContext`. The delegation runs shielded from the HTTP handler, because the relay disconnects exactly
   when it stops waiting.
3. `ask_alek` goes to `AlekGatewayAgent`: zero LLM, it frames the commission with the call's exchanges and routes it to the
   user's Router, so Smart answers with memory. Reading-shaped answers (links, tables) are also copied to chat. Its
   timeout `ASK_ALEK_TIMEOUT_MS` deliberately outlasts the relay's wait.
4. `send_to_chat` is the errand: ASYNC (a Cloud Task), the gateway posts Alek's whole answer to chat, Lelik hears only the
   acknowledgement. The relay keeps errands apart (`state.errands`): no waiting notes, no abandon. A failed errand is
   reported to chat by `AgentWorkerHandler`.
5. The answer is injected by `_inject_answer`. Which channel the provider speaks depends on the adapter flag: a late answer
   (the caller spoke since dispatch) goes in as a system note on OpenAI, which drops a late `function_call_output`, and as
   the `function_call_output` on xAI, which ignores a late note. Then `_reply_or_owe` starts the reply only when the floor
   is free; otherwise the reply is owed and `_reply_if_owed` starts it after the caller's turn or the active response.

**Late answers.** If the relay times out or the call ends with answers unheard, it posts `/voice/delegate/abandon`; the
main service writes the result marker and the abandoned marker in opposite orders on its two routes and claims the post
by an atomic `get_and_delete` on the result key (`_post_claimed`), so the answer reaches chat once, whichever side is
second. An interpreter call never posts a late answer, and a failure is kept as a flag and never as its text.

### Modes

`VoiceMode` has the companion call (`lelik`) and two single-purpose modes started from their own web page: `interpreter`
and `tutor`. Both are xAI-only (OpenAI has no `mode_profiles`), non-overridable (a user's fixed-language setting never
reaches them), and get no after-call summary to chat or memory (`VOICE_MODES_WITH_CALL_SUMMARY`). Phone calls stay in
the companion mode. The mode travels in the ticket and in the call-kind record.

**Interpreter.** Declares no tools and carries no MCP server; the owner's rule is that a tool that only translates must
have nothing a mistranslated phrase could act on (the provider's own read-only web and X search stays). Every utterance
gets a system note placed on `speech_started`: a translate note, or, when the caller pressed "Ask Lelik", an ask note.
The button is the `AskLatch` state machine (pure, injected clock, `AskStep` tells the service what to do). A pressed
button marks the caller's next utterance for Lelik, releases when his answer ends, and times out
(`ASK_ARMED_TIMEOUT_S`, `ASK_REPLY_TIMEOUT_S`, `ASK_RUNAWAY_REPLY_S`). While a cycle is open the relay offers one tool,
`set_language_pair`, through `update_session`, and withdraws it afterwards; `_handle_language_pair` accepts a call only
when code agrees (two different codes of a closed set, one of the turns committed since the press is the marked one, none
accepted since). An accepted pair is appended to the session instructions as a standing block and named in every later
translate note, because a note placed at `speech_started` can land late when speech begins in the tail of Lelik's playback.

**Tutor.** The same session with another persona token; it keeps the delegation tools and carries no MCP server.

**Live text** (both modes, web calls with a control link): `LiveTextPacer` holds Lelik's transcript phrases until the audio
they belong to has played (the provider streams text far faster than the audio plays), anchors each phrase to a byte offset
in the call's outbound audio, and on barge-in releases only what was played and drops the rest. `_run_live_text` sends its
frames on the control channel; the consume loop never waits on the page's socket.

**Geo.** A position the caller shares on purpose becomes one system note (`_handle_geo`; rules in the geo marks RFC).

### MCP tools of a call

Only the plain companion call on xAI (`voice_mcp_eligible`) with the `VOICE_MCP_TOOLS` knob on carries a remote MCP server,
the drive (index reads only, results cut at a voice cap). Memory is not an MCP server of any call; it goes by the
`search_memory` delegation. `LelikAgent.session_config` puts the descriptor (no token) on the config;
`/voice/session-config` mints a per-call token at redemption (never at prepare, never stored) and returns it; the relay
pairs token and descriptor, checking the token's audience against the server's URL, and the adapter hands the entry to xAI,
which calls the server itself. Teardown deletes the live-call record, so the token stops verifying at hang-up, not at expiry.
The provider runs these calls without the relay: the adapter reports `remote_tool_done`, and since xAI starts no follow-up
reply on its own, `_note_remote_tools` and `_reply_if_owed` start it.

### Ending a call

`submit_transcript` (the same blueprint) records usage per model, drops a voicemail (`is_voicemail`: the answering-machine
verdict or the caller's words), keeps the resume handle unless the call ended by the button, and, for companion calls only,
calls the summary consumer in `main.py`: `CompanionExtractorRunner` runs `LelikSummarizerAgent` as a separate cheap-tier
model, never Lelik, and `notify_call_summary` posts the note and appends the event pair to history
(`call_event_from_turns` writes the event line). A summary failure raises an ops alert; without it the failure would be
silent. In `finally` it flushes the prompt-content store (Cloud Run throttles CPU after the response), revokes the MCP
tokens and releases the one-call marker.

### Resume

xAI keeps a conversation server-side. The adapter always asks for `resumption`; the relay reports the handle with the
end-of-call report; a re-dial within `RESUME_TTL_S`, same user, mode and provider, gets it from `/voice/session-config`
(`VoiceResumeService.take` consumes it) under `RESUME_CONFIG_FIELD`, and the adapter reconnects with `conversation_id`. xAI
raises no error for an unknown id, so "resumed" means a replayed item arrived before `session.updated`
(`XaiRealtimeAdapter.resumed`); a resumed call gets a resume note and neither the caller opening nor the pickup note.

### Keeping a call as a file

An interpreter or tutor web page sends the call's lines it holds to `/api/voice/call-text/file`.
`parse_call_text_request` refuses any body of another shape whole. `CallTextFileService.save` asks the summarizer (the
mode's own profile, time-bounded; a failed summary still saves the file, untitled), renders Markdown and delivers it to chat
or to the drive Inbox's voice-calls folder. It never raises; the route answers from the outcome.

## Invariants and traps

- **Barge-in reads before it clears.** Twilio echoes the marks of dropped audio after a clear, so clearing first counts
  unheard audio as played and truncates the item too late. `_barge_in` also guards `cancel_response` on an active response:
  a cancel with nothing in flight is a provider error that ends the call, and so does any provider `error` event
  (`_consume_events`). No test pins the order.
- **`response.create` during an active response is a call-ending error.** `_start_response` claims the slot before its
  first `await`; answers wait in `queued_answers` and are replied to once. Never add a second starter.
- **A tool call of a barged-in response is answered, not run.** Its `function_call` still needs an output or it dangles,
  but running it spends a Smart turn on half a question. `tests/unit/services/test_voice_session_cancelled_tool_call.py`.
- **The relay is the only place the interpreter's tool-lessness is built, and `/voice/delegate` enforces the allowlist
  only when it knows the mode.** The route reads the call-kind record only when `voice_mcp_minting` is on
  (`_run_delegation`); with the knob off the mode is unknown, `permitted_intents` is judged as the companion's, and the
  interpreter's empty allowlist is not enforced there. The interpreter still declares no tools, so nothing calls the route;
  no test pins the gap. `tests/unit/agents/test_lelik_interpreter_no_tools.py`,
  `tests/unit/agents/test_lelik_delegate_intent_allowlist.py`.
- **Only the companion call leaves a note.** Interpreter and tutor sessions are tools, not conversations to remember:
  `VOICE_MODES_WITH_CALL_SUMMARY` holds `lelik` alone. `tests/unit/domain/test_voice_mode.py`.
- **The model id is a billing key, and an unpriced model bills at zero without an error.** `calculate_realtime_cost`
  returns 0 for an unknown model, so a bump of `_MODEL` in either adapter must come with its price; the guard tests
  fail instead of the bill. `tests/unit/domain/test_billing_realtime.py`. xAI's `billable_audio_seconds` is a session
  running total: the adapter emits only the increment, because `VoiceCallBuffer` sums. The main service deploys before the
  relay for that reason.
- **Tokens never reach a log.** The MCP token is held only by the session object; exceptions and provider text pass through
  `redact_exception` / `redact_for_log` with the call's secrets. A descriptor never carries a token
  (`RemoteToolServer.to_dict`).
- **A system note is never deleted.** A delete of a missing item ends the call, so the anchor and notes stay.
- **The call event line is written as `[System: ...]` into history** (`call_event_text`), a deviation from the repo-wide
  rule that code-written text never claims the system's voice. It exists so Alek does not mistake the note for his own
  reply. Do not copy the shape.
- **Prompt delivery guidance lives in one place** (decision `lelik_delivery_single_source.md`): the spoken paragraph of
  `build_persona_anchor`. The `SPOKEN_DELIVERY` token must still exist, because the anchor's spoken paragraph is gated on its
  section and the silence rule reacts to the relay's silence note. A Firestore token ships by token upload, not by deploy;
  a prompt change here and the code that relies on it are two releases.
- **No inline fallback prompt.** The summarizer fails closed when its prompt is missing, which is why the end-of-call summary
  has an alert. `tests/unit/services/test_lelik_summarizer_has_no_fallback.py`.
- **A resume handle is consumed once, belongs to one mode and provider, and is never logged or kept after a button
  hangup.** `tests/unit/services/test_voice_resume_service.py`, `tests/unit/services/test_voice_session_resumption.py`.
- **Prompt-content rows of voice calls are labelled with a fixed provider** (`submit_transcript`), so xAI calls appear under the
  wrong provider in `prompt_content`.

## Links

- RFCs: [`VOICE_COMPANION_RFC.md`](../../10_rfcs/VOICE_COMPANION_RFC.md) (the session loop, delegation, §4.15 tools, §4.16
  xAI), [`VOICE_MULTI_PROVIDER_RFC.md`](../../10_rfcs/VOICE_MULTI_PROVIDER_RFC.md) (turn ownership, profiles),
  [`VOICE_CALL_LIVE_TEXT_RFC.md`](../../10_rfcs/VOICE_CALL_LIVE_TEXT_RFC.md),
  [`LELIK_VOICE_MCP_RFC.md`](../../10_rfcs/LELIK_VOICE_MCP_RFC.md),
  [`VOICE_CALL_CONTROL_CHANNEL_RFC.md`](../../10_rfcs/VOICE_CALL_CONTROL_CHANNEL_RFC.md) (§5 Ask Lelik, §6 resume).
- Decisions: [`lelik_voice_modes.md`](../../04_solution_strategy/decisions/lelik_voice_modes.md),
  [`lelik_warm_context.md`](../../04_solution_strategy/decisions/lelik_warm_context.md),
  [`lelik_send_to_chat.md`](../../04_solution_strategy/decisions/lelik_send_to_chat.md),
  [`lelik_delivery_single_source.md`](../../04_solution_strategy/decisions/lelik_delivery_single_source.md),
  [`response_summary_in_character.md`](../../04_solution_strategy/decisions/response_summary_in_character.md),
  [`voice_xai_grok_voice_3.md`](../../04_solution_strategy/decisions/voice_xai_grok_voice_3.md),
  [`voice_xai_protocol_probe.md`](../../04_solution_strategy/decisions/voice_xai_protocol_probe.md),
  [`billing_cache_write_and_voice_seconds.md`](../../04_solution_strategy/decisions/billing_cache_write_and_voice_seconds.md).
- Probes behind the protocol facts (late function-call output, reasoning effort, native truncation, callback round trip,
  xAI wire shapes, the ask note): `scripts/voice/test_late_function_call_output_poc.py`,
  `scripts/voice/test_xai_realtime_protocol_poc.py`, `scripts/voice/ask_note_probe.py`. Read a probe before changing an
  adapter.
- The audio side of a call is the voice call transport block; the text side's use of the same user drive is
  [user drive](../user_drive/README.md); the MCP resources a call reads are [remote MCP server](../remote_mcp_server/README.md).

## Where to start reading

1. `src/services/voice_session_service.py::VoiceSessionService._run_call`: open, loops, teardown, report.
2. `src/services/voice_session_service.py::VoiceSessionService._consume_events`: every provider event and its rule.
3. `src/ports/realtime_session_port.py::RealtimeSessionPort`: what a provider must be, and the event vocabulary.
4. `src/agents/lelik_agent.py::LelikAgent.session_config`: what a call is given (prompt, tools, spec, MCP).
5. `src/domain/voice_provider_profile.py::VOICE_PROVIDER_PROFILES`: what differs per provider.
6. `src/web/voice_control_plane_app.py::create_voice_control_plane_blueprint`: the main service's half of a call.
7. `src/domain/voice_ask_latch.py::AskLatch`: the interpreter's button, its state table and timeouts.
